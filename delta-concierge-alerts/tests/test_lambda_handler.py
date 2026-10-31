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
from src.handlers.lambda_handler import _compute_ttl, evaluate_itinerary, handler
from src.models.types import Itinerary
from tests.conftest import make_segment


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
# Extended coverage: group delegation, requirements overrides, persistence,
# notifications, TTL, and itinerary changes.  Uses made-up country codes
# (XA/XB) via ``requirements_override`` and 2030+ dates so results do not
# depend on COUNTRY_REQUIREMENTS or today's date.
# ---------------------------------------------------------------------------

_VISA_REQUIRED = {"requires_visa": True, "visa_exempt_nationalities": []}
_VISA_FREE = {"requires_visa": False, "passport_validity_months": 6}


def _segment(
    destination: str = "XA",
    departure: str = "2030-06-01",
    arrival: str = "2030-06-02",
    is_layover: bool = False,
    flight_number: str = "DL300",
) -> dict:
    """Build a raw segment dict for the event payload."""
    return {
        "flight_number": flight_number,
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _visa(country_code: str = "XA", expiry: str = "2031-01-01") -> dict:
    """Build a raw visa record dict for the event payload."""
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _override_event(endpoint_arn: str, segments=None, overrides=None, **profile) -> dict:
    """Build an event isolated from production data via requirements_override."""
    event = _base_event(endpoint_arn)
    event["profile"]["passport_expiry"] = "2035-01-01"
    event["profile"].update(profile)
    event["itinerary"]["segments"] = segments or [_segment()]
    event["requirements_override"] = (
        overrides if overrides is not None
        else {"XA": dict(_VISA_REQUIRED), "XB": dict(_VISA_FREE)}
    )
    return event


def _endpoint_messages(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages published to a moto platform endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    messages = backend.platform_endpoints[endpoint_arn].messages
    return [json.loads(m) for m in messages.values()]


def _new_endpoint(token: str) -> str:
    """Create an additional moto SNS platform endpoint."""
    sns = notif_mod.sns_client
    app_arn = sns.list_platform_applications()["PlatformApplications"][0][
        "PlatformApplicationArn"
    ]
    return sns.create_platform_endpoint(
        PlatformApplicationArn=app_arn, Token=token,
    )["EndpointArn"]


class TestGroupDelegation:
    """Events with a ``travelers`` key are delegated to the group handler."""

    def _group_event(self, endpoint_a: str, endpoint_b: str) -> dict:
        return {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "111",
            "segments": [_segment()],
            "travelers": [
                {
                    "skymiles_number": "111",
                    "first_name": "Ann",
                    "last_name": "A",
                    "nationality": "US",
                    "passport_number": "PA",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_a,
                    "visa_records": [_visa("XA")],
                },
                {
                    "skymiles_number": "222",
                    "first_name": "Bob",
                    "last_name": "B",
                    "nationality": "US",
                    "passport_number": "PB",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_b,
                    "visa_records": [],
                },
            ],
            "requirements_override": {"XA": dict(_VISA_REQUIRED)},
        }

    def test_group_event_returns_group_response(self, endpoint_arn):
        """Group payload yields the group response shape, not the single one."""
        event = self._group_event(endpoint_arn, _new_endpoint("tok-b"))

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 1
        assert body["group_severity"] == "CRITICAL"
        assert "passport_status" not in body

    def test_group_alerts_attributed_per_traveler(self, endpoint_arn):
        """Only the traveler missing a visa gets a persisted alert."""
        event = self._group_event(endpoint_arn, _new_endpoint("tok-b"))

        response = handler(event, None)

        results = {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}
        assert results["111"]["visa_status"] == "OK"
        assert results["222"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert [i["skymiles_number"] for i in items] == ["222"]
        assert items[0]["itinerary_ref"] == "DL-GROUP"

    def test_group_without_travelers_key_uses_single_flow(self, endpoint_arn):
        """A normal event without ``travelers`` returns the single-traveler shape."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA")])

        response = handler(event, None)

        assert set(response["body"]) == {"alerts_sent", "passport_status", "visa_status"}


class TestRequirementsOverride:
    """requirements_override entries replace or extend the default requirements."""

    def test_override_with_defaults_requires_visa(self, endpoint_arn):
        """An empty override entry defaults to visa required, no exemptions."""
        event = _override_event(endpoint_arn, overrides={"XA": {}})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["No visa on file for XA"]

    def test_override_visa_not_required(self, endpoint_arn):
        """requires_visa=False in the override suppresses the visa alert."""
        event = _override_event(endpoint_arn, segments=[_segment("XB")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_visa_exempt_nationality(self, endpoint_arn):
        """A nationality in visa_exempt_nationalities needs no visa."""
        overrides = {"XA": {"requires_visa": True, "visa_exempt_nationalities": ["IN"]}}
        event = _override_event(endpoint_arn, overrides=overrides)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_replaces_existing_country(self, endpoint_arn):
        """Overriding a real country code (CN) takes precedence over defaults."""
        overrides = {"CN": {"requires_visa": False}}
        event = _override_event(endpoint_arn, segments=[_segment("CN")], overrides=overrides)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_country_code_field_used(self, endpoint_arn):
        """An explicit country_code in the override payload is accepted."""
        overrides = {"XA": {"country_code": "XA", "requires_visa": False}}
        event = _override_event(endpoint_arn, overrides=overrides)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_passport_validity_months_met(self, endpoint_arn):
        """Passport valid 4 months past departure satisfies a 3-month override."""
        overrides = {"XB": {"requires_visa": False, "passport_validity_months": 3}}
        event = _override_event(
            endpoint_arn, segments=[_segment("XB", "2030-01-01", "2030-01-02")],
            overrides=overrides, passport_expiry="2030-05-01",
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"

    def test_override_passport_validity_months_not_met(self, endpoint_arn):
        """Same passport fails a 6-month override (the default)."""
        overrides = {"XB": {"requires_visa": False}}
        event = _override_event(
            endpoint_arn, segments=[_segment("XB", "2030-01-01", "2030-01-02")],
            overrides=overrides, passport_expiry="2030-05-01",
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == [
            "Passport does not meet XB's 6-month validity requirement for DL300"
        ]

    def test_empty_override_dict_uses_defaults(self, endpoint_arn):
        """An empty requirements_override is ignored (falsy)."""
        event = _override_event(endpoint_arn, segments=[_segment("CN")], overrides={})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestLayoverHandling:
    """Layover segments only need a visa when transit_visa_required is set."""

    def test_layover_without_transit_visa_requirement(self, endpoint_arn):
        """Layover in a visa country with no transit requirement → no alert."""
        event = _override_event(endpoint_arn, segments=[
            _segment("XA", is_layover=True, flight_number="DL1"),
            _segment("XB", flight_number="DL2"),
        ])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement(self, endpoint_arn):
        """Layover where transit_visa_required=True → CRITICAL visa alert."""
        overrides = {
            "XA": {"requires_visa": True, "transit_visa_required": True},
            "XB": dict(_VISA_FREE),
        }
        event = _override_event(endpoint_arn, overrides=overrides, segments=[
            _segment("XA", is_layover=True, flight_number="DL1"),
            _segment("XB", flight_number="DL2"),
        ])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_is_layover_defaults_to_false(self, endpoint_arn):
        """A segment without is_layover is treated as a destination."""
        seg = _segment("XA")
        del seg["is_layover"]
        event = _override_event(endpoint_arn, segments=[seg])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestVisaRecordParsing:
    """Visa records in the profile are parsed and matched against destinations."""

    def test_valid_visa_clears_alert(self, endpoint_arn):
        """A visa expiring well after travel → OK."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA", "2031-01-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_expired_visa_is_critical(self, endpoint_arn):
        """A visa that expired in 2020 → CRITICAL."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA", "2020-01-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["Visa for XA has expired"]

    def test_visa_expiring_before_travel_is_critical(self, endpoint_arn):
        """A visa expiring before departure → CRITICAL."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA", "2030-05-01")])

        response = handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["Visa for XA expires before date of travel"]

    def test_visa_expiring_on_travel_date_is_warning(self, endpoint_arn):
        """A visa expiring on the departure date → WARNING."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA", "2030-06-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"

    def test_visa_expiring_soon_after_travel_is_info(self, endpoint_arn):
        """A visa expiring within 30 days after departure → INFO."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA", "2030-06-15")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "INFO"

    def test_visa_for_other_country_does_not_match(self, endpoint_arn):
        """A visa for a different country does not satisfy XA."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XC", "2031-01-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_missing_visa_records_key_defaults_to_empty(self, endpoint_arn):
        """A profile without visa_records is treated as having none."""
        event = _override_event(endpoint_arn)
        del event["profile"]["visa_records"]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_missing_passport_number_key_is_critical(self, endpoint_arn):
        """A profile without passport_number → incomplete passport alert."""
        event = _override_event(endpoint_arn, segments=[_segment("XB")])
        del event["profile"]["passport_number"]

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"


class TestAlertPersistenceAndNotification:
    """Each actionable evaluation persists one alert and sends one push."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        """Expired passport + missing visa → two alerts and two pushes."""
        event = _override_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        body = response["body"]
        assert body == {"alerts_sent": 2, "passport_status": "CRITICAL", "visa_status": "CRITICAL"}
        items = alert_store_mod.table.scan()["Items"]
        assert sorted(i["alert_type"] for i in items) == ["PASSPORT", "VISA"]
        titles = sorted(
            json.loads(m["GCM"])["notification"]["title"]
            for m in _endpoint_messages(endpoint_arn)
        )
        assert titles == [
            "Delta Concierge — Passport Alert",
            "Delta Concierge — Visa Alert",
        ]

    def test_persisted_alert_fields(self, endpoint_arn):
        """Persisted alert carries member, booking ref, ACTIVE status, and TTL."""
        event = _override_event(endpoint_arn)

        handler(event, None)

        item = alert_store_mod.table.scan()["Items"][0]
        expected_ttl = int((datetime(2030, 6, 2) + timedelta(days=30)).timestamp())
        assert item["skymiles_number"] == "1234567890"
        assert item["itinerary_ref"] == "DL-TEST"
        assert item["status"] == "ACTIVE"
        assert item["severity"] == "CRITICAL"
        assert int(item["ttl"]) == expected_ttl

    def test_visa_push_payload(self, endpoint_arn):
        """Visa push data contains type, severity, and confirmation number."""
        event = _override_event(endpoint_arn)

        handler(event, None)

        (message,) = _endpoint_messages(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert message["default"] == "No visa on file for XA"
        assert gcm["data"] == {
            "alert_type": "VISA",
            "severity": "CRITICAL",
            "confirmation_number": "DL-TEST",
        }

    def test_passport_push_joins_reasons(self, endpoint_arn):
        """Multiple passport reasons are joined with '; ' in the push body."""
        overrides = {"XB": dict(_VISA_FREE)}
        event = _override_event(endpoint_arn, overrides=overrides, passport_expiry="2030-06-01",
                                segments=[
                                    _segment("XB", "2030-01-01", "2030-01-02", flight_number="DL1"),
                                    _segment("XB", "2030-02-01", "2030-02-02", flight_number="DL2"),
                                ])

        handler(event, None)

        (message,) = _endpoint_messages(endpoint_arn)
        assert message["default"] == (
            "Passport does not meet XB's 6-month validity requirement for DL1; "
            "Passport does not meet XB's 6-month validity requirement for DL2"
        )
        assert json.loads(message["GCM"])["data"]["alert_type"] == "PASSPORT"

    def test_no_push_when_no_alerts(self, endpoint_arn):
        """A compliant traveler gets no persisted alerts and no pushes."""
        event = _override_event(endpoint_arn, visa_records=[_visa("XA")])

        handler(event, None)

        assert alert_store_mod.table.scan()["Items"] == []
        assert _endpoint_messages(endpoint_arn) == []


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new destinations."""

    def test_change_to_visa_country_creates_new_alert(self, endpoint_arn):
        """Booking moves from visa-free XB to visa-required XA → new alert."""
        first = _override_event(endpoint_arn, segments=[_segment("XB")])
        changed = _override_event(endpoint_arn, segments=[_segment("XA")])

        first_response = handler(first, None)
        changed_response = handler(changed, None)

        assert first_response["body"]["visa_status"] == "OK"
        assert changed_response["body"]["visa_status"] == "CRITICAL"
        assert len(alert_store_mod.table.scan()["Items"]) == 1

    def test_change_to_later_date_updates_ttl(self, endpoint_arn):
        """Moving the trip later pushes the alert TTL out accordingly."""
        event = _override_event(endpoint_arn, segments=[
            _segment("XA", "2030-06-01", "2030-06-02"),
            _segment("XB", "2030-08-01", "2030-08-03"),
        ])

        handler(event, None)

        item = alert_store_mod.table.scan()["Items"][0]
        assert int(item["ttl"]) == int((datetime(2030, 8, 3) + timedelta(days=30)).timestamp())


class TestComputeTtl:
    """TTL is 30 days after the latest arrival, or today when no segments."""

    def test_uses_latest_arrival(self):
        """The max arrival date across segments drives the TTL."""
        itinerary = Itinerary(confirmation_number="DL-T", segments=[
            make_segment(departure=date(2030, 3, 1), arrival=date(2030, 3, 5)),
            make_segment(departure=date(2030, 1, 1), arrival=date(2030, 1, 2)),
        ])

        ttl = _compute_ttl(itinerary)

        assert ttl == int((datetime(2030, 3, 5) + timedelta(days=30)).timestamp())

    def test_empty_segments_uses_today(self):
        """No segments → TTL is 30 days after today."""
        itinerary = Itinerary(confirmation_number="DL-T", segments=[])

        ttl = _compute_ttl(itinerary)

        today = datetime.combine(date.today(), datetime.min.time())
        assert ttl == int((today + timedelta(days=30)).timestamp())


class TestEvaluateItinerary:
    """evaluate_itinerary is a thin wrapper over handler."""

    def test_matches_handler_result(self, endpoint_arn):
        """Wrapper returns the same response as calling handler directly."""
        event = _override_event(endpoint_arn)

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 1, "passport_status": "OK", "visa_status": "CRITICAL"},
        }
