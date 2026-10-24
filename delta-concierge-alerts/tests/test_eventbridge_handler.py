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

SKYMILES = "1234567890"
CONFIRMATION = "DL-CHG01"
RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

REQUIREMENTS = {
    "CN": {
        "requires_visa": True,
        "transit_visa_required": True,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
    "DE": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB", "CA"],
    },
    "JP": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": ["US", "GB"],
    },
    "MX": {
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
}


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the alerts table (with GSI) and an SNS endpoint."""
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
    destination: str = "DE",
    departure: str = "2030-06-01",
    arrival: str = "2030-06-02",
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


def _visa(country_code: str = "CN", expiry: str = "2035-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoint_arn: str, segments=None, change_type="DATE_CHANGE", **profile_overrides) -> dict:
    """Build a valid ItineraryChanged envelope: US traveler to DE, no issues."""
    profile = {
        "skymiles_number": SKYMILES,
        "first_name": "Test",
        "last_name": "User",
        "nationality": "US",
        "passport_number": "P999999",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    profile.update(profile_overrides)
    detail = {
        "profile": profile,
        "itinerary": {
            "confirmation_number": CONFIRMATION,
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": REQUIREMENTS,
    }
    if change_type is not None:
        detail["change_type"] = change_type
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(alert_id: str, status: str = "ACTIVE", itinerary_ref: str = CONFIRMATION,
                skymiles_number: str = SKYMILES) -> None:
    alert_store_mod.table.put_item(Item={
        "skymiles_number": skymiles_number,
        "alert_id": alert_id,
        "alert_type": "VISA",
        "severity": "CRITICAL",
        "reasons": ["old reason"],
        "created_at": "2025-01-01T00:00:00",
        "itinerary_ref": itinerary_ref,
        "status": status,
    })


def _get_alert(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _new_alerts() -> list[dict]:
    return [i for i in alert_store_mod.table.scan()["Items"] if i["status"] == "ACTIVE"]


def _pushed_messages(endpoint_arn: str) -> list[dict]:
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


class TestHappyPath:
    """A clean re-evaluated itinerary returns 200 with no resolutions and no alerts."""

    def test_response_shape(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {
                "change_type": "DATE_CHANGE",
                "resolved_alerts": 0,
                "evaluation_result": {
                    "statusCode": 200,
                    "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
                },
            },
        }

    def test_no_alerts_persisted_or_pushed(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert alert_store_mod.table.scan()["Items"] == []
        assert _pushed_messages(endpoint_arn) == []

    def test_context_is_ignored(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, object())

        assert response["statusCode"] == 200


class TestChangeType:
    """The change_type from the event detail is echoed back, defaulting to UNKNOWN."""

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


class TestStaleAlertResolution:
    """Only ACTIVE/ACKNOWLEDGED alerts for this member + itinerary are resolved."""

    def test_active_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_alert("old-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == RESOLUTION
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-ack", status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert("old-ack")["status"] == "RESOLVED"

    def test_multiple_alerts_counted(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        _seed_alert("old-2", status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2

    @pytest.mark.parametrize("status", ["RESOLVED", "EXPIRED"])
    def test_terminal_status_not_resolved_again(self, endpoint_arn, status):
        _seed_alert("old-1", status=status)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("old-1")["status"] == status

    def test_other_itinerary_untouched(self, endpoint_arn):
        _seed_alert("other-trip", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("other-trip")["status"] == "ACTIVE"

    def test_other_member_untouched(self, endpoint_arn):
        _seed_alert("companion", skymiles_number="5555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("companion", skymiles_number="5555555")["status"] == "ACTIVE"

    def test_old_alert_resolved_and_new_alert_created(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE", segments=[_segment(destination="CN")]
        )

        response = handler(event, None)

        alerts = alert_store_mod.get_alerts_by_itinerary(CONFIRMATION)
        statuses = sorted(a["status"] for a in alerts)
        assert response["body"]["resolved_alerts"] == 1
        assert statuses == ["ACTIVE", "RESOLVED"]
        assert _get_alert("old-1")["status"] == "RESOLVED"


class TestReEvaluationAlerts:
    """Re-evaluation persists and pushes alerts for the changed itinerary."""

    def test_route_change_to_visa_country_raises_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE", segments=[_segment(destination="CN")]
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        alerts = _new_alerts()
        assert len(alerts) == 1
        assert alerts[0]["itinerary_ref"] == CONFIRMATION
        assert "No visa on file for CN" in alerts[0]["reasons"]

    def test_alert_pushed_to_traveler_endpoint(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE", segments=[_segment(destination="CN")]
        )

        handler(event, None)

        messages = _pushed_messages(endpoint_arn)
        assert len(messages) == 1
        assert "No visa on file for CN" in messages[0]["default"]

    def test_route_change_to_visa_free_country(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE",
            segments=[_segment(destination="MX")], nationality="IN",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_segment_removed_clears_issue(self, endpoint_arn):
        _seed_alert("old-cn", status="ACTIVE")
        event = _base_event(endpoint_arn, change_type="SEGMENT_REMOVED", segments=[_segment()])

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0
        assert _new_alerts() == []


class TestLayovers:
    """Layovers only trigger visa alerts where a transit visa is required."""

    def test_added_layover_requiring_transit_visa(self, endpoint_arn):
        segments = [
            _segment(destination="CN", is_layover=True, flight_number="DL1"),
            _segment(destination="DE", flight_number="DL2"),
        ]
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED", segments=segments)

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_added_layover_without_transit_visa_requirement(self, endpoint_arn):
        segments = [
            _segment(destination="JP", is_layover=True, flight_number="DL1"),
            _segment(destination="DE", flight_number="DL2"),
        ]
        event = _base_event(
            endpoint_arn, change_type="SEGMENT_ADDED", segments=segments, nationality="IN",
            visa_records=[_visa("DE")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"


class TestPassportLimits:
    """Changed travel dates are checked against each country's passport validity window."""

    def test_date_change_inside_six_month_requirement(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="JP", departure="2030-03-01",
                                             arrival="2030-03-02")],
            passport_expiry="2030-06-01",
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        passport_alerts = [a for a in _new_alerts() if a["alert_type"] == "PASSPORT"]
        assert "6-month validity requirement" in passport_alerts[0]["reasons"][0]

    def test_schengen_three_month_requirement_met(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="DE", departure="2030-01-01",
                                             arrival="2030-01-02")],
            passport_expiry="2030-05-01",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "INFO"

    def test_schengen_three_month_requirement_not_met(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="DE", departure="2030-03-01",
                                             arrival="2030-03-02")],
            passport_expiry="2030-05-01",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_date_change_after_passport_expiry(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(departure="2031-01-01", arrival="2031-01-02")],
            passport_expiry="2030-12-01",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_expired_passport(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"


class TestVisaLimits:
    """Changed travel dates are checked against the traveler's visa validity."""

    def test_date_change_past_visa_expiry(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="CN", departure="2031-01-01",
                                             arrival="2031-01-02")],
            visa_records=[_visa("CN", expiry="2030-12-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        visa_alerts = [a for a in _new_alerts() if a["alert_type"] == "VISA"]
        assert "Visa for CN expires before date of travel" in visa_alerts[0]["reasons"]

    def test_valid_visa_for_new_destination(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE",
            segments=[_segment(destination="CN")], visa_records=[_visa("CN")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_expired_visa(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="CN")],
            visa_records=[_visa("CN", expiry="2020-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="CN", departure="2030-03-01",
                                             arrival="2030-03-02")],
            passport_expiry="2030-06-01",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 2
        assert len(_pushed_messages(endpoint_arn)) == 2
