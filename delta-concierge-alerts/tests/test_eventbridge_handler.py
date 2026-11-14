"""Tests for the EventBridge ``ItineraryChanged`` handler."""

import copy
import json

import boto3
import moto
import moto.core
import moto.sns.models
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType

RESOLUTION_TEXT = "Auto-resolved: itinerary changed, re-evaluating"
SKYMILES = "1234567890"
CONFIRMATION = "DL-EB01"

# Made-up country codes so tests don't depend on COUNTRY_REQUIREMENTS data.
REQUIREMENTS_OVERRIDE = {
    # Visa required, no transit visa, US exempt, 3-month passport rule
    "XA": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US"],
    },
    # Visa required including transit, no exemptions, 6-month passport rule
    "XB": {
        "requires_visa": True,
        "transit_visa_required": True,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
    # No visa required
    "XC": {
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
}


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and wire a ConciergeAlerts table (with GSI) and SNS endpoint."""
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
                {"AttributeName": "itinerary_ref", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "itinerary-ref-index",
                    "KeySchema": [
                        {"AttributeName": "itinerary_ref", "KeyType": "HASH"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="EventBridgeTestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="eb-token",
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


def _segment(
    destination: str = "XA",
    departure: str = "2030-06-01",
    arrival: str = "2030-06-02",
    flight_number: str = "DL100",
    is_layover: bool = False,
) -> dict:
    return {
        "flight_number": flight_number,
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid clean ItineraryChanged event (US traveler to visa-exempt XA).

    Overrides may target ``detail`` keys (e.g. ``change_type``, ``itinerary``)
    or ``profile`` keys (e.g. ``nationality``, ``passport_expiry``).
    """
    event = {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": {
            "profile": {
                "skymiles_number": SKYMILES,
                "first_name": "Test",
                "last_name": "Traveler",
                "nationality": "US",
                "passport_number": "P123456",
                "passport_expiry": "2040-01-01",
                "endpoint_arn": endpoint_arn,
                "visa_records": [],
            },
            "itinerary": {
                "confirmation_number": CONFIRMATION,
                "segments": [_segment()],
            },
            "change_type": "DATE_CHANGE",
            "requirements_override": copy.deepcopy(REQUIREMENTS_OVERRIDE),
        },
    }
    detail = event["detail"]
    for key, val in overrides.items():
        if key in detail["profile"]:
            detail["profile"][key] = val
        else:
            detail[key] = val
    return event


def _seed_alert(
    alert_id: str,
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
    status: AlertStatus = AlertStatus.ACTIVE,
) -> None:
    alert_store_mod.save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles_number,
            alert_type=AlertType.VISA,
            severity=AlertSeverity.CRITICAL,
            reasons=["No visa on file for XB"],
            created_at="2030-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )


def _get_item(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _sns_messages(endpoint_arn: str) -> list[dict]:
    backend = moto.sns.models.sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


class TestResponseShape:
    """A clean itinerary change returns 200 with change_type, resolved count and evaluation."""

    def test_happy_path_response(self, endpoint_arn):
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

    def test_clean_change_persists_nothing_and_sends_nothing(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert alert_store_mod.table.scan()["Items"] == []
        assert _sns_messages(endpoint_arn) == []

    def test_context_is_ignored(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, object())

        assert response["statusCode"] == 200


class TestChangeType:
    """change_type is echoed back verbatim, defaulting to UNKNOWN when absent."""

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_known_change_types_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_unrecognised_change_type_passed_through(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type="CABIN_UPGRADE")

        response = handler(event, None)

        assert response["body"]["change_type"] == "CABIN_UPGRADE"


class TestStaleAlertResolution:
    """Only ACTIVE/ACKNOWLEDGED alerts for this member + booking are resolved."""

    def test_active_alert_resolved(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_item("old-active")
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == RESOLUTION_TEXT
        assert item["resolved_at"]

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-ack", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_item("old-ack")["status"] == AlertStatus.RESOLVED.value

    def test_multiple_alerts_all_resolved(self, endpoint_arn):
        _seed_alert("a1")
        _seed_alert("a2", status=AlertStatus.ACKNOWLEDGED)
        _seed_alert("a3")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 3
        for alert_id in ("a1", "a2", "a3"):
            assert _get_item(alert_id)["status"] == AlertStatus.RESOLVED.value

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_terminal_status_alerts_not_counted(self, endpoint_arn, status):
        _seed_alert("terminal", status=status)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = _get_item("terminal")
        assert item["status"] == status.value
        assert "resolution" not in item

    def test_other_itinerary_alert_untouched(self, endpoint_arn):
        _seed_alert("other-booking", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item("other-booking")["status"] == AlertStatus.ACTIVE.value

    def test_other_member_alert_on_same_booking_untouched(self, endpoint_arn):
        _seed_alert("companion", skymiles_number="5555555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item("companion", "5555555555")["status"] == AlertStatus.ACTIVE.value


class TestReEvaluation:
    """After resolving stale alerts the updated itinerary is re-evaluated."""

    def test_missing_visa_creates_new_active_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="IN")

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["visa_status"] == "CRITICAL"
        assert evaluation["alerts_sent"] == 1
        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["status"] == AlertStatus.ACTIVE.value
        assert items[0]["itinerary_ref"] == CONFIRMATION
        assert "No visa on file for XA" in items[0]["reasons"]

    def test_new_alert_pushed_via_sns(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="IN")

        handler(event, None)

        messages = _sns_messages(endpoint_arn)
        assert len(messages) == 1
        gcm = json.loads(messages[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Visa Alert"
        assert gcm["data"]["confirmation_number"] == CONFIRMATION

    def test_old_alert_resolved_and_new_alert_active_on_same_booking(self, endpoint_arn):
        _seed_alert("stale")
        event = _base_event(endpoint_arn, nationality="IN")

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        alerts = alert_store_mod.get_alerts_by_itinerary(CONFIRMATION)
        statuses = sorted(a["status"] for a in alerts)
        assert statuses == [AlertStatus.ACTIVE.value, AlertStatus.RESOLVED.value]
        assert _get_item("stale")["status"] == AlertStatus.RESOLVED.value

    def test_repeat_event_resolves_previous_evaluation_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="IN")
        handler(event, None)

        response = handler(copy.deepcopy(event), None)

        assert response["body"]["resolved_alerts"] == 1
        alerts = alert_store_mod.get_alerts_by_itinerary(CONFIRMATION)
        assert len(alerts) == 2
        active = [a for a in alerts if a["status"] == AlertStatus.ACTIVE.value]
        assert len(active) == 1

    def test_route_change_to_visa_free_country_clears_alerts(self, endpoint_arn):
        _seed_alert("stale")
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            change_type="ROUTE_CHANGE",
            itinerary={"confirmation_number": CONFIRMATION, "segments": [_segment("XC")]},
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0
        assert alert_store_mod.get_active_alerts(SKYMILES) == []

    def test_segment_added_to_visa_country_raises_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            change_type="SEGMENT_ADDED",
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [
                    _segment("XA"),
                    _segment("XB", "2030-06-10", "2030-06-11", flight_number="DL200"),
                ],
            },
        )

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["No visa on file for XB"]

    def test_date_change_past_visa_expiry_is_critical(self, endpoint_arn):
        visa = {
            "country_code": "XB",
            "visa_type": "TOURIST",
            "issue_date": "2029-01-01",
            "expiry_date": "2030-05-01",
            "visa_number": "V100",
        }
        event = _base_event(
            endpoint_arn,
            visa_records=[visa],
            itinerary={"confirmation_number": CONFIRMATION, "segments": [_segment("XB")]},
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["Visa for XB expires before date of travel"]

    def test_passport_below_country_minimum_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-07-15")

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == "CRITICAL"
        assert evaluation["visa_status"] == "OK"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["alert_type"] == AlertType.PASSPORT.value

    def test_passport_and_visa_issues_both_alerted(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="IN", passport_expiry="2030-07-15")

        response = handler(event, None)

        evaluation = response["body"]["evaluation_result"]["body"]
        assert evaluation["alerts_sent"] == 2
        assert len(_sns_messages(endpoint_arn)) == 2


class TestLayovers:
    """Layovers only trigger visa alerts when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_no_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [_segment("XA", is_layover=True)],
            },
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [_segment("XB", is_layover=True)],
            },
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"


class TestMalformedEvents:
    """Required envelope fields missing raise KeyError before any side effects."""

    def test_missing_detail_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]["skymiles_number"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_confirmation_number_raises_without_resolving(self, endpoint_arn):
        _seed_alert("keep")
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)

        assert _get_item("keep")["status"] == AlertStatus.ACTIVE.value
