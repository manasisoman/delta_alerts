"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import boto3
import moto
import moto.core
import moto.sns.models
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler

SKYMILES = "1234567890"
CONFIRMATION = "DL-CHG01"
RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

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
    departure: str = "2030-03-01",
    arrival: str = "2030-03-02",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> dict:
    """Build a raw itinerary segment dict."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope with optional detail overrides.

    Default: US citizen, passport valid through 2035, single DE segment in 2030
    (visa-exempt, passport well within the 3-month rule) -> no alerts.
    """
    profile = {
        "skymiles_number": SKYMILES,
        "first_name": "Test",
        "last_name": "User",
        "nationality": overrides.pop("nationality", "US"),
        "passport_number": "P999999",
        "passport_expiry": overrides.pop("passport_expiry", "2035-01-01"),
        "endpoint_arn": endpoint_arn,
        "visa_records": overrides.pop("visa_records", []),
    }
    detail = {
        "profile": profile,
        "itinerary": {
            "confirmation_number": overrides.pop("confirmation_number", CONFIRMATION),
            "segments": overrides.pop("segments", [_segment()]),
        },
        "change_type": "DATE_CHANGE",
        "requirements_override": REQUIREMENTS_OVERRIDE,
    }
    detail.update(overrides)
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    alert_id: str,
    status: str = "ACTIVE",
    itinerary_ref: str = CONFIRMATION,
    skymiles_number: str = SKYMILES,
) -> None:
    """Insert a pre-existing alert directly into the moto table."""
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "reasons": ["old reason"],
            "created_at": "2029-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )


def _get_alert(alert_id: str, skymiles_number: str = SKYMILES) -> dict:
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _all_alerts(skymiles_number: str = SKYMILES) -> list[dict]:
    return alert_store_mod.get_alerts_by_member(skymiles_number)


def _new_active_alerts(seeded_ids: set[str] = frozenset()) -> list[dict]:
    return [
        a for a in _all_alerts()
        if a["alert_id"] not in seeded_ids and a["status"] == "ACTIVE"
    ]


def _sns_messages(endpoint_arn: str) -> list[str]:
    backend = moto.sns.models.sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return list(backend.platform_endpoints[endpoint_arn].messages.values())


class TestHappyPath:
    """A change with no stale alerts and compliant documents returns 200 with nothing sent."""

    def test_returns_200_with_change_type_and_zero_counts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["statusCode"] == 200
        body = result["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 0
        assert body["evaluation_result"]["statusCode"] == 200
        assert body["evaluation_result"]["body"] == {
            "alerts_sent": 0,
            "passport_status": "OK",
            "visa_status": "OK",
        }

    def test_no_alerts_persisted_or_pushed(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert _all_alerts() == []
        assert _sns_messages(endpoint_arn) == []


class TestChangeType:
    """change_type is echoed back verbatim and defaults to UNKNOWN when absent."""

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
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


class TestStaleAlertResolution:
    """Only ACTIVE/ACKNOWLEDGED alerts for this member + itinerary are resolved before re-evaluation."""

    def test_active_alert_resolved_with_resolution_text(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 1
        item = _get_alert("old-1")
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == RESOLUTION
        assert "resolved_at" in item

    def test_acknowledged_alert_resolved(self, endpoint_arn):
        _seed_alert("old-ack", status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 1
        assert _get_alert("old-ack")["status"] == "RESOLVED"

    def test_multiple_stale_alerts_counted(self, endpoint_arn):
        _seed_alert("old-1", status="ACTIVE")
        _seed_alert("old-2", status="ACKNOWLEDGED")
        _seed_alert("old-3", status="ACTIVE")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 3

    def test_already_resolved_alert_not_recounted(self, endpoint_arn):
        _seed_alert("done", status="RESOLVED")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 0
        assert "resolution" not in _get_alert("done")

    def test_expired_alert_left_untouched(self, endpoint_arn):
        _seed_alert("exp", status="EXPIRED")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 0
        assert _get_alert("exp")["status"] == "EXPIRED"

    def test_alert_for_other_itinerary_left_active(self, endpoint_arn):
        _seed_alert("other-trip", itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 0
        assert _get_alert("other-trip")["status"] == "ACTIVE"

    def test_alert_for_other_member_left_active(self, endpoint_arn):
        _seed_alert("someone-else", skymiles_number="5555555555")
        event = _base_event(endpoint_arn)

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 0
        assert _get_alert("someone-else", "5555555555")["status"] == "ACTIVE"


class TestReEvaluationPassport:
    """After resolving stale alerts, the updated itinerary is re-evaluated for passport validity."""

    def test_passport_below_country_minimum_raises_new_alert(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(endpoint_arn, passport_expiry="2030-04-15")

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 1
        eval_body = result["body"]["evaluation_result"]["body"]
        assert eval_body["passport_status"] == "CRITICAL"
        assert eval_body["alerts_sent"] == 1
        new = _new_active_alerts({"old-1"})
        assert len(new) == 1
        assert new[0]["alert_type"] == "PASSPORT"
        assert new[0]["itinerary_ref"] == CONFIRMATION
        assert "DE's 3-month validity requirement" in new[0]["reasons"][0]

    def test_passport_meets_schengen_3_month_minimum_only_info(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-07-01")

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["passport_status"] == "INFO"

    def test_passport_expired_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        result = handler(event, None)

        eval_body = result["body"]["evaluation_result"]["body"]
        assert eval_body["passport_status"] == "CRITICAL"
        assert _new_active_alerts()[0]["reasons"] == ["Passport has expired"]

    def test_new_alert_pushed_to_endpoint(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-04-15")

        handler(event, None)

        messages = _sns_messages(endpoint_arn)
        assert len(messages) == 1
        assert "Passport" in messages[0]


class TestReEvaluationVisa:
    """Visa requirements are re-checked against the changed itinerary, including layover rules."""

    def test_missing_visa_for_new_destination_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, nationality="IN")

        result = handler(event, None)

        eval_body = result["body"]["evaluation_result"]["body"]
        assert eval_body["visa_status"] == "CRITICAL"
        new = _new_active_alerts()
        assert [a["alert_type"] for a in new] == ["VISA"]
        assert new[0]["reasons"] == ["No visa on file for DE"]

    def test_valid_visa_no_alert(self, endpoint_arn):
        visa = {
            "country_code": "DE",
            "visa_type": "TOURIST",
            "issue_date": "2029-01-01",
            "expiry_date": "2031-01-01",
            "visa_number": "V1",
        }
        event = _base_event(endpoint_arn, nationality="IN", visa_records=[visa])

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_visa_expires_before_new_departure_date(self, endpoint_arn):
        visa = {
            "country_code": "DE",
            "visa_type": "TOURIST",
            "issue_date": "2029-01-01",
            "expiry_date": "2030-02-01",
            "visa_number": "V1",
        }
        event = _base_event(endpoint_arn, nationality="IN", visa_records=[visa])

        handler(event, None)

        assert _new_active_alerts()[0]["reasons"] == [
            "Visa for DE expires before date of travel"
        ]

    def test_layover_without_transit_requirement_skipped(self, endpoint_arn):
        segments = [_segment("DE", is_layover=True), _segment("GB", flight_number="DL200")]
        overrides = dict(REQUIREMENTS_OVERRIDE)
        overrides["GB"] = {"requires_visa": False, "passport_validity_months": 0}
        event = _base_event(
            endpoint_arn, nationality="IN", segments=segments,
            requirements_override=overrides,
        )

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_layover_with_transit_requirement_alerts(self, endpoint_arn):
        segments = [
            _segment("CN", is_layover=True),
            _segment("JP", flight_number="DL200"),
        ]
        event = _base_event(endpoint_arn, nationality="US", segments=segments)

        result = handler(event, None)

        eval_body = result["body"]["evaluation_result"]["body"]
        assert eval_body["visa_status"] == "CRITICAL"
        assert _new_active_alerts()[0]["reasons"] == ["No visa on file for CN"]


class TestItineraryChangeScenarios:
    """End-to-end ItineraryChanged flows: old alerts resolved, new state reflects the updated trip."""

    def test_segment_removed_clears_problem(self, endpoint_arn):
        _seed_alert("cn-visa")
        event = _base_event(
            endpoint_arn, change_type="SEGMENT_REMOVED",
            segments=[_segment("JP")],
        )

        result = handler(event, None)

        assert result["body"]["resolved_alerts"] == 1
        assert result["body"]["evaluation_result"]["body"]["alerts_sent"] == 0
        assert _new_active_alerts({"cn-visa"}) == []

    def test_segment_added_introduces_new_alert(self, endpoint_arn):
        segments = [_segment("JP"), _segment("CN", flight_number="DL300")]
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED", segments=segments)

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_date_change_pushes_passport_below_minimum(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2030-12-01",
            segments=[_segment("JP", departure="2030-08-01", arrival="2030-08-02")],
        )

        result = handler(event, None)

        assert result["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_route_change_passport_and_visa_alerts(self, endpoint_arn):
        _seed_alert("old-1")
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE", nationality="IN",
            passport_expiry="2030-05-01", segments=[_segment("CN")],
        )

        result = handler(event, None)

        eval_body = result["body"]["evaluation_result"]["body"]
        assert eval_body["alerts_sent"] == 2
        assert sorted(a["alert_type"] for a in _new_active_alerts({"old-1"})) == [
            "PASSPORT", "VISA",
        ]
        assert len(_sns_messages(endpoint_arn)) == 2

    def test_reprocessing_same_change_resolves_previous_run_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-04-15")
        handler(event, None)

        result = handler(_base_event(endpoint_arn, passport_expiry="2030-04-15"), None)

        assert result["body"]["resolved_alerts"] == 1
        assert len(_new_active_alerts()) == 1


class TestMalformedEvent:
    """Missing required envelope fields raise KeyError before any state change."""

    @pytest.mark.parametrize("path", [("detail",), ("detail", "profile"), ("detail", "itinerary")])
    def test_missing_required_key_raises(self, endpoint_arn, path):
        event = _base_event(endpoint_arn)
        container = event
        for key in path[:-1]:
            container = container[key]
        del container[path[-1]]

        with pytest.raises(KeyError):
            handler(event, None)
