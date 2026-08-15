"""Tests for the EventBridge ``ItineraryChanged`` handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
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


SKYMILES = "1234567890"
CONFIRMATION = "DL-CHANGED"


def _segment(**overrides) -> dict:
    """Build a valid segment dict with optional overrides."""
    segment = {
        "flight_number": "DL100",
        "origin": "JFK",
        "destination": "CN",
        "departure_date": "2030-09-15",
        "arrival_date": "2030-09-16",
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


def _detail(endpoint_arn: str, **overrides) -> dict:
    """Build a valid EventBridge ``detail`` payload with optional overrides."""
    detail = {
        "profile": {
            "skymiles_number": SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2035-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": CONFIRMATION,
            "segments": [_segment()],
        },
        "change_type": "DATE_CHANGE",
    }
    detail.update(overrides)
    return detail


def _event(endpoint_arn: str, **detail_overrides) -> dict:
    """Build a full EventBridge envelope wrapping a ``detail`` payload."""
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": _detail(endpoint_arn, **detail_overrides),
    }


def _seed_alert(
    *,
    alert_id: str = "old-alert-1",
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
    status: AlertStatus = AlertStatus.ACTIVE,
) -> None:
    """Persist a pre-existing alert so the handler has something to resolve."""
    alert_store_mod.save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles_number,
            alert_type=AlertType.VISA,
            severity=AlertSeverity.CRITICAL,
            reasons=["Stale alert from previous itinerary"],
            created_at="2026-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
            ttl=None,
        )
    )


def _items() -> list[dict]:
    """Return every alert item currently in the mocked table."""
    return alert_store_mod.table.scan()["Items"]


class TestResponseShape:
    """The handler returns statusCode 200 and echoes change metadata."""

    def test_response_contains_change_type_and_evaluation(self, endpoint_arn):
        event = _event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200

    def test_change_type_defaults_to_unknown_when_absent(self, endpoint_arn):
        detail = _detail(endpoint_arn)
        del detail["change_type"]
        event = {"detail": detail}

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_passed_through(self, endpoint_arn, change_type):
        event = _event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_detail_raises_key_error(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)


class TestStaleAlertResolution:
    """Alerts for the changed itinerary are auto-resolved before re-evaluation."""

    def test_active_alert_for_itinerary_is_resolved(self, endpoint_arn):
        _seed_alert()

        response = handler(_event(endpoint_arn), None)

        assert response["body"]["resolved_alerts"] == 1
        old = next(i for i in _items() if i["alert_id"] == "old-alert-1")
        assert old["status"] == AlertStatus.RESOLVED.value
        assert old["resolution"] == (
            "Auto-resolved: itinerary changed, re-evaluating"
        )

    def test_acknowledged_alert_for_itinerary_is_resolved(self, endpoint_arn):
        _seed_alert(status=AlertStatus.ACKNOWLEDGED)

        response = handler(_event(endpoint_arn), None)

        assert response["body"]["resolved_alerts"] == 1

    def test_multiple_active_alerts_all_resolved(self, endpoint_arn):
        _seed_alert(alert_id="old-alert-1")
        _seed_alert(alert_id="old-alert-2")

        response = handler(_event(endpoint_arn), None)

        assert response["body"]["resolved_alerts"] == 2

    def test_alerts_for_other_itineraries_untouched(self, endpoint_arn):
        _seed_alert(alert_id="other-itinerary", itinerary_ref="DL-OTHER")

        response = handler(_event(endpoint_arn), None)

        assert response["body"]["resolved_alerts"] == 0
        other = next(i for i in _items() if i["alert_id"] == "other-itinerary")
        assert other["status"] == AlertStatus.ACTIVE.value

    def test_already_resolved_alert_not_recounted(self, endpoint_arn):
        _seed_alert(status=AlertStatus.RESOLVED)

        response = handler(_event(endpoint_arn), None)

        assert response["body"]["resolved_alerts"] == 0


class TestReEvaluation:
    """The changed itinerary is re-evaluated and new alerts are persisted."""

    def test_missing_visa_for_destination_is_critical(self, endpoint_arn):
        response = handler(_event(endpoint_arn), None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] >= 1
        visa_alerts = [i for i in _items() if i["alert_type"] == "VISA"]
        assert any(
            "No visa on file for CN" in i["reasons"] for i in visa_alerts
        )

    def test_clean_itinerary_produces_no_new_alerts(self, endpoint_arn):
        event = _event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"] = [_segment(destination="DE")]

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0
        assert _items() == []

    def test_expired_passport_after_change_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"

    def test_layover_without_transit_visa_is_skipped(self, endpoint_arn):
        """DE requires no transit visa, so a layover there needs no visa."""
        event = _event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"] = [
            _segment(destination="DE", is_layover=True)
        ]

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_layover_requiring_transit_visa_is_critical(self, endpoint_arn):
        """CN requires a transit visa, so a layover there still alerts."""
        event = _event(endpoint_arn)
        event["detail"]["itinerary"]["segments"] = [
            _segment(destination="CN", is_layover=True)
        ]

        response = handler(event, None)

        assert (
            response["body"]["evaluation_result"]["body"]["visa_status"]
            == "CRITICAL"
        )

    def test_added_segment_is_evaluated(self, endpoint_arn):
        """A SEGMENT_ADDED change re-evaluates every segment in the itinerary."""
        event = _event(endpoint_arn, change_type="SEGMENT_ADDED")
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"] = [
            _segment(destination="DE"),
            _segment(
                flight_number="DL200",
                destination="CN",
                departure_date="2030-09-20",
                arrival_date="2030-09-21",
            ),
        ]

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"

    def test_requirements_override_suppresses_visa_alert(self, endpoint_arn):
        """An override marking CN visa-free removes the otherwise-CRITICAL alert."""
        event = _event(endpoint_arn)
        event["detail"]["requirements_override"] = {
            "CN": {
                "requires_visa": False,
                "transit_visa_required": False,
                "passport_validity_months": 3,
                "visa_exempt_nationalities": [],
            }
        }

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "OK"

    def test_resolution_and_reevaluation_happen_together(self, endpoint_arn):
        """Stale alerts are resolved and fresh ones created in the same call."""
        _seed_alert()

        response = handler(_event(endpoint_arn), None)

        body = response["body"]
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["body"]["alerts_sent"] >= 1
        statuses = {i["alert_id"]: i["status"] for i in _items()}
        assert statuses["old-alert-1"] == AlertStatus.RESOLVED.value
        assert any(
            alert_id != "old-alert-1"
            and status == AlertStatus.ACTIVE.value
            for alert_id, status in statuses.items()
        )
