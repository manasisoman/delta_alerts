"""Critical-path tests for the group Lambda handler end-to-end flow."""

from datetime import date

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
    skymiles_number: str = "1111111111",
    first_name: str = "Primary",
    last_name: str = "Traveler",
    nationality: str = "US",
    passport_number: str | None = "P111111",
    passport_expiry: str | None = "2030-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler profile dict with sensible defaults (clean US traveler)."""
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
    departure_date: str = "2026-09-15",
    arrival_date: str = "2026-09-16",
    **extra,
) -> dict:
    """Build a flight segment dict with sensible defaults."""
    seg = {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
    }
    seg.update(extra)
    return seg


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event; a clean US traveler flying to DE."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1111111111",
        "segments": [_segment(is_layover=False)],
        "travelers": [_traveler(endpoint_arn)],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _stored_alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


class TestHappyPath:
    """A clean group produces no alerts and an OK summary."""

    def test_clean_group_returns_ok_and_persists_nothing(self, endpoint_arn):
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
        assert _stored_alerts() == []


class TestPassportAlerts:
    """Passport failures persist a PASSPORT alert and surface in the response."""

    def test_expired_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2020-01-01")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["traveler_results"][0]["alerts_sent"] == 1

        items = _stored_alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "PASSPORT"
        assert items[0]["severity"] == "CRITICAL"
        assert items[0]["itinerary_ref"] == "DL-GROUP"
        assert items[0]["skymiles_number"] == "1111111111"

    def test_missing_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, passport_number=None, passport_expiry=None)
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert [i["alert_type"] for i in _stored_alerts()] == ["PASSPORT"]

    def test_passport_expiring_soon_persists_info_alert(self, endpoint_arn):
        # Valid for DE (3 months) but expires within 6 months of travel → INFO
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2027-01-15")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "INFO"
        assert body["group_severity"] == "INFO"
        assert body["total_alerts_sent"] == 1
        assert [i["severity"] for i in _stored_alerts()] == ["INFO"]


class TestVisaAlerts:
    """Visa failures persist a VISA alert and surface in the response."""

    def test_missing_visa_persists_critical_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 1
        assert body["traveler_results"][0]["passport_status"] == "OK"
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

        items = _stored_alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "VISA"
        assert "No visa on file for CN" in items[0]["reasons"]

    def test_visa_expiring_on_departure_reports_warning(self, endpoint_arn):
        visa = {
            "country_code": "CN",
            "visa_type": "TOURIST",
            "issue_date": "2025-01-01",
            "expiry_date": "2026-09-15",
            "visa_number": "V123",
        }
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, visa_records=[visa])],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "WARNING"
        assert body["group_severity"] == "WARNING"
        assert [i["severity"] for i in _stored_alerts()] == ["WARNING"]

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            segments=[
                _segment(destination="DE", is_layover=True),
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert _stored_alerts() == []


class TestBothAlerts:
    """A traveler failing both checks gets two alerts counted once each."""

    def test_passport_and_visa_alerts_both_recorded(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, passport_expiry="2020-01-01")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 2
        assert body["traveler_results"][0]["alerts_sent"] == 2
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert sorted(i["alert_type"] for i in _stored_alerts()) == ["PASSPORT", "VISA"]


class TestMultipleTravelers:
    """Alerts are attributed per traveler and aggregated across the group."""

    def test_per_traveler_results_and_group_totals(self, endpoint_arn):
        clean = _traveler(endpoint_arn)
        expired = _traveler(
            endpoint_arn,
            skymiles_number="2222222222",
            first_name="Second",
            passport_expiry="2020-01-01",
        )
        event = _base_event(endpoint_arn, travelers=[clean, expired])

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        assert len(body["traveler_results"]) == 2
        by_sm = {r["skymiles_number"]: r for r in body["traveler_results"]}
        assert by_sm["1111111111"]["alerts_sent"] == 0
        assert by_sm["1111111111"]["passport_status"] == "OK"
        assert by_sm["2222222222"]["alerts_sent"] == 1
        assert by_sm["2222222222"]["passport_status"] == "CRITICAL"
        assert by_sm["2222222222"]["first_name"] == "Second"

        items = _stored_alerts()
        assert len(items) == 1
        assert items[0]["skymiles_number"] == "2222222222"


class TestRequirementsOverride:
    """Overrides replace default country rules for the evaluation."""

    def test_override_disabling_visa_suppresses_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            requirements_override={
                "CN": {
                    "country_code": "CN",
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

    def test_override_with_defaults_requires_visa(self, endpoint_arn):
        # Empty override dict → requires_visa defaults to True, no exemptions
        event = _base_event(endpoint_arn, requirements_override={"DE": {}})

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1

    def test_override_for_unknown_country_is_added(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="ZZ")],
            requirements_override={
                "ZZ": {"requires_visa": True, "visa_exempt_nationalities": ["US"]}
            },
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0


class TestPrimaryTravelerNotification:
    """The group summary is only sent when the primary traveler exists."""

    def test_unknown_primary_traveler_does_not_fail(self, endpoint_arn):
        event = _base_event(endpoint_arn, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["total_alerts_sent"] == 0


class TestParseGroupItinerary:
    """Event deserialization builds the GroupItinerary model."""

    def test_parses_segments_and_travelers(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [_segment(is_layover=True)]

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1111111111"
        assert len(group.segments) == 1
        seg = group.segments[0]
        assert seg.destination == "DE"
        assert seg.departure_date == date(2026, 9, 15)
        assert seg.arrival_date == date(2026, 9, 16)
        assert seg.is_layover is True
        assert len(group.travelers) == 1
        assert group.travelers[0].skymiles_number == "1111111111"
        assert group.travelers[0].passport_expiry == date(2030, 1, 1)

    def test_is_layover_defaults_to_false(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment()])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestFindTraveler:
    """Traveler lookup by SkyMiles number."""

    def test_returns_matching_traveler(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        found = _find_traveler(group, "1111111111")

        assert found is not None
        assert found.first_name == "Primary"

    def test_returns_none_when_missing(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert _find_traveler(group, "0000000000") is None
