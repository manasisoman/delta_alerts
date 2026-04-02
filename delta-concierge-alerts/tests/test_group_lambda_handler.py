"""End-to-end tests for the group Lambda handler."""

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
from src.models.types import GroupItinerary, SkyMilesProfile


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
            PlatformApplicationArn=app_arn, Token="fake-token-1",
        )["EndpointArn"]
        endpoint_arn_2 = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token-2",
        )["EndpointArn"]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield {"endpoint_arn": endpoint_arn, "endpoint_arn_2": endpoint_arn_2}
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def arns(_mock_aws):
    """Expose both moto SNS endpoint ARNs."""
    return _mock_aws


def _base_group_event(endpoint_arn: str, endpoint_arn_2: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional overrides."""
    event = {
        "confirmation_number": "GRP-001",
        "primary_traveler": "1111111",
        "travelers": [
            {
                "skymiles_number": "1111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "US",
                "passport_number": "P111",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoint_arn,
                "visa_records": [],
            },
            {
                "skymiles_number": "2222222",
                "first_name": "Bob",
                "last_name": "Jones",
                "nationality": "IN",
                "passport_number": "P222",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoint_arn_2,
                "visa_records": [],
            },
        ],
        "segments": [
            {
                "flight_number": "DL300",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ],
        "requirements_override": None,
    }
    for key, val in overrides.items():
        event[key] = val
    return event


class TestGroupHandlerHappyPath:
    """All travelers are clean — no alerts expected."""

    def test_clean_group_returns_200_no_alerts(self, arns):
        """US national to DE (visa-exempt) with valid passport → OK, 0 alerts."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        # Make both travelers US nationals to DE (visa-exempt)
        event["travelers"][1]["nationality"] = "US"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["group_severity"] == "OK"
        assert body["total_alerts_sent"] == 0
        for result in body["traveler_results"]:
            assert result["passport_status"] == "OK"
            assert result["visa_status"] == "OK"
            assert result["alerts_sent"] == 0

    def test_clean_group_no_dynamodb_records(self, arns):
        """When no alerts fire, DynamoDB should be empty."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["travelers"][1]["nationality"] = "US"

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 0


class TestExpiredPassport:
    """A traveler with an expired passport triggers CRITICAL alerts."""

    def test_expired_passport_critical(self, arns):
        """Expired passport on Bob → CRITICAL passport status for Bob."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["group_severity"] == "CRITICAL"

        bob_result = next(
            r for r in body["traveler_results"] if r["skymiles_number"] == "2222222"
        )
        assert bob_result["passport_status"] == "CRITICAL"
        assert bob_result["alerts_sent"] >= 1

    def test_expired_passport_persisted_to_dynamodb(self, arns):
        """Expired passport alert should be persisted in DynamoDB."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [
            i for i in items
            if i["alert_type"] == "PASSPORT" and i["skymiles_number"] == "2222222"
        ]
        assert len(passport_alerts) >= 1
        assert passport_alerts[0]["severity"] == "CRITICAL"
        assert passport_alerts[0]["itinerary_ref"] == "GRP-001"


class TestMissingVisa:
    """A traveler missing a required visa triggers CRITICAL alerts."""

    def test_missing_visa_for_cn_critical(self, arns):
        """IN national to CN with no visa → CRITICAL visa alert."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        # Change destination to CN (requires visa for all nationalities)
        event["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["group_severity"] == "CRITICAL"

        # Both travelers need visas for CN (no exemptions)
        for result in body["traveler_results"]:
            assert result["visa_status"] == "CRITICAL"

    def test_missing_visa_persisted_to_dynamodb(self, arns):
        """Visa alert for CN should be stored in DynamoDB."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 2  # both travelers
        for alert in visa_alerts:
            assert alert["severity"] == "CRITICAL"
            assert "No visa on file for CN" in alert["reasons"]


class TestMixedGroup:
    """One clean traveler + one with issues → alerts only for affected traveler."""

    def test_mixed_group_alerts_only_for_affected(self, arns):
        """US national (visa-exempt for DE) + IN national (needs visa for DE)."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        # Alice (US) is visa-exempt for DE; Bob (IN) is not

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]

        alice = next(
            r for r in body["traveler_results"] if r["skymiles_number"] == "1111111"
        )
        bob = next(
            r for r in body["traveler_results"] if r["skymiles_number"] == "2222222"
        )

        assert alice["passport_status"] == "OK"
        assert alice["visa_status"] == "OK"
        assert alice["alerts_sent"] == 0

        assert bob["visa_status"] == "CRITICAL"
        assert bob["alerts_sent"] >= 1

    def test_mixed_group_dynamodb_only_affected(self, arns):
        """Only Bob (IN national) should have DynamoDB records for DE visa."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        # Only Bob should have alerts
        skymiles_with_alerts = {i["skymiles_number"] for i in items}
        assert "1111111" not in skymiles_with_alerts
        assert "2222222" in skymiles_with_alerts


class TestRequirementsOverride:
    """Custom requirements_override replaces default country requirements."""

    def test_override_makes_visa_exempt(self, arns):
        """Override DE to make IN visa-exempt → Bob gets no visa alert."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["requirements_override"] = {
            "DE": {
                "country_code": "DE",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 3,
                "visa_exempt_nationalities": ["US", "GB", "CA", "IN"],
            }
        }

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "OK"
        assert body["total_alerts_sent"] == 0
        for result in body["traveler_results"]:
            assert result["visa_status"] == "OK"

    def test_override_requires_visa_for_normally_exempt(self, arns):
        """Override DE to remove US from exempt list → Alice gets visa alert."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["requirements_override"] = {
            "DE": {
                "country_code": "DE",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 3,
                "visa_exempt_nationalities": [],  # nobody is exempt
            }
        }

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        # Both travelers should now need a visa
        for result in body["traveler_results"]:
            assert result["visa_status"] == "CRITICAL"


class TestFindTraveler:
    """Edge case: _find_traveler returns None when not found."""

    def test_find_traveler_returns_none_for_unknown(self):
        """Searching for a nonexistent skymiles_number returns None."""
        group = GroupItinerary(
            confirmation_number="GRP-999",
            segments=[],
            travelers=[
                SkyMilesProfile(
                    skymiles_number="1111111",
                    first_name="Alice",
                    last_name="Smith",
                    nationality="US",
                    passport_number="P111",
                    passport_expiry=None,
                    visa_records=[],
                    endpoint_arn="arn:fake",
                ),
            ],
            primary_traveler="1111111",
        )

        result = _find_traveler(group, "9999999")

        assert result is None

    def test_find_traveler_returns_match(self):
        """Searching for an existing skymiles_number returns the profile."""
        group = GroupItinerary(
            confirmation_number="GRP-999",
            segments=[],
            travelers=[
                SkyMilesProfile(
                    skymiles_number="1111111",
                    first_name="Alice",
                    last_name="Smith",
                    nationality="US",
                    passport_number="P111",
                    passport_expiry=None,
                    visa_records=[],
                    endpoint_arn="arn:fake",
                ),
            ],
            primary_traveler="1111111",
        )

        result = _find_traveler(group, "1111111")

        assert result is not None
        assert result.skymiles_number == "1111111"
        assert result.first_name == "Alice"


class TestParseGroupItinerary:
    """Verify _parse_group_itinerary correctly deserializes events."""

    def test_parse_produces_correct_structure(self, arns):
        """Event should be deserialized into a GroupItinerary dataclass."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])

        group = _parse_group_itinerary(event)

        assert isinstance(group, GroupItinerary)
        assert group.confirmation_number == "GRP-001"
        assert group.primary_traveler == "1111111"
        assert len(group.travelers) == 2
        assert len(group.segments) == 1
        assert group.segments[0].destination == "DE"
        assert group.segments[0].is_layover is False

    def test_parse_with_layover_segment(self, arns):
        """Layover flag should be correctly parsed."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["segments"].append({
            "flight_number": "DL301",
            "origin": "DE",
            "destination": "CN",
            "departure_date": "2026-09-17",
            "arrival_date": "2026-09-18",
            "is_layover": True,
        })

        group = _parse_group_itinerary(event)

        assert len(group.segments) == 2
        assert group.segments[1].is_layover is True
        assert group.segments[1].destination == "CN"


class TestResponseShape:
    """Verify the response structure from group_handler."""

    def test_response_contains_all_fields(self, arns):
        """Response body should have confirmation_number, total_alerts_sent,
        group_severity, and traveler_results."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])

        response = group_handler(event, None)

        assert "statusCode" in response
        body = response["body"]
        assert "confirmation_number" in body
        assert "total_alerts_sent" in body
        assert "group_severity" in body
        assert "traveler_results" in body
        assert len(body["traveler_results"]) == 2

    def test_traveler_result_shape(self, arns):
        """Each traveler result should have required fields."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])

        response = group_handler(event, None)

        for result in response["body"]["traveler_results"]:
            assert "skymiles_number" in result
            assert "first_name" in result
            assert "last_name" in result
            assert "passport_status" in result
            assert "visa_status" in result
            assert "alerts_sent" in result


class TestDynamoDBRecordDetails:
    """Verify DynamoDB alert records have correct structure and content."""

    def test_alert_record_fields(self, arns):
        """Alert records should contain all expected fields."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        # Bob (IN) to DE → visa alert
        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) >= 1

        alert = items[0]
        assert "alert_id" in alert
        assert "skymiles_number" in alert
        assert "alert_type" in alert
        assert "severity" in alert
        assert "reasons" in alert
        assert "created_at" in alert
        assert "itinerary_ref" in alert
        assert "status" in alert
        assert alert["status"] == "ACTIVE"

    def test_ttl_is_set(self, arns):
        """Alert records should have a TTL value set."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) >= 1
        for item in items:
            assert "ttl" in item
            # TTL should be a reasonable future epoch timestamp
            assert int(item["ttl"]) > 0


class TestExpiredPassportAndMissingVisa:
    """Traveler with both expired passport and missing visa gets both alerts."""

    def test_both_alerts_for_same_traveler(self, arns):
        """Bob with expired passport + missing visa for CN → 2 alerts."""
        event = _base_group_event(arns["endpoint_arn"], arns["endpoint_arn_2"])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"
        event["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = group_handler(event, None)

        body = response["body"]
        bob = next(
            r for r in body["traveler_results"] if r["skymiles_number"] == "2222222"
        )
        assert bob["passport_status"] == "CRITICAL"
        assert bob["visa_status"] == "CRITICAL"
        assert bob["alerts_sent"] == 2

        # DynamoDB should have both alert types for Bob
        items = alert_store_mod.table.scan()["Items"]
        bob_alerts = [i for i in items if i["skymiles_number"] == "2222222"]
        alert_types = {a["alert_type"] for a in bob_alerts}
        assert "PASSPORT" in alert_types
        assert "VISA" in alert_types
