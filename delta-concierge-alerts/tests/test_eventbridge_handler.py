"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertStatus


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
    """Build a flight segment with optional overrides."""
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


def _base_event(endpoint_arn: str, *, detail_overrides: dict | None = None,
                profile_overrides: dict | None = None,
                segments: list[dict] | None = None) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope with optional overrides."""
    profile = {
        "skymiles_number": SKYMILES,
        "first_name": "Test",
        "last_name": "User",
        "nationality": "IN",
        "passport_number": "P999999",
        "passport_expiry": "2035-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    profile.update(profile_overrides or {})

    detail = {
        "profile": profile,
        "itinerary": {
            "confirmation_number": CONFIRMATION,
            "segments": segments if segments is not None else [_segment()],
        },
        "change_type": "DATE_CHANGE",
        "requirements_override": None,
    }
    detail.update(detail_overrides or {})

    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(itinerary_ref: str = CONFIRMATION,
                alert_id: str = "alert-1",
                status: str = AlertStatus.ACTIVE.value,
                skymiles_number: str = SKYMILES) -> None:
    """Insert a pre-existing alert row directly into the mocked table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "reasons": ["stale reason"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


class TestHappyPath:
    """A valid ItineraryChanged event re-evaluates and returns a 200 response."""

    def test_response_shape_and_evaluation(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_clean_traveler_produces_no_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            profile_overrides={"nationality": "US"},
            segments=[_segment(destination="DE", flight_number="DL200")],
        )

        response = handler(event, None)

        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["passport_status"] == "OK"
        assert eval_body["visa_status"] == "OK"
        assert eval_body["alerts_sent"] == 0


class TestChangeTypeHandling:
    """``change_type`` is echoed back, defaulting to UNKNOWN when absent."""

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_is_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, detail_overrides={"change_type": change_type})

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type


class TestStaleAlertResolution:
    """Existing alerts for the changed itinerary are auto-resolved first."""

    def test_active_alerts_are_resolved(self, endpoint_arn):
        _seed_alert(alert_id="alert-1")
        _seed_alert(alert_id="alert-2")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2
        items = alert_store_mod.table.scan()["Items"]
        seeded = [i for i in items if i["alert_id"].startswith("alert-")]
        assert all(i["status"] == AlertStatus.RESOLVED.value for i in seeded)
        assert all(
            i["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
            for i in seeded
        )

    def test_acknowledged_alerts_are_resolved(self, endpoint_arn):
        _seed_alert(alert_id="alert-ack", status=AlertStatus.ACKNOWLEDGED.value)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1

    def test_already_resolved_alerts_are_skipped(self, endpoint_arn):
        _seed_alert(alert_id="alert-done", status=AlertStatus.RESOLVED.value)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_alerts_for_other_itineraries_are_untouched(self, endpoint_arn):
        _seed_alert(alert_id="alert-other", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        other = alert_store_mod.table.get_item(
            Key={"skymiles_number": SKYMILES, "alert_id": "alert-other"}
        )["Item"]
        assert other["status"] == AlertStatus.ACTIVE.value

    def test_alerts_for_other_members_are_untouched(self, endpoint_arn):
        _seed_alert(alert_id="alert-x", skymiles_number="0000000000")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0


class TestReEvaluationAfterChange:
    """Re-evaluation reflects the *updated* itinerary, including layovers."""

    def test_layover_requiring_transit_visa_is_flagged(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="CN", is_layover=True),
                _segment(destination="DE", flight_number="DL300",
                         departure_date="2030-09-16", arrival_date="2030-09-17"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_expired_passport_after_change_is_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, profile_overrides={"passport_expiry": "2020-01-01"}
        )

        response = handler(event, None)

        eval_body = response["body"]["evaluation_result"]["body"]
        assert eval_body["passport_status"] == "CRITICAL"
        assert eval_body["alerts_sent"] >= 1

    def test_new_alerts_persisted_after_stale_ones_resolved(self, endpoint_arn):
        _seed_alert(alert_id="alert-1")
        event = _base_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        fresh = [i for i in items if not i["alert_id"].startswith("alert-")]
        assert fresh
        assert all(i["status"] == AlertStatus.ACTIVE.value for i in fresh)
        assert all(i["itinerary_ref"] == CONFIRMATION for i in fresh)


class TestMalformedEvents:
    """Missing required envelope fields surface as KeyError."""

    def test_missing_detail_raises(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)

    def test_missing_profile_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)
