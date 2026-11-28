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
SECOND = "1000000002"
THIRD = "1000000003"

# Made-up country codes keep tests isolated from real COUNTRY_REQUIREMENTS.
OVERRIDES = {
    "XA": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US"],
    },
    "XB": {
        "requires_visa": True,
        "transit_visa_required": True,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
    "XC": {
        "requires_visa": False,
        "passport_validity_months": 6,
    },
}


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the alerts table and one SNS endpoint per traveler."""
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
            for number in (PRIMARY, SECOND, THIRD)
        }

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoints
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoints(_mock_aws):
    """Map of SkyMiles number to moto SNS endpoint ARN."""
    return _mock_aws


def _messages(endpoint_arn: str) -> list[dict]:
    """Return the decoded JSON messages pushed to a moto SNS endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    raw = backend.platform_endpoints[endpoint_arn].messages
    return [json.loads(m) for m in raw.values()]


def _alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


def _traveler(number: str, endpoint_arn: str, **overrides) -> dict:
    traveler = {
        "skymiles_number": number,
        "first_name": f"First{number[-1]}",
        "last_name": "Traveler",
        "nationality": "US",
        "passport_number": f"P{number}",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    traveler.update(overrides)
    return traveler


def _segment(**overrides) -> dict:
    segment = {
        "flight_number": "DL200",
        "origin": "ATL",
        "destination": "XA",
        "departure_date": "2030-06-01",
        "arrival_date": "2030-06-02",
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


def _visa(country_code: str, expiry: str = "2035-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "tourist",
        "issue_date": "2029-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoints: dict, travelers: list[dict] | None = None, **overrides) -> dict:
    """Build a clean single-traveler group event; overrides replace top-level keys."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "travelers": travelers or [_traveler(PRIMARY, endpoints[PRIMARY])],
        "segments": [_segment()],
        "requirements_override": OVERRIDES,
    }
    event.update(overrides)
    return event


def _result_for(body: dict, number: str) -> dict:
    return next(r for r in body["traveler_results"] if r["skymiles_number"] == number)


class TestCleanGroup:
    """A fully compliant group produces no alerts but still notifies the primary traveler."""

    def test_response_shape_for_clean_group(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [{
            "skymiles_number": PRIMARY,
            "first_name": "First1",
            "last_name": "Traveler",
            "passport_status": "OK",
            "visa_status": "OK",
            "alerts_sent": 0,
        }]

    def test_no_alerts_persisted(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert _alerts() == []

    def test_group_summary_sent_to_primary(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        messages = _messages(endpoints[PRIMARY])
        assert len(messages) == 1
        assert messages[0]["default"] == "All 1 travelers are cleared for travel."


class TestPassportAlerts:
    """Passport issues are persisted as PASSPORT alerts and pushed to the traveler."""

    def test_missing_passport_expiry_is_critical(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry=None)
        event = _base_event(endpoints, travelers=[traveler])

        body = group_handler(event, None)["body"]

        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        result = _result_for(body, PRIMARY)
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 1

    def test_passport_alert_persisted(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01")
        event = _base_event(endpoints, travelers=[traveler])

        group_handler(event, None)

        items = _alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "PASSPORT"
        assert items[0]["severity"] == "CRITICAL"
        assert items[0]["itinerary_ref"] == "DL-GROUP"
        assert items[0]["status"] == "ACTIVE"
        assert items[0]["reasons"] == ["Passport has expired"]

    def test_passport_push_payload(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01")
        event = _base_event(endpoints, travelers=[traveler])

        group_handler(event, None)

        pushes = [m for m in _messages(endpoints[PRIMARY]) if "alert_type" in json.loads(m["APNS"])]
        assert len(pushes) == 1
        apns = json.loads(pushes[0]["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert apns["aps"]["alert"]["body"] == "Passport has expired"
        assert apns["alert_type"] == "PASSPORT"
        assert apns["severity"] == "CRITICAL"
        assert apns["confirmation_number"] == "DL-GROUP"

    def test_below_country_minimum_is_critical(self, endpoints):
        """XA needs 3 months; a passport expiring 1 month after departure fails."""
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2030-07-01")
        event = _base_event(endpoints, travelers=[traveler])

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["passport_status"] == "CRITICAL"
        assert "3-month validity requirement" in _alerts()[0]["reasons"][0]

    def test_within_six_months_but_meets_minimum_is_info(self, endpoints):
        """XA's 3-month rule is met but expiry is within 6 months -> INFO alert still sent."""
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2030-10-01")
        event = _base_event(endpoints, travelers=[traveler])

        body = group_handler(event, None)["body"]

        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "INFO"
        assert _result_for(body, PRIMARY)["passport_status"] == "INFO"


class TestVisaAlerts:
    """Visa issues are persisted as VISA alerts and pushed to the traveler."""

    def test_missing_visa_is_critical(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="XB")])

        body = group_handler(event, None)["body"]

        result = _result_for(body, PRIMARY)
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "OK"
        assert result["alerts_sent"] == 1
        items = _alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "VISA"
        assert items[0]["reasons"] == ["No visa on file for XB"]

    def test_visa_push_payload(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="XB")])

        group_handler(event, None)

        pushes = [m for m in _messages(endpoints[PRIMARY]) if "alert_type" in json.loads(m["APNS"])]
        assert len(pushes) == 1
        gcm = json.loads(pushes[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for XB"
        assert gcm["data"] == {
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_valid_visa_clears_alert(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], visa_records=[_visa("XB")])
        event = _base_event(endpoints, travelers=[traveler], segments=[_segment(destination="XB")])

        body = group_handler(event, None)["body"]

        assert body["total_alerts_sent"] == 0
        assert _result_for(body, PRIMARY)["visa_status"] == "OK"

    def test_visa_expiring_on_travel_date_is_warning(self, endpoints):
        traveler = _traveler(
            PRIMARY, endpoints[PRIMARY], visa_records=[_visa("XB", expiry="2030-06-01")]
        )
        event = _base_event(endpoints, travelers=[traveler], segments=[_segment(destination="XB")])

        body = group_handler(event, None)["body"]

        assert body["group_severity"] == "WARNING"
        assert _result_for(body, PRIMARY)["visa_status"] == "WARNING"

    def test_visa_expires_before_departure_is_critical(self, endpoints):
        traveler = _traveler(
            PRIMARY, endpoints[PRIMARY], visa_records=[_visa("XB", expiry="2030-05-01")]
        )
        event = _base_event(endpoints, travelers=[traveler], segments=[_segment(destination="XB")])

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["visa_status"] == "CRITICAL"
        assert _alerts()[0]["reasons"] == ["Visa for XB expires before date of travel"]

    def test_visa_exempt_nationality_skipped(self, endpoints):
        """US nationals are exempt for XA, IN nationals are not."""
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")
        event = _base_event(endpoints, travelers=[traveler])

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["visa_status"] == "CRITICAL"


class TestLayovers:
    """Layover segments only trigger visa alerts when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_skipped(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")
        segments = [
            _segment(destination="XA", is_layover=True),
            _segment(flight_number="DL201", destination="XC",
                     departure_date="2030-06-02", arrival_date="2030-06-03"),
        ]
        event = _base_event(endpoints, travelers=[traveler], segments=segments)

        body = group_handler(event, None)["body"]

        assert body["total_alerts_sent"] == 0

    def test_layover_with_transit_visa_requirement_alerts(self, endpoints):
        segments = [
            _segment(destination="XB", is_layover=True),
            _segment(flight_number="DL201", destination="XC",
                     departure_date="2030-06-02", arrival_date="2030-06-03"),
        ]
        event = _base_event(endpoints, segments=segments)

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["visa_status"] == "CRITICAL"
        assert _alerts()[0]["reasons"] == ["No visa on file for XB"]

    def test_is_layover_defaults_to_false(self, endpoints):
        segment = _segment()
        del segment["is_layover"]
        event = _base_event(endpoints, segments=[segment])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestCombinedAlerts:
    """A traveler with both passport and visa issues gets two alerts and two pushes."""

    def test_both_alerts_counted(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01")
        event = _base_event(endpoints, travelers=[traveler], segments=[_segment(destination="XB")])

        body = group_handler(event, None)["body"]

        assert body["total_alerts_sent"] == 2
        assert _result_for(body, PRIMARY)["alerts_sent"] == 2
        assert sorted(i["alert_type"] for i in _alerts()) == ["PASSPORT", "VISA"]

    def test_both_pushes_plus_group_summary(self, endpoints):
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01")
        event = _base_event(endpoints, travelers=[traveler], segments=[_segment(destination="XB")])

        group_handler(event, None)

        assert len(_messages(endpoints[PRIMARY])) == 3


class TestMultiTravelerGroup:
    """Per-traveler results are independent and group severity is the highest across travelers."""

    def _event(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], visa_records=[_visa("XB")]),
            _traveler(SECOND, endpoints[SECOND]),
            _traveler(THIRD, endpoints[THIRD], visa_records=[_visa("XB", expiry="2030-06-01")]),
        ]
        return _base_event(endpoints, travelers=travelers, segments=[_segment(destination="XB")])

    def test_group_severity_is_highest(self, endpoints):
        body = group_handler(self._event(endpoints), None)["body"]

        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 2

    def test_per_traveler_statuses(self, endpoints):
        body = group_handler(self._event(endpoints), None)["body"]

        assert [r["skymiles_number"] for r in body["traveler_results"]] == [PRIMARY, SECOND, THIRD]
        assert _result_for(body, PRIMARY)["visa_status"] == "OK"
        assert _result_for(body, SECOND)["visa_status"] == "CRITICAL"
        assert _result_for(body, THIRD)["visa_status"] == "WARNING"

    def test_alerts_pushed_to_each_travelers_own_endpoint(self, endpoints):
        group_handler(self._event(endpoints), None)

        assert len(_messages(endpoints[SECOND])) == 1
        assert len(_messages(endpoints[THIRD])) == 1
        # Primary has no personal alert, only the group summary
        primary_messages = _messages(endpoints[PRIMARY])
        assert len(primary_messages) == 1
        assert primary_messages[0]["default"].startswith("2 of 3 travelers need action:")

    def test_alerts_persisted_per_traveler(self, endpoints):
        group_handler(self._event(endpoints), None)

        assert sorted(i["skymiles_number"] for i in _alerts()) == [SECOND, THIRD]


class TestPrimaryTravelerNotification:
    """The group summary is only sent when the primary traveler is part of the group."""

    def test_unknown_primary_skips_group_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []

    def test_unknown_primary_still_sends_personal_alerts(self, endpoints):
        event = _base_event(
            endpoints, primary_traveler="0000000000", segments=[_segment(destination="XB")]
        )

        group_handler(event, None)

        messages = _messages(endpoints[PRIMARY])
        assert len(messages) == 1
        assert json.loads(messages[0]["APNS"])["alert_type"] == "VISA"


class TestRequirementsOverride:
    """Overrides replace country rules and fill missing fields with defaults."""

    def test_override_defaults_require_visa_and_six_months(self, endpoints):
        """An empty override entry defaults to visa required and 6-month validity."""
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2030-10-01")
        event = _base_event(
            endpoints,
            travelers=[traveler],
            segments=[_segment(destination="XD")],
            requirements_override={"XD": {}},
        )

        body = group_handler(event, None)["body"]

        result = _result_for(body, PRIMARY)
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "CRITICAL"
        reasons = {i["alert_type"]: i["reasons"] for i in _alerts()}
        assert reasons["VISA"] == ["No visa on file for XD"]
        assert "XD's 6-month validity requirement" in reasons["PASSPORT"][0]

    def test_override_replaces_existing_country_rules(self, endpoints):
        """Overriding a real country code (DE) to not require a visa clears the alert."""
        traveler = _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")
        event = _base_event(
            endpoints,
            travelers=[traveler],
            segments=[_segment(destination="DE")],
            requirements_override={"DE": {"requires_visa": False}},
        )

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["visa_status"] == "OK"

    def test_no_override_uses_default_requirements(self, endpoints):
        """Without overrides, an unknown country falls back to visa-required defaults."""
        event = _base_event(
            endpoints, segments=[_segment(destination="XA")], requirements_override=None
        )

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["visa_status"] == "CRITICAL"

    def test_missing_override_key_uses_default_requirements(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="XA")])
        del event["requirements_override"]

        body = group_handler(event, None)["body"]

        assert _result_for(body, PRIMARY)["visa_status"] == "CRITICAL"


class TestItineraryChange:
    """Re-running the handler for a changed itinerary reflects the new segments."""

    def test_changed_destination_triggers_new_alert(self, endpoints):
        original = _base_event(endpoints, segments=[_segment(destination="XC")])
        changed = _base_event(endpoints, segments=[_segment(destination="XB")])

        first = group_handler(original, None)["body"]
        second = group_handler(changed, None)["body"]

        assert first["total_alerts_sent"] == 0
        assert second["total_alerts_sent"] == 1
        assert [i["reasons"] for i in _alerts()] == [["No visa on file for XB"]]

    def test_changed_departure_beyond_visa_expiry(self, endpoints):
        traveler = _traveler(
            PRIMARY, endpoints[PRIMARY], visa_records=[_visa("XB", expiry="2030-12-31")]
        )
        original = _base_event(
            endpoints, travelers=[traveler], segments=[_segment(destination="XB")]
        )
        changed = _base_event(
            endpoints,
            travelers=[traveler],
            segments=[_segment(destination="XB", departure_date="2031-02-01",
                               arrival_date="2031-02-02")],
        )

        first = group_handler(original, None)["body"]
        second = group_handler(changed, None)["body"]

        assert _result_for(first, PRIMARY)["visa_status"] == "OK"
        assert _result_for(second, PRIMARY)["visa_status"] == "CRITICAL"


class TestAlertTtl:
    """Alert TTL is 30 days after the last arrival across all segments."""

    def test_ttl_uses_latest_arrival(self, endpoints):
        segments = [
            _segment(destination="XB", arrival_date="2030-06-02"),
            _segment(flight_number="DL201", destination="XC",
                     departure_date="2030-06-10", arrival_date="2030-06-11"),
        ]
        event = _base_event(endpoints, segments=segments)

        group_handler(event, None)

        expected = int(datetime.combine(date(2030, 6, 11) + timedelta(days=30),
                                        datetime.min.time()).timestamp())
        assert int(_alerts()[0]["ttl"]) == expected


class TestParseGroupItinerary:
    """Event deserialization into a GroupItinerary."""

    def test_parses_segments_and_travelers(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], visa_records=[_visa("XB")]),
            _traveler(SECOND, endpoints[SECOND]),
        ]
        event = _base_event(endpoints, travelers=travelers,
                            segments=[_segment(is_layover=True)])

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert group.segments[0].departure_date == date(2030, 6, 1)
        assert group.segments[0].arrival_date == date(2030, 6, 2)
        assert group.segments[0].is_layover is True
        assert [t.skymiles_number for t in group.travelers] == [PRIMARY, SECOND]
        assert group.travelers[0].visa_records[0].expiry_date == date(2035, 1, 1)
        assert group.travelers[1].passport_expiry == date(2040, 1, 1)


class TestFindTraveler:
    """Traveler lookup by SkyMiles number."""

    def test_returns_matching_traveler(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY]), _traveler(SECOND, endpoints[SECOND])]
        group = _parse_group_itinerary(_base_event(endpoints, travelers=travelers))

        found = _find_traveler(group, SECOND)

        assert found is group.travelers[1]

    def test_returns_none_when_missing(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, "0000000000") is None


class TestSummaryWithoutTraveler:
    """Alerts for a summary with no matching traveler are persisted but not pushed."""

    def test_alert_saved_without_push(self, endpoints):
        def fake_evaluate(group, requirements):
            profile = group.travelers[0]
            return GroupEvaluationResult(
                confirmation_number=group.confirmation_number,
                traveler_summaries=[TravelerAlertSummary(
                    skymiles_number="0000000000",
                    first_name="Ghost",
                    last_name="Traveler",
                    passport_result=PassportEvaluation(
                        profile=profile, segments_evaluated=group.segments,
                        is_alert_required=True, severity=AlertSeverity.CRITICAL,
                        reasons=["Passport has expired"], validation_errors=[],
                    ),
                    visa_result=VisaEvaluation(
                        profile=profile, segments_evaluated=group.segments,
                        is_alert_required=True, severity=AlertSeverity.WARNING,
                        reasons=["Visa for XB expires on date of travel"],
                        validation_errors=[],
                    ),
                )],
                group_severity=AlertSeverity.CRITICAL,
            )

        event = _base_event(endpoints)
        original = group_mod.evaluate_group_itinerary
        group_mod.evaluate_group_itinerary = fake_evaluate
        try:
            body = group_handler(event, None)["body"]
        finally:
            group_mod.evaluate_group_itinerary = original

        assert body["total_alerts_sent"] == 2
        assert body["group_severity"] == "CRITICAL"
        assert sorted(i["skymiles_number"] for i in _alerts()) == ["0000000000"] * 2
        # Primary only receives the group summary, no personal pushes
        messages = _messages(endpoints[PRIMARY])
        assert len(messages) == 1
        assert messages[0]["default"].startswith("1 of 1 travelers need action: Ghost Traveler")
