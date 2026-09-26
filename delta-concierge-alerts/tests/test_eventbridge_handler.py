"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import uuid
from datetime import datetime

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler

SKYMILES = "1234567890"
CONFIRMATION = "DL-EB-TEST"


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
    destination: str = "CN",
    departure: str = "2028-09-15",
    arrival: str = "2028-09-16",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "JFK",
) -> dict:
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _visa(
    country_code: str = "CN",
    expiry_date: str = "2035-01-01",
    issue_date: str = "2025-01-01",
) -> dict:
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": "V001",
    }


def _base_event(
    endpoint_arn: str,
    change_type: str | None = "DATE_CHANGE",
    segments: list[dict] | None = None,
    **profile_overrides,
) -> dict:
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
    profile.update(profile_overrides)

    detail = {
        "profile": profile,
        "itinerary": {
            "confirmation_number": CONFIRMATION,
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
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
    status: str = "ACTIVE",
    alert_type: str = "VISA",
) -> str:
    """Insert a pre-existing alert into the mocked table and return its alert_id."""
    alert_id = str(uuid.uuid4())
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": alert_type,
            "severity": "CRITICAL",
            "reasons": ["seeded"],
            "created_at": datetime.utcnow().isoformat(),
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )
    return alert_id


def _get_item(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _active_alerts(alert_type: str) -> list[dict]:
    items = alert_store_mod.table.scan()["Items"]
    return [
        i for i in items
        if i["alert_type"] == alert_type and i["status"] == "ACTIVE"
    ]


class TestResponseShape:
    """Handler returns 200 with change_type, resolved count and evaluation result."""

    def test_happy_path_no_prior_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200
        assert body["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_echoed(self, endpoint_arn, change_type):
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_missing_detail_raises(self):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)


class TestStaleAlertResolution:
    """Existing alerts for the changed itinerary are resolved before re-evaluation."""

    def test_active_alert_resolved(self, endpoint_arn):
        alert_id = _seed_alert(status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_item(alert_id)
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == "Auto-resolved: itinerary changed, re-evaluating"
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        alert_id = _seed_alert(status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_item(alert_id)["status"] == "RESOLVED"

    def test_already_resolved_alert_not_counted(self, endpoint_arn):
        _seed_alert(status="RESOLVED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0

    def test_multiple_alerts_all_resolved(self, endpoint_arn):
        ids = [_seed_alert(alert_type="VISA"), _seed_alert(alert_type="PASSPORT")]
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2
        assert all(_get_item(i)["status"] == "RESOLVED" for i in ids)

    def test_other_itinerary_untouched(self, endpoint_arn):
        other_id = _seed_alert(itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item(other_id)["status"] == "ACTIVE"

    def test_other_traveler_untouched(self, endpoint_arn):
        other_id = _seed_alert(skymiles_number="0000000001")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_item(other_id, skymiles_number="0000000001")["status"] == "ACTIVE"

    def test_stale_resolved_then_fresh_alert_created(self, endpoint_arn):
        """Old alert is RESOLVED and a new ACTIVE alert exists after re-evaluation."""
        old_id = _seed_alert(alert_type="VISA")
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert _get_item(old_id)["status"] == "RESOLVED"
        fresh = _active_alerts("VISA")
        assert len(fresh) == 1
        assert fresh[0]["alert_id"] != old_id
        assert fresh[0]["itinerary_ref"] == CONFIRMATION


class TestItineraryChangeReEvaluation:
    """Re-evaluation reflects the *new* itinerary, not the one that raised the old alert."""

    def test_route_change_to_exempt_destination_clears_alerts(self, endpoint_arn):
        """Old CN alert resolved; US national rerouted to DE has no visa issue."""
        _seed_alert(alert_type="VISA")
        event = _base_event(
            endpoint_arn,
            change_type="ROUTE_CHANGE",
            segments=[_segment(destination="DE", origin="ATL")],
            nationality="US",
        )

        response = handler(event, None)

        body = response["body"]
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["body"]["visa_status"] == "OK"
        assert body["evaluation_result"]["body"]["passport_status"] == "OK"
        assert body["evaluation_result"]["body"]["alerts_sent"] == 0
        assert _active_alerts("VISA") == []

    def test_date_change_past_visa_expiry_raises_critical(self, endpoint_arn):
        """Visa valid for original dates, but new departure is after visa expiry."""
        event = _base_event(
            endpoint_arn,
            change_type="DATE_CHANGE",
            segments=[_segment(departure="2028-12-01", arrival="2028-12-02")],
            visa_records=[_visa(expiry_date="2028-10-01")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        reasons = _active_alerts("VISA")[0]["reasons"]
        assert "Visa for CN expires before date of travel" in reasons

    def test_date_change_within_visa_validity_is_ok(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            change_type="DATE_CHANGE",
            segments=[_segment(departure="2028-06-01", arrival="2028-06-02")],
            visa_records=[_visa(expiry_date="2035-01-01")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0

    def test_segment_added_with_layover_requiring_transit_visa(self, endpoint_arn):
        """CN requires a transit visa; layover without visa → CRITICAL."""
        event = _base_event(
            endpoint_arn,
            change_type="SEGMENT_ADDED",
            nationality="US",
            segments=[
                _segment(destination="CN", is_layover=True, flight_number="DL100"),
                _segment(
                    destination="JP", origin="PEK", flight_number="DL101",
                    departure="2028-09-16", arrival="2028-09-17",
                ),
            ],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert "No visa on file for CN" in _active_alerts("VISA")[0]["reasons"]

    def test_segment_added_with_layover_not_requiring_transit_visa(self, endpoint_arn):
        """DE does not require a transit visa; layover for IN national is skipped."""
        event = _base_event(
            endpoint_arn,
            change_type="SEGMENT_ADDED",
            nationality="IN",
            segments=[
                _segment(destination="DE", origin="ATL", is_layover=True),
                _segment(
                    destination="US", origin="FRA", flight_number="DL101",
                    departure="2028-09-16", arrival="2028-09-17",
                ),
            ],
            visa_records=[_visa(country_code="US")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0

    def test_segment_removed_to_no_segments(self, endpoint_arn):
        """Itinerary with all segments removed evaluates clean and resolves old alerts."""
        _seed_alert()
        event = _base_event(endpoint_arn, change_type="SEGMENT_REMOVED", segments=[])

        response = handler(event, None)

        body = response["body"]
        assert body["resolved_alerts"] == 1
        assert body["evaluation_result"]["body"]["alerts_sent"] == 0


class TestPassportRequirementsOnReEvaluation:
    """Passport validity is checked against the destination's minimum on re-evaluation."""

    def test_expired_passport_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        assert len(_active_alerts("PASSPORT")) == 1

    def test_missing_passport_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_number=None, passport_expiry=None)

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_passport_below_six_month_minimum_for_cn(self, endpoint_arn):
        """CN requires 6 months validity; passport with ~2 months left → CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", departure="2028-09-15", arrival="2028-09-16")],
            passport_expiry="2028-11-15",
            visa_records=[_visa(country_code="CN")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_passport_meets_schengen_three_month_minimum(self, endpoint_arn):
        """DE requires 3 months; ~4 months remaining passes but gets 6-month advisory."""
        event = _base_event(
            endpoint_arn,
            nationality="US",
            segments=[_segment(destination="DE", origin="ATL", departure="2028-09-15", arrival="2028-09-16")],
            passport_expiry="2029-01-20",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "INFO"

    def test_passport_well_within_validity_is_ok(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="US",
            segments=[_segment(destination="DE", origin="ATL")],
            passport_expiry="2035-01-01",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "OK"
