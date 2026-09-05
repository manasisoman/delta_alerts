"""Critical-path tests for the group Lambda handler end-to-end flow."""

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
    skymiles_number: str = "1000000001",
    first_name: str = "Primary",
    last_name: str = "Traveler",
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
    departure_date: str = "2030-06-01",
    arrival_date: str = "2030-06-02",
    is_layover: bool = False,
) -> dict:
    """Build a flight segment dict with sensible defaults."""
    return {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid clean group event (US national → DE) with optional overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1000000001",
        "segments": [_segment()],
        "travelers": [_traveler(endpoint_arn)],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _scan_alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


class TestCleanGroup:
    """A group with no document issues persists nothing and reports OK everywhere."""

    def test_response_shape_and_no_alerts(self, endpoint_arn):
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
                "first_name": "Primary",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            }
        ]
        assert _scan_alerts() == []


class TestPassportAlerts:
    """Passport problems persist a PASSPORT alert and surface in the traveler result."""

    def test_expired_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2020-01-01")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        result = body["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 1

        items = _scan_alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "PASSPORT"
        assert items[0]["severity"] == "CRITICAL"
        assert items[0]["itinerary_ref"] == "DL-GROUP"
        assert items[0]["skymiles_number"] == "1000000001"

    def test_missing_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, passport_number=None, passport_expiry=None)
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert [i["alert_type"] for i in _scan_alerts()] == ["PASSPORT"]

    def test_expiring_passport_is_info_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2030-10-01")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "INFO"
        assert body["group_severity"] == "INFO"
        assert body["total_alerts_sent"] == 1
        assert [i["severity"] for i in _scan_alerts()] == ["INFO"]


class TestVisaAlerts:
    """Visa problems persist a VISA alert and surface in the traveler result."""

    def test_missing_visa_persists_critical_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )

        response = group_handler(event, None)

        body = response["body"]
        result = body["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        assert body["total_alerts_sent"] == 1

        items = _scan_alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "VISA"
        assert "No visa on file for CN" in items[0]["reasons"]

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        visa = {
            "country_code": "CN",
            "visa_type": "TOURIST",
            "issue_date": "2029-01-01",
            "expiry_date": "2030-06-01",
            "visa_number": "V123",
        }
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, nationality="IN", visa_records=[visa])],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "WARNING"
        assert body["group_severity"] == "WARNING"
        assert [i["severity"] for i in _scan_alerts()] == ["WARNING"]

    def test_passport_and_visa_alerts_both_counted(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(endpoint_arn, nationality="IN", passport_expiry="2020-01-01")
            ],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] == 2
        assert body["traveler_results"][0]["alerts_sent"] == 2
        assert sorted(i["alert_type"] for i in _scan_alerts()) == ["PASSPORT", "VISA"]


class TestMultipleTravelers:
    """Each traveler is evaluated independently; group severity is the highest."""

    def test_mixed_group_reports_per_traveler(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Second",
                    passport_expiry="2020-01-01",
                ),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000003",
                    first_name="Third",
                    nationality="IN",
                ),
            ],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 2
        by_sm = {r["skymiles_number"]: r for r in body["traveler_results"]}
        assert by_sm["1000000001"]["alerts_sent"] == 0
        assert by_sm["1000000002"]["passport_status"] == "CRITICAL"
        assert by_sm["1000000002"]["alerts_sent"] == 1
        assert by_sm["1000000003"]["visa_status"] == "CRITICAL"
        assert by_sm["1000000003"]["alerts_sent"] == 1

        items = _scan_alerts()
        assert sorted(i["skymiles_number"] for i in items) == ["1000000002", "1000000003"]

    def test_unknown_primary_traveler_skips_group_notification(self, endpoint_arn):
        event = _base_event(endpoint_arn, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["total_alerts_sent"] == 0


class TestRequirementsOverride:
    """requirements_override replaces the default rules for the given country."""

    def test_override_marks_destination_visa_free(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            requirements_override={"CN": {"requires_visa": False}},
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert _scan_alerts() == []

    def test_override_with_explicit_fields(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE")],
            requirements_override={
                "DE": {
                    "country_code": "DE",
                    "requires_visa": True,
                    "transit_visa_required": True,
                    "passport_validity_months": 12,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert [i["alert_type"] for i in _scan_alerts()] == ["VISA"]

    def test_override_adds_unknown_country(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="ZZ")],
            requirements_override={"ZZ": {"visa_exempt_nationalities": ["US"]}},
        )

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"


class TestParsing:
    """Event deserialization helpers build the expected domain objects."""

    def test_parse_group_itinerary(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(), _segment(destination="CN", is_layover=True)],
            travelers=[
                _traveler(endpoint_arn),
                _traveler(endpoint_arn, skymiles_number="1000000002"),
            ],
        )

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1000000001"
        assert [s.destination for s in group.segments] == ["DE", "CN"]
        assert [s.is_layover for s in group.segments] == [False, True]
        assert group.segments[0].departure_date.isoformat() == "2030-06-01"
        assert [t.skymiles_number for t in group.travelers] == [
            "1000000001",
            "1000000002",
        ]

    def test_segment_layover_defaults_false(self, endpoint_arn):
        seg = _segment()
        del seg["is_layover"]
        event = _base_event(endpoint_arn, segments=[seg])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False

    def test_find_traveler_returns_match_or_none(self, endpoint_arn):
        group = _parse_group_itinerary(
            _base_event(
                endpoint_arn,
                travelers=[
                    _traveler(endpoint_arn),
                    _traveler(endpoint_arn, skymiles_number="1000000002"),
                ],
            )
        )

        found = _find_traveler(group, "1000000002")
        missing = _find_traveler(group, "0000000000")

        assert found is not None
        assert found.skymiles_number == "1000000002"
        assert missing is None
