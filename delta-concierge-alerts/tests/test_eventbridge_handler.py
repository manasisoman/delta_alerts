"""Critical-path tests for the EventBridge ItineraryChanged handler."""

import uuid

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.eventbridge_handler import handler

_SKYMILES = "1234567890"
_CONFIRMATION = "DL-CHANGE"
_RESOLUTION = "Auto-resolved: itinerary changed, re-evaluating"

# Explicit per-country rules so tests don't depend on production static data.
_REQUIREMENTS_OVERRIDE = {
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
    "MX": {
        "requires_visa": False,
        "transit_visa_required": False,
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
    destination: str = "DE",
    departure_date: str = "2030-06-01",
    arrival_date: str = "2030-06-02",
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> dict:
    """Build a serialized flight segment with sensible defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _visa(
    country_code: str = "CN",
    expiry_date: str = "2035-01-01",
    issue_date: str = "2025-01-01",
) -> dict:
    """Build a serialized visa record with sensible defaults."""
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": f"V-{country_code}",
    }


def _base_event(
    endpoint_arn: str,
    change_type: str | None = "DATE_CHANGE",
    segments: list[dict] | None = None,
    **profile_overrides,
) -> dict:
    """Build a valid EventBridge ItineraryChanged envelope.

    Defaults describe a clean US traveler flying to DE with a far-future
    passport, so each test only varies the dimension it cares about.
    """
    profile = {
        "skymiles_number": _SKYMILES,
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
            "confirmation_number": _CONFIRMATION,
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": _REQUIREMENTS_OVERRIDE,
    }
    if change_type is not None:
        detail["change_type"] = change_type

    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


def _seed_alert(
    status: str = "ACTIVE",
    skymiles_number: str = _SKYMILES,
    itinerary_ref: str = _CONFIRMATION,
    alert_type: str = "VISA",
) -> str:
    """Insert a pre-existing alert directly into DynamoDB and return its id."""
    alert_id = str(uuid.uuid4())
    alert_store_mod.table.put_item(
        Item={
            "skymiles_number": skymiles_number,
            "alert_id": alert_id,
            "alert_type": alert_type,
            "severity": "CRITICAL",
            "reasons": ["Seeded alert"],
            "created_at": "2026-01-01T00:00:00",
            "itinerary_ref": itinerary_ref,
            "status": status,
        }
    )
    return alert_id


def _get_alert(alert_id: str, skymiles_number: str = _SKYMILES) -> dict:
    """Fetch a single alert item by key."""
    return alert_store_mod.table.get_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id}
    )["Item"]


def _all_alerts() -> list[dict]:
    """Return every alert currently in the table."""
    return alert_store_mod.table.scan()["Items"]


class TestResponseEnvelope:
    """The handler always returns 200 with change_type, resolved count, and evaluation."""

    def test_happy_path_clean_itinerary(self, endpoint_arn):
        """Clean traveler with no prior alerts → nothing resolved, no new alerts."""
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["change_type"] == "DATE_CHANGE"
        assert response["body"]["resolved_alerts"] == 0
        assert response["body"]["evaluation_result"] == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
        assert _all_alerts() == []

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_is_echoed(self, endpoint_arn, change_type):
        """Each documented change_type is passed through to the response body."""
        event = _base_event(endpoint_arn, change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type

    def test_missing_change_type_defaults_to_unknown(self, endpoint_arn):
        """A detail without change_type is reported as UNKNOWN but still processed."""
        event = _base_event(endpoint_arn, change_type=None)

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["change_type"] == "UNKNOWN"
        assert response["body"]["evaluation_result"]["statusCode"] == 200

    def test_missing_detail_raises(self, endpoint_arn):
        """An envelope without a detail payload is rejected with KeyError."""
        event = _base_event(endpoint_arn)
        del event["detail"]

        with pytest.raises(KeyError):
            handler(event, None)


class TestStaleAlertResolution:
    """Only ACTIVE/ACKNOWLEDGED alerts for this traveler + itinerary are resolved."""

    def test_active_alert_is_resolved(self, endpoint_arn):
        """An ACTIVE alert on the changed itinerary is marked RESOLVED."""
        alert_id = _seed_alert(status="ACTIVE")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        item = _get_alert(alert_id)
        assert item["status"] == "RESOLVED"
        assert item["resolution"] == _RESOLUTION
        assert "resolved_at" in item

    def test_acknowledged_alert_is_resolved(self, endpoint_arn):
        """An ACKNOWLEDGED alert on the changed itinerary is also resolved."""
        alert_id = _seed_alert(status="ACKNOWLEDGED")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert(alert_id)["status"] == "RESOLVED"

    @pytest.mark.parametrize("status", ["RESOLVED", "EXPIRED"])
    def test_terminal_status_alert_is_not_counted(self, endpoint_arn, status):
        """Already RESOLVED/EXPIRED alerts are left untouched and not counted."""
        alert_id = _seed_alert(status=status)
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        item = _get_alert(alert_id)
        assert item["status"] == status
        assert "resolution" not in item

    def test_alert_for_other_itinerary_is_untouched(self, endpoint_arn):
        """An ACTIVE alert for a different booking stays ACTIVE."""
        alert_id = _seed_alert(itinerary_ref="DL-OTHER")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert(alert_id)["status"] == "ACTIVE"

    def test_alert_for_other_traveler_is_untouched(self, endpoint_arn):
        """An ACTIVE alert for another SkyMiles member on the same booking stays ACTIVE."""
        alert_id = _seed_alert(skymiles_number="5555555555")
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0
        assert _get_alert(alert_id, skymiles_number="5555555555")["status"] == "ACTIVE"

    def test_multiple_stale_alerts_are_all_counted(self, endpoint_arn):
        """Every matching stale alert (passport + visa) is resolved and counted."""
        ids = [_seed_alert(alert_type="PASSPORT"), _seed_alert(alert_type="VISA")]
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 2
        assert all(_get_alert(i)["status"] == "RESOLVED" for i in ids)


class TestResolveBeforeReEvaluate:
    """Stale alerts are resolved first, so new alerts from re-evaluation stay ACTIVE."""

    def test_new_alert_survives_and_old_alert_resolved(self, endpoint_arn):
        """Route changed to CN without a visa → old alert resolved, new CRITICAL alert ACTIVE."""
        old_id = _seed_alert(status="ACTIVE")
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE", segments=[_segment(destination="CN")]
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert(old_id)["status"] == "RESOLVED"
        active = [a for a in _all_alerts() if a["status"] == "ACTIVE"]
        assert len(active) == 1
        assert active[0]["alert_type"] == "VISA"
        assert active[0]["itinerary_ref"] == _CONFIRMATION
        assert "No visa on file for CN" in active[0]["reasons"]

    def test_repeated_event_resolves_previous_run_alerts(self, endpoint_arn):
        """A second change event resolves the alert produced by the first."""
        event = _base_event(
            endpoint_arn, change_type="ROUTE_CHANGE", segments=[_segment(destination="CN")]
        )
        handler(event, None)

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        statuses = sorted(a["status"] for a in _all_alerts())
        assert statuses == ["ACTIVE", "RESOLVED"]


class TestItineraryChanges:
    """Re-evaluation reflects the updated itinerary, not the original booking."""

    def test_segment_removed_clears_visa_issue(self, endpoint_arn):
        """CN leg dropped → prior CN visa alert resolved and visa status OK."""
        old_id = _seed_alert(alert_type="VISA")
        event = _base_event(
            endpoint_arn, change_type="SEGMENT_REMOVED", segments=[_segment(destination="DE")]
        )

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 1
        assert _get_alert(old_id)["status"] == "RESOLVED"
        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"
        assert response["body"]["evaluation_result"]["body"]["alerts_sent"] == 0

    def test_segment_added_requiring_visa(self, endpoint_arn):
        """Adding a CN leg to a DE trip without a CN visa → visa CRITICAL."""
        segments = [
            _segment(destination="DE"),
            _segment(
                destination="CN",
                departure_date="2030-06-10",
                arrival_date="2030-06-11",
                flight_number="DL200",
                origin="FRA",
            ),
        ]
        event = _base_event(endpoint_arn, change_type="SEGMENT_ADDED", segments=segments)

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "OK"
        assert result["alerts_sent"] == 1

    def test_date_change_past_passport_expiry(self, endpoint_arn):
        """Departure moved after passport expiry → passport CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(departure_date="2041-03-01", arrival_date="2041-03-02")],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        passport_alerts = [a for a in _all_alerts() if a["alert_type"] == "PASSPORT"]
        assert passport_alerts[0]["reasons"] == [
            "Passport expires before departure on DL100 to DE"
        ]

    def test_date_change_past_visa_expiry(self, endpoint_arn):
        """CN departure moved after visa expiry → visa CRITICAL 'expires before date of travel'."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", departure_date="2036-01-01", arrival_date="2036-01-02")],
            visa_records=[_visa(expiry_date="2035-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"
        visa_alerts = [a for a in _all_alerts() if a["alert_type"] == "VISA"]
        assert visa_alerts[0]["reasons"] == ["Visa for CN expires before date of travel"]

    def test_route_change_to_visa_free_country(self, endpoint_arn):
        """Rerouted to a country with no visa requirement → visa OK even without a visa."""
        event = _base_event(
            endpoint_arn,
            change_type="ROUTE_CHANGE",
            segments=[_segment(destination="MX")],
            nationality="IN",
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"


class TestLayoverTransit:
    """Layovers only require a visa when the country mandates a transit visa."""

    def test_layover_without_transit_requirement_skipped(self, endpoint_arn):
        """Non-exempt traveler laying over in DE (no transit visa) → visa OK."""
        segments = [
            _segment(destination="DE", is_layover=True),
            _segment(destination="MX", flight_number="DL200", origin="FRA"),
        ]
        event = _base_event(endpoint_arn, segments=segments, nationality="IN")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_layover_with_transit_requirement_alerts(self, endpoint_arn):
        """Laying over in CN (transit visa required) without a visa → visa CRITICAL."""
        segments = [
            _segment(destination="CN", is_layover=True),
            _segment(destination="MX", flight_number="DL200", origin="PEK"),
        ]
        event = _base_event(endpoint_arn, segments=segments)

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_layover_still_checks_passport_validity(self, endpoint_arn):
        """Passport below CN's 6-month minimum at a CN layover → passport CRITICAL."""
        segments = [
            _segment(destination="CN", is_layover=True),
            _segment(destination="MX", flight_number="DL200", origin="PEK"),
        ]
        event = _base_event(
            endpoint_arn,
            segments=segments,
            passport_expiry="2030-09-01",
            visa_records=[_visa()],
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"


class TestPassportValidityWindow:
    """Passport validity is measured against each destination's minimum months."""

    def test_below_country_minimum_is_critical(self, endpoint_arn):
        """2 months remaining for DE (3-month rule) → passport CRITICAL."""
        event = _base_event(endpoint_arn, passport_expiry="2030-08-01")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"
        passport_alerts = [a for a in _all_alerts() if a["alert_type"] == "PASSPORT"]
        assert passport_alerts[0]["reasons"] == [
            "Passport does not meet DE's 3-month validity requirement for DL100"
        ]

    def test_meets_minimum_but_under_six_months_is_info(self, endpoint_arn):
        """4 months remaining for DE (3-month rule) → advisory INFO only."""
        event = _base_event(endpoint_arn, passport_expiry="2030-10-01")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "INFO"

    def test_same_passport_fails_six_month_country(self, endpoint_arn):
        """4 months remaining for CN (6-month rule) → passport CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            passport_expiry="2030-10-01",
            visa_records=[_visa()],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_already_expired_passport_is_critical(self, endpoint_arn):
        """Passport expired in the past → passport CRITICAL."""
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"

    def test_missing_passport_is_critical(self, endpoint_arn):
        """No passport on file → passport CRITICAL."""
        event = _base_event(endpoint_arn, passport_number=None, passport_expiry=None)

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["passport_status"] == "CRITICAL"


class TestVisaLimits:
    """Visa status after a change depends on exemption and visa expiry vs. travel."""

    def test_valid_visa_covers_trip(self, endpoint_arn):
        """CN visa valid well past travel → visa OK, no alerts."""
        event = _base_event(
            endpoint_arn, segments=[_segment(destination="CN")], visa_records=[_visa()]
        )

        response = handler(event, None)

        result = response["body"]["evaluation_result"]["body"]
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0

    def test_exempt_nationality_needs_no_visa(self, endpoint_arn):
        """US national to DE (exempt) → visa OK."""
        event = _base_event(endpoint_arn, nationality="US")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"

    def test_non_exempt_nationality_needs_visa(self, endpoint_arn):
        """IN national to DE (not exempt) without a visa → visa CRITICAL."""
        event = _base_event(endpoint_arn, nationality="IN")

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "CRITICAL"

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        """CN visa expires on the departure date → visa WARNING."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            visa_records=[_visa(expiry_date="2030-06-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "WARNING"

    def test_visa_expiring_within_30_days_is_info(self, endpoint_arn):
        """CN visa expires 2 weeks after departure → visa INFO."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            visa_records=[_visa(expiry_date="2030-06-15")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "INFO"

    def test_latest_visa_record_wins(self, endpoint_arn):
        """Expired and valid CN visas on file → the valid one is used, visa OK."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            visa_records=[_visa(expiry_date="2020-01-01"), _visa(expiry_date="2035-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["evaluation_result"]["body"]["visa_status"] == "OK"
