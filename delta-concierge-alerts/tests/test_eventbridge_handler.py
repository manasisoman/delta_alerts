"""Tests for the EventBridge ItineraryChanged handler."""

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


def _segment(
    destination: str = "CN",
    departure: str = "2030-09-15",
    arrival: str = "2030-09-16",
    is_layover: bool = False,
    flight_number: str = "DL100",
) -> dict:
    """Build a raw segment dict for an EventBridge detail payload."""
    return {
        "flight_number": flight_number,
        "origin": "JFK",
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **detail_overrides) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope with detail overrides."""
    detail: dict = {
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
    detail.update(detail_overrides)

    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    alert_id: str = "old-alert-1",
    itinerary_ref: str = CONFIRMATION,
    status: str = AlertStatus.ACTIVE.value,
) -> None:
    """Insert a pre-existing alert record directly into the mocked table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": SKYMILES,
            "alert_id": alert_id,
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "reasons": ["Stale reason"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


def _get_alert(alert_id: str) -> dict:
    """Fetch a single alert item by sort key from the mocked table."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": SKYMILES, "alert_id": alert_id}
    )["Item"]


class TestResponseShape:
    """The handler returns statusCode 200 and echoes change metadata."""

    def test_happy_path_response(self, endpoint_arn):
        """A clean traveler yields no resolved alerts and an OK evaluation."""
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"] = [_segment(destination="DE")]

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["body"]["alerts_sent"] == 0
        assert body["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """An absent change_type falls back to UNKNOWN."""
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_change_type_is_passed_through(self, endpoint_arn):
        """Any change_type value is echoed verbatim in the response body."""
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")

        response = handler(event, None)

        assert response["body"]["change_type"] == "SEGMENT_ADDED"


class TestStaleAlertResolution:
    """Alerts for the changed itinerary are auto-resolved before re-evaluation."""

    def test_active_alert_for_itinerary_is_resolved(self, endpoint_arn):
        """An ACTIVE alert on the changed itinerary is resolved and counted."""
        _seed_alert()
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        resolved = _get_alert("old-alert-1")
        assert resolved["status"] == AlertStatus.RESOLVED.value
        assert resolved["resolution"] == (
            "Auto-resolved: itinerary changed, re-evaluating"
        )

    def test_alerts_for_other_itineraries_are_untouched(self, endpoint_arn):
        """An alert on a different confirmation number stays ACTIVE."""
        _seed_alert(alert_id="other-alert", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("other-alert")["status"] == AlertStatus.ACTIVE.value

    def test_already_resolved_alert_is_not_recounted(self, endpoint_arn):
        """A RESOLVED alert on the same itinerary is not resolved again."""
        _seed_alert(alert_id="done-alert", status=AlertStatus.RESOLVED.value)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_multiple_stale_alerts_are_all_resolved(self, endpoint_arn):
        """Every active/acknowledged alert for the itinerary is resolved."""
        _seed_alert(alert_id="stale-1")
        _seed_alert(alert_id="stale-2", status=AlertStatus.ACKNOWLEDGED.value)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2


class TestReEvaluation:
    """The updated itinerary is re-evaluated against the traveler's documents."""

    def test_missing_visa_after_change_creates_new_alert(self, endpoint_arn):
        """An IN national flying to CN gets a fresh CRITICAL visa alert."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]
        assert evaluation["body"]["visa_status"] == "CRITICAL"
        assert evaluation["body"]["alerts_sent"] >= 1
        new_alerts = [
            i
            for i in alert_store_mod.table.scan()["Items"]
            if i["status"] == AlertStatus.ACTIVE.value
        ]
        assert len(new_alerts) >= 1

    def test_added_layover_requiring_transit_visa_alerts(self, endpoint_arn):
        """A newly added CN layover triggers a visa alert for a US national."""
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED")
        event["detail"]["profile"]["nationality"] = "US"
        event["detail"]["itinerary"]["segments"] = [
            _segment(destination="CN", is_layover=True),
            _segment(
                destination="DE",
                departure="2030-09-16",
                arrival="2030-09-17",
                flight_number="DL200",
            ),
        ]

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]
        assert evaluation["body"]["visa_status"] == "CRITICAL"

    def test_expired_passport_after_change_alerts(self, endpoint_arn):
        """A past passport expiry yields a CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == (
            "CRITICAL"
        )

    def test_stale_alert_resolved_and_itinerary_re_evaluated(self, endpoint_arn):
        """Resolution and re-evaluation both happen within a single invocation."""
        _seed_alert()
        event = _base_event(endpoint_arn, change_type="ROUTE_CHANGE")

        response = handler(event, None)

        body = response["body"]
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["statusCode"] == 200
        assert body["evaluation_result"]["body"]["alerts_sent"] >= 1
