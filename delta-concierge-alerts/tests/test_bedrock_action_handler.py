"""Tests for the Bedrock Agent Action Group handler.

Covers all four action paths (getActiveAlerts, acknowledgeAlert,
resolveAlert, evaluateItinerary), input validation branches, the
unknown-action fallback, and the _DecimalEncoder utility.
"""

import decimal
import json

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.bedrock_action_handler import handler
from src.models.types import AlertSeverity, AlertStatus, AlertType


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS for every test."""
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
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn,
            Token="fake-token",
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
    """Expose the moto SNS endpoint ARN."""
    return _mock_aws


def _seed_alert(
    skymiles_number: str = "9999999",
    alert_id: str = "alert-001",
    status: str = AlertStatus.ACTIVE.value,
):
    """Insert a minimal alert row into the mocked DynamoDB table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": AlertType.VISA.value,
            "severity": AlertSeverity.CRITICAL.value,
            "reasons": ["No visa on file for CN"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": "DL-TEST",
            "status": status,
        }
    )


# ------------------------------------------------------------------
# Helpers to build Bedrock Agent event structures
# ------------------------------------------------------------------

def _bedrock_get_event(api_path: str, parameters: list[dict] | None = None) -> dict:
    """Build a Bedrock Agent GET-style event."""
    return {
        "apiPath": api_path,
        "httpMethod": "GET",
        "actionGroup": "DeltaConciergeActions",
        "parameters": parameters or [],
    }


def _bedrock_post_event(api_path: str, body_props: list[dict] | None = None) -> dict:
    """Build a Bedrock Agent POST-style event with a JSON request body."""
    return {
        "apiPath": api_path,
        "httpMethod": "POST",
        "actionGroup": "DeltaConciergeActions",
        "parameters": [],
        "requestBody": {
            "content": {
                "application/json": {
                    "properties": body_props or [],
                }
            }
        },
    }


def _prop(name: str, value) -> dict:
    """Build a single Bedrock Agent request body property.

    Bedrock Agent events transmit all property values as strings.  For
    non-string values (dicts, lists, etc.) we JSON-encode them so that
    ``_extract_request_body`` can ``json.loads`` them back.  Plain string
    values are wrapped in an extra layer of quotes so they survive the
    ``json.loads`` round-trip as strings (otherwise a purely-numeric
    string like ``"9999999"`` would be decoded as an ``int``).
    """
    if isinstance(value, str):
        return {"name": name, "value": json.dumps(value)}
    return {"name": name, "value": json.dumps(value)}


def _parse_response_body(response: dict) -> dict:
    """Extract and parse the JSON body from a Bedrock Agent response."""
    raw = response["response"]["responseBody"]["application/json"]["body"]
    return json.loads(raw)


def _response_status(response: dict) -> int:
    """Extract httpStatusCode from a Bedrock Agent response."""
    return response["response"]["httpStatusCode"]


# ------------------------------------------------------------------
# 1. Unknown action → 400
# ------------------------------------------------------------------

class TestUnknownAction:
    def test_unknown_action_returns_400(self):
        event = _bedrock_get_event("/deleteEverything")
        response = handler(event, None)

        assert _response_status(response) == 400
        body = _parse_response_body(response)
        assert "Unknown action" in body["error"]


# ------------------------------------------------------------------
# 2-3. getActiveAlerts
# ------------------------------------------------------------------

class TestGetActiveAlerts:
    def test_returns_matching_active_alerts(self):
        _seed_alert(alert_id="a1", status=AlertStatus.ACTIVE.value)
        _seed_alert(alert_id="a2", status=AlertStatus.ACTIVE.value)
        _seed_alert(alert_id="a3", status=AlertStatus.RESOLVED.value)

        event = _bedrock_get_event(
            "/getActiveAlerts",
            parameters=[{"name": "skymiles_number", "value": "9999999"}],
        )
        response = handler(event, None)

        assert _response_status(response) == 200
        body = _parse_response_body(response)
        assert body["count"] == 2
        assert len(body["alerts"]) == 2

    def test_missing_skymiles_returns_400(self):
        event = _bedrock_get_event("/getActiveAlerts")
        response = handler(event, None)

        assert _response_status(response) == 400
        body = _parse_response_body(response)
        assert "skymiles_number is required" in body["error"]


# ------------------------------------------------------------------
# 4-5. acknowledgeAlert
# ------------------------------------------------------------------

class TestAcknowledgeAlert:
    def test_acknowledges_alert_successfully(self):
        _seed_alert(alert_id="ack-001", status=AlertStatus.ACTIVE.value)

        event = _bedrock_post_event(
            "/acknowledgeAlert",
            body_props=[
                _prop("skymiles_number", "9999999"),
                _prop("alert_id", "ack-001"),
            ],
        )
        response = handler(event, None)

        assert _response_status(response) == 200
        body = _parse_response_body(response)
        assert "acknowledged" in body["message"]

        # Verify DynamoDB was updated
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "ack-001"}
        )["Item"]
        assert item["status"] == AlertStatus.ACKNOWLEDGED.value

    def test_missing_fields_returns_400(self):
        event = _bedrock_post_event(
            "/acknowledgeAlert",
            body_props=[_prop("skymiles_number", "9999999")],
        )
        response = handler(event, None)

        assert _response_status(response) == 400
        body = _parse_response_body(response)
        assert "skymiles_number and alert_id are required" in body["error"]


# ------------------------------------------------------------------
# 6-7. resolveAlert
# ------------------------------------------------------------------

class TestResolveAlert:
    def test_resolves_alert_successfully(self):
        _seed_alert(alert_id="res-001", status=AlertStatus.ACTIVE.value)

        event = _bedrock_post_event(
            "/resolveAlert",
            body_props=[
                _prop("skymiles_number", "9999999"),
                _prop("alert_id", "res-001"),
                _prop("resolution", "Renewed passport"),
            ],
        )
        response = handler(event, None)

        assert _response_status(response) == 200
        body = _parse_response_body(response)
        assert "resolved" in body["message"]

        # Verify DynamoDB was updated
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999", "alert_id": "res-001"}
        )["Item"]
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Renewed passport"
        assert "resolved_at" in item

    def test_missing_resolution_returns_400(self):
        event = _bedrock_post_event(
            "/resolveAlert",
            body_props=[
                _prop("skymiles_number", "9999999"),
                _prop("alert_id", "res-001"),
            ],
        )
        response = handler(event, None)

        assert _response_status(response) == 400
        body = _parse_response_body(response)
        assert "skymiles_number, alert_id, and resolution are required" in body["error"]


# ------------------------------------------------------------------
# 8-9. evaluateItinerary
# ------------------------------------------------------------------

class TestEvaluateItinerary:
    def test_evaluate_itinerary_delegates_to_handler(self, endpoint_arn):
        profile = {
            "skymiles_number": "1234567890",
            "first_name": "Test",
            "last_name": "User",
            "nationality": "US",
            "passport_number": "P999999",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        }
        itinerary = {
            "confirmation_number": "DL-TEST",
            "segments": [
                {
                    "flight_number": "DL200",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        }

        event = _bedrock_post_event(
            "/evaluateItinerary",
            body_props=[
                _prop("profile", profile),
                _prop("itinerary", itinerary),
            ],
        )
        response = handler(event, None)

        assert _response_status(response) == 200
        body = _parse_response_body(response)
        assert "passport_status" in body
        assert "visa_status" in body

    def test_missing_profile_returns_400(self):
        event = _bedrock_post_event(
            "/evaluateItinerary",
            body_props=[
                _prop("itinerary", {"confirmation_number": "X", "segments": []}),
            ],
        )
        response = handler(event, None)

        assert _response_status(response) == 400
        body = _parse_response_body(response)
        assert "profile and itinerary are required" in body["error"]


# ------------------------------------------------------------------
# 10. _DecimalEncoder
# ------------------------------------------------------------------

class TestDecimalEncoder:
    def test_integer_decimal_serialized_as_int(self):
        _seed_alert(alert_id="dec-001")

        # get_active_alerts returns DynamoDB items that may contain Decimal
        event = _bedrock_get_event(
            "/getActiveAlerts",
            parameters=[{"name": "skymiles_number", "value": "9999999"}],
        )
        response = handler(event, None)

        # The response should be valid JSON (encoder didn't choke on Decimals)
        raw_body = response["response"]["responseBody"]["application/json"]["body"]
        parsed = json.loads(raw_body)
        assert isinstance(parsed["count"], int)

    def test_fractional_decimal_serialized_as_float(self):
        from src.handlers.bedrock_action_handler import _DecimalEncoder

        result = json.loads(json.dumps({"val": decimal.Decimal("3.14")}, cls=_DecimalEncoder))
        assert isinstance(result["val"], float)
        assert result["val"] == pytest.approx(3.14)

    def test_non_decimal_falls_through_to_default(self):
        from src.handlers.bedrock_action_handler import _DecimalEncoder

        with pytest.raises(TypeError):
            json.dumps({"val": object()}, cls=_DecimalEncoder)
