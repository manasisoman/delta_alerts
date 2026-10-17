"""Tests for the group itinerary Lambda handler end-to-end flow."""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import moto.core
import moto.sns.models
import pytest

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
    GroupItinerary,
    PassportEvaluation,
    SkyMilesProfile,
    TravelDocRequirements,
    TravelerAlertSummary,
    VisaEvaluation,
)


REGION = "us-east-1"
PRIMARY = "1000000001"
COMPANION = "1000000002"
DEPARTURE = "2031-06-01"
ARRIVAL = "2031-06-02"
VALID_PASSPORT = "2035-01-01"


def _test_requirements() -> dict[str, TravelDocRequirements]:
    """Fixture country requirements used instead of production data."""
    return {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB", "CA"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "JP": TravelDocRequirements(
            country_code="JP",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US", "GB"],
        ),
        "MX": TravelDocRequirements(
            country_code="MX",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=0,
            visa_exempt_nationalities=[],
        ),
    }


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create DynamoDB/SNS resources, and swap module-level clients."""
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name=REGION)
        ddb.create_table(
            TableName="ConciergeAlerts",
            KeySchema=[
                {"AttributeName": "skymiles_number", "KeyType": "HASH"},
                {"AttributeName": "alert_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "skymiles_number", "AttributeType": "S"},
                {"AttributeName": "alert_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sns = boto3.client("sns", region_name=REGION)
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoints = {
            sky: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"token-{sky}",
            )["EndpointArn"]
            for sky in (PRIMARY, COMPANION)
        }

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client
        orig_requirements = group_mod.COUNTRY_REQUIREMENTS

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns
        group_mod.COUNTRY_REQUIREMENTS = _test_requirements()

        try:
            yield endpoints
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client
            group_mod.COUNTRY_REQUIREMENTS = orig_requirements


@pytest.fixture
def endpoints(_mock_aws) -> dict[str, str]:
    """Map of SkyMiles number to moto SNS endpoint ARN."""
    return _mock_aws


def _traveler(
    skymiles_number: str,
    endpoint_arn: str,
    first_name: str = "Test",
    nationality: str = "US",
    passport_expiry: str | None = VALID_PASSPORT,
    passport_number: str | None = "P123456",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler profile dict as it appears in the Lambda event."""
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


def _segment(
    destination: str = "JP",
    departure: str = DEPARTURE,
    arrival: str = ARRIVAL,
    is_layover: bool = False,
    flight_number: str = "DL100",
) -> dict:
    """Build a flight segment dict as it appears in the Lambda event."""
    return {
        "flight_number": flight_number,
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _visa(country_code: str, expiry: str = "2035-01-01") -> dict:
    """Build a visa record dict as it appears in the Lambda event."""
    return {
        "country_code": country_code,
        "visa_type": "tourist",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoints: dict[str, str], **overrides) -> dict:
    """Build a valid two-traveler group event (clean by default) with overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat"),
            _traveler(COMPANION, endpoints[COMPANION], first_name="Casey"),
        ],
    }
    event.update(overrides)
    return event


def _sns_messages(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages published to a moto platform endpoint."""
    backend = moto.sns.models.sns_backends[moto.core.DEFAULT_ACCOUNT_ID][REGION]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


def _gcm(message: dict) -> dict:
    """Decode the GCM payload of an SNS message."""
    return json.loads(message["GCM"])


def _saved_alerts() -> list[dict]:
    """Return every alert item stored in the moto DynamoDB table."""
    return alert_store_mod.table.scan()["Items"]


def _results_by_traveler(response: dict) -> dict[str, dict]:
    return {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}


class TestCleanGroup:
    """A fully compliant group sends no per-traveler alerts, only an all-clear summary."""

    def test_returns_ok_statuses_and_no_alerts(self, endpoints):
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

    def test_nothing_persisted(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert _saved_alerts() == []

    def test_primary_receives_all_clear_summary(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        primary_msgs = _sns_messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == "All 2 travelers are cleared for travel."
        assert _gcm(primary_msgs[0])["data"] == {
            "confirmation_number": "DL-GROUP",
            "group_severity": "OK",
        }
        assert _sns_messages(endpoints[COMPANION]) == []


class TestPassportAlerts:
    """Passport issues produce a persisted PASSPORT alert and a push to that traveler."""

    def test_expired_passport_alerts_only_affected_traveler(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat"),
            _traveler(COMPANION, endpoints[COMPANION], first_name="Casey",
                      passport_expiry="2020-01-01"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[COMPANION]["passport_status"] == "CRITICAL"
        assert results[COMPANION]["alerts_sent"] == 1
        assert results[PRIMARY]["alerts_sent"] == 0
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_alert_record_persisted_with_itinerary_ref_and_ttl(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION], passport_expiry="2020-01-01"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        group_handler(event, None)

        items = _saved_alerts()
        assert len(items) == 1
        item = items[0]
        assert item["skymiles_number"] == COMPANION
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["reasons"] == ["Passport has expired"]
        assert item["itinerary_ref"] == "DL-GROUP"
        assert item["status"] == "ACTIVE"
        expected_ttl = int(
            datetime.combine(date(2031, 6, 2), datetime.min.time()).timestamp()
            + timedelta(days=30).total_seconds()
        )
        assert int(item["ttl"]) == expected_ttl

    def test_push_sent_to_affected_traveler_endpoint(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION], passport_expiry="2020-01-01"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        group_handler(event, None)

        companion_msgs = _sns_messages(endpoints[COMPANION])
        assert len(companion_msgs) == 1
        gcm = _gcm(companion_msgs[0])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert gcm["notification"]["body"] == "Passport has expired"
        assert gcm["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_missing_passport_number_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_number=None),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[PRIMARY]["passport_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["Passport information is incomplete"]

    def test_missing_passport_expiry_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry=None),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        assert _results_by_traveler(response)[PRIMARY]["passport_status"] == "CRITICAL"

    def test_primary_summary_lists_passport_issue(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat"),
            _traveler(COMPANION, endpoints[COMPANION], first_name="Casey",
                      passport_expiry="2020-01-01"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        group_handler(event, None)

        primary_msgs = _sns_messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == (
            "1 of 2 travelers need action: Casey Traveler - Passport has expired"
        )
        assert _gcm(primary_msgs[0])["data"]["group_severity"] == "CRITICAL"


class TestPassportValidityWithinCountryRequirements:
    """Passport validity is checked against each destination's required months."""

    def test_meets_3_month_rule_but_within_6_months_is_info(self, endpoints):
        # Expires 4 months after departure: OK for DE's 3-month rule, INFO for renewal.
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2031-10-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=[_segment("DE")])

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[PRIMARY]["passport_status"] == "INFO"
        assert results[PRIMARY]["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "INFO"

    def test_below_6_month_rule_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2031-10-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=[_segment("JP")])

        response = group_handler(event, None)

        assert _results_by_traveler(response)[PRIMARY]["passport_status"] == "CRITICAL"
        assert "6-month validity requirement" in _saved_alerts()[0]["reasons"][0]

    def test_info_only_issue_omitted_from_group_summary(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2031-10-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=[_segment("DE")])

        group_handler(event, None)

        primary_msgs = _sns_messages(endpoints[PRIMARY])
        # Personal INFO alert + group summary that still reports all clear.
        assert len(primary_msgs) == 2
        assert primary_msgs[1]["default"] == "All 2 travelers are cleared for travel."

    def test_expires_before_departure_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2031-05-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers)

        group_handler(event, None)

        assert _saved_alerts()[0]["reasons"] == [
            "Passport expires before departure on DL100 to JP"
        ]


class TestVisaAlerts:
    """Visa issues produce a persisted VISA alert and a push to that traveler."""

    def test_no_visa_for_required_country(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[COMPANION]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["passport_status"] == "OK"
        assert results[COMPANION]["alerts_sent"] == 1
        item = _saved_alerts()[0]
        assert item["alert_type"] == "VISA"
        assert item["reasons"] == ["No visa on file for JP"]

    def test_visa_push_payload(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        group_handler(event, None)

        gcm = _gcm(_sns_messages(endpoints[COMPANION])[0])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for JP"
        assert gcm["data"] == {
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_valid_visa_clears_traveler(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN",
                      visa_records=[_visa("JP")]),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert _results_by_traveler(response)[COMPANION]["visa_status"] == "OK"

    def test_visa_expiring_on_travel_date_is_warning(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN",
                      visa_records=[_visa("JP", expiry=DEPARTURE)]),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        assert _results_by_traveler(response)[COMPANION]["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"

    def test_visa_not_required_destination(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN"),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=[_segment("MX")])

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0


class TestPassportAndVisaForSameTraveler:
    """A traveler with both issues gets two alerts; counts aggregate across the group."""

    def test_both_alerts_counted(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat",
                      nationality="IN", passport_expiry="2020-01-01"),
            _traveler(COMPANION, endpoints[COMPANION], first_name="Casey",
                      nationality="IN"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[PRIMARY]["alerts_sent"] == 2
        assert results[PRIMARY]["passport_status"] == "CRITICAL"
        assert results[PRIMARY]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["alerts_sent"] == 1
        assert response["body"]["total_alerts_sent"] == 3
        assert sorted(i["alert_type"] for i in _saved_alerts()) == [
            "PASSPORT", "VISA", "VISA",
        ]

    def test_primary_gets_personal_alerts_then_summary(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat",
                      nationality="IN", passport_expiry="2020-01-01"),
            _traveler(COMPANION, endpoints[COMPANION], first_name="Casey",
                      nationality="IN"),
        ]
        event = _base_event(endpoints, travelers=travelers)

        group_handler(event, None)

        primary_msgs = _sns_messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 3
        assert primary_msgs[-1]["default"] == (
            "2 of 2 travelers need action: "
            "Pat Traveler - Passport has expired, No visa on file for JP; "
            "Casey Traveler - No visa on file for JP"
        )


class TestLayovers:
    """Layover segments only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_skipped(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN"),
        ]
        segments = [
            _segment("JP", is_layover=True, flight_number="DL1"),
            _segment("MX", flight_number="DL2"),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=segments)

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_with_transit_visa_requirement_alerts(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN"),
        ]
        segments = [
            _segment("CN", is_layover=True, flight_number="DL1"),
            _segment("MX", flight_number="DL2"),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=segments)

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        assert {i["reasons"][0] for i in _saved_alerts()} == {"No visa on file for CN"}

    def test_layover_passport_validity_still_checked(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2031-10-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        segments = [
            _segment("JP", is_layover=True, flight_number="DL1"),
            _segment("MX", flight_number="DL2"),
        ]
        event = _base_event(endpoints, travelers=travelers, segments=segments)

        response = group_handler(event, None)

        assert _results_by_traveler(response)[PRIMARY]["passport_status"] == "CRITICAL"


class TestItineraryChange:
    """Re-running the handler on a changed itinerary re-evaluates every traveler."""

    def test_reroute_to_visa_country_creates_new_alerts(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        original = _base_event(endpoints, travelers=travelers, segments=[_segment("JP")])
        changed = _base_event(endpoints, travelers=travelers, segments=[_segment("CN")])

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert first["body"]["total_alerts_sent"] == 0
        assert second["body"]["total_alerts_sent"] == 2
        assert second["body"]["group_severity"] == "CRITICAL"
        assert len(_saved_alerts()) == 2

    def test_later_departure_breaks_passport_validity(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2032-03-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        original = _base_event(endpoints, travelers=travelers)
        changed = _base_event(
            endpoints, travelers=travelers,
            segments=[_segment("JP", departure="2031-11-01", arrival="2031-11-02")],
        )

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert _results_by_traveler(first)[PRIMARY]["passport_status"] == "OK"
        assert _results_by_traveler(second)[PRIMARY]["passport_status"] == "CRITICAL"

    def test_ttl_follows_new_last_arrival(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        changed = _base_event(
            endpoints, travelers=travelers,
            segments=[
                _segment("JP", flight_number="DL1"),
                _segment("MX", departure="2031-07-10", arrival="2031-07-11",
                         flight_number="DL2"),
            ],
        )

        group_handler(changed, None)

        expected = int(
            (datetime(2031, 7, 11) + timedelta(days=30)).timestamp()
        )
        assert int(_saved_alerts()[0]["ttl"]) == expected


class TestRequirementsOverride:
    """requirements_override replaces default country rules for the request."""

    def test_override_marks_country_visa_free(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN"),
        ]
        event = _base_event(
            endpoints, travelers=travelers,
            requirements_override={
                "JP": {"country_code": "JP", "requires_visa": False,
                       "passport_validity_months": 6},
            },
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0

    def test_override_defaults_require_visa_and_6_months(self, endpoints):
        # Only nationality list given; requires_visa defaults True, validity 6 months.
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN",
                      passport_expiry="2031-10-01"),
            _traveler(COMPANION, endpoints[COMPANION], nationality="US"),
        ]
        event = _base_event(
            endpoints, travelers=travelers, segments=[_segment("MX")],
            requirements_override={"MX": {"visa_exempt_nationalities": ["US"]}},
        )

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[PRIMARY]["visa_status"] == "CRITICAL"
        assert results[PRIMARY]["passport_status"] == "CRITICAL"
        assert results[COMPANION]["visa_status"] == "OK"

    def test_override_raises_passport_validity_limit(self, endpoints):
        # 4 months of validity passes DE's default 3-month rule but not a 6-month override.
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2031-10-01"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(
            endpoints, travelers=travelers, segments=[_segment("DE")],
            requirements_override={
                "DE": {"requires_visa": True, "passport_validity_months": 6,
                       "visa_exempt_nationalities": ["US"]},
            },
        )

        response = group_handler(event, None)

        assert _results_by_traveler(response)[PRIMARY]["passport_status"] == "CRITICAL"

    def test_override_transit_visa_requirement_for_layover(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(COMPANION, endpoints[COMPANION], nationality="IN",
                      visa_records=[_visa("JP")]),
        ]
        event = _base_event(
            endpoints, travelers=travelers,
            segments=[_segment("JP", is_layover=True), _segment("MX", flight_number="DL2")],
            requirements_override={"JP": {"transit_visa_required": True}},
        )

        response = group_handler(event, None)

        results = _results_by_traveler(response)
        assert results[PRIMARY]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["visa_status"] == "OK"

    def test_override_does_not_mutate_module_defaults(self, endpoints):
        event = _base_event(
            endpoints, requirements_override={"JP": {"requires_visa": False}},
        )

        group_handler(event, None)

        assert group_mod.COUNTRY_REQUIREMENTS["JP"].requires_visa is True

    def test_empty_override_uses_defaults(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(COMPANION, endpoints[COMPANION]),
        ]
        event = _base_event(endpoints, travelers=travelers, requirements_override={})

        response = group_handler(event, None)

        assert _results_by_traveler(response)[PRIMARY]["visa_status"] == "CRITICAL"


class TestPrimaryTravelerLookup:
    """The group summary goes only to the primary traveler, when present in the group."""

    def test_unknown_primary_skips_group_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _sns_messages(endpoints[PRIMARY]) == []
        assert _sns_messages(endpoints[COMPANION]) == []

    def test_companion_as_primary_receives_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler=COMPANION)

        group_handler(event, None)

        assert len(_sns_messages(endpoints[COMPANION])) == 1
        assert _sns_messages(endpoints[PRIMARY]) == []


def _evaluation(cls, is_alert_required: bool, severity: AlertSeverity | None,
                reasons: list[str] | None = None):
    profile = SkyMilesProfile(
        skymiles_number="x", first_name="x", last_name="x", nationality="US",
        passport_number="P1", passport_expiry=date(2035, 1, 1),
        visa_records=[], endpoint_arn="arn",
    )
    return cls(
        profile=profile, segments_evaluated=[], is_alert_required=is_alert_required,
        severity=severity, reasons=reasons or [], validation_errors=[],
    )


class TestEvaluatorResultEdgeCases:
    """Handler guards against summaries for unknown travelers and alerts without severity."""

    @pytest.fixture
    def stub_group_result(self):
        """Swap in a stub evaluate_group_itinerary returning a fixed result."""
        orig = group_mod.evaluate_group_itinerary
        holder: dict = {}

        def _stub(group, requirements):
            return holder["result"]

        group_mod.evaluate_group_itinerary = _stub
        try:
            yield holder
        finally:
            group_mod.evaluate_group_itinerary = orig

    def test_alert_for_unknown_traveler_saved_but_not_pushed(self, endpoints, stub_group_result):
        stub_group_result["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number="5555555555", first_name="Ghost", last_name="Traveler",
                passport_result=_evaluation(PassportEvaluation, True,
                                            AlertSeverity.CRITICAL, ["Passport has expired"]),
                visa_result=_evaluation(VisaEvaluation, True,
                                        AlertSeverity.CRITICAL, ["No visa on file for JP"]),
            )],
            group_severity=AlertSeverity.CRITICAL,
        )
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        assert {i["skymiles_number"] for i in _saved_alerts()} == {"5555555555"}
        assert len(_sns_messages(endpoints[PRIMARY])) == 1  # group summary only
        assert _sns_messages(endpoints[COMPANION]) == []

    def test_alert_required_without_severity_is_skipped(self, endpoints, stub_group_result):
        stub_group_result["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number=PRIMARY, first_name="Pat", last_name="Traveler",
                passport_result=_evaluation(PassportEvaluation, True, None, ["?"]),
                visa_result=_evaluation(VisaEvaluation, True, None, ["?"]),
            )],
            group_severity=None,
        )
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"][0]["passport_status"] == "OK"
        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert _saved_alerts() == []


class TestParseGroupItinerary:
    """Event payloads deserialize into a GroupItinerary with typed fields."""

    def test_parses_segments_travelers_and_primary(self, endpoints):
        event = _base_event(
            endpoints,
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN",
                                 visa_records=[_visa("CN", expiry="2034-05-05")])],
        )

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert group.segments[0].departure_date == date(2031, 6, 1)
        assert group.segments[0].arrival_date == date(2031, 6, 2)
        assert group.segments[0].is_layover is False
        traveler = group.travelers[0]
        assert traveler.passport_expiry == date(2035, 1, 1)
        assert traveler.visa_records[0].expiry_date == date(2034, 5, 5)

    def test_is_layover_defaults_to_false_when_absent(self, endpoints):
        segment = _segment()
        del segment["is_layover"]
        event = _base_event(endpoints, segments=[segment])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False

    def test_layover_flag_preserved(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(is_layover=True)])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is True

    def test_missing_required_field_raises(self, endpoints):
        event = _base_event(endpoints)
        del event["primary_traveler"]

        with pytest.raises(KeyError):
            _parse_group_itinerary(event)


class TestFindTraveler:
    """_find_traveler returns the matching profile or None."""

    def _group(self, endpoints) -> GroupItinerary:
        return _parse_group_itinerary(_base_event(endpoints))

    def test_returns_matching_traveler(self, endpoints):
        group = self._group(endpoints)

        traveler = _find_traveler(group, COMPANION)

        assert traveler is not None
        assert traveler.first_name == "Casey"

    def test_returns_none_when_absent(self, endpoints):
        group = self._group(endpoints)

        assert _find_traveler(group, "0000000000") is None
