"""Critical-path tests for the Lambda handler end-to-end flow."""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import (
    _compute_ttl,
    _parse_itinerary,
    _parse_profile,
    evaluate_itinerary,
    handler,
)
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


# ------------------------------------------------------------------
# Helpers for far-future, fully compliant events
# ------------------------------------------------------------------

_DEPARTURE = "2030-06-15"
_ARRIVAL = "2030-06-16"


def _segment(
    destination: str = "DE",
    departure_date: str = _DEPARTURE,
    arrival_date: str = _ARRIVAL,
    is_layover: bool = False,
    flight_number: str = "DL300",
    origin: str = "ATL",
) -> dict:
    """Build a raw flight segment dict as it appears in the event payload."""
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
    expiry_date: str = "2040-01-01",
    issue_date: str = "2025-01-01",
    visa_type: str = "TOURIST",
    visa_number: str = "V123",
) -> dict:
    """Build a raw visa record dict as it appears in the event payload."""
    return {
        "country_code": country_code,
        "visa_type": visa_type,
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": visa_number,
    }


def _future_event(
    endpoint_arn: str,
    segments: list[dict] | None = None,
    requirements_override: dict | None = None,
    confirmation_number: str = "DL-FUTURE",
    **profile_overrides,
) -> dict:
    """Build a clean event (US national to DE, far-future dates) with overrides."""
    profile = {
        "skymiles_number": "5555555555",
        "first_name": "Clean",
        "last_name": "Traveler",
        "nationality": "US",
        "passport_number": "P555555",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    profile.update(profile_overrides)
    return {
        "profile": profile,
        "itinerary": {
            "confirmation_number": confirmation_number,
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": requirements_override,
    }


def _alerts(alert_type: str | None = None) -> list[dict]:
    """Return persisted alerts, optionally filtered by alert_type."""
    items = alert_store_mod.table.scan()["Items"]
    if alert_type is None:
        return items
    return [i for i in items if i["alert_type"] == alert_type]


def _pushes(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages that moto delivered to the endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _expected_ttl(arrival: str) -> int:
    """TTL the handler should compute for a given last-arrival date."""
    dt = datetime.combine(date.fromisoformat(arrival), datetime.min.time())
    return int((dt + timedelta(days=30)).timestamp())


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestCleanTravelerBaseline:
    """A compliant traveler produces no alerts, no records, and no pushes."""

    def test_no_alerts_persisted_or_pushed(self, endpoint_arn):
        event = _future_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
        assert _alerts() == []
        assert _pushes(endpoint_arn) == []


class TestGroupDelegation:
    """Events containing a ``travelers`` key are routed to the group handler."""

    def test_travelers_key_delegates_to_group_handler(self, endpoint_arn):
        traveler = _future_event(endpoint_arn, nationality="IN")["profile"]
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": traveler["skymiles_number"],
            "travelers": [traveler],
            "segments": [_segment(destination="CN")],
        }

        response = handler(event, None)

        body = response["body"]
        assert response["statusCode"] == 200
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["group_severity"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert "alerts_sent" not in body

    def test_group_delegation_ignores_single_traveler_keys(self, endpoint_arn):
        traveler = _future_event(endpoint_arn)["profile"]
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": traveler["skymiles_number"],
            "travelers": [traveler],
            "segments": [_segment()],
        }

        response = handler(event, None)

        assert response["body"]["group_severity"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0


class TestRequirementsOverride:
    """``requirements_override`` replaces default country rules for the request."""

    def test_override_waives_visa_requirement(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            requirements_override={"CN": {"requires_visa": False}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_adds_visa_exempt_nationality(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            requirements_override={"CN": {"visa_exempt_nationalities": ["IN"]}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_defaults_require_visa(self, endpoint_arn):
        """An empty override dict falls back to requires_visa=True, no exemptions."""
        event = _future_event(endpoint_arn, requirements_override={"DE": {}})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts("VISA")[0]["reasons"] == ["No visa on file for DE"]

    def test_override_tightens_passport_validity(self, endpoint_arn):
        """DE normally needs 3 months; overriding to 12 flags a 10-month passport."""
        event = _future_event(
            endpoint_arn,
            passport_expiry="2031-04-15",
            requirements_override={
                "DE": {
                    "requires_visa": False,
                    "passport_validity_months": 12,
                }
            },
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _alerts("PASSPORT")[0]["reasons"] == [
            "Passport does not meet DE's 12-month validity requirement for DL300"
        ]

    def test_override_enables_transit_visa_for_layover(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="DE", is_layover=True)],
            requirements_override={"DE": {"transit_visa_required": True}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_override_for_unlisted_country(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            segments=[_segment(destination="ZZ")],
            requirements_override={"ZZ": {"requires_visa": False}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_empty_override_uses_defaults(self, endpoint_arn):
        event = _future_event(endpoint_arn, requirements_override={})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["passport_status"] == "OK"

    def test_missing_override_key_uses_defaults(self, endpoint_arn):
        event = _future_event(endpoint_arn)
        del event["requirements_override"]

        response = handler(event, None)

        assert response["body"]["alerts_sent"] == 0

    def test_override_does_not_leak_into_next_request(self, endpoint_arn):
        overridden = _future_event(endpoint_arn, requirements_override={"DE": {}})
        handler(overridden, None)

        response = handler(_future_event(endpoint_arn), None)

        assert response["body"]["visa_status"] == "OK"


class TestLayovers:
    """Layover segments only need a visa when the country requires a transit visa."""

    def test_layover_without_transit_requirement_skips_visa(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="GB", is_layover=True)],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_requirement_needs_visa(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN", is_layover=True)],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts("VISA")[0]["reasons"] == ["No visa on file for CN"]

    def test_layover_then_final_destination_both_evaluated(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[
                _segment(destination="CN", is_layover=True, flight_number="DL1"),
                _segment(
                    destination="AU",
                    departure_date="2030-06-16",
                    arrival_date="2030-06-17",
                    flight_number="DL2",
                ),
            ],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts("VISA")[0]["reasons"] == [
            "No visa on file for CN",
            "No visa on file for AU",
        ]

    def test_layover_defaults_to_false_when_omitted(self, endpoint_arn):
        segment = _segment(destination="GB")
        del segment["is_layover"]
        event = _future_event(endpoint_arn, nationality="IN", segments=[segment])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestPassportValidityByCountry:
    """Passport validity is checked against each destination's month requirement."""

    def test_schengen_three_month_rule_met_returns_info(self, endpoint_arn):
        """4 months left: meets DE's 3-month rule but is under 6 months → INFO."""
        event = _future_event(endpoint_arn, passport_expiry="2030-10-15")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"
        assert response["body"]["alerts_sent"] == 1

    def test_schengen_three_month_rule_not_met(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2030-08-15")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _alerts("PASSPORT")[0]["reasons"] == [
            "Passport does not meet DE's 3-month validity requirement for DL300"
        ]

    def test_six_month_country_rule_not_met(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            passport_expiry="2030-10-15",
            segments=[_segment(destination="JP")],
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _alerts("PASSPORT")[0]["reasons"] == [
            "Passport does not meet JP's 6-month validity requirement for DL300"
        ]

    def test_passport_expires_before_departure(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2030-06-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _alerts("PASSPORT")[0]["reasons"] == [
            "Passport expires before departure on DL300 to DE"
        ]

    def test_empty_passport_expiry_treated_as_missing(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _alerts("PASSPORT")[0]["reasons"] == ["Passport information is incomplete"]


class TestVisaLimitsByCountry:
    """Visa status reflects visa presence and expiry relative to travel."""

    def test_valid_visa_clears_requirement(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            visa_records=[_visa()],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_visa_expiring_on_travel_date_warns(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            visa_records=[_visa(expiry_date=_DEPARTURE)],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"

    def test_visa_expiring_within_warning_window_is_info(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            visa_records=[_visa(expiry_date="2030-07-01")],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "INFO"

    def test_visa_for_wrong_country_does_not_count(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            visa_records=[_visa(country_code="AU")],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_latest_visa_selected_when_multiple_on_file(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment(destination="CN")],
            visa_records=[
                _visa(expiry_date="2020-01-01", visa_number="OLD"),
                _visa(expiry_date="2040-01-01", visa_number="NEW"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"


class TestAlertPersistenceAndNotification:
    """Each actionable evaluation persists one record and sends one push."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            passport_expiry="2020-01-01",
            segments=[_segment(destination="CN")],
        )

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2,
            "passport_status": "CRITICAL",
            "visa_status": "CRITICAL",
        }
        assert sorted(a["alert_type"] for a in _alerts()) == ["PASSPORT", "VISA"]
        assert len(_pushes(endpoint_arn)) == 2

    def test_alert_record_fields(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2030-08-15")

        handler(event, None)

        record = _alerts("PASSPORT")[0]
        assert record["skymiles_number"] == "5555555555"
        assert record["severity"] == "CRITICAL"
        assert record["status"] == "ACTIVE"
        assert record["itinerary_ref"] == "DL-FUTURE"
        assert int(record["ttl"]) == _expected_ttl(_ARRIVAL)
        assert record["created_at"]
        assert record["alert_id"]

    def test_passport_push_payload(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2030-08-15")

        handler(event, None)

        push = _pushes(endpoint_arn)[0]
        gcm = json.loads(push["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Passport Alert"
        assert push["default"] == (
            "Passport does not meet DE's 3-month validity requirement for DL300"
        )
        assert gcm["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-FUTURE",
        }

    def test_visa_push_payload_joins_reasons(self, endpoint_arn):
        event = _future_event(
            endpoint_arn,
            nationality="IN",
            segments=[
                _segment(destination="CN", flight_number="DL1"),
                _segment(destination="AU", flight_number="DL2"),
            ],
        )

        handler(event, None)

        push = _pushes(endpoint_arn)[0]
        gcm = json.loads(push["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Visa Alert"
        assert push["default"] == "No visa on file for CN; No visa on file for AU"
        assert gcm["data"]["alert_type"] == "VISA"


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rerouting_to_visa_country_triggers_alert(self, endpoint_arn):
        original = _future_event(endpoint_arn, nationality="IN", visa_records=[_visa()],
                                 segments=[_segment(destination="CN")])
        handler(original, None)
        changed = _future_event(endpoint_arn, nationality="IN", visa_records=[_visa()],
                                segments=[_segment(destination="AU")])

        response = handler(changed, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts("VISA")[0]["reasons"] == ["No visa on file for AU"]

    def test_rerouting_to_transit_free_layover_clears_alert(self, endpoint_arn):
        original = _future_event(endpoint_arn, nationality="IN",
                                 segments=[_segment(destination="CN")])
        handler(original, None)
        changed = _future_event(endpoint_arn, nationality="IN",
                                segments=[_segment(destination="GB", is_layover=True)])

        response = handler(changed, None)

        assert response["body"]["visa_status"] == "OK"

    def test_date_change_past_visa_expiry_triggers_alert(self, endpoint_arn):
        visa = _visa(expiry_date="2030-12-31")
        changed = _future_event(
            endpoint_arn,
            nationality="IN",
            visa_records=[visa],
            segments=[_segment(destination="CN", departure_date="2031-02-01",
                               arrival_date="2031-02-02")],
        )

        response = handler(changed, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts("VISA")[0]["reasons"] == [
            "Visa for CN expires before date of travel"
        ]

    def test_date_change_updates_ttl(self, endpoint_arn):
        changed = _future_event(
            endpoint_arn,
            passport_expiry="2020-01-01",
            segments=[_segment(departure_date="2031-02-01", arrival_date="2031-02-02")],
        )

        handler(changed, None)

        assert int(_alerts("PASSPORT")[0]["ttl"]) == _expected_ttl("2031-02-02")

    def test_new_confirmation_number_recorded(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2020-01-01",
                              confirmation_number="DL-REBOOKED")

        handler(event, None)

        assert _alerts("PASSPORT")[0]["itinerary_ref"] == "DL-REBOOKED"


class TestComputeTtl:
    """TTL is 30 days after the latest arrival (or today when no segments)."""

    def test_uses_latest_arrival_across_segments(self):
        itinerary = _parse_itinerary({
            "confirmation_number": "DL-TTL",
            "segments": [
                _segment(arrival_date="2030-06-20"),
                _segment(arrival_date="2030-06-16"),
            ],
        })

        ttl = _compute_ttl(itinerary)

        assert ttl == _expected_ttl("2030-06-20")

    def test_empty_segments_fall_back_to_today(self):
        itinerary = Itinerary(confirmation_number="DL-EMPTY", segments=[])

        ttl = _compute_ttl(itinerary)

        assert ttl == _expected_ttl(date.today().isoformat())


class TestParsing:
    """Event payload deserialization into domain models."""

    def test_profile_parses_visa_records_and_dates(self, endpoint_arn):
        data = _future_event(endpoint_arn, visa_records=[_visa()])["profile"]

        profile = _parse_profile(data)

        assert profile.passport_expiry == date(2040, 1, 1)
        assert profile.visa_records[0].expiry_date == date(2040, 1, 1)
        assert profile.visa_records[0].country_code == "CN"

    def test_profile_without_visa_records_key(self, endpoint_arn):
        data = _future_event(endpoint_arn)["profile"]
        del data["visa_records"]

        profile = _parse_profile(data)

        assert profile.visa_records == []

    def test_profile_without_passport_fields(self, endpoint_arn):
        data = _future_event(endpoint_arn)["profile"]
        del data["passport_number"]
        del data["passport_expiry"]

        profile = _parse_profile(data)

        assert profile.passport_number is None
        assert profile.passport_expiry is None


class TestEvaluateItinerary:
    """``evaluate_itinerary`` is a thin wrapper around ``handler``."""

    def test_returns_same_response_as_handler(self, endpoint_arn):
        event = _future_event(endpoint_arn, passport_expiry="2020-01-01")

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 1, "passport_status": "CRITICAL", "visa_status": "OK"},
        }
