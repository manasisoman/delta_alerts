"""Critical-path tests for the group itinerary Lambda handler."""

from datetime import datetime, timedelta

import boto3
import moto
import pytest

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.handlers.lambda_handler import handler


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
            Name="TestGroupApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-group-token",
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


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = "1111111111",
    first_name: str = "Primary",
    last_name: str = "Traveler",
    nationality: str = "US",
    passport_number: str | None = "P111111",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler payload dict with sensible, fully compliant defaults."""
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
    departure_date: str = "2030-09-15",
    arrival_date: str = "2030-09-16",
    flight_number: str = "DL100",
    origin: str = "JFK",
    **extra,
) -> dict:
    """Build a flight segment payload dict; ``extra`` may add ``is_layover``."""
    segment = {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
    }
    segment.update(extra)
    return segment


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group event (one compliant US traveler to DE)."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1111111111",
        "travelers": [_traveler(endpoint_arn)],
        "segments": [_segment()],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _alerts_by_type(alert_type: str) -> list[dict]:
    """Scan the mocked DynamoDB table for alerts of the given type."""
    items = alert_store_mod.table.scan()["Items"]
    return [i for i in items if i["alert_type"] == alert_type]


class TestCompliantGroup:
    """A group where every traveler is compliant produces no alerts."""

    def test_no_alerts_and_ok_statuses(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [
            {
                "skymiles_number": "1111111111",
                "first_name": "Primary",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            }
        ]
        assert alert_store_mod.table.scan()["Items"] == []


class TestPassportAlerts:
    """Passport problems for a group traveler are persisted and reported."""

    def test_expired_passport_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"
        passport_alerts = _alerts_by_type("PASSPORT")
        assert len(passport_alerts) == 1
        assert passport_alerts[0]["skymiles_number"] == "1111111111"
        assert passport_alerts[0]["itinerary_ref"] == "DL-GROUP"
        assert passport_alerts[0]["status"] == "ACTIVE"
        assert passport_alerts[0]["ttl"] is not None

    def test_missing_passport_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert "Passport information is incomplete" in _alerts_by_type("PASSPORT")[0]["reasons"]

    def test_passport_below_country_minimum_is_critical(self, endpoint_arn):
        """DE requires 3 months of validity beyond departure."""
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2030-10-01"

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"


class TestVisaAlerts:
    """Visa problems for a group traveler are persisted and reported."""

    def test_missing_visa_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        visa_alerts = _alerts_by_type("VISA")
        assert len(visa_alerts) == 1
        assert "No visa on file for DE" in visa_alerts[0]["reasons"]

    def test_visa_expiring_near_travel_is_info(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "DE",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2030-10-01",
                "visa_number": "V-DE-1",
            }
        ]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "INFO"
        assert response["body"]["group_severity"] == "INFO"

    def test_valid_visa_produces_no_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "DE",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2033-01-01",
                "visa_number": "V-DE-1",
            }
        ]

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"


class TestMultipleTravelers:
    """Per-traveler results are independent and group severity is the highest."""

    def test_each_traveler_evaluated_independently(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"] = [
            _traveler(endpoint_arn),
            _traveler(
                endpoint_arn,
                skymiles_number="2222222222",
                first_name="Second",
                nationality="IN",
            ),
            _traveler(
                endpoint_arn,
                skymiles_number="3333333333",
                first_name="Third",
                passport_expiry="2020-01-01",
            ),
        ]

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 2
        statuses = {
            r["skymiles_number"]: (r["passport_status"], r["visa_status"], r["alerts_sent"])
            for r in body["traveler_results"]
        }
        assert statuses["1111111111"] == ("OK", "OK", 0)
        assert statuses["2222222222"] == ("OK", "CRITICAL", 1)
        assert statuses["3333333333"] == ("CRITICAL", "OK", 1)
        assert len(_alerts_by_type("VISA")) == 1
        assert len(_alerts_by_type("PASSPORT")) == 1

    def test_traveler_with_both_alerts_counts_twice(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"] = [
            _traveler(
                endpoint_arn,
                nationality="IN",
                passport_expiry="2020-01-01",
            )
        ]

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2


class TestLayoverSegments:
    """Layover segments only trigger visa checks when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [
            _segment(destination="DE", is_layover=True),
            _segment(
                destination="ZZ",
                flight_number="DL101",
                origin="FRA",
                departure_date="2030-09-16",
                arrival_date="2030-09-17",
            ),
        ]
        event["requirements_override"] = {
            "ZZ": {"requires_visa": False, "passport_validity_months": 0},
        }

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        """CN requires a transit visa, so a CN layover still needs a visa."""
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [_segment(destination="CN", is_layover=True)]

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert "No visa on file for CN" in _alerts_by_type("VISA")[0]["reasons"]

    def test_layover_defaults_to_false_when_absent(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestRequirementsOverride:
    """requirements_override replaces the default country requirements."""

    def test_override_waives_visa_requirement(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["requirements_override"] = {
            "DE": {"requires_visa": False, "passport_validity_months": 3},
        }

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"

    def test_override_exempts_nationality(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["requirements_override"] = {
            "DE": {
                "requires_visa": True,
                "visa_exempt_nationalities": ["IN"],
                "passport_validity_months": 3,
            },
        }

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0

    def test_override_raises_passport_validity_requirement(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2031-01-01"
        event["requirements_override"] = {
            "DE": {
                "requires_visa": False,
                "passport_validity_months": 24,
            },
        }

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"

    def test_override_defaults_country_code_to_key(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [_segment(destination="XX")]
        event["requirements_override"] = {
            "XX": {"requires_visa": True, "passport_validity_months": 0},
        }

        response = group_handler(event, None)

        assert "No visa on file for XX" in _alerts_by_type("VISA")[0]["reasons"]


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_added_segment_introduces_new_alert(self, endpoint_arn):
        original = _base_event(endpoint_arn)
        original["travelers"][0]["nationality"] = "US"

        first_response = group_handler(original, None)
        assert first_response["body"]["total_alerts_sent"] == 0

        changed = _base_event(endpoint_arn)
        changed["travelers"][0]["nationality"] = "US"
        changed["segments"] = [
            _segment(destination="DE"),
            _segment(
                destination="CN",
                flight_number="DL900",
                origin="FRA",
                departure_date="2030-09-20",
                arrival_date="2030-09-21",
            ),
        ]

        second_response = group_handler(changed, None)

        assert second_response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert second_response["body"]["total_alerts_sent"] == 1

    def test_ttl_uses_latest_arrival_date(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"
        event["segments"] = [
            _segment(destination="DE"),
            _segment(
                destination="CN",
                flight_number="DL900",
                origin="FRA",
                departure_date="2030-09-20",
                arrival_date="2030-09-25",
            ),
        ]

        group_handler(event, None)

        expected = int(
            (datetime(2030, 9, 25) + timedelta(days=30)).timestamp()
        )
        assert int(_alerts_by_type("PASSPORT")[0]["ttl"]) == expected


class TestGroupSummaryNotification:
    """The group summary notification targets the primary traveler only."""

    def test_summary_sent_to_primary_traveler(self, endpoint_arn, monkeypatch):
        sent: list[tuple[str, object]] = []
        monkeypatch.setattr(
            group_mod,
            "send_group_notification",
            lambda arn, result: sent.append((arn, result)),
        )
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        assert len(sent) == 1
        assert sent[0][0] == endpoint_arn

    def test_summary_skipped_when_primary_not_in_group(self, endpoint_arn, monkeypatch):
        sent: list[tuple[str, object]] = []
        monkeypatch.setattr(
            group_mod,
            "send_group_notification",
            lambda arn, result: sent.append((arn, result)),
        )
        event = _base_event(endpoint_arn, primary_traveler="9999999999")

        response = group_handler(event, None)

        assert sent == []
        assert response["statusCode"] == 200


class TestParsing:
    """Event deserialization produces a fully populated GroupItinerary."""

    def test_parse_group_itinerary_fields(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(destination="CN", is_layover=True)]
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2033-01-01",
                "visa_number": "V-CN-1",
            }
        ]

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1111111111"
        assert len(group.travelers) == 1
        assert group.travelers[0].visa_records[0].visa_number == "V-CN-1"
        segment = group.segments[0]
        assert segment.destination == "CN"
        assert segment.is_layover is True
        assert segment.departure_date.isoformat() == "2030-09-15"
        assert segment.arrival_date.isoformat() == "2030-09-16"

    def test_find_traveler_returns_match_and_none(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"].append(
            _traveler(endpoint_arn, skymiles_number="2222222222", first_name="Second")
        )
        group = _parse_group_itinerary(event)

        assert _find_traveler(group, "2222222222").first_name == "Second"
        assert _find_traveler(group, "0000000000") is None


class TestHandlerRouting:
    """The single-traveler handler delegates group payloads to group_handler."""

    def test_travelers_key_routes_to_group_handler(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert "traveler_results" in response["body"]
