"""Critical-path tests for the Lambda handler end-to-end flow."""

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
from src.handlers.lambda_handler import _compute_ttl, evaluate_itinerary, handler
from tests.conftest import make_itinerary, make_segment


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


def _future_event(endpoint_arn: str, destination: str = "XA", **profile_overrides) -> dict:
    """Event with 2030+ dates and a made-up destination isolated via requirements_override."""
    event = _base_event(endpoint_arn, **profile_overrides)
    event["profile"]["passport_expiry"] = profile_overrides.get("passport_expiry", "2040-01-01")
    event["itinerary"]["segments"] = [
        {
            "flight_number": "DL300",
            "origin": "ATL",
            "destination": destination,
            "departure_date": "2031-03-01",
            "arrival_date": "2031-03-02",
            "is_layover": False,
        }
    ]
    event["requirements_override"] = {
        "XA": {
            "country_code": "XA",
            "requires_visa": False,
            "passport_validity_months": 3,
        },
    }
    return event


def _pushed_messages(endpoint_arn: str) -> list[dict]:
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


class TestGroupDelegation:
    """Events containing a travelers key are delegated to the group handler."""

    def test_travelers_key_routes_to_group_handler(self, endpoint_arn):
        calls = []

        def fake_group_handler(event, context):
            calls.append((event, context))
            return {"statusCode": 200, "body": {"delegated": True}}

        event = {"travelers": [], "segments": []}
        orig = group_mod.group_handler
        group_mod.group_handler = fake_group_handler
        try:
            response = handler(event, "ctx")
        finally:
            group_mod.group_handler = orig

        assert response == {"statusCode": 200, "body": {"delegated": True}}
        assert calls == [(event, "ctx")]
        assert alert_store_mod.table.scan()["Items"] == []


class TestRequirementsOverride:
    """requirements_override entries replace or extend the default country rules."""

    def test_override_marks_unknown_country_clear(self, endpoint_arn):
        event = _future_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 0,
            "passport_status": "OK",
            "visa_status": "OK",
        }

    def test_override_defaults_require_visa(self, endpoint_arn):
        event = _future_event(endpoint_arn, destination="XB")
        event["requirements_override"] = {"XB": {}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["passport_status"] == "OK"
        items = alert_store_mod.table.scan()["Items"]
        assert [i["alert_type"] for i in items] == ["VISA"]
        assert "No visa on file for XB" in items[0]["reasons"]

    def test_override_visa_exempt_nationality(self, endpoint_arn):
        event = _future_event(endpoint_arn, destination="XB")
        event["requirements_override"] = {
            "XB": {"requires_visa": True, "visa_exempt_nationalities": ["IN"]},
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_validity_months_triggers_passport_alert(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2031-05-01")
        event["requirements_override"]["XA"]["passport_validity_months"] = 6

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 1

    def test_override_with_valid_visa_record(self, endpoint_arn):
        event = _future_event(endpoint_arn, destination="XB")
        event["requirements_override"] = {"XB": {"requires_visa": True}}
        event["profile"]["visa_records"] = [
            {
                "country_code": "XB",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2035-01-01",
                "visa_number": "V123",
            }
        ]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0


class TestAlertPersistenceAndNotification:
    """Each actionable evaluation persists one alert and sends one push."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _future_event(endpoint_arn, destination="XB", passport_expiry="2020-01-01")
        event["requirements_override"] = {"XB": {}}

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2,
            "passport_status": "CRITICAL",
            "visa_status": "CRITICAL",
        }
        items = alert_store_mod.table.scan()["Items"]
        assert sorted(i["alert_type"] for i in items) == ["PASSPORT", "VISA"]
        assert all(i["itinerary_ref"] == "DL-TEST" for i in items)
        assert all(i["status"] == "ACTIVE" for i in items)
        assert len(_pushed_messages(endpoint_arn)) == 2

    def test_passport_alert_record_fields(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2020-01-01")

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["skymiles_number"] == "1234567890"
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        expected_ttl = int(
            (datetime(2031, 3, 2) + timedelta(days=30)).timestamp()
        )
        assert int(item["ttl"]) == expected_ttl

    def test_passport_warning_info_severity_reported(self, endpoint_arn):
        # Meets XA's 3-month rule but expires within 6 months of departure
        event = _future_event(endpoint_arn, passport_expiry="2031-07-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] in ("INFO", "WARNING")
        assert response["body"]["alerts_sent"] == 1
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["alert_type"] == "PASSPORT"

    def test_push_payload_contains_alert_metadata(self, endpoint_arn):
        event = _future_event(endpoint_arn, destination="XB")
        event["requirements_override"] = {"XB": {}}

        handler(event, None)

        messages = _pushed_messages(endpoint_arn)
        assert len(messages) == 1
        raw = json.dumps(messages[0])
        assert "Visa Alert" in raw
        assert "No visa on file for XB" in raw
        assert "DL-TEST" in raw

    def test_no_push_when_clear(self, endpoint_arn):
        event = _future_event(endpoint_arn)

        handler(event, None)

        assert _pushed_messages(endpoint_arn) == []
        assert alert_store_mod.table.scan()["Items"] == []


class TestLayoverAndItineraryChanges:
    """Layover segments and changed itineraries are evaluated on each invocation."""

    def test_layover_segment_parsed_and_evaluated(self, endpoint_arn):
        event = _future_event(endpoint_arn)
        event["itinerary"]["segments"].insert(
            0,
            {
                "flight_number": "DL299",
                "origin": "ATL",
                "destination": "XB",
                "departure_date": "2031-02-28",
                "arrival_date": "2031-03-01",
                "is_layover": True,
            },
        )
        event["requirements_override"]["XB"] = {
            "requires_visa": True,
            "transit_visa_required": False,
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "OK"

    def test_changed_itinerary_re_evaluated(self, endpoint_arn):
        clean = _future_event(endpoint_arn)
        changed = _future_event(endpoint_arn, destination="XB")
        changed["requirements_override"]["XB"] = {}

        first = handler(clean, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"
        assert len(alert_store_mod.table.scan()["Items"]) == 1


class TestComputeTtl:
    """TTL is 30 days after the latest arrival, or after today with no segments."""

    def test_uses_latest_arrival(self):
        itinerary = make_itinerary(
            make_segment(departure=date(2031, 1, 1), arrival=date(2031, 1, 2)),
            make_segment(departure=date(2031, 2, 1), arrival=date(2031, 2, 5)),
        )

        ttl = _compute_ttl(itinerary)

        assert ttl == int((datetime(2031, 2, 5) + timedelta(days=30)).timestamp())

    def test_empty_segments_falls_back_to_today(self):
        itinerary = make_itinerary()

        ttl = _compute_ttl(itinerary)

        expected = datetime.combine(date.today(), datetime.min.time()) + timedelta(days=30)
        assert ttl == int(expected.timestamp())


class TestEvaluateItinerary:
    """evaluate_itinerary is a thin wrapper around handler."""

    def test_returns_handler_response(self, endpoint_arn):
        event = _future_event(endpoint_arn)

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
