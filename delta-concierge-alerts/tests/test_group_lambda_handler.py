"""Tests for the group itinerary Lambda handler end-to-end flow."""

from datetime import date

import boto3
import moto
import pytest

import src.handlers.group_lambda_handler as group_handler_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.models.types import AlertSeverity, GroupEvaluationResult

from tests.conftest import make_profile, make_segment


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
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
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


@pytest.fixture
def group_notifications(monkeypatch):
    """Capture calls to send_group_notification made by the handler."""
    calls: list[tuple[str, GroupEvaluationResult]] = []

    def _record(endpoint: str, result: GroupEvaluationResult) -> dict:
        calls.append((endpoint, result))
        return {}

    monkeypatch.setattr(group_handler_mod, "send_group_notification", _record)
    return calls


@pytest.fixture
def push_notifications(monkeypatch):
    """Capture per-traveler push notifications made by the handler."""
    calls = []

    def _record(payload) -> dict:
        calls.append(payload)
        return {}

    monkeypatch.setattr(group_handler_mod, "send_push_notification", _record)
    return calls


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = "1000000001",
    first_name: str = "Alice",
    last_name: str = "Smith",
    nationality: str = "US",
    passport_number: str | None = "P111111",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler profile dict with sensible defaults."""
    return {
        "skymiles_number": skymiles_number,
        "first_name": first_name,
        "last_name": last_name,
        "nationality": nationality,
        "passport_number": passport_number,
        "passport_expiry": passport_expiry,
        "endpoint_arn": endpoint_arn,
        "visa_records": visa_records or [],
    }


def _segment(
    destination: str = "DE",
    departure_date: str = "2027-06-01",
    arrival_date: str = "2027-06-02",
    is_layover: bool | None = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> dict:
    """Build a segment dict; ``is_layover=None`` omits the key entirely."""
    seg = {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
    }
    if is_layover is not None:
        seg["is_layover"] = is_layover
    return seg


def _visa(
    country_code: str = "CN",
    expiry_date: str = "2035-01-01",
    issue_date: str = "2025-01-01",
) -> dict:
    """Build a visa record dict with sensible defaults."""
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1000000001",
        "travelers": [_traveler(endpoint_arn)],
        "segments": [_segment()],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _result_for(response: dict, skymiles_number: str) -> dict:
    """Return the traveler_results entry for a given SkyMiles number."""
    return next(
        r for r in response["body"]["traveler_results"]
        if r["skymiles_number"] == skymiles_number
    )


def _stored_alerts(alert_type: str | None = None) -> list[dict]:
    """Return alerts persisted in the moto DynamoDB table."""
    items = alert_store_mod.table.scan()["Items"]
    if alert_type is None:
        return items
    return [i for i in items if i["alert_type"] == alert_type]


class TestHappyPath:
    """A clean group produces no alerts, OK statuses, and a group summary."""

    def test_clean_group_response_shape(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [
            {
                "skymiles_number": "1000000001",
                "first_name": "Alice",
                "last_name": "Smith",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            }
        ]
        assert _stored_alerts() == []

    def test_group_summary_sent_to_primary_traveler(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        assert len(group_notifications) == 1
        endpoint, result = group_notifications[0]
        assert endpoint == endpoint_arn
        assert result.confirmation_number == "DL-GROUP"
        assert result.group_severity is None

    def test_group_summary_uses_real_sns(self, endpoint_arn):
        """Without monkeypatching, the group summary publishes through moto SNS."""
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200


class TestPassportAlerts:
    """Passport findings persist a PASSPORT alert and push to the traveler."""

    def test_expired_passport_is_critical(self, endpoint_arn, group_notifications, push_notifications):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        result = _result_for(response, "1000000001")
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 1

        stored = _stored_alerts("PASSPORT")
        assert len(stored) == 1
        assert stored[0]["skymiles_number"] == "1000000001"
        assert stored[0]["itinerary_ref"] == "DL-GROUP"
        assert stored[0]["status"] == "ACTIVE"
        assert stored[0]["severity"] == "CRITICAL"
        assert "ttl" in stored[0]

        assert len(push_notifications) == 1
        payload = push_notifications[0]
        assert payload.endpoint_arn == endpoint_arn
        assert payload.title == "Delta Concierge \u2014 Passport Alert"
        assert payload.push_data == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_missing_passport_is_critical(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["passport_status"] == "CRITICAL"
        assert len(_stored_alerts("PASSPORT")) == 1

    def test_passport_below_destination_validity_window_is_critical(
        self, endpoint_arn, group_notifications
    ):
        """CN needs 6 months validity; 4 months remaining at travel is CRITICAL."""
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["visa_records"] = [_visa("CN")]
        event["travelers"][0]["passport_expiry"] = "2027-10-01"

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["passport_status"] == "CRITICAL"
        reasons = _stored_alerts("PASSPORT")[0]["reasons"]
        assert any("6-month validity requirement" in r for r in reasons)

    def test_passport_meeting_schengen_window_is_info_only(
        self, endpoint_arn, group_notifications, push_notifications
    ):
        """DE needs 3 months; 4 months remaining passes but is within 6 months → INFO."""
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="DE")]
        event["travelers"][0]["passport_expiry"] = "2027-10-01"

        response = group_handler(event, None)

        body = response["body"]
        assert _result_for(response, "1000000001")["passport_status"] == "INFO"
        assert body["group_severity"] == "INFO"
        assert body["total_alerts_sent"] == 1
        assert _stored_alerts("PASSPORT")[0]["severity"] == "INFO"
        assert push_notifications[0].push_data["severity"] == "INFO"

    def test_real_push_notification_via_moto_sns(self, endpoint_arn, group_notifications):
        """Without monkeypatching send_push_notification, moto SNS accepts the publish."""
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 1


class TestVisaAlerts:
    """Visa findings persist a VISA alert and push to the traveler."""

    def test_missing_visa_is_critical(self, endpoint_arn, group_notifications, push_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["nationality"] = "IN"

        response = group_handler(event, None)

        result = _result_for(response, "1000000001")
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

        stored = _stored_alerts("VISA")
        assert len(stored) == 1
        assert "No visa on file for CN" in stored[0]["reasons"]

        payload = push_notifications[0]
        assert payload.title == "Delta Concierge \u2014 Visa Alert"
        assert payload.body == "No visa on file for CN"
        assert payload.push_data["alert_type"] == "VISA"

    def test_valid_visa_produces_no_alert(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [_visa("CN")]

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "OK"
        assert _stored_alerts("VISA") == []

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN", departure_date="2027-06-01")]
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [_visa("CN", expiry_date="2027-06-01")]

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"
        assert _stored_alerts("VISA")[0]["severity"] == "WARNING"

    def test_visa_expiring_within_warning_window_is_info(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN", departure_date="2027-06-01")]
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [_visa("CN", expiry_date="2027-06-15")]

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "INFO"

    def test_visa_exempt_nationality_skips_visa_check(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="DE")]
        event["travelers"][0]["nationality"] = "US"

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "OK"

    def test_non_exempt_nationality_needs_visa_for_same_destination(
        self, endpoint_arn, group_notifications
    ):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="DE")]
        event["travelers"][0]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"


class TestLayovers:
    """Layover segments only require a visa when transit visas are required."""

    def test_layover_without_transit_requirement_is_skipped(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [
            _segment(destination="DE", is_layover=True, flight_number="DL100"),
            _segment(destination="JP", is_layover=False, flight_number="DL101",
                     departure_date="2027-06-02", arrival_date="2027-06-03"),
        ]
        event["travelers"][0]["nationality"] = "US"

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_with_transit_requirement_still_needs_visa(
        self, endpoint_arn, group_notifications
    ):
        event = _base_event(endpoint_arn)
        event["segments"] = [
            _segment(destination="CN", is_layover=True, flight_number="DL100"),
            _segment(destination="JP", is_layover=False, flight_number="DL101",
                     departure_date="2027-06-02", arrival_date="2027-06-03"),
        ]
        event["travelers"][0]["nationality"] = "US"

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"
        assert "No visa on file for CN" in _stored_alerts("VISA")[0]["reasons"]


class TestMultipleTravelers:
    """Each traveler is evaluated independently; group severity is the max."""

    def test_mixed_group_reports_per_traveler(
        self, endpoint_arn, group_notifications, push_notifications
    ):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"] = [
            _traveler(endpoint_arn, skymiles_number="1000000001", first_name="Alice",
                      nationality="US", visa_records=[_visa("CN")]),
            _traveler(endpoint_arn, skymiles_number="1000000002", first_name="Bob",
                      nationality="IN"),
            _traveler(endpoint_arn, skymiles_number="1000000003", first_name="Cara",
                      nationality="IN", passport_expiry="2020-01-01"),
        ]

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 3
        assert len(body["traveler_results"]) == 3

        alice = _result_for(response, "1000000001")
        assert (alice["passport_status"], alice["visa_status"], alice["alerts_sent"]) == ("OK", "OK", 0)

        bob = _result_for(response, "1000000002")
        assert (bob["passport_status"], bob["visa_status"], bob["alerts_sent"]) == ("OK", "CRITICAL", 1)

        cara = _result_for(response, "1000000003")
        assert (cara["passport_status"], cara["visa_status"], cara["alerts_sent"]) == ("CRITICAL", "CRITICAL", 2)
        assert cara["first_name"] == "Cara"

        assert len(_stored_alerts()) == 3
        assert len(push_notifications) == 3
        assert {p.alert_record.skymiles_number for p in push_notifications} == {
            "1000000002", "1000000003",
        }

    def test_group_severity_is_highest_across_travelers(self, endpoint_arn, group_notifications):
        """INFO for one traveler + WARNING for another → WARNING at group level."""
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN", departure_date="2027-06-01")]
        event["travelers"] = [
            _traveler(endpoint_arn, skymiles_number="1000000001", nationality="IN",
                      passport_expiry="2035-01-01",
                      visa_records=[_visa("CN", expiry_date="2027-06-15")]),
            _traveler(endpoint_arn, skymiles_number="1000000002", nationality="IN",
                      visa_records=[_visa("CN", expiry_date="2027-06-01")]),
        ]

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "INFO"
        assert _result_for(response, "1000000002")["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"


class TestGroupSummaryNotification:
    """The group summary goes to the primary traveler only when they are in the group."""

    def test_summary_skipped_when_primary_not_in_group(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert group_notifications == []

    def test_summary_sent_to_primary_endpoint_not_first_traveler(
        self, endpoint_arn, group_notifications
    ):
        event = _base_event(endpoint_arn, primary_traveler="1000000002")
        event["travelers"] = [
            _traveler(endpoint_arn, skymiles_number="1000000001"),
            _traveler("arn:aws:sns:us-east-1:000:endpoint/primary", skymiles_number="1000000002"),
        ]

        group_handler(event, None)

        assert len(group_notifications) == 1
        assert group_notifications[0][0] == "arn:aws:sns:us-east-1:000:endpoint/primary"


class TestRequirementsOverride:
    """requirements_override replaces default country rules per request."""

    def test_override_waives_visa_requirement(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["nationality"] = "IN"
        event["requirements_override"] = {"CN": {"requires_visa": False}}

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "OK"

    def test_override_defaults_apply_when_fields_omitted(self, endpoint_arn, group_notifications):
        """An empty override still requires a visa (default True) with 6-month passport rule."""
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="DE")]
        event["travelers"][0]["nationality"] = "US"
        event["travelers"][0]["passport_expiry"] = "2027-10-01"
        event["requirements_override"] = {"DE": {}}

        response = group_handler(event, None)

        result = _result_for(response, "1000000001")
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "CRITICAL"

    def test_override_full_fields_respected(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="XX", is_layover=True)]
        event["travelers"][0]["nationality"] = "IN"
        event["requirements_override"] = {
            "XX": {
                "country_code": "XX",
                "requires_visa": True,
                "transit_visa_required": True,
                "passport_validity_months": 1,
                "visa_exempt_nationalities": ["IN"],
            }
        }

        response = group_handler(event, None)

        result = _result_for(response, "1000000001")
        assert result["visa_status"] == "OK"
        assert result["passport_status"] == "OK"

    def test_empty_override_dict_uses_defaults(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN")]
        event["travelers"][0]["nationality"] = "IN"
        event["requirements_override"] = {}

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"


class TestItineraryChange:
    """Re-running the handler after an itinerary change re-evaluates from scratch."""

    def test_changed_destination_flips_visa_status(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [_visa("CN")]
        event["segments"] = [_segment(destination="CN")]

        first = group_handler(event, None)
        event["segments"] = [_segment(destination="JP")]
        second = group_handler(event, None)

        assert _result_for(first, "1000000001")["visa_status"] == "OK"
        assert _result_for(second, "1000000001")["visa_status"] == "CRITICAL"
        assert len(_stored_alerts("VISA")) == 1

    def test_later_departure_pushes_visa_past_expiry(self, endpoint_arn, group_notifications):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [_visa("CN", expiry_date="2027-07-01")]
        event["segments"] = [_segment(destination="CN", departure_date="2027-05-01",
                                      arrival_date="2027-05-02")]

        first = group_handler(event, None)
        event["segments"] = [_segment(destination="CN", departure_date="2027-08-01",
                                      arrival_date="2027-08-02")]
        second = group_handler(event, None)

        assert _result_for(first, "1000000001")["visa_status"] == "OK"
        assert _result_for(second, "1000000001")["visa_status"] == "CRITICAL"
        assert "Visa for CN expires before date of travel" in _stored_alerts("VISA")[0]["reasons"]


class TestParseGroupItinerary:
    """Event deserialization into a GroupItinerary."""

    def test_parses_all_fields(self):
        event = {
            "confirmation_number": "DL-PARSE",
            "primary_traveler": "1000000002",
            "travelers": [
                _traveler("arn:a", skymiles_number="1000000001"),
                _traveler("arn:b", skymiles_number="1000000002",
                          visa_records=[_visa("CN", expiry_date="2030-01-01")]),
            ],
            "segments": [
                _segment(destination="CN", is_layover=True, flight_number="DL1"),
                _segment(destination="JP", is_layover=None, flight_number="DL2",
                         departure_date="2027-06-02", arrival_date="2027-06-03"),
            ],
        }

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-PARSE"
        assert group.primary_traveler == "1000000002"
        assert [t.skymiles_number for t in group.travelers] == ["1000000001", "1000000002"]
        assert group.travelers[1].visa_records[0].country_code == "CN"
        assert group.travelers[1].visa_records[0].expiry_date == date(2030, 1, 1)
        assert group.travelers[1].endpoint_arn == "arn:b"

        first, second = group.segments
        assert first.flight_number == "DL1"
        assert first.destination == "CN"
        assert first.departure_date == date(2027, 6, 1)
        assert first.arrival_date == date(2027, 6, 2)
        assert first.is_layover is True
        assert second.is_layover is False

    def test_missing_required_key_raises(self):
        event = _base_event("arn:x")
        del event["primary_traveler"]

        with pytest.raises(KeyError):
            _parse_group_itinerary(event)


class TestFindTraveler:
    """Lookup of a traveler in the group by SkyMiles number."""

    def test_returns_matching_traveler(self):
        alice = make_profile()
        alice.skymiles_number = "A"
        bob = make_profile()
        bob.skymiles_number = "B"
        group = _parse_group_itinerary(_base_event("arn:x"))
        group.travelers = [alice, bob]

        assert _find_traveler(group, "B") is bob

    def test_returns_none_when_absent(self):
        group = _parse_group_itinerary(_base_event("arn:x"))

        assert _find_traveler(group, "not-there") is None

    def test_empty_group_returns_none(self):
        group = _parse_group_itinerary(_base_event("arn:x", travelers=[]))
        group.segments = [make_segment()]

        assert _find_traveler(group, "1000000001") is None
