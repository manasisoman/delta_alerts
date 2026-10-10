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
from src.handlers.lambda_handler import handler
from src.models.types import TravelerAlertSummary

PRIMARY = "1000000001"
COMPANION = "1000000002"


@pytest.fixture(autouse=True)
def _mock_aws(base_requirements):
    """Activate moto mocks, create DynamoDB table (with GSI) + SNS endpoints, isolate requirements."""
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
            sm: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"token-{sm}",
            )["EndpointArn"]
            for sm in (PRIMARY, COMPANION)
        }

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client
        orig_requirements = group_mod.COUNTRY_REQUIREMENTS

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns
        group_mod.COUNTRY_REQUIREMENTS = base_requirements

        try:
            yield endpoints
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client
            group_mod.COUNTRY_REQUIREMENTS = orig_requirements


@pytest.fixture
def endpoints(_mock_aws):
    """Map of skymiles_number -> moto SNS endpoint ARN."""
    return _mock_aws


def _messages(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages published to a moto platform endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


def _segment(
    destination: str = "DE",
    departure: str = "2030-09-01",
    arrival: str = "2030-09-02",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> dict:
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _traveler(
    skymiles_number: str,
    endpoint_arn: str,
    first_name: str = "Test",
    nationality: str = "US",
    passport_number: str | None = "P123456",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    return {
        "skymiles_number": skymiles_number,
        "first_name": first_name,
        "last_name": "Traveler",
        "nationality": nationality,
        "passport_number": passport_number,
        "passport_expiry": passport_expiry,
        "endpoint_arn": endpoint_arn,
        "visa_records": visa_records or [],
    }


def _visa(country_code: str = "CN", expiry: str = "2035-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "tourist",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoints: dict, **overrides) -> dict:
    """Clean two-traveler US-national group to DE (no alerts) with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "travelers": [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat"),
            _traveler(COMPANION, endpoints[COMPANION], first_name="Casey"),
        ],
        "segments": [_segment()],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _result_for(response: dict, skymiles_number: str) -> dict:
    return next(
        r for r in response["body"]["traveler_results"]
        if r["skymiles_number"] == skymiles_number
    )


def _alerts(skymiles_number: str) -> list[dict]:
    return alert_store_mod.get_alerts_by_member(skymiles_number)


class TestCleanGroup:
    """A group with valid documents yields OK statuses, no alerts, and an all-clear summary."""

    def test_response_shape_and_ok_statuses(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [
            {
                "skymiles_number": PRIMARY,
                "first_name": "Pat",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            },
            {
                "skymiles_number": COMPANION,
                "first_name": "Casey",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            },
        ]

    def test_no_alerts_persisted_or_individual_pushes(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert alert_store_mod.table.scan()["Items"] == []
        assert _messages(endpoints[COMPANION]) == []

    def test_primary_receives_all_clear_summary(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        messages = _messages(endpoints[PRIMARY])
        assert len(messages) == 1
        assert messages[0]["default"] == "All 2 travelers are cleared for travel."
        gcm = json.loads(messages[0]["GCM"])
        assert gcm["data"] == {"confirmation_number": "DL-GROUP", "group_severity": "OK"}

    def test_routed_from_main_handler_when_travelers_present(self, endpoints):
        event = _base_event(endpoints)

        response = handler(event, None)

        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert response["body"]["group_severity"] == "OK"


class TestPassportAlerts:
    """Passport issues create a PASSPORT alert record and a push to that traveler only."""

    def test_expired_passport_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["passport_status"] == "CRITICAL"
        assert companion["visa_status"] == "OK"
        assert companion["alerts_sent"] == 1
        assert _result_for(response, PRIMARY)["alerts_sent"] == 0
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_alert_record_persisted_with_itinerary_ref_and_ttl(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        items = _alerts(COMPANION)
        assert len(items) == 1
        item = items[0]
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-GROUP"
        assert item["reasons"] == ["Passport has expired"]
        expected_ttl = int((datetime(2030, 9, 2) + timedelta(days=30)).timestamp())
        assert int(item["ttl"]) == expected_ttl
        assert _alerts(PRIMARY) == []

    def test_push_sent_to_affected_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        messages = _messages(endpoints[COMPANION])
        assert len(messages) == 1
        assert messages[0]["default"] == "Passport has expired"
        gcm = json.loads(messages[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert gcm["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_missing_passport_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_number"] = None
        event["travelers"][1]["passport_expiry"] = None

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        assert _alerts(COMPANION)[0]["reasons"] == ["Passport information is incomplete"]


class TestPassportValidityByCountry:
    """Passport validity is judged against each destination's required months."""

    def test_meets_schengen_three_months_but_under_six_is_info(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2031-01-01"

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["passport_status"] == "INFO"
        assert companion["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "INFO"
        assert "consider renewing" in _alerts(COMPANION)[0]["reasons"][0]

    def test_below_schengen_three_months_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2030-11-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        assert _alerts(COMPANION)[0]["reasons"] == [
            "Passport does not meet DE's 3-month validity requirement for DL100"
        ]

    def test_same_passport_fails_six_month_country(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="JP")])
        event["travelers"][1]["passport_expiry"] = "2031-01-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        assert _alerts(COMPANION)[0]["reasons"] == [
            "Passport does not meet JP's 6-month validity requirement for DL100"
        ]

    def test_passport_expires_before_departure(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2030-08-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        assert _alerts(COMPANION)[0]["reasons"] == [
            "Passport expires before departure on DL100 to DE"
        ]


class TestVisaAlerts:
    """Visa issues create a VISA alert record and a push to that traveler only."""

    def test_missing_visa_is_critical(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])

        response = group_handler(event, None)

        for sm in (PRIMARY, COMPANION):
            result = _result_for(response, sm)
            assert result["visa_status"] == "CRITICAL"
            assert result["passport_status"] == "OK"
            assert result["alerts_sent"] == 1
        assert response["body"]["total_alerts_sent"] == 2
        item = _alerts(COMPANION)[0]
        assert item["alert_type"] == "VISA"
        assert item["reasons"] == ["No visa on file for CN"]

    def test_visa_push_payload(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])

        group_handler(event, None)

        messages = _messages(endpoints[COMPANION])
        assert len(messages) == 1
        gcm = json.loads(messages[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for CN"
        assert gcm["data"]["alert_type"] == "VISA"
        assert gcm["data"]["severity"] == "CRITICAL"

    def test_valid_visa_clears_traveler(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][1]["visa_records"] = [_visa("CN")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "OK"
        assert _result_for(response, COMPANION)["alerts_sent"] == 0
        assert _alerts(COMPANION) == []

    def test_visa_expires_on_departure_is_warning(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][1]["visa_records"] = [_visa("CN", expiry="2030-09-01")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "WARNING"
        assert _alerts(COMPANION)[0]["reasons"] == ["Visa for CN expires on date of travel"]

    def test_visa_expires_within_warning_window_is_info(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][1]["visa_records"] = [_visa("CN", expiry="2030-09-20")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "INFO"

    def test_visa_exempt_nationality_has_no_alert(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="JP")])
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["visa_status"] == "OK"
        assert _result_for(response, COMPANION)["visa_status"] == "CRITICAL"


class TestPassportAndVisaTogether:
    """A traveler with both passport and visa issues gets two alerts and two pushes."""

    def test_two_alerts_for_one_traveler(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"
        event["travelers"][0]["visa_records"] = [_visa("CN")]

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["passport_status"] == "CRITICAL"
        assert companion["visa_status"] == "CRITICAL"
        assert companion["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        assert sorted(a["alert_type"] for a in _alerts(COMPANION)) == ["PASSPORT", "VISA"]
        assert len(_messages(endpoints[COMPANION])) == 2


class TestGroupSeverityAndSummary:
    """Group severity is the highest across travelers; the primary gets a summary of who needs action."""

    def test_highest_severity_wins(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = "2031-01-01"
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["passport_status"] == "INFO"
        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_primary_summary_lists_travelers_needing_action(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == (
            "1 of 2 travelers need action: Casey Traveler - Passport has expired"
        )
        gcm = json.loads(primary_msgs[0]["GCM"])
        assert gcm["data"]["group_severity"] == "CRITICAL"

    def test_no_summary_when_primary_not_in_travelers(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []
        assert len(_messages(endpoints[COMPANION])) == 1


class TestLayovers:
    """Layover segments only require a visa where the country mandates a transit visa."""

    def test_layover_in_transit_visa_country_alerts(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment(destination="CN", is_layover=True, flight_number="DL1"),
            _segment(destination="DE", origin="PEK", departure="2030-09-02",
                     arrival="2030-09-03", flight_number="DL2"),
        ])

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "CRITICAL"
        assert _alerts(COMPANION)[0]["reasons"] == ["No visa on file for CN"]

    def test_layover_without_transit_requirement_is_skipped(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment(destination="DE", is_layover=True, flight_number="DL1"),
            _segment(destination="CN", origin="FRA", departure="2030-09-02",
                     arrival="2030-09-03", flight_number="DL2"),
        ])
        for t in event["travelers"]:
            t["nationality"] = "IN"
            t["visa_records"] = [_visa("CN")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_passport_validity_still_checked(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment(destination="JP", is_layover=True, flight_number="DL1"),
            _segment(destination="DE", origin="NRT", departure="2030-09-02",
                     arrival="2030-09-03", flight_number="DL2"),
        ])
        event["travelers"][1]["passport_expiry"] = "2031-01-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["passport_status"] == "CRITICAL"
        assert "JP's 6-month" in _alerts(COMPANION)[0]["reasons"][0]

    def test_ttl_uses_last_arrival_across_segments(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment(destination="JP", is_layover=True, arrival="2030-09-02", flight_number="DL1"),
            _segment(destination="DE", departure="2030-09-05", arrival="2030-09-06", flight_number="DL2"),
        ])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        expected_ttl = int((datetime(2030, 9, 6) + timedelta(days=30)).timestamp())
        assert int(_alerts(COMPANION)[0]["ttl"]) == expected_ttl


class TestItineraryChanges:
    """Re-evaluating a changed booking reflects the new segments and accumulates alerts by itinerary."""

    def test_rerouted_itinerary_triggers_new_alert(self, endpoints):
        original = _base_event(endpoints)
        changed = _base_event(endpoints, segments=[_segment(destination="CN")])

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert first["body"]["total_alerts_sent"] == 0
        assert second["body"]["total_alerts_sent"] == 2
        summary = alert_store_mod.get_group_alert_summary("DL-GROUP")
        assert set(summary) == {PRIMARY, COMPANION}
        assert all(a["alert_type"] == "VISA" for alerts in summary.values() for a in alerts)

    def test_date_change_pushes_departure_past_passport_expiry(self, endpoints):
        original = _base_event(endpoints)
        original["travelers"][1]["passport_expiry"] = "2031-06-01"
        changed = _base_event(endpoints, segments=[
            _segment(departure="2031-07-01", arrival="2031-07-02"),
        ])
        changed["travelers"][1]["passport_expiry"] = "2031-06-01"

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert _result_for(first, COMPANION)["passport_status"] == "OK"
        assert _result_for(second, COMPANION)["passport_status"] == "CRITICAL"
        alerts = alert_store_mod.get_alerts_by_itinerary("DL-GROUP")
        assert [a["reasons"] for a in alerts] == [
            ["Passport expires before departure on DL100 to DE"]
        ]

    def test_traveler_added_to_booking(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"].append(
            _traveler("1000000003", endpoints[COMPANION], first_name="Sam",
                      passport_expiry="2020-01-01")
        )

        response = group_handler(event, None)

        assert len(response["body"]["traveler_results"]) == 3
        assert _result_for(response, "1000000003")["passport_status"] == "CRITICAL"
        assert _messages(endpoints[PRIMARY])[0]["default"].startswith("1 of 3 travelers")


class TestRequirementsOverride:
    """requirements_override replaces default country rules, filling unspecified fields with defaults."""

    def test_override_makes_country_visa_free(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[_segment(destination="CN")],
            requirements_override={
                "CN": {"requires_visa": False, "passport_validity_months": 3},
            },
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"

    def test_override_defaults_require_visa_and_six_months(self, endpoints):
        event = _base_event(endpoints, requirements_override={"DE": {}})
        event["travelers"][1]["passport_expiry"] = "2031-01-01"

        response = group_handler(event, None)

        companion = _result_for(response, COMPANION)
        assert companion["visa_status"] == "CRITICAL"
        assert companion["passport_status"] == "CRITICAL"
        passport_alert = next(a for a in _alerts(COMPANION) if a["alert_type"] == "PASSPORT")
        assert "DE's 6-month" in passport_alert["reasons"][0]

    def test_override_adds_unlisted_country(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[_segment(destination="ZZ")],
            requirements_override={
                "ZZ": {"requires_visa": True, "visa_exempt_nationalities": ["US"]},
            },
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0

    def test_unknown_country_without_override_uses_default_rules(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="ZZ")])

        response = group_handler(event, None)

        assert _result_for(response, COMPANION)["visa_status"] == "CRITICAL"
        assert _alerts(COMPANION)[0]["reasons"] == ["No visa on file for ZZ"]


class TestUnmatchedTravelerSummary:
    """Alerts are still persisted (but not pushed) when a summary has no matching traveler profile."""

    def test_alert_saved_without_push(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"
        real_evaluate = group_mod.evaluate_group_itinerary

        def _evaluate_with_unknown_traveler(group, requirements):
            result = real_evaluate(group, requirements)
            s = result.traveler_summaries[1]
            result.traveler_summaries[1] = TravelerAlertSummary(
                skymiles_number="9999999999",
                first_name=s.first_name,
                last_name=s.last_name,
                passport_result=s.passport_result,
                visa_result=s.visa_result,
            )
            return result

        group_mod.evaluate_group_itinerary = _evaluate_with_unknown_traveler
        try:
            response = group_handler(event, None)
        finally:
            group_mod.evaluate_group_itinerary = real_evaluate

        assert _result_for(response, "9999999999")["alerts_sent"] == 2
        assert len(_alerts("9999999999")) == 2
        assert _messages(endpoints[COMPANION]) == []


class TestParseGroupItinerary:
    """Event payloads deserialize into a GroupItinerary with typed segments and profiles."""

    def test_parses_segments_travelers_and_primary(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["visa_records"] = [_visa("CN")]

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert group.segments[0].departure_date == date(2030, 9, 1)
        assert group.segments[0].is_layover is False
        assert [t.skymiles_number for t in group.travelers] == [PRIMARY, COMPANION]
        assert group.travelers[1].visa_records[0].expiry_date == date(2035, 1, 1)

    def test_is_layover_defaults_to_false(self, endpoints):
        seg = _segment()
        del seg["is_layover"]
        event = _base_event(endpoints, segments=[seg])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False

    def test_missing_passport_expiry_parses_as_none(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = None

        group = _parse_group_itinerary(event)

        assert group.travelers[0].passport_expiry is None


class TestFindTraveler:
    """_find_traveler returns the matching profile or None."""

    def test_returns_match(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, COMPANION).first_name == "Casey"

    def test_returns_none_when_absent(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, "0000000000") is None
