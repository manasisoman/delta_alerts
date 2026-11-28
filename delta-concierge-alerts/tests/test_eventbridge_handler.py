"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import boto3
import moto
import moto.core
import pytest
from moto.sns.models import sns_backends

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType

SKYMILES = "1234567890"
CONFIRMATION = "DL-EB-TEST"
AUTO_RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

# Made-up country codes keep tests isolated from real COUNTRY_REQUIREMENTS.
REQUIREMENTS_OVERRIDE = {
    "XA": {
        "country_code": "XA",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US"],
    },
    "XB": {
        "country_code": "XB",
        "requires_visa": True,
        "transit_visa_required": True,
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


def _segment(
    destination: str = "XA",
    departure: str = "2030-09-15",
    arrival: str = "2030-09-16",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> dict:
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid ItineraryChanged EventBridge envelope.

    Defaults describe a US national flying to XA (visa-exempt) with a
    passport valid until 2040, which yields no alerts. Profile-level fields
    and ``change_type``/``segments`` may be overridden by keyword.
    """
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
        "change_type": "DATE_CHANGE",
        "requirements_override": REQUIREMENTS_OVERRIDE,
    }
    for key, val in overrides.items():
        if key in detail["profile"]:
            detail["profile"][key] = val
        elif key == "segments":
            detail["itinerary"]["segments"] = val
        else:
            detail[key] = val
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    alert_id: str,
    status: AlertStatus = AlertStatus.ACTIVE,
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
) -> None:
    alert_store_mod.save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles_number,
            alert_type=AlertType.VISA,
            severity=AlertSeverity.CRITICAL,
            reasons=["seeded"],
            created_at="2030-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )


def _get_item(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _all_items() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


def _sns_messages(endpoint_arn: str) -> list:
    backend = sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return list(backend.platform_endpoints[endpoint_arn].messages.values())


class TestHappyPath:
    """A clean itinerary change resolves nothing and returns OK statuses."""

    def test_response_shape_for_clean_itinerary(self, endpoint_arn):
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
        assert _all_items() == []
        assert _sns_messages(endpoint_arn) == []


class TestChangeType:
    """The event's change_type is echoed back, defaulting to UNKNOWN."""

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

        assert response["statusCode"] == 200
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
        assert item["resolution"] == AUTO_RESOLUTION
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-ack", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_item("old-ack")["status"] == AlertStatus.RESOLVED.value

    def test_multiple_stale_alerts_counted(self, endpoint_arn):
        _seed_alert("a1")
        _seed_alert("a2", status=AlertStatus.ACKNOWLEDGED)
        _seed_alert("a3")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 3

    def test_already_resolved_alert_not_recounted(self, endpoint_arn):
        _seed_alert("done", status=AlertStatus.RESOLVED)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert "resolution" not in _get_item("done")

    def test_other_itinerary_alert_untouched(self, endpoint_arn):
        _seed_alert("other-itin", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item("other-itin")["status"] == AlertStatus.ACTIVE.value

    def test_other_member_alert_untouched(self, endpoint_arn):
        _seed_alert("other-member", skymiles_number="5555555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = _get_item("other-member", skymiles_number="5555555555")
        assert item["status"] == AlertStatus.ACTIVE.value


class TestReEvaluationAfterChange:
    """The updated itinerary is re-evaluated and new alerts are raised."""

    def test_route_change_to_visa_country_raises_new_alert(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(
            endpoint_arn,
            change_type="ROUTE_CHANGE",
            segments=[_segment(destination="XB")],
        )

        response = handler(event, None)

        body = response["body"]
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        assert body["evaluation_result"]["body"]["alerts_sent"] == 1
        active = [i for i in _all_items() if i["status"] == AlertStatus.ACTIVE.value]
        assert len(active) == 1
        assert active[0]["alert_type"] == "VISA"
        assert active[0]["itinerary_ref"] == CONFIRMATION
        assert "No visa on file for XB" in active[0]["reasons"]
        assert len(_sns_messages(endpoint_arn)) == 1

    def test_date_change_past_passport_validity_raises_passport_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2030-10-01",
            segments=[_segment(departure="2030-09-15", arrival="2030-09-16")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"
        reasons = _all_items()[0]["reasons"]
        assert any("3-month validity requirement" in r for r in reasons)

    def test_passport_meeting_country_rule_within_six_months_is_info(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-01-01",
            segments=[_segment(departure="2030-09-15", arrival="2030-09-16")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "INFO"

    def test_date_moved_after_visa_expiry_raises_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            visa_records=[
                {
                    "country_code": "XB",
                    "visa_type": "TOURIST",
                    "issue_date": "2029-09-01",
                    "expiry_date": "2030-09-01",
                    "visa_number": "V123",
                }
            ],
            segments=[_segment(destination="XB", departure="2030-09-15", arrival="2030-09-16")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        reasons = _all_items()[0]["reasons"]
        assert "Visa for XB expires before date of travel" in reasons

    def test_segment_removed_clears_alerts(self, endpoint_arn):
        _seed_alert("old-xb")
        event = _base_event(
            endpoint_arn,
            change_type="SEGMENT_REMOVED",
            segments=[_segment(destination="XA")],
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0
        assert all(i["status"] == AlertStatus.RESOLVED.value for i in _all_items())


class TestLayoverSegments:
    """Layovers only trigger visa alerts when the country requires a transit visa."""

    def test_layover_without_transit_requirement_no_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            change_type="SEGMENT_ADDED",
            segments=[
                _segment(destination="XA", is_layover=True, flight_number="DL1"),
                _segment(destination="XA", is_layover=True, flight_number="DL2",
                         departure="2030-09-16", arrival="2030-09-17"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_layover_with_transit_requirement_raises_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            change_type="SEGMENT_ADDED",
            segments=[_segment(destination="XB", is_layover=True)],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"


class TestMalformedEvent:
    """Required envelope fields missing raise KeyError before any side effects."""

    def test_missing_detail_raises(self, endpoint_arn):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)

    def test_missing_skymiles_number_raises(self, endpoint_arn):
        _seed_alert("old-active")
        event = _base_event(endpoint_arn)
        del event["detail"]["profile"]["skymiles_number"]

        with pytest.raises(KeyError):
            handler(event, None)

        assert _get_item("old-active")["status"] == AlertStatus.ACTIVE.value

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)
