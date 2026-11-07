"""Tests for the EventBridge ItineraryChanged handler."""

import json
import uuid

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType
from src.services.alert_store import get_alerts_by_itinerary, save_alert

SKYMILES = "1234567890"
CONFIRMATION = "DL-EB-TEST"
RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto with a ConciergeAlerts table (incl. GSI) and an SNS endpoint."""
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
                    "KeySchema": [{"AttributeName": "itinerary_ref", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
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


def _segment(destination="XA", departure="2030-06-01", arrival="2030-06-02",
             is_layover=False, flight_number="DL100", origin="JFK") -> dict:
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _requirements(requires_visa=False, transit_visa_required=False,
                  passport_validity_months=6, exempt=None) -> dict:
    return {
        "country_code": "XA",
        "requires_visa": requires_visa,
        "transit_visa_required": transit_visa_required,
        "passport_validity_months": passport_validity_months,
        "visa_exempt_nationalities": exempt or [],
    }


def _base_event(endpoint_arn: str, *, change_type="DATE_CHANGE", segments=None,
                passport_expiry="2035-01-01", requirements=None,
                confirmation=CONFIRMATION, skymiles=SKYMILES,
                visa_records=None) -> dict:
    """Build a valid ItineraryChanged EventBridge envelope for a clean traveler."""
    detail = {
        "profile": {
            "skymiles_number": skymiles,
            "first_name": "Test",
            "last_name": "User",
            "nationality": "US",
            "passport_number": "P999999",
            "passport_expiry": passport_expiry,
            "endpoint_arn": endpoint_arn,
            "visa_records": visa_records or [],
        },
        "itinerary": {
            "confirmation_number": confirmation,
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": {"XA": requirements or _requirements()},
    }
    if change_type is not None:
        detail["change_type"] = change_type
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(*, skymiles=SKYMILES, itinerary_ref=CONFIRMATION,
                status=AlertStatus.ACTIVE, alert_type=AlertType.VISA) -> str:
    alert_id = str(uuid.uuid4())
    save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles,
            alert_type=alert_type,
            severity=AlertSeverity.CRITICAL,
            reasons=["Old alert"],
            created_at="2030-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )
    return alert_id


def _get_item(alert_id: str, skymiles: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles, "alert_id": alert_id}
    )["Item"]


def _sns_messages(endpoint_arn: str) -> list[dict]:
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


class TestResponseShape:
    """The handler always returns 200 with change_type, resolved count and evaluation result."""

    def test_happy_path_clean_traveler(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"] == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }

    @pytest.mark.parametrize(
        "change_type", ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"]
    )
    def test_change_type_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_context_is_ignored(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, object())

        assert response["statusCode"] == 200


class TestStaleAlertResolution:
    """Actionable alerts on the changed itinerary are resolved before re-evaluation."""

    def test_active_alert_resolved(self, endpoint_arn):
        alert_id = _seed_alert()
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_item(alert_id)
        assert item["status"] == AlertStatus.RESOLVED.value
        assert item["resolution"] == RESOLUTION
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        alert_id = _seed_alert(status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_item(alert_id)["status"] == AlertStatus.RESOLVED.value

    def test_multiple_alerts_counted(self, endpoint_arn):
        ids = [_seed_alert(alert_type=AlertType.VISA), _seed_alert(alert_type=AlertType.PASSPORT)]
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2
        assert all(_get_item(i)["status"] == AlertStatus.RESOLVED.value for i in ids)

    @pytest.mark.parametrize("status", [AlertStatus.RESOLVED, AlertStatus.EXPIRED])
    def test_non_actionable_alerts_untouched(self, endpoint_arn, status):
        alert_id = _seed_alert(status=status)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item(alert_id)["status"] == status.value

    def test_other_itinerary_untouched(self, endpoint_arn):
        alert_id = _seed_alert(itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item(alert_id)["status"] == AlertStatus.ACTIVE.value

    def test_other_member_untouched(self, endpoint_arn):
        alert_id = _seed_alert(skymiles="5555555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item(alert_id, "5555555555")["status"] == AlertStatus.ACTIVE.value


class TestReEvaluation:
    """After resolution, the updated itinerary is re-evaluated and new alerts are raised."""

    def test_new_destination_requiring_visa_raises_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE",
            requirements=_requirements(requires_visa=True),
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        assert len(_sns_messages(endpoint_arn)) == 1

    def test_visa_exempt_nationality_no_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, requirements=_requirements(requires_visa=True, exempt=["US"]),
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_valid_visa_on_file_no_alert(self, endpoint_arn):
        visa = {
            "country_code": "XA", "visa_type": "TOURIST", "issue_date": "2029-01-01",
            "expiry_date": "2034-01-01", "visa_number": "V1",
        }
        event = _base_event(
            endpoint_arn, requirements=_requirements(requires_visa=True), visa_records=[visa],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_date_change_past_passport_validity_raises_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, passport_expiry="2030-08-01",
            segments=[_segment(departure="2030-06-01", arrival="2030-06-02")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1

    def test_added_layover_without_transit_visa_requirement_no_alert(self, endpoint_arn):
        segments = [
            _segment(destination="XA", is_layover=True, flight_number="DL100"),
            _segment(destination="XB", origin="XA", flight_number="DL200"),
        ]
        event = _base_event(
            endpoint_arn, change_type="SEGMENT_ADDED", segments=segments,
            requirements=_requirements(requires_visa=True, transit_visa_required=False),
        )
        event["detail"]["requirements_override"]["XB"] = {
            **_requirements(), "country_code": "XB",
        }

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_added_layover_requiring_transit_visa_raises_alert(self, endpoint_arn):
        segments = [
            _segment(destination="XA", is_layover=True, flight_number="DL100"),
            _segment(destination="XB", origin="XA", flight_number="DL200"),
        ]
        event = _base_event(
            endpoint_arn, change_type="SEGMENT_ADDED", segments=segments,
            requirements=_requirements(requires_visa=True, transit_visa_required=True),
        )
        event["detail"]["requirements_override"]["XB"] = {
            **_requirements(), "country_code": "XB",
        }

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_old_alert_resolved_and_new_alert_active(self, endpoint_arn):
        old_id = _seed_alert()
        event = _base_event(endpoint_arn, requirements=_requirements(requires_visa=True))

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        alerts = {a["alert_id"]: a for a in get_alerts_by_itinerary(CONFIRMATION)}
        assert alerts[old_id]["status"] == AlertStatus.RESOLVED.value
        new = [a for i, a in alerts.items() if i != old_id]
        assert len(new) == 1
        assert new[0]["status"] == AlertStatus.ACTIVE.value
        assert new[0]["alert_type"] == AlertType.VISA.value

    def test_new_alerts_not_resolved_by_same_event(self, endpoint_arn):
        event = _base_event(endpoint_arn, requirements=_requirements(requires_visa=True))

        handler(event, None)
        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        statuses = sorted(a["status"] for a in get_alerts_by_itinerary(CONFIRMATION))
        assert statuses == [AlertStatus.ACTIVE.value, AlertStatus.RESOLVED.value]


class TestMalformedEvents:
    """Required envelope fields are enforced via KeyError."""

    def test_missing_detail(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)

    def test_missing_profile(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_confirmation_number(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)
