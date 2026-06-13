"""Tests for the group Lambda handler end-to-end flow."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import group_handler


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
            # Restore original clients
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoints(_mock_aws):
    """Expose the moto SNS endpoint ARNs created by _mock_aws."""
    return _mock_aws


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with sensible defaults."""
    event = {
        "confirmation_number": "DL-GRP-001",
        "primary_traveler": "1111111111",
        "segments": [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ],
        "travelers": [
            {
                "skymiles_number": "1111111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "IN",
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
    """Group handler returns correct response when all travelers are clean."""

    def test_no_alerts_for_visa_exempt_traveler(self, endpoints):
        """US national traveling to DE (visa exempt) with valid passport → no alerts."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "US"
        event["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GRP-001"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 1
        assert body["traveler_results"][0]["passport_status"] == "OK"
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["traveler_results"][0]["alerts_sent"] == 0


class TestPassportAlertProcessing:
    """Passport issues trigger save_alert and push notifications."""

    def test_expired_passport_triggers_critical(self, endpoints):
        """Expired passport should persist alert and send notification."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["alerts_sent"] >= 1

        # Verify DynamoDB has passport alert
        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [i for i in items if i["alert_type"] == "PASSPORT"]
        assert len(passport_alerts) >= 1

    def test_missing_passport_triggers_critical(self, endpoints):
        """Null passport fields should return CRITICAL passport status."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"

    def test_passport_insufficient_validity_months(self, endpoints):
        """Passport valid but below country's required validity months → CRITICAL."""
        event = _base_event(endpoints["endpoint_arn"])
        # CN requires 6 months validity; set passport to expire 3 months after travel
        event["travelers"][0]["passport_expiry"] = "2026-12-01"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"


class TestVisaAlertProcessing:
    """Visa issues trigger save_alert and push notifications."""

    def test_missing_visa_for_cn_triggers_critical(self, endpoints):
        """IN national to CN with no visa → CRITICAL visa alert."""
        event = _base_event(endpoints["endpoint_arn"])

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert body["total_alerts_sent"] >= 1

        # Verify DynamoDB has visa alert
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1

    def test_expired_visa_triggers_critical(self, endpoints):
        """Traveler with an expired visa should get CRITICAL visa alert."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2019-01-01",
                "expiry_date": "2020-01-01",
                "visa_number": "V999999",
            }
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

    def test_valid_visa_no_alert(self, endpoints):
        """Traveler with valid visa for destination → no visa alert."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2025-01-01",
                "expiry_date": "2030-01-01",
                "visa_number": "V888888",
            }
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"


class TestLayoverHandling:
    """Layover segments with transit_visa_required flag."""

    def test_layover_with_transit_visa_required(self, endpoints):
        """Layover in CN (transit_visa_required=True) without visa → alert."""
        event = _base_event(endpoints["endpoint_arn"])
        event["segments"] = [
            {
                "flight_number": "DL300",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-15",
                "is_layover": True,
            },
            {
                "flight_number": "DL301",
                "origin": "CN",
                "destination": "JP",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            },
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        # CN has transit_visa_required=True, so layover should trigger visa alert
        assert body["total_alerts_sent"] >= 1


class TestRequirementsOverride:
    """requirements_override parsing and merging with defaults."""

    def test_with_override_changes_requirements(self, endpoints):
        """Override that exempts IN nationality should clear visa alert."""
        event = _base_event(endpoints["endpoint_arn"])
        event["requirements_override"] = {
            "CN": {
                "country_code": "CN",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": ["IN"],
            }
        }

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_without_override_uses_defaults(self, endpoints):
        """No override should use default COUNTRY_REQUIREMENTS (IN not exempt for CN)."""
        event = _base_event(endpoints["endpoint_arn"])
        event["requirements_override"] = None

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

    def test_override_with_partial_fields(self, endpoints):
        """Override with only some fields should use defaults for omitted ones."""
        event = _base_event(endpoints["endpoint_arn"])
        event["requirements_override"] = {
            "CN": {
                "visa_exempt_nationalities": ["IN", "US"],
            }
        }

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        # IN should be visa exempt now
        assert body["traveler_results"][0]["visa_status"] == "OK"


class TestMultipleTravelers:
    """Group with multiple travelers — per-traveler results and group severity."""

    def test_two_travelers_mixed_results(self, endpoints):
        """One clean traveler, one with visa issue → group severity CRITICAL."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"] = [
            {
                "skymiles_number": "1111111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "US",
                "passport_number": "P111111",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoints["endpoint_arn"],
                "visa_records": [
                    {
                        "country_code": "CN",
                        "visa_type": "TOURIST",
                        "issue_date": "2025-01-01",
                        "expiry_date": "2030-01-01",
                        "visa_number": "V111111",
                    }
                ],
            },
            {
                "skymiles_number": "2222222222",
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

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert len(body["traveler_results"]) == 2

        # Alice (US with valid visa) should be clean
        alice_result = next(
            r for r in body["traveler_results"] if r["skymiles_number"] == "1111111111"
        )
        assert alice_result["visa_status"] == "OK"

        # Bob (IN without visa) should have CRITICAL visa
        bob_result = next(
            r for r in body["traveler_results"] if r["skymiles_number"] == "2222222222"
        )
        assert bob_result["visa_status"] == "CRITICAL"

    def test_all_travelers_clean(self, endpoints):
        """All travelers clean → group severity OK."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "US"
        event["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["group_severity"] == "OK"


class TestFindTravelerNone:
    """_find_traveler returns None when skymiles_number not in group."""

    def test_traveler_not_found_skips_notification(self, endpoints):
        """If _find_traveler returns None, notification is skipped but alert saved."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_expiry"] = "2020-01-01"
        # Make the primary_traveler a skymiles number not in the travelers list
        event["primary_traveler"] = "9999999999"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        # Alerts should still be saved
        assert body["total_alerts_sent"] >= 1
        # DynamoDB should have records
        items = alert_store_mod.table.scan()["Items"]
        assert len(items) >= 1


class TestGroupNotification:
    """Group summary notification to the primary traveler."""

    def test_group_notification_sent_to_primary(self, endpoints):
        """Primary traveler receives group summary notification."""
        event = _base_event(endpoints["endpoint_arn"])
        # Ensure primary traveler is in the list and has alerts
        event["travelers"] = [
            {
                "skymiles_number": "1111111111",
                "first_name": "Alice",
                "last_name": "Smith",
                "nationality": "IN",
                "passport_number": "P111111",
                "passport_expiry": "2030-01-01",
                "endpoint_arn": endpoints["endpoint_arn"],
                "visa_records": [],
            },
            {
                "skymiles_number": "2222222222",
                "first_name": "Bob",
                "last_name": "Jones",
                "nationality": "IN",
                "passport_number": "P222222",
                "passport_expiry": "2020-01-01",
                "endpoint_arn": endpoints["endpoint_arn_2"],
                "visa_records": [],
            },
        ]
        event["primary_traveler"] = "1111111111"

        response = group_handler(event, None)

        # Should succeed — group notification sent without error
        assert response["statusCode"] == 200
        assert response["body"]["total_alerts_sent"] >= 2


class TestParseGroupItinerary:
    """_parse_group_itinerary correctly parses segments, travelers, confirmation."""

    def test_parses_multiple_segments(self, endpoints):
        """Multiple segments are all parsed into the itinerary."""
        event = _base_event(endpoints["endpoint_arn"])
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "LHR",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            },
            {
                "flight_number": "DL200",
                "origin": "LHR",
                "destination": "DE",
                "departure_date": "2026-09-17",
                "arrival_date": "2026-09-17",
                "is_layover": False,
            },
        ]
        event["travelers"][0]["nationality"] = "US"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GRP-001"

    def test_parses_travelers_with_visa_records(self, endpoints):
        """Travelers with visa records are parsed and used for evaluation."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "BUSINESS",
                "issue_date": "2025-06-01",
                "expiry_date": "2030-06-01",
                "visa_number": "V123456",
            }
        ]

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_is_layover_defaults_to_false(self, endpoints):
        """Segment without is_layover field defaults to False."""
        event = _base_event(endpoints["endpoint_arn"])
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
            }
        ]
        event["travelers"][0]["nationality"] = "US"

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"


class TestStatusStringGeneration:
    """Status strings use severity.value or 'OK' when no severity."""

    def test_passport_ok_when_no_severity(self, endpoints):
        """Valid passport produces 'OK' status string."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "US"
        event["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"

    def test_severity_value_used_when_present(self, endpoints):
        """When severity exists, its .value is used as the status string."""
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"


class TestItineraryChanged:
    """When customer's itinerary changes, re-evaluation produces correct results."""

    def test_destination_change_adds_new_alerts(self, endpoints):
        """Changing destination to a visa-required country triggers new alerts."""
        # First: destination where traveler is exempt
        event = _base_event(endpoints["endpoint_arn"])
        event["travelers"][0]["nationality"] = "US"
        event["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]

        response = group_handler(event, None)
        assert response["body"]["total_alerts_sent"] == 0

        # Now change to CN where US needs visa
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
        assert response["body"]["total_alerts_sent"] >= 1
        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
