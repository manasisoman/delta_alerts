"""Critical-path tests for the EventBridge ``ItineraryChanged`` handler."""

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


def _base_detail(
    endpoint_arn: str,
    *,
    skymiles_number: str = "1234567890",
    confirmation_number: str = "DL-CHANGED",
    nationality: str = "IN",
    destination: str = "CN",
    change_type: str | None = "DATE_CHANGE",
) -> dict:
    """Build a valid ``ItineraryChanged`` detail payload."""
    detail = {
        "profile": {
            "skymiles_number": skymiles_number,
            "first_name": "Test",
            "last_name": "User",
            "nationality": nationality,
            "passport_number": "P999999",
            "passport_expiry": "2035-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": confirmation_number,
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": destination,
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }
    if change_type is not None:
        detail["change_type"] = change_type
    return detail


def _base_event(endpoint_arn: str, **kwargs) -> dict:
    """Build a valid EventBridge envelope wrapping an ``ItineraryChanged`` detail."""
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": _base_detail(endpoint_arn, **kwargs),
    }


def _seed_alert(
    *,
    skymiles_number: str = "1234567890",
    alert_id: str = "alert-1",
    itinerary_ref: str = "DL-CHANGED",
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
            created_at="2030-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )


def _items_by_id(skymiles_number: str = "1234567890") -> dict[str, dict]:
    """Return the member's persisted alerts keyed by alert_id."""
    items = alert_store_mod.get_alerts_by_member(skymiles_number)
    return {item["alert_id"]: item for item in items}


class TestItineraryChangedEndToEnd:
    """Full round-trip: envelope → stale-alert resolution → re-evaluation → response."""

    def test_reevaluates_and_returns_response_shape(self, endpoint_arn):
        """Missing visa for CN should persist a new alert and report the change type."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        assert body["evaluation_result"]["body"]["alerts_sent"] >= 1

        visa_alerts = [
            item
            for item in alert_store_mod.get_alerts_by_member("1234567890")
            if item["alert_type"] == "VISA"
        ]
        assert len(visa_alerts) == 1
        assert visa_alerts[0]["itinerary_ref"] == "DL-CHANGED"

    def test_clean_traveler_produces_no_alerts(self, endpoint_arn):
        """US national to DE with a valid passport → re-evaluation raises no alerts."""
        event = _base_event(endpoint_arn, nationality="US", destination="DE")

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == "OK"
        assert evaluation["visa_status"] == "OK"
        assert evaluation["alerts_sent"] == 0
        assert alert_store_mod.get_alerts_by_member("1234567890") == []

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """A detail without ``change_type`` should still process and report UNKNOWN."""
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["change_type"] == "UNKNOWN"


class TestStaleAlertResolution:
    """Alerts for the changed itinerary are auto-resolved before re-evaluation."""

    def test_existing_active_alert_is_resolved(self, endpoint_arn):
        """A pre-existing ACTIVE alert for the itinerary should be resolved and counted."""
        _seed_alert()
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        stale = _items_by_id()["alert-1"]
        assert stale["status"] == AlertStatus.RESOLVED.value
        assert stale["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"

    def test_acknowledged_alert_is_also_resolved(self, endpoint_arn):
        """ACKNOWLEDGED alerts count as stale and are resolved too."""
        _seed_alert(alert_id="alert-ack", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _items_by_id()["alert-ack"]["status"] == AlertStatus.RESOLVED.value

    def test_alerts_for_other_itineraries_are_untouched(self, endpoint_arn):
        """Only alerts matching the changed confirmation number are resolved."""
        _seed_alert(alert_id="alert-other", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _items_by_id()["alert-other"]["status"] == AlertStatus.ACTIVE.value

    def test_already_resolved_alert_is_not_recounted(self, endpoint_arn):
        """RESOLVED alerts are not stale and must not inflate the resolved count."""
        _seed_alert(alert_id="alert-done", status=AlertStatus.RESOLVED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_multiple_stale_alerts_all_resolved(self, endpoint_arn):
        """Every stale alert for the itinerary is resolved and counted."""
        _seed_alert(alert_id="alert-a")
        _seed_alert(alert_id="alert-b", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2
        items = _items_by_id()
        assert items["alert-a"]["status"] == AlertStatus.RESOLVED.value
        assert items["alert-b"]["status"] == AlertStatus.RESOLVED.value


class TestMalformedEvent:
    """Envelopes missing required keys surface as KeyError rather than silent success."""

    def test_missing_detail_raises(self, endpoint_arn):
        """An envelope without ``detail`` cannot be processed."""
        event = {"source": "delta.booking-system", "detail-type": "ItineraryChanged"}

        with pytest.raises(KeyError, match="detail"):
            handler(event, None)

    def test_missing_profile_raises(self, endpoint_arn):
        """A detail without ``profile`` cannot yield a skymiles number."""
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]

        with pytest.raises(KeyError, match="profile"):
            handler(event, None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        """A profile without ``skymiles_number`` cannot be looked up."""
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]["skymiles_number"]

        with pytest.raises(KeyError, match="skymiles_number"):
            handler(event, None)

    def test_missing_itinerary_raises(self, endpoint_arn):
        """A detail without ``itinerary`` cannot yield a confirmation number."""
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]

        with pytest.raises(KeyError, match="itinerary"):
            handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        """An itinerary without ``confirmation_number`` cannot be matched to alerts."""
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError, match="confirmation_number"):
            handler(event, None)

    def test_no_alerts_persisted_when_event_is_malformed(self, endpoint_arn):
        """A malformed envelope must not leave partial state in DynamoDB."""
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]

        with pytest.raises(KeyError):
            handler(event, None)

        assert alert_store_mod.get_alerts_by_member("1234567890") == []
