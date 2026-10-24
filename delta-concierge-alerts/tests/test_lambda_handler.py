"""Critical-path tests for the Lambda handler end-to-end flow."""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import moto.core
import moto.sns.models
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import _compute_ttl, evaluate_itinerary, handler
from src.models.types import Itinerary


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
    with moto.mock_aws():
        # DynamoDB
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

        # SNS
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        # Save original module-level boto3 clients
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        # Patch module-level boto3 clients to use the moto context
        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid Lambda event with optional field overrides."""
    event = {
        "profile": {
            "skymiles_number": "1234567890",
            "first_name": "Test",
            "last_name": "User",
            "nationality": "IN",
            "passport_number": "P999999",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-TEST",
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }
    for key, val in overrides.items():
        if key in event["profile"]:
            event["profile"][key] = val
    return event


class TestHandlerEndToEnd:
    """Full round-trip: event → evaluations → DynamoDB + SNS → response."""

    def test_alerts_persisted_and_response_shape(self, endpoint_arn):
        """Missing visa for CN should persist alert and return CRITICAL."""
        event = _base_event(endpoint_arn)
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["visa_status"] == "CRITICAL"
        assert body["alerts_sent"] >= 1

        # Verify DynamoDB has the record
        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) >= 1
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]

    def test_no_alerts_for_clean_traveler(self, endpoint_arn):
        """US national to DE with valid passport → no alerts."""
        event = _base_event(endpoint_arn)
        event["profile"]["nationality"] = "US"
        event["itinerary"]["segments"] = [
            {
                "flight_number": "DL200",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-09-15",
                "arrival_date": "2026-09-16",
                "is_layover": False,
            }
        ]
        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["passport_status"] == "OK"
        assert body["visa_status"] == "OK"
        assert body["alerts_sent"] == 0

    def test_expired_passport_triggers_critical(self, endpoint_arn):
        """Expired passport should return CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_expiry"] = "2020-01-01"

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] >= 1

    def test_missing_passport_triggers_critical(self, endpoint_arn):
        """Null passport fields should return CRITICAL passport status."""
        event = _base_event(endpoint_arn)
        event["profile"]["passport_number"] = None
        event["profile"]["passport_expiry"] = None

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] >= 1


# ---------------------------------------------------------------------------
# Helpers for far-future, override-isolated scenarios
# ---------------------------------------------------------------------------

_DEPART = "2030-06-01"
_ARRIVE = "2030-06-02"


def _strict_reqs(**overrides) -> dict:
    """Override entry for a fictional country: visa required, no exemptions."""
    reqs = {
        "country_code": "XA",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    reqs.update(overrides)
    return reqs


def _open_reqs(**overrides) -> dict:
    """Override entry for a fictional country with no visa requirement."""
    return _strict_reqs(requires_visa=False, **overrides)


def _segment(destination: str = "XA", **overrides) -> dict:
    seg = {
        "flight_number": "DL300",
        "origin": "ATL",
        "destination": destination,
        "departure_date": _DEPART,
        "arrival_date": _ARRIVE,
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


def _visa(country_code: str = "XA", expiry: str = "2035-01-01", number: str = "V1") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": number,
    }


def _future_event(
    endpoint_arn: str,
    segments: list[dict] | None = None,
    requirements_override: dict | None = None,
    confirmation_number: str = "DL-FUTURE",
    **profile_overrides,
) -> dict:
    """Valid event with 2030 travel and a fictional 'XA' destination.

    Defaults produce no alerts: passport valid to 2040, valid XA visa on file.
    """
    profile = {
        "skymiles_number": "5555555555",
        "first_name": "Future",
        "last_name": "Traveler",
        "nationality": "IN",
        "passport_number": "P555555",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [_visa()],
    }
    profile.update(profile_overrides)
    return {
        "profile": profile,
        "itinerary": {
            "confirmation_number": confirmation_number,
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": (
            requirements_override
            if requirements_override is not None
            else {"XA": _strict_reqs()}
        ),
    }


def _sns_messages(endpoint_arn: str) -> list[dict]:
    backend = moto.sns.models.sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


def _scan() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """A fully documented traveler gets 200, OK statuses, and nothing persisted or pushed."""

    def test_clean_traveler_no_alerts(self, endpoint_arn):
        event = _future_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
        assert _scan() == []
        assert _sns_messages(endpoint_arn) == []


class TestGroupDelegation:
    """Events containing 'travelers' are routed to the group handler."""

    def test_travelers_key_delegates_to_group_handler(self, endpoint_arn):
        traveler = _future_event(endpoint_arn)["profile"]
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": traveler["skymiles_number"],
            "travelers": [traveler],
            "segments": [_segment()],
            "requirements_override": {"XA": _strict_reqs()},
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["traveler_results"][0]["skymiles_number"] == "5555555555"
        assert "alerts_sent" not in body


class TestRequirementsOverride:
    """requirements_override entries replace country rules; missing keys use defaults."""

    def test_override_waives_visa_requirement(self, endpoint_arn):
        event = _future_event(
            endpoint_arn, visa_records=[], requirements_override={"XA": _open_reqs()}
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_exempts_nationality(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            visa_records=[],
            requirements_override={"XA": _strict_reqs(visa_exempt_nationalities=["IN"])},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_with_empty_entry_uses_strict_defaults(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[], requirements_override={"XA": {}})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = _scan()
        assert items[0]["reasons"] == ["No visa on file for XA"]

    def test_override_default_passport_validity_is_six_months(self, endpoint_arn):
        event = _future_event(
            endpoint_arn, passport_expiry="2030-10-01", requirements_override={"XA": {}}
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        reasons = [i for i in _scan() if i["alert_type"] == "PASSPORT"][0]["reasons"]
        assert reasons == ["Passport does not meet XA's 6-month validity requirement for DL300"]

    def test_override_country_code_defaults_to_key(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            segments=[_segment("XB")],
            visa_records=[],
            requirements_override={"XB": {"requires_visa": True}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["No visa on file for XB"]


class TestPassportValidityLimits:
    """Passport status follows each destination's required validity window."""

    def test_passport_below_country_minimum_is_critical(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            passport_expiry="2030-08-01",
            requirements_override={"XA": _strict_reqs(passport_validity_months=3)},
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_passport_meets_minimum_but_under_six_months_is_info(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            passport_expiry="2030-10-01",
            requirements_override={"XA": _strict_reqs(passport_validity_months=3)},
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"
        assert response["body"]["alerts_sent"] == 1

    def test_passport_expires_before_departure_is_critical(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2030-05-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        reasons = _scan()[0]["reasons"]
        assert reasons == ["Passport expires before departure on DL300 to XA"]

    def test_expired_passport_is_critical(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["Passport has expired"]

    def test_missing_passport_expiry_key_is_critical(self, endpoint_arn):
        event = _future_event(endpoint_arn)
        del event["profile"]["passport_expiry"]

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["Passport information is incomplete"]


class TestVisaLimits:
    """Visa status reflects the best matching visa relative to the travel date."""

    def test_missing_visa_is_critical(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_expired_visa_is_critical(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[_visa(expiry="2020-01-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["Visa for XA has expired"]

    def test_visa_expires_before_travel_is_critical(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[_visa(expiry="2030-05-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["Visa for XA expires before date of travel"]

    def test_visa_expires_on_travel_date_is_warning(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[_visa(expiry=_DEPART)])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"

    def test_visa_expires_soon_after_travel_is_info(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[_visa(expiry="2030-06-10")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "INFO"

    def test_visa_for_other_country_does_not_count(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[_visa(country_code="XB")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestVisaSelection:
    """When several visas exist for a country, the latest-expiring one is used."""

    def test_valid_visa_chosen_over_expired(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            visa_records=[_visa(expiry="2020-01-01", number="OLD"), _visa(number="NEW")],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0


class TestLayovers:
    """Layover segments only need a visa when the country requires a transit visa."""

    def _layover_event(self, endpoint_arn, transit_required: bool) -> dict:
        return _future_event(
            endpoint_arn,
            segments=[
                _segment("XT", flight_number="DL301", is_layover=True),
                _segment("XA", flight_number="DL302"),
            ],
            requirements_override={
                "XT": _strict_reqs(transit_visa_required=transit_required),
                "XA": _strict_reqs(),
            },
        )

    def test_layover_without_transit_requirement_is_ok(self, endpoint_arn):
        event = self._layover_event(endpoint_arn, transit_required=False)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_requirement_is_critical(self, endpoint_arn):
        event = self._layover_event(endpoint_arn, transit_required=True)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["No visa on file for XT"]

    def test_layover_still_checks_passport_validity(self, endpoint_arn):
        event = self._layover_event(endpoint_arn, transit_required=False)
        event["requirements_override"]["XT"]["passport_validity_months"] = 12
        event["profile"]["passport_expiry"] = "2031-01-01"

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        reasons = _scan()[0]["reasons"]
        assert reasons == ["Passport does not meet XT's 12-month validity requirement for DL301"]


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rebooking_to_visa_required_country_creates_alert(self, endpoint_arn):
        reqs = {"XA": _open_reqs(), "XB": _strict_reqs()}
        original = _future_event(endpoint_arn, visa_records=[], requirements_override=reqs)
        changed = _future_event(
            endpoint_arn,
            segments=[_segment("XB")],
            visa_records=[],
            requirements_override=reqs,
        )

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"
        items = _scan()
        assert len(items) == 1
        assert items[0]["itinerary_ref"] == "DL-FUTURE"

    def test_rebooking_to_later_date_flags_expiring_visa(self, endpoint_arn):
        original = _future_event(endpoint_arn, visa_records=[_visa(expiry="2030-12-31")])
        changed = _future_event(
            endpoint_arn,
            segments=[_segment(departure_date="2031-03-01", arrival_date="2031-03-02")],
            visa_records=[_visa(expiry="2030-12-31")],
        )

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"

    def test_added_segment_is_evaluated(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            segments=[_segment("XA"), _segment("XB", flight_number="DL400")],
            requirements_override={"XA": _strict_reqs(), "XB": _strict_reqs()},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _scan()[0]["reasons"] == ["No visa on file for XB"]


class TestPersistenceAndNotifications:
    """Each alerting evaluation persists one record and pushes one notification."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2020-01-01", visa_records=[])

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2,
            "passport_status": "CRITICAL",
            "visa_status": "CRITICAL",
        }
        items = _scan()
        assert sorted(i["alert_type"] for i in items) == ["PASSPORT", "VISA"]
        assert all(i["status"] == "ACTIVE" for i in items)
        assert all(i["skymiles_number"] == "5555555555" for i in items)
        assert len(_sns_messages(endpoint_arn)) == 2

    def test_visa_push_payload(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[])

        handler(event, None)

        (message,) = _sns_messages(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for XA"
        assert gcm["data"]["alert_type"] == "VISA"
        assert gcm["data"]["severity"] == "CRITICAL"
        assert gcm["data"]["confirmation_number"] == "DL-FUTURE"

    def test_passport_push_payload(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2020-01-01")

        handler(event, None)

        (message,) = _sns_messages(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Passport Alert"
        assert gcm["data"]["alert_type"] == "PASSPORT"


class TestTtl:
    """TTL is 30 days after the latest arrival, or today when there are no segments."""

    def test_persisted_ttl_uses_latest_arrival(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            segments=[
                _segment("XA", arrival_date="2030-06-20", departure_date="2030-06-19"),
                _segment("XA", flight_number="DL301"),
            ],
            visa_records=[],
        )
        expected = int(datetime(2030, 7, 20).timestamp())

        handler(event, None)

        assert int(_scan()[0]["ttl"]) == expected

    def test_empty_segments_fall_back_to_today(self):
        itinerary = Itinerary(confirmation_number="DL-EMPTY", segments=[])
        expected = int(
            datetime.combine(date.today() + timedelta(days=30), datetime.min.time()).timestamp()
        )

        ttl = _compute_ttl(itinerary)

        assert ttl == expected


class TestEvaluateItinerary:
    """evaluate_itinerary is a thin wrapper around handler."""

    def test_returns_handler_response(self, endpoint_arn):
        event = _future_event(endpoint_arn, visa_records=[])

        response = evaluate_itinerary(event)

        assert response["statusCode"] == 200
        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1
