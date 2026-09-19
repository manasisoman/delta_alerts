"""Unit tests for EventBridge itinerary-change handling."""

from datetime import datetime
import uuid

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler


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


def _base_detail(endpoint_arn: str, **overrides) -> dict:
    """Build a valid itinerary-change detail with optional profile overrides."""
    detail = {
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
    for key, value in overrides.items():
        if key in detail["profile"]:
            detail["profile"][key] = value
    return detail


def _base_event(
    endpoint_arn: str, change_type: str | None = "DATE_CHANGE", **overrides
) -> dict:
    """Build an EventBridge ItineraryChanged event."""
    detail = _base_detail(endpoint_arn, **overrides)
    if change_type is not None:
        detail["change_type"] = change_type
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    skymiles: str,
    confirmation: str,
    status: str = "ACTIVE",
    alert_type: str = "VISA",
    alert_id: str | None = None,
) -> str:
    """Insert a simple alert row and return its identifier."""
    alert_id = alert_id or str(uuid.uuid4())
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles,
            "alert_id": alert_id,
            "alert_type": alert_type,
            "severity": "CRITICAL",
            "reasons": ["test alert"],
            "created_at": datetime.utcnow().isoformat(),
            "itinerary_ref": confirmation,
            "status": status,
        }
    )
    return alert_id


class TestHappyPath:
    """Successful EventBridge handling and re-evaluation behavior."""

    def test_response_shape_with_empty_table(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        evaluation_result = body["evaluation_result"]
        assert evaluation_result["statusCode"] == 200
        assert {
            "alerts_sent",
            "passport_status",
            "visa_status",
        } <= evaluation_result["body"].keys()

    def test_re_evaluation_persists_missing_visa_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [
            item
            for item in items
            if item["alert_type"] == "VISA"
            and item["itinerary_ref"] == "DL-TEST"
            and item["status"] == "ACTIVE"
        ]
        assert len(visa_alerts) == 1


class TestChangeType:
    """EventBridge change type extraction."""

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    @pytest.mark.parametrize(
        "change_type", ["SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"]
    )
    def test_change_type_is_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type


class TestStaleAlertResolution:
    """Resolution of actionable alerts before itinerary re-evaluation."""

    def test_active_alert_for_itinerary_is_resolved(self, endpoint_arn):
        alert_id = _seed_alert("1234567890", "DL-TEST")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": alert_id}
        )["Item"]
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
        assert "resolved_at" in item

    def test_acknowledged_alert_for_itinerary_is_resolved(self, endpoint_arn):
        alert_id = _seed_alert(
            "1234567890", "DL-TEST", status="ACKNOWLEDGED"
        )
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": alert_id}
        )["Item"]
        assert item["status"] == "RESOLVED"

    def test_resolved_alert_is_not_counted_or_changed(self, endpoint_arn):
        alert_id = _seed_alert("1234567890", "DL-TEST", status="RESOLVED")
        before = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": alert_id}
        )["Item"]
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        after = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": alert_id}
        )["Item"]
        assert after == before

    def test_alert_for_different_confirmation_is_not_resolved(self, endpoint_arn):
        alert_id = _seed_alert("1234567890", "DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "1234567890", "alert_id": alert_id}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_alert_for_different_skymiles_is_not_resolved(self, endpoint_arn):
        alert_id = _seed_alert("9999999999", "DL-TEST")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = alert_store_mod.table.get_item(
            Key={"skymiles_number": "9999999999", "alert_id": alert_id}
        )["Item"]
        assert item["status"] == "ACTIVE"

    def test_multiple_actionable_alerts_are_all_resolved(self, endpoint_arn):
        alert_ids = [
            _seed_alert("1234567890", "DL-TEST", status="ACTIVE"),
            _seed_alert("1234567890", "DL-TEST", status="ACTIVE"),
            _seed_alert("1234567890", "DL-TEST", status="ACKNOWLEDGED"),
        ]
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 3
        items = alert_store_mod.table.scan()["Items"]
        resolved_ids = {
            item["alert_id"]
            for item in items
            if item["status"] == "RESOLVED"
        }
        assert set(alert_ids) <= resolved_ids

    def test_re_evaluation_creates_new_alert_after_resolution(self, endpoint_arn):
        old_alert_id = _seed_alert("1234567890", "DL-TEST")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        items = alert_store_mod.table.scan()["Items"]
        old_alert = next(item for item in items if item["alert_id"] == old_alert_id)
        new_alerts = [
            item
            for item in items
            if item["itinerary_ref"] == "DL-TEST"
            and item["alert_id"] != old_alert_id
        ]
        assert old_alert["status"] == "RESOLVED"
        assert any(item["status"] == "ACTIVE" for item in new_alerts)


class TestReEvaluationOfChangedItinerary:
    """Updated itinerary and document requirement evaluation semantics."""

    def test_date_change_past_passport_expiry_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="DATE_CHANGE")
        event["detail"]["profile"]["passport_expiry"] = "2026-12-01"
        event["detail"]["itinerary"]["segments"][0].update(
            departure_date="2027-01-10", arrival_date="2027-01-11"
        )

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["passport_status"] == "CRITICAL"

    def test_route_change_to_visa_exempt_country_is_clean(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"][0].update(
            origin="ATL", destination="DE"
        )

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["visa_status"] == "OK"
        assert body["passport_status"] == "OK"
        assert body["alerts_sent"] == 0
        assert response["body"]["resolved_alerts"] == 0

    def test_segment_added_with_transit_visa_requirement_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")
        event["detail"]["itinerary"]["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            },
            {
                "flight_number": "DL201",
                "origin": "DE",
                "destination": "CN",
                "departure_date": "2026-09-16",
                "arrival_date": "2026-09-17",
                "is_layover": True,
            },
        ]

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["visa_status"] == "CRITICAL"

    def test_layover_in_transit_visa_exempt_country_is_clean(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["itinerary"]["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": True,
            }
        ]

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["visa_status"] == "OK"

    def test_passport_below_country_validity_window_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2026-12-01"

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["passport_status"] == "CRITICAL"

    def test_passport_below_schengen_validity_window_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["profile"]["passport_expiry"] = "2026-11-01"
        event["detail"]["itinerary"]["segments"][0]["destination"] = "DE"

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["passport_status"] == "CRITICAL"

    def test_valid_visa_on_file_is_clean(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            visa_records=[
                {
                    "country_code": "CN",
                    "visa_type": "TOURIST",
                    "issue_date": "2025-01-01",
                    "expiry_date": "2030-01-01",
                    "visa_number": "V1",
                }
            ],
        )

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["visa_status"] == "OK"

    def test_requirements_override_can_remove_visa_requirement(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["requirements_override"] = {
            "CN": {"requires_visa": False}
        }

        response = handler(event, None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["visa_status"] == "OK"


class TestMalformedEvent:
    """Required EventBridge envelope and itinerary fields."""

    def test_missing_detail_raises_key_error_and_writes_no_alerts(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({}, None)

        assert alert_store_mod.table.scan()["Items"] == []

    def test_missing_profile_raises_key_error(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({"detail": {}}, None)

    def test_missing_confirmation_number_raises_key_error(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)
