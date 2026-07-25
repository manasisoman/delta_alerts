"""Critical-path tests for the Lambda handler end-to-end flow."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import handler


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
    with moto.mock_aws():
        # DynamoDB
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

        # SNS
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        # Save original module-level boto3 clients
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        # Patch module-level boto3 clients to use the moto context
        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid Lambda event with optional field overrides."""
    event = {
        "profile": {
            "skymiles_number": "1234567890",
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-TEST",
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }
    for key, val in overrides.items():
        if key in event["profile"]:
            event["profile"][key] = val
    return event


class TestHandlerEndToEnd:
    """Full round-trip: event → evaluations → DynamoDB + SNS → response."""

    def test_alerts_persisted_and_response_shape(self, endpoint_arn):
        """Missing visa for CN should persist alert and return CRITICAL."""
        event = _base_event(endpoint_arn)
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["visa_status"] == "CRITICAL"
        assert body["alerts_sent"] >= 1

        # Verify DynamoDB has the record
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]

    def test_no_alerts_for_clean_traveler(self, endpoint_arn):
        """US national to DE with valid passport → no alerts."""
        event = _base_event(endpoint_arn)
        event["profile"]["nationality"] = "US"
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["passport_status"] == "OK"
        assert body["visa_status"] == "OK"
        assert body["alerts_sent"] == 0

    def test_expired_passport_triggers_critical(self, endpoint_arn):
        """Expired passport should return CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] >= 1

    def test_missing_passport_triggers_critical(self, endpoint_arn):
        """Null passport fields should return CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_number"] = None
        event["profile"]["passport_expiry"] = None

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] >= 1


def _visa_record(country: str = "CN", expiry: str = "2030-01-01", **overrides) -> dict:
    """Build a visa record dict for an event profile."""
    record = {
        "country_code": country,
        "visa_type": "TOURIST",
        "issue_date": "2024-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country}-{expiry}",
    }
    record.update(overrides)
    return record


def _segment(destination: str = "CN", **overrides) -> dict:
    """Build a flight segment dict for an event itinerary."""
    segment = {
        "flight_number": "DL100",
        "origin": "JFK",
        "destination": destination,
        "departure_date": "2026-09-15",
        "arrival_date": "2026-09-16",
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


class TestRequirementsOverride:
    """Overrides in the event replace the default country requirements."""

    def test_override_removing_visa_requirement_suppresses_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {
                "requires_visa": False,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": [],
            }
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_exempt_nationality_suppresses_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {"visa_exempt_nationalities": ["IN"]}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_raises_passport_validity_requirement(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2026-10-01"
        event["requirements_override"] = {
            "CN": {"requires_visa": False, "passport_validity_months": 24}
        }

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1


class TestLayoverHandling:
    """Layover segments only require a visa when transit visas are required."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("CN", is_layover=True)]
        event["requirements_override"] = {
            "CN": {"requires_visa": True, "transit_visa_required": False}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_layover_requiring_transit_visa_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("CN", is_layover=True)]
        event["requirements_override"] = {
            "CN": {"requires_visa": True, "transit_visa_required": True}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1


class TestVisaRecordSelection:
    """The visa record with the latest expiry date wins for a destination."""

    def test_latest_expiry_visa_clears_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            _visa_record("CN", "2020-01-01"),
            _visa_record("CN", "2030-01-01"),
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_all_expired_visas_still_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [
            _visa_record("CN", "2019-01-01"),
            _visa_record("CN", "2020-01-01"),
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert "Visa for CN has expired" in visa_alerts[0]["reasons"]

    def test_visa_for_other_country_does_not_match(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [_visa_record("JP", "2030-01-01")]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestChangedItinerary:
    """Re-evaluating a modified itinerary reflects the new segments."""

    def test_added_destination_produces_additional_alert_reason(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["profile"]["visa_records"] = [_visa_record("CN", "2030-01-01")]
        event["itinerary"]["segments"] = [
            _segment("CN"),
            _segment("IN", flight_number="DL200", departure_date="2026-09-20",
                     arrival_date="2026-09-21"),
        ]

        response = handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert response["body"]["visa_status"] == "CRITICAL"
        assert "No visa on file for IN" in visa_alerts[0]["reasons"]

    def test_ttl_follows_latest_arrival_date(self, endpoint_arn):
        from datetime import date, datetime, timedelta

        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [
            _segment("CN"),
            _segment("CN", flight_number="DL200", departure_date="2026-10-01",
                     arrival_date="2026-10-05"),
        ]

        handler(event, None)

        expected = int(
            (datetime.combine(date(2026, 10, 5), datetime.min.time())
             + timedelta(days=30)).timestamp()
        )
        items = alert_store_mod.table.scan()["Items"]
        assert int(items[0]["ttl"]) == expected


class TestComputeTtl:
    """TTL falls back to today when the itinerary has no segments."""

    def test_empty_itinerary_uses_today(self):
        from datetime import date, datetime, timedelta

        from src.handlers.lambda_handler import _compute_ttl
        from src.models.types import Itinerary

        ttl = _compute_ttl(Itinerary(confirmation_number="DL-EMPTY", segments=[]))

        expected = int(
            (datetime.combine(date.today(), datetime.min.time())
             + timedelta(days=30)).timestamp()
        )
        assert ttl == expected


class TestEvaluateItinerary:
    """The Bedrock-facing wrapper delegates to the Lambda handler."""

    def test_wrapper_returns_handler_response(self, endpoint_arn):
        from src.handlers.lambda_handler import evaluate_itinerary

        response = evaluate_itinerary(_base_event(endpoint_arn))

        assert response["statusCode"] == 200
        assert response["body"]["visa_status"] == "CRITICAL"


class TestGroupDelegation:
    """Events containing travelers are delegated to the group handler."""

    def _group_event(self, endpoint_arn: str) -> dict:
        traveler = dict(_base_event(endpoint_arn)["profile"])
        second = dict(traveler)
        second["skymiles_number"] = "2222222222"
        second["passport_expiry"] = "2020-01-01"
        return {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": traveler["skymiles_number"],
            "travelers": [traveler, second],
            "segments": [_segment("CN")],
        }

    def test_group_event_returns_per_traveler_results(self, endpoint_arn):
        response = handler(self._group_event(endpoint_arn), None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert len(body["traveler_results"]) == 2
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] >= 3
