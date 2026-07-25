"""Critical-path tests for the group itinerary Lambda handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)


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


def _traveler(
    endpoint_arn: str,
    *,
    skymiles_number: str = "1111111111",
    first_name: str = "Ann",
    last_name: str = "Traveler",
    nationality: str = "US",
    passport_number: str | None = "P111111",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler payload with sensible, alert-free defaults."""
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
    *,
    destination: str = "DE",
    flight_number: str = "DL100",
    origin: str = "ATL",
    departure_date: str = "2030-09-15",
    arrival_date: str = "2030-09-16",
    is_layover: bool = False,
) -> dict:
    """Build a flight segment payload with sensible defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1111111111",
        "travelers": [_traveler(endpoint_arn)],
        "segments": [_segment()],
    }
    event.update(overrides)
    return event


def _alerts() -> list[dict]:
    """Return all alert records currently in the mocked DynamoDB table."""
    return alert_store_mod.table.scan()["Items"]


class TestCleanGroup:
    """A group with no document issues produces no alerts."""

    def test_happy_path_response_shape(self, endpoint_arn):
        """US nationals to DE with valid passports → OK statuses, no alerts."""
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
                "first_name": "Ann",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            }
        ]
        assert _alerts() == []


class TestPassportAlerts:
    """Passport problems produce persisted PASSPORT alerts."""

    def test_expired_passport_is_critical(self, endpoint_arn):
        """A passport expiring in the past yields a CRITICAL passport alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2020-01-01")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
        assert [a["alert_type"] for a in _alerts()] == ["PASSPORT"]

    def test_missing_passport_is_critical(self, endpoint_arn):
        """Null passport fields yield a CRITICAL passport alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, passport_number=None, passport_expiry=None)
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert response["body"]["group_severity"] == "CRITICAL"


class TestVisaAlerts:
    """Visa problems produce persisted VISA alerts."""

    def test_missing_visa_is_critical(self, endpoint_arn):
        """A non-exempt national travelling to CN without a visa is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            segments=[_segment(destination="CN")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1

        visa_alerts = [a for a in _alerts() if a["alert_type"] == "VISA"]
        assert len(visa_alerts) == 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]
        assert visa_alerts[0]["itinerary_ref"] == "DL-GROUP"
        assert visa_alerts[0]["status"] == "ACTIVE"

    def test_valid_visa_produces_no_alert(self, endpoint_arn):
        """A visa valid well past departure clears the visa check."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    nationality="IN",
                    visa_records=[
                        {
                            "country_code": "CN",
                            "visa_type": "TOURIST",
                            "issue_date": "2025-01-01",
                            "expiry_date": "2035-01-01",
                            "visa_number": "V1",
                        }
                    ],
                )
            ],
            segments=[_segment(destination="CN")],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_requiring_transit_visa_alerts(self, endpoint_arn):
        """A CN layover still requires a transit visa and alerts when missing."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            segments=[_segment(destination="CN", is_layover=True)],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        """A DE layover does not require a transit visa, so no alert is raised."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            segments=[_segment(destination="DE", is_layover=True)],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0


class TestBothAlertTypes:
    """A traveler with both problems gets two alerts."""

    def test_passport_and_visa_alerts_counted_per_traveler(self, endpoint_arn):
        """Expired passport plus missing CN visa yields two alerts."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn, nationality="IN", passport_expiry="2020-01-01"
                )
            ],
            segments=[_segment(destination="CN")],
        )

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        assert sorted(a["alert_type"] for a in _alerts()) == ["PASSPORT", "VISA"]


class TestMultipleTravelers:
    """Group aggregation across several travelers."""

    def test_mixed_group_aggregates_severity_and_alerts(self, endpoint_arn):
        """Only travelers with issues produce alerts; group severity is the max."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    visa_records=[
                        {
                            "country_code": "CN",
                            "visa_type": "TOURIST",
                            "issue_date": "2025-01-01",
                            "expiry_date": "2035-01-01",
                            "visa_number": "V1",
                        }
                    ],
                ),
                _traveler(
                    endpoint_arn,
                    skymiles_number="2222222222",
                    first_name="Bob",
                    nationality="IN",
                ),
            ],
            segments=[_segment(destination="CN")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert len(body["traveler_results"]) == 2
        assert body["traveler_results"][0]["alerts_sent"] == 0
        assert body["traveler_results"][1]["alerts_sent"] == 1
        assert body["traveler_results"][1]["skymiles_number"] == "2222222222"
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"


class TestRequirementsOverride:
    """The requirements_override payload replaces the default country data."""

    def test_override_removes_visa_requirement(self, endpoint_arn):
        """Setting requires_visa=False for CN suppresses the missing-visa alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            segments=[_segment(destination="CN")],
            requirements_override={
                "CN": {
                    "requires_visa": False,
                    "transit_visa_required": False,
                    "passport_validity_months": 6,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_override_adds_visa_requirement(self, endpoint_arn):
        """Removing US from DE's exempt list makes a US national need a visa."""
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "DE": {
                    "country_code": "DE",
                    "requires_visa": True,
                    "visa_exempt_nationalities": [],
                    "passport_validity_months": 3,
                }
            },
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"


class TestPrimaryTravelerLookup:
    """The group summary notification depends on finding the primary traveler."""

    def test_unknown_primary_traveler_skips_group_notification(self, endpoint_arn):
        """An unmatched primary_traveler still returns a normal response."""
        event = _base_event(endpoint_arn, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["total_alerts_sent"] == 0

    def test_find_traveler_returns_match_and_none(self, endpoint_arn):
        """_find_traveler matches by SkyMiles number and returns None otherwise."""
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert _find_traveler(group, "1111111111").first_name == "Ann"
        assert _find_traveler(group, "nope") is None


class TestParsing:
    """Event deserialization into a GroupItinerary."""

    def test_segments_and_travelers_are_parsed(self, endpoint_arn):
        """Dates become date objects and is_layover defaults to False."""
        event = _base_event(endpoint_arn)
        del event["segments"][0]["is_layover"]

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1111111111"
        assert len(group.travelers) == 1
        segment = group.segments[0]
        assert segment.departure_date.isoformat() == "2030-09-15"
        assert segment.arrival_date.isoformat() == "2030-09-16"
        assert segment.is_layover is False
