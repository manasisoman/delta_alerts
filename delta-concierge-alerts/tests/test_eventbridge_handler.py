"""Tests for the EventBridge ItineraryChanged handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType
from src.services.alert_store import save_alert

SKYMILES = "1234567890"
CONFIRMATION = "DL-CHANGE"
RESOLUTION_TEXT = "Auto-resolved: itinerary changed, re-evaluating"

# Made-up country codes keep tests isolated from real COUNTRY_REQUIREMENTS.
REQUIREMENTS_OVERRIDE = {
    "XA": {
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": [],
    },
    "XB": {
        "requires_visa": True,
        "transit_visa_required": True,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
    "XC": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
}


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


def _segment(**overrides) -> dict:
    """Build a valid itinerary segment dict with optional overrides."""
    segment = {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": "XA",
        "departure_date": "2030-09-15",
        "arrival_date": "2030-09-16",
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


def _base_event(endpoint_arn: str, **detail_overrides) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope with no expected alerts."""
    detail = {
        "profile": {
            "skymiles_number": SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": "US",
            "passport_number": "P999999",
            "passport_expiry": "2040-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": CONFIRMATION,
            "segments": [_segment()],
        },
        "requirements_override": REQUIREMENTS_OVERRIDE,
        "change_type": "DATE_CHANGE",
    }
    detail.update(detail_overrides)
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    alert_id: str,
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
    status: AlertStatus = AlertStatus.ACTIVE,
) -> None:
    """Persist a pre-existing alert record into the mocked table."""
    save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles_number,
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.WARNING,
            reasons=["Old alert"],
            created_at="2030-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )


def _get_item(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _endpoint_messages(endpoint_arn: str) -> list:
    backend = moto.sns.models.sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return list(backend.platform_endpoints[endpoint_arn].messages.values())


class TestHappyPath:
    """A clean itinerary change returns 200 with no resolved or new alerts."""

    def test_response_shape(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {
                "change_type": "DATE_CHANGE",
                "resolved_alerts": 0,
                "evaluation_result": {
                    "statusCode": 200,
                    "body": {
                        "alerts_sent": 0,
                        "passport_status": "OK",
                        "visa_status": "OK",
                    },
                },
            },
        }

    def test_no_alerts_persisted_or_pushed(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert alert_store_mod.table.scan()["Items"] == []
        assert _endpoint_messages(endpoint_arn) == []

    def test_context_is_ignored(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, object())

        assert response["statusCode"] == 200


class TestChangeType:
    """The change_type from the event detail is echoed, defaulting to UNKNOWN."""

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"


class TestStaleAlertResolution:
    """Only ACTIVE/ACKNOWLEDGED alerts for this member + itinerary are resolved."""

    def test_active_alert_resolved(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_item("old-active")
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == RESOLUTION_TEXT
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-ack", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_item("old-ack")["status"] == AlertStatus.RESOLVED.value

    def test_multiple_alerts_counted(self, endpoint_arn):
        _seed_alert("old-1")
        _seed_alert("old-2", status=AlertStatus.ACKNOWLEDGED)
        _seed_alert("old-3")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 3

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_inactive_alert_not_resolved(self, endpoint_arn, status):
        _seed_alert("old-inactive", status=status)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = _get_item("old-inactive")
        assert item["status"] == status.value
        assert "resolution" not in item

    def test_alert_for_other_itinerary_untouched(self, endpoint_arn):
        _seed_alert("other-trip", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item("other-trip")["status"] == AlertStatus.ACTIVE.value

    def test_alert_for_other_member_untouched(self, endpoint_arn):
        _seed_alert("other-member", skymiles_number="5555555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = _get_item("other-member", skymiles_number="5555555555")
        assert item["status"] == AlertStatus.ACTIVE.value


class TestReEvaluationAfterChange:
    """The updated itinerary is re-evaluated and new alerts are raised as ACTIVE."""

    def test_stale_alert_resolved_and_new_alert_created(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(endpoint_arn)
        event["detail"]["itinerary"]["segments"] = [_segment(destination="XB")]

        response = handler(event, None)

        body = response["body"]
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        assert body["evaluation_result"]["body"]["alerts_sent"] == 1
        items = alert_store_mod.table.scan()["Items"]
        new_items = [i for i in items if i["alert_id"] != "old-active"]
        assert len(new_items) == 1
        assert new_items[0]["status"] == AlertStatus.ACTIVE.value
        assert new_items[0]["alert_type"] == AlertType.VISA.value
        assert new_items[0]["itinerary_ref"] == CONFIRMATION
        assert "No visa on file for XB" in new_items[0]["reasons"]
        assert _get_item("old-active")["status"] == AlertStatus.RESOLVED.value

    def test_new_alert_pushed_to_endpoint(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["itinerary"]["segments"] = [_segment(destination="XB")]

        handler(event, None)

        assert len(_endpoint_messages(endpoint_arn)) == 1

    def test_date_change_past_passport_expiry_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["itinerary"]["segments"] = [
            _segment(departure_date="2040-06-01", arrival_date="2040-06-02")
        ]

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == "CRITICAL"
        assert evaluation["visa_status"] == "OK"
        assert evaluation["alerts_sent"] == 1

    def test_date_change_below_country_validity_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2030-11-01"

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == "CRITICAL"

    def test_passport_meeting_validity_but_within_six_months_is_info(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2031-01-15"

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == "INFO"

    def test_layover_added_in_transit_visa_country_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")
        event["detail"]["itinerary"]["segments"] = [
            _segment(destination="XB", is_layover=True, flight_number="DL101"),
            _segment(origin="XB", flight_number="DL102"),
        ]

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_layover_added_without_transit_visa_requirement_is_ok(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")
        event["detail"]["itinerary"]["segments"] = [
            _segment(destination="XC", is_layover=True, flight_number="DL101"),
            _segment(origin="XC", flight_number="DL102"),
        ]

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_segment_removed_clears_visa_alert(self, endpoint_arn):
        _seed_alert("old-visa")
        event = _base_event(endpoint_arn, change_type="SEGMENT_REMOVED")

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0
        assert _get_item("old-visa")["status"] == AlertStatus.RESOLVED.value

    def test_visa_expiring_before_new_departure_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="DATE_CHANGE")
        event["detail"]["itinerary"]["segments"] = [_segment(destination="XB")]
        event["detail"]["profile"]["visa_records"] = [
            {
                "country_code": "XB",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2030-09-01",
                "visa_number": "V123",
            }
        ]

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_valid_visa_for_new_route_is_ok(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")
        event["detail"]["itinerary"]["segments"] = [_segment(destination="XB")]
        event["detail"]["profile"]["visa_records"] = [
            {
                "country_code": "XB",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2035-01-01",
                "visa_number": "V123",
            }
        ]

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["visa_status"] == "OK"
        assert evaluation["alerts_sent"] == 0


class TestMalformedEvent:
    """Required envelope fields raise KeyError before any alerts are touched."""

    def test_missing_detail_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]["skymiles_number"]

        with pytest.raises(KeyError):
            handler(event, None)

        assert _get_item("old-active")["status"] == AlertStatus.ACTIVE.value

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)

        assert _get_item("old-active")["status"] == AlertStatus.ACTIVE.value
