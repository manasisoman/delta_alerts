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
from src.handlers.lambda_handler import evaluate_itinerary, handler


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
# Far-future helpers (keep tests stable regardless of today's date)
# ---------------------------------------------------------------------------

_DEPARTURE = "2031-03-01"
_ARRIVAL = "2031-03-02"


def _segment(
    destination: str,
    flight_number: str = "DL100",
    origin: str = "ATL",
    departure_date: str = _DEPARTURE,
    arrival_date: str = _ARRIVAL,
    is_layover: bool = False,
) -> dict:
    """Build a raw segment dict as it appears in the Lambda event."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _visa(
    country_code: str,
    expiry_date: str = "2035-01-01",
    issue_date: str = "2025-01-01",
    visa_number: str = "V0001",
) -> dict:
    """Build a raw visa record dict as it appears in the Lambda event."""
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": visa_number,
    }


def _profile(
    endpoint_arn: str,
    skymiles_number: str = "1234567890",
    nationality: str = "US",
    passport_number: str | None = "P999999",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
    first_name: str = "Test",
) -> dict:
    """Build a raw profile dict with far-future passport validity."""
    return {
        "skymiles_number": skymiles_number,
        "first_name": first_name,
        "last_name": "User",
        "nationality": nationality,
        "passport_number": passport_number,
        "passport_expiry": passport_expiry,
        "endpoint_arn": endpoint_arn,
        "visa_records": visa_records or [],
    }


def _event(
    endpoint_arn: str,
    segments: list[dict] | None = None,
    requirements_override: dict | None = None,
    confirmation_number: str = "DL-FUTURE",
    **profile_kwargs,
) -> dict:
    """Build a single-traveler event: US national, ATL→DE, valid passport."""
    return {
        "profile": _profile(endpoint_arn, **profile_kwargs),
        "itinerary": {
            "confirmation_number": confirmation_number,
            "segments": segments if segments is not None else [_segment("DE")],
        },
        "requirements_override": requirements_override,
    }


def _saved_alerts() -> list[dict]:
    """Return every alert item persisted to the moto DynamoDB table."""
    return alert_store_mod.table.scan()["Items"]


def _published_messages(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages published to the given moto endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    raw = backend.platform_endpoints[endpoint_arn].messages.values()
    return [json.loads(m) for m in raw]


def _expected_ttl(last_arrival: date) -> int:
    expiry = datetime.combine(last_arrival, datetime.min.time()) + timedelta(days=30)
    return int(expiry.timestamp())


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHappyPath:
    """A compliant traveler produces no alerts, no records, and no pushes."""

    def test_clean_itinerary_returns_ok_and_sends_nothing(self, endpoint_arn):
        event = _event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
        assert _saved_alerts() == []
        assert _published_messages(endpoint_arn) == []

    def test_missing_requirements_override_key_uses_defaults(self, endpoint_arn):
        event = _event(endpoint_arn)
        del event["requirements_override"]

        response = handler(event, None)

        assert response["body"]["alerts_sent"] == 0

    def test_empty_requirements_override_uses_defaults(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("CN")], requirements_override={})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestGroupDelegation:
    """Events containing a ``travelers`` key are routed to the group handler."""

    def _group_event(self, endpoint_arn: str) -> dict:
        return {
            "confirmation_number": "DL-GRP",
            "primary_traveler": "111",
            "segments": [_segment("CN")],
            "travelers": [
                _profile(endpoint_arn, skymiles_number="111", visa_records=[_visa("CN")]),
                _profile(endpoint_arn, skymiles_number="222", nationality="IN"),
            ],
        }

    def test_travelers_key_returns_group_response_shape(self, endpoint_arn):
        event = self._group_event(endpoint_arn)

        response = handler(event, None)

        body = response["body"]
        assert response["statusCode"] == 200
        assert body["confirmation_number"] == "DL-GRP"
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        assert [t["skymiles_number"] for t in body["traveler_results"]] == ["111", "222"]
        assert "alerts_sent" not in body

    def test_group_alerts_persisted_for_offending_traveler_only(self, endpoint_arn):
        event = self._group_event(endpoint_arn)

        handler(event, None)

        items = _saved_alerts()
        assert len(items) == 1
        assert items[0]["skymiles_number"] == "222"
        assert items[0]["itinerary_ref"] == "DL-GRP"
        assert items[0]["reasons"] == ["No visa on file for CN"]


class TestRequirementsOverride:
    """``requirements_override`` replaces country rules, with documented defaults."""

    def test_override_waives_visa_for_destination(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("CN")],
            requirements_override={"CN": {"requires_visa": False}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_empty_country_override_defaults_to_visa_required_no_exemptions(self, endpoint_arn):
        # US nationals are normally exempt for DE; an empty override drops the exemption.
        event = _event(endpoint_arn, requirements_override={"DE": {}})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["No visa on file for DE"]

    def test_override_adds_exempt_nationality(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            nationality="IN",
            segments=[_segment("CN")],
            requirements_override={"CN": {"visa_exempt_nationalities": ["IN"]}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_stricter_passport_validity(self, endpoint_arn):
        # 8 months remaining satisfies DE's default 3-month rule but not 12 months.
        event = _event(
            endpoint_arn,
            passport_expiry="2031-11-01",
            requirements_override={
                "DE": {
                    "passport_validity_months": 12,
                    "visa_exempt_nationalities": ["US"],
                }
            },
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["visa_status"] == "OK"
        assert _saved_alerts()[0]["reasons"] == [
            "Passport does not meet DE's 12-month validity requirement for DL100"
        ]

    def test_override_for_unlisted_country(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("ZZ")],
            requirements_override={"ZZ": {"requires_visa": False}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"


class TestPassportValidityPerCountry:
    """Passport validity is judged against each destination's month requirement."""

    def test_schengen_three_month_rule_met_but_under_six_months_is_info(self, endpoint_arn):
        event = _event(endpoint_arn, passport_expiry="2031-07-01")

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 1,
            "passport_status": "INFO",
            "visa_status": "OK",
        }

    def test_schengen_exact_three_month_boundary_is_info(self, endpoint_arn):
        event = _event(endpoint_arn, passport_expiry="2031-06-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"

    def test_schengen_under_three_months_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn, passport_expiry="2031-05-31")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == [
            "Passport does not meet DE's 3-month validity requirement for DL100"
        ]

    def test_six_month_country_with_four_months_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("JP")], passport_expiry="2031-07-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == [
            "Passport does not meet JP's 6-month validity requirement for DL100"
        ]

    def test_passport_expiring_before_departure_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn, passport_expiry="2031-02-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == [
            "Passport expires before departure on DL100 to DE"
        ]


class TestLayovers:
    """Layovers skip visa checks unless transit visas are required, but passports are always checked."""

    def test_layover_in_non_transit_country_skips_visa(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            nationality="IN",
            visa_records=[_visa("GB")],
            segments=[
                _segment("DE", flight_number="DL1", is_layover=True),
                _segment("GB", flight_number="DL2", origin="DE"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_in_transit_visa_country_requires_visa(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            nationality="IN",
            visa_records=[_visa("GB")],
            segments=[
                _segment("CN", flight_number="DL1", is_layover=True),
                _segment("GB", flight_number="DL2", origin="CN"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["No visa on file for CN"]

    def test_layover_passport_validity_still_enforced(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            passport_expiry="2031-07-01",
            segments=[
                _segment("JP", flight_number="DL1", is_layover=True),
                _segment("DE", flight_number="DL2", origin="JP"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["visa_status"] == "OK"
        reasons = _saved_alerts()[0]["reasons"]
        assert "Passport does not meet JP's 6-month validity requirement for DL1" in reasons

    def test_transit_override_requires_visa_on_layover(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            nationality="IN",
            visa_records=[_visa("GB")],
            segments=[
                _segment("DE", flight_number="DL1", is_layover=True),
                _segment("GB", flight_number="DL2", origin="DE"),
            ],
            requirements_override={"DE": {"transit_visa_required": True}},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["No visa on file for DE"]

    def test_is_layover_defaults_to_false_when_omitted(self, endpoint_arn):
        segment = _segment("DE")
        del segment["is_layover"]
        event = _event(endpoint_arn, nationality="IN", segments=[segment])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestVisaValidity:
    """Visa expiry relative to today and the departure date sets the visa status."""

    def test_valid_visa_is_ok(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("CN")], visa_records=[_visa("CN")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_expired_visa_is_critical(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("CN")],
            visa_records=[_visa("CN", issue_date="2015-01-01", expiry_date="2020-01-01")],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["Visa for CN has expired"]

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("CN")],
            visa_records=[_visa("CN", expiry_date="2030-06-01")],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["Visa for CN expires before date of travel"]

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("CN")],
            visa_records=[_visa("CN", expiry_date=_DEPARTURE)],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"
        assert response["body"]["alerts_sent"] == 1

    def test_visa_expiring_within_warning_window_is_info(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("CN")],
            visa_records=[_visa("CN", expiry_date="2031-03-20")],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "INFO"

    def test_latest_visa_selected_when_multiple_on_file(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[_segment("CN")],
            visa_records=[
                _visa("CN", expiry_date="2020-01-01", visa_number="OLD"),
                _visa("CN", expiry_date="2035-01-01", visa_number="NEW"),
            ],
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"


class TestAlertPersistenceAndNotification:
    """Each actionable evaluation is saved to DynamoDB and pushed via SNS."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("CN")], passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2,
            "passport_status": "CRITICAL",
            "visa_status": "CRITICAL",
        }
        items = _saved_alerts()
        assert sorted(i["alert_type"] for i in items) == ["PASSPORT", "VISA"]
        assert all(i["status"] == "ACTIVE" for i in items)
        assert all(i["itinerary_ref"] == "DL-FUTURE" for i in items)
        assert all(i["skymiles_number"] == "1234567890" for i in items)
        assert len(_published_messages(endpoint_arn)) == 2

    def test_visa_push_payload_contents(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("CN")])

        handler(event, None)

        messages = _published_messages(endpoint_arn)
        assert len(messages) == 1
        assert messages[0]["default"] == "No visa on file for CN"
        gcm = json.loads(messages[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Visa Alert"
        assert gcm["data"] == {
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "confirmation_number": "DL-FUTURE",
        }

    def test_passport_push_payload_contents(self, endpoint_arn):
        event = _event(endpoint_arn, passport_expiry="2031-07-01")

        handler(event, None)

        messages = _published_messages(endpoint_arn)
        assert len(messages) == 1
        gcm = json.loads(messages[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge — Passport Alert"
        assert gcm["data"]["alert_type"] == "PASSPORT"
        assert gcm["data"]["severity"] == "INFO"

    def test_multiple_reasons_joined_in_push_body(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[
                _segment("CN", flight_number="DL1"),
                _segment("IN", flight_number="DL2", origin="CN"),
            ],
        )

        handler(event, None)

        body = _published_messages(endpoint_arn)[0]["default"]
        assert body == "No visa on file for CN; No visa on file for IN"

    def test_ttl_is_30_days_after_latest_arrival(self, endpoint_arn):
        event = _event(
            endpoint_arn,
            segments=[
                _segment("CN", flight_number="DL1", arrival_date="2031-04-10"),
                _segment("DE", flight_number="DL2", origin="CN",
                         departure_date="2031-03-05", arrival_date="2031-03-06"),
            ],
        )

        handler(event, None)

        assert int(_saved_alerts()[0]["ttl"]) == _expected_ttl(date(2031, 4, 10))

    def test_ttl_falls_back_to_today_without_segments(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[], passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["visa_status"] == "OK"
        assert int(_saved_alerts()[0]["ttl"]) == _expected_ttl(date.today())


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rebooked_to_visa_country_triggers_alert(self, endpoint_arn):
        original = _event(endpoint_arn)
        changed = _event(endpoint_arn, segments=[_segment("CN")])

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["alerts_sent"] == 0
        assert second["body"]["visa_status"] == "CRITICAL"
        items = _saved_alerts()
        assert len(items) == 1
        assert items[0]["itinerary_ref"] == "DL-FUTURE"

    def test_rebooked_away_from_visa_country_clears_alert(self, endpoint_arn):
        original = _event(endpoint_arn, segments=[_segment("CN")])
        changed = _event(endpoint_arn, segments=[_segment("JP")])

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "CRITICAL"
        assert second["body"]["visa_status"] == "OK"
        assert second["body"]["alerts_sent"] == 0

    def test_departure_moved_past_visa_expiry(self, endpoint_arn):
        visas = [_visa("CN", expiry_date="2031-06-01")]
        original = _event(endpoint_arn, segments=[_segment("CN")], visa_records=visas)
        changed = _event(
            endpoint_arn,
            segments=[_segment("CN", departure_date="2031-07-01", arrival_date="2031-07-02")],
            visa_records=visas,
        )

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["Visa for CN expires before date of travel"]

    def test_departure_moved_past_passport_validity(self, endpoint_arn):
        original = _event(endpoint_arn, passport_expiry="2032-01-01")
        changed = _event(
            endpoint_arn,
            passport_expiry="2032-01-01",
            segments=[_segment("DE", departure_date="2031-11-01", arrival_date="2031-11-02")],
        )

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["passport_status"] == "OK"
        assert second["body"]["passport_status"] == "CRITICAL"

    def test_added_transit_layover_triggers_alert(self, endpoint_arn):
        original = _event(endpoint_arn, segments=[_segment("GB")])
        changed = _event(
            endpoint_arn,
            segments=[
                _segment("CN", flight_number="DL1", is_layover=True),
                _segment("GB", flight_number="DL2", origin="CN"),
            ],
        )

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"


class TestEventParsing:
    """Optional profile fields are tolerated and default sensibly."""

    def test_missing_passport_expiry_key_is_incomplete(self, endpoint_arn):
        event = _event(endpoint_arn)
        del event["profile"]["passport_expiry"]

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["Passport information is incomplete"]

    def test_missing_passport_number_key_is_incomplete(self, endpoint_arn):
        event = _event(endpoint_arn)
        del event["profile"]["passport_number"]

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"

    def test_missing_visa_records_key_treated_as_none_on_file(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("CN")])
        del event["profile"]["visa_records"]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestEvaluateItinerary:
    """``evaluate_itinerary`` is a thin wrapper around ``handler``."""

    def test_returns_same_result_as_handler(self, endpoint_arn):
        event = _event(endpoint_arn, segments=[_segment("CN")])

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 1, "passport_status": "OK", "visa_status": "CRITICAL"},
        }
