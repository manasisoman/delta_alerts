"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import copy
from datetime import datetime, timedelta

import boto3
import moto
import pytest
from boto3.dynamodb.conditions import Key

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler

SKYMILES = "1234567890"
CONFIRMATION = "DL-CHG01"
AUTO_RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

REQUIREMENTS_OVERRIDE = {
    "DE": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB", "CA"],
    },
    "CN": {
        "requires_visa": True,
        "transit_visa_required": True,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    },
    "JP": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": ["US", "GB"],
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
    destination: str = "DE",
    departure_date: str = "2031-03-01",
    arrival_date: str | None = None,
    flight_number: str = "DL100",
    origin: str = "ATL",
    is_layover: bool = False,
) -> dict:
    """Build a serialized flight segment with far-future defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date or departure_date,
        "is_layover": is_layover,
    }


def _visa(
    country_code: str,
    expiry_date: str = "2035-01-01",
    issue_date: str = "2029-01-01",
) -> dict:
    """Build a serialized visa record."""
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": f"V-{country_code}",
    }


def _base_event(
    endpoint_arn: str,
    segments: list[dict] | None = None,
    change_type: str | None = "DATE_CHANGE",
    **profile_overrides,
) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope.

    Defaults to a US national with a passport valid until 2035 flying to DE
    (visa-exempt), which produces no alerts.
    """
    profile = {
        "skymiles_number": SKYMILES,
        "first_name": "Test",
        "last_name": "User",
        "nationality": "US",
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
        "requirements_override": copy.deepcopy(REQUIREMENTS_OVERRIDE),
    }
    if change_type is not None:
        detail["change_type"] = change_type
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    alert_id: str,
    status: str = "ACTIVE",
    skymiles_number: str = SKYMILES,
    itinerary_ref: str = CONFIRMATION,
) -> None:
    """Insert a pre-existing alert directly into the moto table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "reasons": ["old reason"],
            "created_at": "2030-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


def _get_alert(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _new_alerts(exclude_ids: set[str] = frozenset()) -> list[dict]:
    items = alert_store_mod.table.query(
        KeyConditionExpression=Key("skymiles_number").eq(SKYMILES)
    )["Items"]
    return [i for i in items if i["alert_id"] not in exclude_ids]


class TestResponseShape:
    """Handler always returns 200 with change_type, resolved count and evaluation result."""

    def test_happy_path_clean_itinerary(self, endpoint_arn):
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
        assert _new_alerts() == []

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
        assert response["statusCode"] == 200

    def test_context_is_ignored(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, object())

        assert response["statusCode"] == 200


class TestMalformedEvent:
    """Required envelope fields missing raise KeyError before any DB writes."""

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

    def test_missing_confirmation_number_raises_and_leaves_alerts(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(endpoint_arn)
        del event["detail"]["itinerary"]["confirmation_number"]

        with pytest.raises(KeyError):
            handler(event, None)

        assert _get_alert("old-1")["status"] == "ACTIVE"


class TestStaleAlertResolution:
    """Actionable alerts on the changed itinerary are resolved before re-evaluation."""

    def test_active_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_alert("old-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == AUTO_RESOLUTION
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-1", status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert("old-1")["status"] == "RESOLVED"

    def test_multiple_alerts_counted(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        _seed_alert("old-2", status="ACKNOWLEDGED")
        _seed_alert("old-3", status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 3

    @pytest.mark.parametrize("status", ["RESOLVED", "EXPIRED"])
    def test_non_actionable_alert_untouched(self, endpoint_arn, status):
        _seed_alert("old-1", status=status)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = _get_alert("old-1")
        assert item["status"] == status
        assert "resolution" not in item

    def test_other_itinerary_alert_untouched(self, endpoint_arn):
        _seed_alert("other-1", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("other-1")["status"] == "ACTIVE"

    def test_other_member_alert_untouched(self, endpoint_arn):
        _seed_alert("other-1", skymiles_number="5555555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert("other-1", skymiles_number="5555555555")["status"] == "ACTIVE"

    def test_new_alerts_created_after_resolution_stay_active(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="CN")]
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert("old-1")["status"] == "RESOLVED"
        new = _new_alerts(exclude_ids={"old-1"})
        assert len(new) == 1
        assert new[0]["status"] == "ACTIVE"
        assert new[0]["alert_type"] == "VISA"
        assert new[0]["itinerary_ref"] == CONFIRMATION


class TestItineraryChangeReevaluation:
    """Re-evaluation reflects the updated itinerary, not the original one."""

    def test_route_change_to_exempt_country_clears_alerts(self, endpoint_arn):
        original = _base_event(
            endpoint_arn, segments=[_segment(destination="CN")], change_type=None
        )
        first = handler(original, None)
        assert first["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

        changed = _base_event(
            endpoint_arn, segments=[_segment(destination="DE")], change_type="ROUTE_CHANGE"
        )
        response = handler(changed, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0
        assert all(a["status"] == "RESOLVED" for a in _new_alerts())

    def test_segment_added_requiring_visa_raises_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="DE", departure_date="2031-03-01"),
                _segment(destination="CN", departure_date="2031-03-10", flight_number="DL200"),
            ],
            change_type="SEGMENT_ADDED",
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "OK"
        assert result["alerts_sent"] == 1
        assert "No visa on file for CN" in _new_alerts()[0]["reasons"]

    def test_segment_removed_drops_visa_alert(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="DE")], change_type="SEGMENT_REMOVED"
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_date_change_past_visa_expiry_triggers_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", departure_date="2033-06-01")],
            visa_records=[_visa("CN", expiry_date="2033-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        assert (
            "Visa for CN expires before date of travel" in _new_alerts()[0]["reasons"]
        )

    def test_date_change_onto_visa_expiry_triggers_warning(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", departure_date="2033-01-01")],
            visa_records=[_visa("CN", expiry_date="2033-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "WARNING"

    def test_date_change_past_passport_expiry_triggers_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE", departure_date="2036-01-01")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        alerts = _new_alerts()
        assert alerts[0]["alert_type"] == "PASSPORT"
        assert "Passport expires before departure on DL100 to DE" in alerts[0]["reasons"]

    def test_new_alert_ttl_tracks_changed_arrival(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", departure_date="2031-03-01",
                               arrival_date="2031-03-02")],
        )

        handler(event, None)

        expected = int((datetime(2031, 3, 2) + timedelta(days=30)).timestamp())
        assert int(_new_alerts()[0]["ttl"]) == expected


class TestLayovers:
    """Layovers only require a visa where the country mandates a transit visa."""

    def test_layover_without_transit_visa_requirement_no_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="JP", is_layover=True)],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", is_layover=True)],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_added_layover_still_checks_passport_validity(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-05-01",
            segments=[
                _segment(destination="JP", departure_date="2031-03-01", is_layover=True),
            ],
            change_type="SEGMENT_ADDED",
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"


class TestPassportValidityLimits:
    """Passport validity is checked against each destination's required months."""

    def test_meets_schengen_three_month_rule_info_only(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-07-01",
            segments=[_segment(destination="DE", departure_date="2031-03-01")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "INFO"
        assert result["alerts_sent"] == 1

    def test_below_schengen_three_month_rule_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-05-01",
            segments=[_segment(destination="DE", departure_date="2031-03-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"
        assert (
            "Passport does not meet DE's 3-month validity requirement for DL100"
            in _new_alerts()[0]["reasons"]
        )

    def test_below_six_month_rule_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-07-01",
            nationality="US",
            segments=[_segment(destination="JP", departure_date="2031-03-01")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"

    def test_expired_passport_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_missing_passport_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_number=None, passport_expiry=None)

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"


class TestVisaValidity:
    """Visa checks use the best matching visa and country exemption lists."""

    def test_valid_visa_no_alert(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            visa_records=[_visa("CN")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0

    def test_non_exempt_nationality_needs_visa(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="IN")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_latest_visa_selected_over_expired(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            visa_records=[_visa("CN", expiry_date="2021-01-01", issue_date="2019-01-01"),
                          _visa("CN", expiry_date="2035-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_expired_visa_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            visa_records=[_visa("CN", expiry_date="2021-01-01", issue_date="2019-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        assert "Visa for CN has expired" in _new_alerts()[0]["reasons"]

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-05-01",
            segments=[_segment(destination="CN", departure_date="2031-03-01")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["alerts_sent"] == 2
        assert {a["alert_type"] for a in _new_alerts()} == {"PASSPORT", "VISA"}
