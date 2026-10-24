"""Critical-path tests for the group itinerary Lambda handler."""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.models.types import (
    AlertSeverity,
    GroupEvaluationResult,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
)

PRIMARY = "1000000001"
COMPANION = "1000000002"
VALID_PASSPORT = "2035-01-01"
EXPIRED_DATE = "2020-01-01"
DEPARTURE = "2030-06-01"
ARRIVAL = "2030-06-02"


def _requirements_override() -> dict:
    """Explicit country requirements so tests never depend on production data."""
    return {
        "DE": {
            "country_code": "DE",
            "requires_visa": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB", "CA"],
        },
        "CN": {
            "country_code": "CN",
            "requires_visa": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": [],
        },
        "JP": {
            "country_code": "JP",
            "requires_visa": True,
            "transit_visa_required": False,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": ["US", "GB"],
        },
        "US": {
            "country_code": "US",
            "requires_visa": False,
            "transit_visa_required": False,
            "passport_validity_months": 0,
            "visa_exempt_nationalities": [],
        },
    }


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the alerts table (with GSI) and one SNS endpoint per traveler."""
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="ConciergeAlerts",
            KeySchema=[
                {"AttributeName": "skymiles_number", "KeyType": "HASH"},
                {"AttributeName": "alert_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "skymiles_number", "AttributeType": "S"},
                {"AttributeName": "alert_id", "AttributeType": "S"},
                {"AttributeName": "itinerary_ref", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "itinerary-ref-index",
                    "KeySchema": [{"AttributeName": "itinerary_ref", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoints = {
            number: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"token-{number}",
            )["EndpointArn"]
            for number in (PRIMARY, COMPANION)
        }

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client
        orig_country_reqs = group_mod.COUNTRY_REQUIREMENTS

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns
        group_mod.COUNTRY_REQUIREMENTS = {}

        try:
            yield endpoints
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client
            group_mod.COUNTRY_REQUIREMENTS = orig_country_reqs


@pytest.fixture
def endpoints(_mock_aws):
    """Map of SkyMiles number -> moto SNS endpoint ARN."""
    return _mock_aws


def _traveler(number: str, endpoint_arn: str, **overrides) -> dict:
    traveler = {
        "skymiles_number": number,
        "first_name": "Primary" if number == PRIMARY else "Companion",
        "last_name": "Traveler",
        "nationality": "US",
        "passport_number": f"P{number}",
        "passport_expiry": VALID_PASSPORT,
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    traveler.update(overrides)
    return traveler


def _segment(**overrides) -> dict:
    segment = {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": "DE",
        "departure_date": DEPARTURE,
        "arrival_date": ARRIVAL,
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


def _visa(country_code: str, expiry: str = "2035-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoints: dict, primary=None, companion=None, **overrides) -> dict:
    """Two clean US travelers flying ATL -> DE with explicit requirements."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": [
            _traveler(PRIMARY, endpoints[PRIMARY], **(primary or {})),
            _traveler(COMPANION, endpoints[COMPANION], **(companion or {})),
        ],
        "requirements_override": _requirements_override(),
    }
    event.update(overrides)
    return event


def _messages(endpoint_arn: str) -> list[dict]:
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _titles(endpoint_arn: str) -> list[str]:
    return [json.loads(m["GCM"])["notification"]["title"] for m in _messages(endpoint_arn)]


def _result_for(response: dict, number: str) -> dict:
    return next(r for r in response["body"]["traveler_results"] if r["skymiles_number"] == number)


class TestHappyPath:
    """Clean groups produce no alerts but the primary still gets a group summary."""

    def test_clean_group_response_shape(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert [r["skymiles_number"] for r in body["traveler_results"]] == [PRIMARY, COMPANION]
        for result in body["traveler_results"]:
            assert result["passport_status"] == "OK"
            assert result["visa_status"] == "OK"
            assert result["alerts_sent"] == 0

    def test_clean_group_persists_nothing(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert alert_store_mod.table.scan()["Items"] == []

    def test_primary_receives_cleared_summary_only(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == "All 2 travelers are cleared for travel."
        assert _messages(endpoints[COMPANION]) == []

    def test_traveler_names_returned(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["first_name"] == "Companion"
        assert companion["last_name"] == "Traveler"


class TestPassportAlerts:
    """A passport issue persists a PASSPORT alert and pushes it to that traveler."""

    def test_expired_passport_alerts_only_that_traveler(self, endpoints):
        event = _base_event(endpoints, companion={"passport_expiry": EXPIRED_DATE})

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["passport_status"] == "CRITICAL"
        assert companion["alerts_sent"] == 1
        assert _result_for(response, PRIMARY)["alerts_sent"] == 0
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_passport_alert_record_persisted(self, endpoints):
        event = _base_event(endpoints, companion={"passport_expiry": EXPIRED_DATE})

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["skymiles_number"] == COMPANION
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-GROUP"
        assert item["reasons"] == ["Passport has expired"]

    def test_passport_push_sent_to_affected_traveler(self, endpoints):
        event = _base_event(endpoints, companion={"passport_expiry": EXPIRED_DATE})

        group_handler(event, None)

        msgs = _messages(endpoints[COMPANION])
        assert len(msgs) == 1
        gcm = json.loads(msgs[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert gcm["notification"]["body"] == "Passport has expired"
        assert gcm["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_missing_passport_is_critical(self, endpoints):
        event = _base_event(endpoints, companion={"passport_number": None, "passport_expiry": None})

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        reasons = alert_store_mod.table.scan()["Items"][0]["reasons"]
        assert reasons == ["Passport information is incomplete"]

    def test_group_summary_lists_traveler_needing_action(self, endpoints):
        event = _base_event(endpoints, companion={"passport_expiry": EXPIRED_DATE})

        group_handler(event, None)

        summary = _messages(endpoints[PRIMARY])[0]["default"]
        assert summary == "1 of 2 travelers need action: Companion Traveler - Passport has expired"


class TestPassportValidityByCountry:
    """Passport validity windows are enforced per destination country's requirement."""

    def test_meets_3_month_schengen_window(self, endpoints):
        # 4 months of validity after departure: OK for DE's 3-month rule, INFO (<6 months)
        event = _base_event(endpoints, companion={"passport_expiry": "2030-10-01"})

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["passport_status"] == "INFO"
        assert response["body"]["group_severity"] == "INFO"

    def test_below_3_month_schengen_window(self, endpoints):
        event = _base_event(endpoints, companion={"passport_expiry": "2030-08-01"})

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        reasons = alert_store_mod.table.scan()["Items"][0]["reasons"]
        assert reasons == ["Passport does not meet DE's 3-month validity requirement for DL100"]

    def test_same_passport_fails_6_month_country(self, endpoints):
        event = _base_event(
            endpoints,
            companion={"passport_expiry": "2030-10-01"},
            segments=[_segment(destination="JP")],
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"

    def test_override_changes_validity_window(self, endpoints):
        reqs = _requirements_override()
        reqs["DE"]["passport_validity_months"] = 6
        event = _base_event(
            endpoints,
            companion={"passport_expiry": "2030-10-01"},
            requirements_override=reqs,
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"

    def test_passport_expires_before_departure(self, endpoints):
        event = _base_event(endpoints, companion={"passport_expiry": "2030-05-01"})

        group_handler(event, None)

        reasons = alert_store_mod.table.scan()["Items"][0]["reasons"]
        assert reasons == ["Passport expires before departure on DL100 to DE"]


class TestVisaAlerts:
    """A visa issue persists a VISA alert and pushes it to that traveler."""

    def test_missing_visa_for_required_country(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "IN"})

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["visa_status"] == "CRITICAL"
        assert companion["passport_status"] == "OK"
        assert companion["alerts_sent"] == 1
        item = alert_store_mod.table.scan()["Items"][0]
        assert item["alert_type"] == "VISA"
        assert item["reasons"] == ["No visa on file for DE"]

    def test_visa_push_sent_to_affected_traveler(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "IN"})

        group_handler(event, None)

        msgs = _messages(endpoints[COMPANION])
        assert len(msgs) == 1
        gcm = json.loads(msgs[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["data"]["alert_type"] == "VISA"
        assert gcm["data"]["severity"] == "CRITICAL"

    def test_valid_visa_clears_traveler(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "IN", "visa_records": [_visa("DE")]})

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_expired_visa_is_critical(self, endpoints):
        event = _base_event(
            endpoints, companion={"nationality": "IN", "visa_records": [_visa("DE", EXPIRED_DATE)]},
        )

        group_handler(event, None)

        assert alert_store_mod.table.scan()["Items"][0]["reasons"] == ["Visa for DE has expired"]

    def test_visa_expiring_near_travel_is_info(self, endpoints):
        event = _base_event(
            endpoints, companion={"nationality": "IN", "visa_records": [_visa("DE", "2030-06-15")]},
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "INFO"
        assert response["body"]["total_alerts_sent"] == 1

    def test_info_alert_excluded_from_group_summary(self, endpoints):
        event = _base_event(
            endpoints, companion={"nationality": "IN", "visa_records": [_visa("DE", "2030-06-15")]},
        )

        group_handler(event, None)

        summary = _messages(endpoints[PRIMARY])[0]["default"]
        assert summary == "All 2 travelers are cleared for travel."

    def test_visa_exempt_nationality_skips_visa(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "GB"})

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "OK"


class TestPassportAndVisaTogether:
    """Both evaluations run independently; each issue yields its own alert and push."""

    def test_both_alerts_counted_per_traveler(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "IN", "passport_expiry": EXPIRED_DATE})

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        types = sorted(i["alert_type"] for i in alert_store_mod.table.scan()["Items"])
        assert types == ["PASSPORT", "VISA"]
        assert sorted(_titles(endpoints[COMPANION])) == [
            "Delta Concierge \u2014 Passport Alert",
            "Delta Concierge \u2014 Visa Alert",
        ]

    def test_alerts_summed_across_travelers(self, endpoints):
        event = _base_event(
            endpoints,
            primary={"passport_expiry": EXPIRED_DATE},
            companion={"nationality": "IN"},
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        summary = [m for m in _messages(endpoints[PRIMARY]) if "travelers need action" in m["default"]]
        assert summary[0]["default"].startswith("2 of 2 travelers need action")


class TestGroupSeverity:
    """group_severity reports the highest severity across all travelers."""

    def test_critical_outranks_info(self, endpoints):
        event = _base_event(
            endpoints,
            primary={"passport_expiry": "2030-10-01"},
            companion={"nationality": "IN"},
        )

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["passport_status"] == "INFO"
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_visa_expiring_on_travel_date_is_warning(self, endpoints):
        event = _base_event(
            endpoints, companion={"nationality": "IN", "visa_records": [_visa("DE", DEPARTURE)]},
        )

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "WARNING"
        summary = _messages(endpoints[PRIMARY])[0]["default"]
        assert "Visa for DE expires on date of travel" in summary


class TestLayovers:
    """Layovers only require visas when the transit country requires a transit visa."""

    def test_layover_in_non_transit_country_skips_visa(self, endpoints):
        event = _base_event(
            endpoints,
            companion={"nationality": "IN", "visa_records": [_visa("DE")]},
            segments=[
                _segment(flight_number="DL1", destination="JP", is_layover=True,
                         departure_date="2030-05-31", arrival_date="2030-06-01"),
                _segment(flight_number="DL2"),
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "OK"

    def test_layover_in_transit_visa_country_alerts(self, endpoints):
        event = _base_event(
            endpoints,
            companion={"nationality": "IN", "visa_records": [_visa("DE")]},
            segments=[
                _segment(flight_number="DL1", destination="CN", is_layover=True,
                         departure_date="2030-05-31", arrival_date="2030-06-01"),
                _segment(flight_number="DL2"),
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "CRITICAL"
        assert alert_store_mod.table.scan()["Items"][0]["reasons"] == ["No visa on file for CN"]

    def test_layover_still_checks_passport_validity(self, endpoints):
        # 4 months left: fine for DE (3) but fails the JP layover's 6-month rule
        event = _base_event(
            endpoints,
            companion={"passport_expiry": "2030-10-01"},
            segments=[
                _segment(flight_number="DL1", destination="JP", is_layover=True,
                         departure_date="2030-05-31", arrival_date="2030-06-01"),
                _segment(flight_number="DL2"),
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        reasons = alert_store_mod.table.scan()["Items"][0]["reasons"]
        assert reasons[0] == "Passport does not meet JP's 6-month validity requirement for DL1"

    def test_is_layover_defaults_to_false(self, endpoints):
        segment = _segment(destination="CN")
        del segment["is_layover"]
        event = _base_event(endpoints, segments=[segment])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestItineraryChanges:
    """Re-evaluating a changed itinerary produces alerts tied to the new booking."""

    def test_destination_change_introduces_visa_alert(self, endpoints):
        companion = {"nationality": "IN", "visa_records": [_visa("DE")]}
        original = _base_event(endpoints, companion=companion)
        changed = _base_event(
            endpoints, companion=companion, confirmation_number="DL-GROUP-2",
            segments=[_segment(destination="JP")],
        )

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert first["body"]["total_alerts_sent"] == 0
        assert second["body"]["total_alerts_sent"] == 1
        assert alert_store_mod.get_alerts_by_itinerary("DL-GROUP") == []
        new_alerts = alert_store_mod.get_alerts_by_itinerary("DL-GROUP-2")
        assert [a["reasons"] for a in new_alerts] == [["No visa on file for JP"]]

    def test_date_change_past_visa_expiry(self, endpoints):
        companion = {"nationality": "IN", "visa_records": [_visa("DE", "2030-07-15")]}
        changed = _base_event(
            endpoints, companion=companion,
            segments=[_segment(departure_date="2030-08-01", arrival_date="2030-08-02")],
        )

        response = group_handler(changed, None)

        assert _result_for(response, COMPANION)["visa_status"] == "CRITICAL"
        assert alert_store_mod.table.scan()["Items"][0]["reasons"] == [
            "Visa for DE expires before date of travel"
        ]

    def test_alerts_grouped_by_traveler_for_itinerary(self, endpoints):
        event = _base_event(
            endpoints,
            primary={"passport_expiry": EXPIRED_DATE},
            companion={"nationality": "IN"},
        )

        group_handler(event, None)

        summary = alert_store_mod.get_group_alert_summary("DL-GROUP")
        assert set(summary) == {PRIMARY, COMPANION}
        assert summary[PRIMARY][0]["alert_type"] == "PASSPORT"
        assert summary[COMPANION][0]["alert_type"] == "VISA"

    def test_ttl_is_30_days_after_last_arrival(self, endpoints):
        event = _base_event(
            endpoints,
            companion={"passport_expiry": EXPIRED_DATE},
            segments=[
                _segment(flight_number="DL1"),
                _segment(flight_number="DL2", destination="US", origin="FRA",
                         departure_date="2030-06-10", arrival_date="2030-06-11"),
            ],
        )

        group_handler(event, None)

        expected = int((datetime(2030, 6, 11) + timedelta(days=30)).timestamp())
        assert int(alert_store_mod.table.scan()["Items"][0]["ttl"]) == expected


class TestRequirementsOverride:
    """Overrides replace default country requirements; missing keys fall back to defaults."""

    def test_override_disabling_visa_clears_alert(self, endpoints):
        reqs = _requirements_override()
        reqs["DE"]["requires_visa"] = False
        event = _base_event(endpoints, companion={"nationality": "IN"}, requirements_override=reqs)

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "OK"

    def test_partial_override_uses_defaults(self, endpoints):
        # Defaults: requires_visa=True, passport_validity_months=6, no exemptions
        event = _base_event(
            endpoints,
            companion={"passport_expiry": "2030-10-01"},
            requirements_override={"DE": {"transit_visa_required": False}},
        )

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["visa_status"] == "CRITICAL"
        assert companion["passport_status"] == "CRITICAL"

    def test_no_override_uses_country_requirements(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "IN"}, requirements_override=None)
        orig = group_mod.COUNTRY_REQUIREMENTS
        group_mod.COUNTRY_REQUIREMENTS = {
            "DE": group_mod.TravelDocRequirements(
                country_code="DE",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            )
        }
        try:
            response = group_handler(event, None)
        finally:
            group_mod.COUNTRY_REQUIREMENTS = orig

        assert _result_for(response, COMPANION)["visa_status"] == "OK"

    def test_missing_override_key_treated_as_none(self, endpoints):
        event = _base_event(endpoints, companion={"nationality": "IN"})
        del event["requirements_override"]
        orig = group_mod.COUNTRY_REQUIREMENTS
        group_mod.COUNTRY_REQUIREMENTS = {
            "DE": group_mod.TravelDocRequirements(
                country_code="DE",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=["IN"],
            )
        }
        try:
            response = group_handler(event, None)
        finally:
            group_mod.COUNTRY_REQUIREMENTS = orig

        assert _result_for(response, COMPANION)["visa_status"] == "OK"


class TestPrimaryTravelerLookup:
    """The group summary only goes out when the primary traveler is in the group."""

    def test_unknown_primary_skips_group_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []
        assert _messages(endpoints[COMPANION]) == []

    def test_companion_as_primary_receives_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler=COMPANION)

        group_handler(event, None)

        assert len(_messages(endpoints[COMPANION])) == 1
        assert _messages(endpoints[PRIMARY]) == []

    def test_find_traveler_returns_match(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        found = _find_traveler(group, COMPANION)

        assert found.skymiles_number == COMPANION
        assert found.endpoint_arn == endpoints[COMPANION]

    def test_find_traveler_returns_none_when_absent(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, "0000000000") is None


class TestUnknownTravelerInResults:
    """Alerts for a summary with no matching traveler are persisted but not pushed."""

    @pytest.fixture
    def ghost_evaluator(self):
        def fake_evaluate(group, requirements):
            passport = PassportEvaluation(
                profile=None, segments_evaluated=group.segments, is_alert_required=True,
                severity=AlertSeverity.CRITICAL, reasons=["Passport has expired"], validation_errors=[],
            )
            visa = VisaEvaluation(
                profile=None, segments_evaluated=group.segments, is_alert_required=True,
                severity=AlertSeverity.WARNING, reasons=["Visa for DE expires on date of travel"],
                validation_errors=[],
            )
            return GroupEvaluationResult(
                confirmation_number=group.confirmation_number,
                traveler_summaries=[TravelerAlertSummary(
                    skymiles_number="0000000000", first_name="Ghost", last_name="Traveler",
                    passport_result=passport, visa_result=visa,
                )],
                group_severity=AlertSeverity.CRITICAL,
            )

        orig = group_mod.evaluate_group_itinerary
        group_mod.evaluate_group_itinerary = fake_evaluate
        try:
            yield
        finally:
            group_mod.evaluate_group_itinerary = orig

    def test_alerts_saved_without_push(self, endpoints, ghost_evaluator):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        assert {i["skymiles_number"] for i in alert_store_mod.table.scan()["Items"]} == {"0000000000"}
        assert _messages(endpoints[COMPANION]) == []
        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"].startswith("1 of 1 travelers need action: Ghost Traveler")

    def test_ghost_statuses_reported(self, endpoints, ghost_evaluator):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        ghost = response["body"]["traveler_results"][0]
        assert ghost["passport_status"] == "CRITICAL"
        assert ghost["visa_status"] == "WARNING"
        assert ghost["alerts_sent"] == 2
