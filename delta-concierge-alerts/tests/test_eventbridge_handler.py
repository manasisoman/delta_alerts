"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import json

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler
from src.models.types import AlertRecord, AlertSeverity, AlertStatus, AlertType
from src.services.alert_store import get_alerts_by_itinerary, get_alerts_by_member, save_alert

SKYMILES = "1234567890"
CONFIRMATION = "DL-EB-TEST"
AUTO_RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks with a DynamoDB table (incl. itinerary GSI) and an SNS endpoint."""
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


def _segment(destination="XA", departure="2031-03-01", arrival="2031-03-02", **overrides) -> dict:
    """Build a serialized flight segment with far-future default dates."""
    seg = {
        "flight_number": "DL200",
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


def _requirements(**overrides) -> dict:
    """Build a requirements_override entry for made-up country XA (no alert by default)."""
    reqs = {
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    reqs.update(overrides)
    return reqs


def _base_event(endpoint_arn: str, **detail_overrides) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope; overrides apply to ``detail``."""
    detail = {
        "profile": {
            "skymiles_number": SKYMILES,
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2040-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": CONFIRMATION,
            "segments": [_segment()],
        },
        "requirements_override": {"XA": _requirements()},
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
    """Persist a pre-existing alert record."""
    save_alert(
        AlertRecord(
            alert_id=alert_id,
            skymiles_number=skymiles_number,
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.WARNING,
            reasons=["old reason"],
            created_at="2030-01-01T00:00:00",
            itinerary_ref=itinerary_ref,
            status=status,
        )
    )


def _alerts_by_id(skymiles_number: str = SKYMILES) -> dict[str, dict]:
    return {a["alert_id"]: a for a in get_alerts_by_member(skymiles_number)}


def _pushed_messages(endpoint_arn: str) -> list[dict]:
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


class TestResponseEnvelope:
    """The handler always returns 200 with change_type, resolved count and evaluation result."""

    def test_happy_path_no_stale_alerts_no_issues(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["statusCode"] == 200
        assert result["body"]["change_type"] == "DATE_CHANGE"
        assert result["body"]["resolved_alerts"] == 0
        assert result["body"]["evaluation_result"] == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }

    @pytest.mark.parametrize(
        "change_type", ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"]
    )
    def test_change_type_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        result = handler(event, None)

        assert result["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["change_type"]

        result = handler(event, None)

        assert result["body"]["change_type"] == "UNKNOWN"

    def test_missing_detail_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_confirmation_number_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)


class TestStaleAlertResolution:
    """Only ACTIVE/ACKNOWLEDGED alerts for this member + itinerary are auto-resolved."""

    def test_active_and_acknowledged_alerts_resolved(self, endpoint_arn):
        _seed_alert("a1", status=AlertStatus.ACTIVE)
        _seed_alert("a2", status=AlertStatus.ACKNOWLEDGED)

        result = handler(_base_event(endpoint_arn), None)

        assert result["body"]["resolved_alerts"] == 2
        alerts = _alerts_by_id()
        for alert_id in ("a1", "a2"):
            assert alerts[alert_id]["status"] == AlertStatus.RESOLVED.value
            assert alerts[alert_id]["resolution"] == AUTO_RESOLUTION
            assert "resolved_at" in alerts[alert_id]

    def test_already_resolved_or_expired_alerts_untouched(self, endpoint_arn):
        _seed_alert("r1", status=AlertStatus.RESOLVED)
        _seed_alert("e1", status=AlertStatus.EXPIRED)

        result = handler(_base_event(endpoint_arn), None)

        assert result["body"]["resolved_alerts"] == 0
        alerts = _alerts_by_id()
        assert alerts["r1"]["status"] == AlertStatus.RESOLVED.value
        assert "resolution" not in alerts["r1"]
        assert alerts["e1"]["status"] == AlertStatus.EXPIRED.value

    def test_alerts_for_other_itinerary_untouched(self, endpoint_arn):
        _seed_alert("other", itinerary_ref="DL-OTHER")

        result = handler(_base_event(endpoint_arn), None)

        assert result["body"]["resolved_alerts"] == 0
        assert _alerts_by_id()["other"]["status"] == AlertStatus.ACTIVE.value

    def test_alerts_for_other_member_on_same_booking_untouched(self, endpoint_arn):
        _seed_alert("companion", skymiles_number="5555555555")

        result = handler(_base_event(endpoint_arn), None)

        assert result["body"]["resolved_alerts"] == 0
        assert _alerts_by_id("5555555555")["companion"]["status"] == AlertStatus.ACTIVE.value


class TestReEvaluationAfterChange:
    """The changed itinerary is re-evaluated and new alerts are saved and pushed."""

    def test_new_route_needs_visa_creates_visa_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            change_type="ROUTE_CHANGE",
            requirements_override={"XA": _requirements(requires_visa=True)},
        )

        result = handler(event, None)

        evaluation = result["body"]["evaluation_result"]["body"]
        assert evaluation["alerts_sent"] == 1
        assert evaluation["visa_status"] != "OK"
        assert evaluation["passport_status"] == "OK"
        alerts = list(_alerts_by_id().values())
        assert len(alerts) == 1
        assert alerts[0]["alert_type"] == AlertType.VISA.value
        assert alerts[0]["status"] == AlertStatus.ACTIVE.value
        assert len(_pushed_messages(endpoint_arn)) == 1

    def test_date_change_inside_passport_window_creates_passport_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [_segment(departure="2039-10-01", arrival="2039-10-02")],
            },
        )

        result = handler(event, None)

        evaluation = result["body"]["evaluation_result"]["body"]
        assert evaluation["alerts_sent"] == 1
        assert evaluation["passport_status"] == AlertSeverity.CRITICAL.value
        assert evaluation["visa_status"] == "OK"
        alerts = list(_alerts_by_id().values())
        assert [a["alert_type"] for a in alerts] == [AlertType.PASSPORT.value]

    def test_passport_meets_shorter_validity_requirement_downgrades_to_info(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [_segment(departure="2039-08-01", arrival="2039-08-02")],
            },
            requirements_override={"XA": _requirements(passport_validity_months=3)},
        )

        result = handler(event, None)

        assert (
            result["body"]["evaluation_result"]["body"]["passport_status"]
            == AlertSeverity.INFO.value
        )

    def test_expired_passport_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["detail"]["profile"]["passport_expiry"] = "2020-01-01"

        result = handler(event, None)

        evaluation = result["body"]["evaluation_result"]["body"]
        assert evaluation["passport_status"] == AlertSeverity.CRITICAL.value

    def test_visa_exempt_nationality_no_visa_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "XA": _requirements(requires_visa=True, visa_exempt_nationalities=["IN"])
            },
        )

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_valid_visa_record_no_visa_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            requirements_override={"XA": _requirements(requires_visa=True)},
        )
        event["detail"]["profile"]["visa_records"] = [
            {
                "country_code": "XA",
                "visa_type": "TOURIST",
                "issue_date": "2030-01-01",
                "expiry_date": "2040-01-01",
                "visa_number": "V123",
            }
        ]

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_segment_added_with_transit_visa_layover_creates_visa_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            change_type="SEGMENT_ADDED",
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [
                    _segment(destination="XB", is_layover=True, flight_number="DL201"),
                    _segment(origin="XB", flight_number="DL202"),
                ],
            },
            requirements_override={
                "XA": _requirements(),
                "XB": _requirements(requires_visa=True, transit_visa_required=True),
            },
        )

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] != "OK"

    def test_layover_without_transit_visa_requirement_no_visa_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            itinerary={
                "confirmation_number": CONFIRMATION,
                "segments": [
                    _segment(destination="XB", is_layover=True, flight_number="DL201"),
                    _segment(origin="XB", flight_number="DL202"),
                ],
            },
            requirements_override={
                "XA": _requirements(),
                "XB": _requirements(requires_visa=True, transit_visa_required=False),
            },
        )

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_old_alerts_resolved_and_new_alerts_active_on_same_booking(self, endpoint_arn):
        _seed_alert("stale-1")
        _seed_alert("stale-2", status=AlertStatus.ACKNOWLEDGED)
        event = _base_event(
            endpoint_arn,
            change_type="ROUTE_CHANGE",
            requirements_override={"XA": _requirements(requires_visa=True)},
        )

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 2
        by_status: dict[str, list[str]] = {}
        for alert in get_alerts_by_itinerary(CONFIRMATION):
            by_status.setdefault(alert["status"], []).append(alert["alert_id"])
        assert sorted(by_status[AlertStatus.RESOLVED.value]) == ["stale-1", "stale-2"]
        assert len(by_status[AlertStatus.ACTIVE.value]) == 1
        assert set(by_status) == {AlertStatus.RESOLVED.value, AlertStatus.ACTIVE.value}

    def test_repeated_change_events_resolve_previous_run_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            requirements_override={"XA": _requirements(requires_visa=True)},
        )
        handler(event, None)

        second = handler(event, None)

        assert second["body"]["resolved_alerts"] == 1
        statuses = sorted(a["status"] for a in get_alerts_by_itinerary(CONFIRMATION))
        assert statuses == [AlertStatus.ACTIVE.value, AlertStatus.RESOLVED.value]
