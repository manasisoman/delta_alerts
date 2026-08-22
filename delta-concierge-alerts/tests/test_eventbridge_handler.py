"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import uuid
from datetime import datetime

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


def _segment(
    destination: str = "CN",
    departure: str = "2030-09-15",
    arrival: str = "2030-09-16",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "JFK",
) -> dict:
    """Build a raw segment dict with sensible defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _base_event(
    endpoint_arn: str,
    *,
    change_type: str | None = "DATE_CHANGE",
    nationality: str = "IN",
    passport_number: str | None = "P999999",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
    segments: list[dict] | None = None,
    confirmation: str = CONFIRMATION,
) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope."""
    detail: dict = {
        "profile": {
            "skymiles_number": SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": nationality,
            "passport_number": passport_number,
            "passport_expiry": passport_expiry,
            "endpoint_arn": endpoint_arn,
            "visa_records": visa_records or [],
        },
        "itinerary": {
            "confirmation_number": confirmation,
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": None,
    }
    if change_type is not None:
        detail["change_type"] = change_type

    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    *,
    itinerary_ref: str = CONFIRMATION,
    status: AlertStatus = AlertStatus.ACTIVE,
    alert_type: AlertType = AlertType.VISA,
) -> str:
    """Persist a pre-existing alert and return its alert_id."""
    alert_id = str(uuid.uuid4())
    alert_store_mod.save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=SKYMILES,
            alert_type=alert_type,
            severity=AlertSeverity.CRITICAL,
            reasons=["Stale alert from previous itinerary"],
            created_at=datetime(2026, 1, 1).isoformat(),
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )
    return alert_id


def _item(alert_id: str) -> dict:
    """Fetch a single alert item from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": SKYMILES, "alert_id": alert_id}
    )["Item"]


class TestHappyPath:
    """A valid ItineraryChanged event re-evaluates and reports the result."""

    def test_response_shape_and_change_type(self, endpoint_arn):
        """IN national to CN without a visa re-evaluates to CRITICAL."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """A detail without change_type is reported as UNKNOWN."""
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_clean_traveler_produces_no_alerts(self, endpoint_arn):
        """US national to DE with a far-future passport raises nothing."""
        event = _base_event(
            endpoint_arn,
            nationality="US",
            segments=[_segment(destination="DE")],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["passport_status"] == "OK"
        assert result_body["visa_status"] == "OK"
        assert result_body["alerts_sent"] == 0


class TestStaleAlertResolution:
    """Alerts for the changed itinerary are resolved before re-evaluation."""

    def test_active_alert_is_resolved(self, endpoint_arn):
        """An ACTIVE alert on the same itinerary is auto-resolved."""
        alert_id = _seed_alert()
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _item(alert_id)
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"

    def test_acknowledged_alert_is_resolved(self, endpoint_arn):
        """An ACKNOWLEDGED alert on the same itinerary is also resolved."""
        alert_id = _seed_alert(status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _item(alert_id)["status"] == AlertStatus.RESOLVED.value

    def test_alerts_for_other_itineraries_are_untouched(self, endpoint_arn):
        """Alerts referencing a different confirmation number stay ACTIVE."""
        other_id = _seed_alert(itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _item(other_id)["status"] == AlertStatus.ACTIVE.value

    def test_multiple_stale_alerts_are_all_resolved(self, endpoint_arn):
        """Every matching stale alert is counted and resolved."""
        first = _seed_alert(alert_type=AlertType.VISA)
        second = _seed_alert(alert_type=AlertType.PASSPORT)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2
        assert _item(first)["status"] == AlertStatus.RESOLVED.value
        assert _item(second)["status"] == AlertStatus.RESOLVED.value

    def test_new_alerts_persisted_after_resolution(self, endpoint_arn):
        """Re-evaluation writes fresh ACTIVE alerts alongside resolved ones."""
        _seed_alert()
        event = _base_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        active = [i for i in items if i["status"] == AlertStatus.ACTIVE.value]
        assert len(active) == 1
        assert "No visa on file for CN" in active[0]["reasons"]


class TestDocumentValidityReEvaluation:
    """Passport and visa limits are re-checked against the changed itinerary."""

    def test_expired_passport_is_critical(self, endpoint_arn):
        """A passport that already expired re-evaluates to CRITICAL."""
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["passport_status"] == "CRITICAL"

    def test_missing_passport_is_critical(self, endpoint_arn):
        """Null passport fields re-evaluate to CRITICAL."""
        event = _base_event(
            endpoint_arn, passport_number=None, passport_expiry=None
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["passport_status"] == "CRITICAL"

    def test_passport_below_destination_validity_window(self, endpoint_arn):
        """Passport valid for under CN's 6-month window is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            nationality="US",
            passport_expiry="2030-10-01",
            segments=[_segment(destination="CN", departure="2030-09-15",
                               arrival="2030-09-16")],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["passport_status"] == "CRITICAL"

    def test_valid_visa_clears_destination_requirement(self, endpoint_arn):
        """A visa outlasting travel satisfies CN's visa requirement."""
        event = _base_event(
            endpoint_arn,
            visa_records=[
                {
                    "country_code": "CN",
                    "visa_type": "TOURIST",
                    "issue_date": "2026-01-01",
                    "expiry_date": "2032-01-01",
                    "visa_number": "V1",
                }
            ],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["visa_status"] == "OK"

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        """A visa expiring before the new departure date is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            visa_records=[
                {
                    "country_code": "CN",
                    "visa_type": "TOURIST",
                    "issue_date": "2026-01-01",
                    "expiry_date": "2030-01-01",
                    "visa_number": "V1",
                }
            ],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["visa_status"] == "CRITICAL"


class TestLayoverSegments:
    """Layovers only require a visa where transit visas are required."""

    def test_layover_without_transit_visa_requirement_is_ok(self, endpoint_arn):
        """A DE layover needs no visa because transit visas aren't required."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE", is_layover=True)],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["visa_status"] == "OK"

    def test_layover_requiring_transit_visa_is_critical(self, endpoint_arn):
        """A CN layover still needs a transit visa, so missing one is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", is_layover=True)],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["visa_status"] == "CRITICAL"

    def test_multi_segment_itinerary_reports_worst_severity(self, endpoint_arn):
        """Severity is consolidated across the layover and final destination."""
        event = _base_event(
            endpoint_arn,
            nationality="US",
            segments=[
                _segment(destination="DE", is_layover=True,
                         departure="2030-09-15", arrival="2030-09-15"),
                _segment(destination="CN", flight_number="DL200", origin="FRA",
                         departure="2030-09-16", arrival="2030-09-17"),
            ],
        )

        response = handler(event, None)

        result_body = response["body"]["evaluation_result"]["body"]
        assert result_body["visa_status"] == "CRITICAL"


class TestMalformedEvents:
    """Missing required envelope fields surface as KeyError."""

    def test_missing_detail_raises(self, endpoint_arn):
        """An envelope without detail cannot be processed."""
        event = {"source": "delta.booking-system", "detail-type": "ItineraryChanged"}

        with pytest.raises(KeyError, match="detail"):
            handler(event, None)

    def test_missing_profile_raises(self, endpoint_arn):
        """A detail without profile cannot be processed."""
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]

        with pytest.raises(KeyError, match="profile"):
            handler(event, None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        """A profile without skymiles_number cannot be processed."""
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]["skymiles_number"]

        with pytest.raises(KeyError, match="skymiles_number"):
            handler(event, None)

    def test_missing_itinerary_raises(self, endpoint_arn):
        """A detail without itinerary cannot be processed."""
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]

        with pytest.raises(KeyError, match="itinerary"):
            handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        """An itinerary without confirmation_number cannot be processed."""
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError, match="confirmation_number"):
            handler(event, None)

    def test_missing_segments_fails_during_re_evaluation(self, endpoint_arn):
        """Segments are required by re-evaluation, after alerts are resolved."""
        alert_id = _seed_alert()
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["segments"]

        with pytest.raises(KeyError, match="segments"):
            handler(event, None)

        assert _item(alert_id)["status"] == AlertStatus.RESOLVED.value
