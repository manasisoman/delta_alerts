"""Tests for the EventBridge ``ItineraryChanged`` handler.

Covers the happy path (stale alerts resolved, itinerary re-evaluated), the
``change_type`` default fallback, several detail-type variants, and malformed
event envelopes. AWS services (DynamoDB/SNS) are mocked with moto and the
module-level boto3 clients are monkey-patched, matching the pattern used in
``test_lambda_handler.py`` and ``test_bedrock_action_handler.py``.
"""

from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType
from src.services.alert_store import get_active_alerts, save_alert


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
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


_SKYMILES = "1234567890"
_CONFIRMATION = "DL-CHANGED"


def _base_detail(endpoint_arn: str, **overrides) -> dict:
    """Build the ``detail`` payload of an EventBridge event.

    Defaults describe an Indian national flying to CN (visa required, not
    exempt), which triggers a CRITICAL visa alert on re-evaluation. Any
    ``profile`` field can be overridden via keyword arguments.
    """
    detail = {
        "change_type": "DATE_CHANGE",
        "profile": {
            "skymiles_number": _SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": _CONFIRMATION,
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2030-09-15",
                    "arrival_date": "2030-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }
    for key, val in overrides.items():
        if key in detail["profile"]:
            detail["profile"][key] = val
    return detail


def _event(detail: dict) -> dict:
    """Wrap a ``detail`` payload in an EventBridge event envelope."""
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_active_alert(
    skymiles_number: str = _SKYMILES,
    itinerary_ref: str = _CONFIRMATION,
    alert_id: str = "seed-alert-1",
) -> None:
    """Persist an ACTIVE alert so the handler has something to resolve."""
    save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles_number,
            alert_type=AlertType.VISA,
            severity=AlertSeverity.CRITICAL,
            reasons=["No visa on file for CN"],
            created_at=datetime.utcnow().isoformat(),
            itinerary_ref=itinerary_ref,
            status=AlertStatus.ACTIVE,
            ttl=None,
        )
    )


class TestHappyPath:
    """A valid ItineraryChanged event resolves stale alerts and re-evaluates."""

    def test_resolves_existing_alerts_and_reevaluates(self, endpoint_arn):
        _seed_active_alert()

        response = handler(_event(_base_detail(endpoint_arn)), None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["statusCode"] == 200
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_stale_alert_marked_resolved_in_store(self, endpoint_arn):
        _seed_active_alert()

        handler(_event(_base_detail(endpoint_arn)), None)

        remaining = [
            a for a in get_active_alerts(_SKYMILES) if a["alert_id"] == "seed-alert-1"
        ]
        assert remaining == []

    def test_no_existing_alerts_resolves_zero(self, endpoint_arn):
        response = handler(_event(_base_detail(endpoint_arn)), None)

        assert response["body"]["resolved_alerts"] == 0
        assert response["statusCode"] == 200

    def test_clean_traveler_produces_no_new_alerts(self, endpoint_arn):
        detail = _base_detail(
            endpoint_arn, nationality="US", passport_expiry="2035-01-01"
        )
        detail["itinerary"]["segments"][0]["destination"] = "DE"

        response = handler(_event(detail), None)

        body = response["body"]["evaluation_result"]["body"]
        assert body["passport_status"] == "OK"
        assert body["visa_status"] == "OK"
        assert body["alerts_sent"] == 0


class TestResolutionScoping:
    """Only alerts for the affected itinerary/member are resolved."""

    def test_other_itinerary_alert_untouched(self, endpoint_arn):
        _seed_active_alert(alert_id="target", itinerary_ref=_CONFIRMATION)
        _seed_active_alert(alert_id="other", itinerary_ref="DL-OTHER")

        response = handler(_event(_base_detail(endpoint_arn)), None)

        assert response["body"]["resolved_alerts"] == 1
        still_active = {a["alert_id"] for a in get_active_alerts(_SKYMILES)}
        assert "other" in still_active
        assert "target" not in still_active


class TestChangeType:
    """The change_type is echoed back, defaulting to UNKNOWN when absent."""

    def test_default_change_type_when_missing(self, endpoint_arn):
        detail = _base_detail(endpoint_arn)
        del detail["change_type"]

        response = handler(_event(detail), None)

        assert response["body"]["change_type"] == "UNKNOWN"

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_echoed(self, endpoint_arn, change_type):
        detail = _base_detail(endpoint_arn)
        detail["change_type"] = change_type

        response = handler(_event(detail), None)

        assert response["body"]["change_type"] == change_type


class TestMalformedEvent:
    """Malformed envelopes surface as KeyErrors rather than silent success."""

    def test_missing_detail_raises(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({"detail-type": "ItineraryChanged"}, None)

    def test_missing_profile_raises(self, endpoint_arn):
        detail = _base_detail(endpoint_arn)
        del detail["profile"]

        with pytest.raises(KeyError):
            handler(_event(detail), None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        detail = _base_detail(endpoint_arn)
        del detail["profile"]["skymiles_number"]

        with pytest.raises(KeyError):
            handler(_event(detail), None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        detail = _base_detail(endpoint_arn)
        del detail["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(_event(detail), None)
