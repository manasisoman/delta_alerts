"""Tests for the group Lambda handler end-to-end flow."""

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
            PlatformApplicationArn=app_arn, Token="fake-token-1",
        )["EndpointArn"]
        endpoint_arn_2 = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token-2",
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
            yield {"endpoint_arn": endpoint_arn, "endpoint_arn_2": endpoint_arn_2}
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoints(_mock_aws):
    """Expose the moto SNS endpoint ARNs created by _mock_aws."""
    return _mock_aws


def _base_group_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group event with defaults that produce NO alerts (US traveler to DE)."""
    event = {
        "confirmation_number": "DL-GROUP-001",
        "primary_traveler": "1111111",
        "segments": [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ],
        "travelers": [
            {
                "skymiles_number": "1111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "US",
                "passport_number": "P111111",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoint_arn,
                "visa_records": [],
            }
        ],
        "requirements_override": None,
    }
    for key, val in overrides.items():
        event[key] = val
    return event


class TestGroupHandlerHappyPath:
    """No alerts when all travelers have valid documents."""

    def test_clean_group_returns_ok(self, endpoints):
        """US national to DE with valid passport → zero alerts, status OK."""
        event = _base_group_event(endpoints["endpoint_arn"])

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP-001"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 1
        result = body["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0

    def test_traveler_results_include_names(self, endpoints):
        """Response includes first_name, last_name, and skymiles_number."""
        event = _base_group_event(endpoints["endpoint_arn"])

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["skymiles_number"] == "1111111"
        assert result["first_name"] == "Alice"
        assert result["last_name"] == "Smith"


class TestPassportAlerts:
    """Passport issues trigger alert persistence and notifications."""

    def test_expired_passport_triggers_alert(self, endpoints):
        """Expired passport → CRITICAL passport alert saved and notification sent."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_expiry"] = "2020-01-01"
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "CN"

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] >= 1
        result = body["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"

        # Verify DynamoDB persisted the passport alert
        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [i for i in items if i["alert_type"] == "PASSPORT"]
        assert len(passport_alerts) >= 1

    def test_missing_passport_triggers_critical(self, endpoints):
        """Null passport fields → CRITICAL passport alert."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "CN"

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] >= 1


class TestVisaAlerts:
    """Visa issues trigger alert persistence and notifications."""

    def test_missing_visa_triggers_critical(self, endpoints):
        """Indian traveler to CN with no visa → CRITICAL visa alert."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "CN"

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] >= 1
        result = body["traveler_results"][0]
        assert result["visa_status"] == "CRITICAL"

        # Verify DynamoDB persisted the visa alert
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1
        assert any("No visa on file for CN" in a["reasons"] for a in visa_alerts)

    def test_expired_visa_triggers_critical(self, endpoints):
        """Visa expired before today → CRITICAL visa alert."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "CN"
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2019-01-01",
                "expiry_date": "2020-01-01",
                "visa_number": "V999",
            }
        ]

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["visa_status"] == "CRITICAL"


class TestBothPassportAndVisaAlerts:
    """Both passport and visa alerts fire for the same traveler."""

    def test_dual_alerts(self, endpoints):
        """Expired passport + missing visa → both alerts saved."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_expiry"] = "2020-01-01"
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "CN"

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] >= 2
        result = body["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "CRITICAL"

        items = alert_store_mod.table.scan()["Items"]
        types = {i["alert_type"] for i in items}
        assert "PASSPORT" in types
        assert "VISA" in types


class TestMultipleTravelers:
    """Multiple travelers in a group with different issues."""

    def test_multiple_travelers_mixed_results(self, endpoints):
        """One clean + one with expired passport → only second has alerts."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"] = [
            {
                "skymiles_number": "1111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "US",
                "passport_number": "P111111",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoints["endpoint_arn"],
                "visa_records": [],
            },
            {
                "skymiles_number": "2222222",
                "first_name": "Bob",
                "last_name": "Jones",
                "nationality": "IN",
                "passport_number": "P222222",
                "passport_expiry": "2020-01-01",
                "endpoint_arn": endpoints["endpoint_arn_2"],
                "visa_records": [],
            },
        ]
        event["segments"][0]["destination"] = "DE"

        response = group_handler(event, None)

        body = response["body"]
        assert len(body["traveler_results"]) == 2

        alice = body["traveler_results"][0]
        assert alice["passport_status"] == "OK"
        assert alice["visa_status"] == "OK"
        assert alice["alerts_sent"] == 0

        bob = body["traveler_results"][1]
        assert bob["passport_status"] == "CRITICAL"
        assert bob["alerts_sent"] >= 1

    def test_multiple_travelers_different_issues(self, endpoints):
        """One with passport issue, another with visa issue."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["segments"][0]["destination"] = "CN"
        event["travelers"] = [
            {
                "skymiles_number": "1111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "US",
                "passport_number": "P111111",
                "passport_expiry": "2020-01-01",
                "endpoint_arn": endpoints["endpoint_arn"],
                "visa_records": [
                    {
                        "country_code": "CN",
                        "visa_type": "TOURIST",
                        "issue_date": "2025-01-01",
                        "expiry_date": "2030-01-01",
                        "visa_number": "V111",
                    }
                ],
            },
            {
                "skymiles_number": "2222222",
                "first_name": "Bob",
                "last_name": "Jones",
                "nationality": "IN",
                "passport_number": "P222222",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoints["endpoint_arn_2"],
                "visa_records": [],
            },
        ]

        response = group_handler(event, None)

        body = response["body"]
        alice = body["traveler_results"][0]
        assert alice["passport_status"] == "CRITICAL"

        bob = body["traveler_results"][1]
        assert bob["visa_status"] == "CRITICAL"

        assert body["group_severity"] == "CRITICAL"


class TestRequirementsOverride:
    """The requirements_override field merges custom requirements."""

    def test_override_applied(self, endpoints):
        """Custom override makes DE require visa for US → visa alert fires."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "US"
        event["segments"][0]["destination"] = "DE"
        event["requirements_override"] = {
            "DE": {
                "country_code": "DE",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 3,
                "visa_exempt_nationalities": [],
            }
        }

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["visa_status"] == "CRITICAL"

    def test_no_override_uses_defaults(self, endpoints):
        """No requirements_override → uses COUNTRY_REQUIREMENTS defaults."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["requirements_override"] = None

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"

    def test_override_partial_fields(self, endpoints):
        """Override with only some fields uses defaults for the rest."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "XX"
        event["requirements_override"] = {
            "XX": {
                "requires_visa": True,
                "visa_exempt_nationalities": ["IN"],
            }
        }

        response = group_handler(event, None)

        # IN is exempt under override → no visa alert
        result = response["body"]["traveler_results"][0]
        assert result["visa_status"] == "OK"


class TestPrimaryTravelerNotification:
    """Group summary notification is sent to the primary traveler."""

    def test_group_notification_sent(self, endpoints):
        """Primary traveler receives group summary notification."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["segments"][0]["destination"] = "CN"

        response = group_handler(event, None)

        # Response should succeed even with notifications
        assert response["statusCode"] == 200
        # Alert was sent (visa issue for IN to CN)
        assert response["body"]["total_alerts_sent"] >= 1

    def test_primary_traveler_not_in_group(self, endpoints):
        """If primary_traveler not found, no crash (graceful None handling)."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["primary_traveler"] = "9999999"

        response = group_handler(event, None)

        assert response["statusCode"] == 200


class TestParseGroupItinerary:
    """_parse_group_itinerary correctly deserializes event into GroupItinerary."""

    def test_parses_segments_and_travelers(self, endpoints):
        """Segments and travelers are deserialized from the event dict."""
        event = {
            "confirmation_number": "DL-GROUP-002",
            "primary_traveler": "3333333",
            "segments": [
                {
                    "flight_number": "DL200",
                    "origin": "JFK",
                    "destination": "JP",
                    "departure_date": "2026-10-01",
                    "arrival_date": "2026-10-02",
                    "is_layover": False,
                },
                {
                    "flight_number": "DL201",
                    "origin": "JP",
                    "destination": "CN",
                    "departure_date": "2026-10-03",
                    "arrival_date": "2026-10-04",
                    "is_layover": True,
                },
            ],
            "travelers": [
                {
                    "skymiles_number": "3333333",
                    "first_name": "Carol",
                    "last_name": "Davis",
                    "nationality": "US",
                    "passport_number": "P333333",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoints["endpoint_arn"],
                    "visa_records": [],
                }
            ],
        }

        result = _parse_group_itinerary(event)

        assert isinstance(result, GroupItinerary)
        assert result.confirmation_number == "DL-GROUP-002"
        assert result.primary_traveler == "3333333"
        assert len(result.segments) == 2
        assert result.segments[0].flight_number == "DL200"
        assert result.segments[0].destination == "JP"
        assert result.segments[0].is_layover is False
        assert result.segments[1].is_layover is True
        assert len(result.travelers) == 1
        assert result.travelers[0].skymiles_number == "3333333"
        assert result.travelers[0].first_name == "Carol"

    def test_parses_visa_records(self, endpoints):
        """Visa records on travelers are deserialized correctly."""
        event = {
            "confirmation_number": "DL-GROUP-003",
            "primary_traveler": "4444444",
            "segments": [
                {
                    "flight_number": "DL300",
                    "origin": "ATL",
                    "destination": "CN",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
            "travelers": [
                {
                    "skymiles_number": "4444444",
                    "first_name": "Dan",
                    "last_name": "Evans",
                    "nationality": "IN",
                    "passport_number": "P444444",
                    "passport_expiry": "2030-01-01",
                    "endpoint_arn": endpoints["endpoint_arn"],
                    "visa_records": [
                        {
                            "country_code": "CN",
                            "visa_type": "BUSINESS",
                            "issue_date": "2025-01-01",
                            "expiry_date": "2030-12-31",
                            "visa_number": "V444",
                        }
                    ],
                }
            ],
        }

        result = _parse_group_itinerary(event)

        assert len(result.travelers[0].visa_records) == 1
        vr = result.travelers[0].visa_records[0]
        assert vr.country_code == "CN"
        assert vr.visa_type == "BUSINESS"


class TestFindTraveler:
    """_find_traveler looks up a traveler by skymiles_number."""

    def test_found(self, endpoints):
        """Returns the matching SkyMilesProfile when found."""
        event = _base_group_event(endpoints["endpoint_arn"])
        group = _parse_group_itinerary(event)

        result = _find_traveler(group, "1111111")

        assert isinstance(result, SkyMilesProfile)
        assert result.skymiles_number == "1111111"
        assert result.first_name == "Alice"

    def test_not_found_returns_none(self, endpoints):
        """Returns None when skymiles_number is not in the group."""
        event = _base_group_event(endpoints["endpoint_arn"])
        group = _parse_group_itinerary(event)

        result = _find_traveler(group, "0000000")

        assert result is None


class TestLayoverHandling:
    """Transit visa requirements for layover segments."""

    def test_layover_with_transit_visa_required(self, endpoints):
        """Layover in CN (transit_visa_required=True) → visa alert for IN national."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [
            {
                "flight_number": "DL400",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            },
            {
                "flight_number": "DL401",
                "origin": "CN",
                "destination": "JP",
                "departure_date": "2026-09-16",
                "arrival_date": "2026-09-17",
                "is_layover": False,
            },
        ]

        response = group_handler(event, None)

        body = response["body"]
        assert body["total_alerts_sent"] >= 1

    def test_layover_no_transit_visa_needed(self, endpoints):
        """Layover in DE (transit_visa_required=False) → no visa alert."""
        event = _base_group_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "IN"
        event["segments"] = [
            {
                "flight_number": "DL500",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            },
            {
                "flight_number": "DL501",
                "origin": "DE",
                "destination": "JP",
                "departure_date": "2026-09-16",
                "arrival_date": "2026-09-17",
                "is_layover": False,
            },
        ]
        # IN needs visa for JP final destination but DE layover should not trigger transit visa
        # Provide override to ensure DE layover doesn't trigger
        event["requirements_override"] = {
            "DE": {
                "country_code": "DE",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 3,
                "visa_exempt_nationalities": [],
            },
            "JP": {
                "country_code": "JP",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": ["IN"],
            },
        }

        response = group_handler(event, None)

        body = response["body"]
        result = body["traveler_results"][0]
        assert result["visa_status"] == "OK"
