"""Critical-path tests for the Lambda handler end-to-end flow."""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import handler


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
# Additional coverage: overrides, layovers, itinerary changes, delegation, TTL
# ---------------------------------------------------------------------------

import json
from datetime import date, datetime, timedelta

from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

from src.handlers.lambda_handler import _compute_ttl, evaluate_itinerary
from src.models.types import FlightSegment, Itinerary


def _segment(destination: str, departure: str = "2030-06-01", arrival: str = "2030-06-02",
             flight_number: str = "DL300", origin: str = "ATL", is_layover: bool = False) -> dict:
    """Build a raw segment dict as it appears in the Lambda event."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
        "is_layover": is_layover,
    }


def _override(**kwargs) -> dict:
    """Build a raw requirements_override entry with test-friendly defaults."""
    reqs = {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    reqs.update(kwargs)
    return reqs


def _event(endpoint_arn: str, segments: list[dict], overrides: dict | None = None,
           **profile_overrides) -> dict:
    """Build a far-future single-traveler event against fixture requirements."""
    event = _base_event(endpoint_arn, **{"passport_expiry": "2040-01-01", **profile_overrides})
    event["itinerary"]["segments"] = segments
    event["requirements_override"] = overrides
    return event


def _visa(country_code: str, expiry: str = "2040-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": f"V-{country_code}",
    }


def _sns_messages(endpoint_arn: str) -> list[dict]:
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


class TestRequirementsOverride:
    """requirements_override entries replace default country rules for the evaluation."""

    def test_override_makes_country_visa_free(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override(requires_visa=False)})

        response = handler(event, None)

        assert response["body"] == {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"}
        assert _alerts() == []

    def test_override_requires_visa_with_defaults(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": {}})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["passport_status"] == "OK"
        assert "No visa on file for ZZ" in _alerts()[0]["reasons"]

    def test_override_visa_exempt_nationality(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")],
                       {"ZZ": _override(visa_exempt_nationalities=["IN"])})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_replaces_default_country_entry(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("CN")], {"CN": _override(requires_visa=False)})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_empty_override_dict_uses_defaults(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("CN")], {})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestPassportValidityWithinCountryRequirements:
    """Passport validity is judged against each destination's required months."""

    def test_passport_meets_custom_three_month_minimum_but_info(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")],
                       {"ZZ": _override(requires_visa=False, passport_validity_months=3)},
                       passport_expiry="2030-10-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"
        assert response["body"]["alerts_sent"] == 1

    def test_passport_below_country_minimum_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")],
                       {"ZZ": _override(requires_visa=False, passport_validity_months=6)},
                       passport_expiry="2030-10-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert "6-month validity requirement" in _alerts()[0]["reasons"][0]

    def test_passport_expires_before_departure_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")],
                       {"ZZ": _override(requires_visa=False)},
                       passport_expiry="2030-05-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert "expires before departure" in _alerts()[0]["reasons"][0]


class TestVisaValidity:
    """Visa records are checked against travel dates for the destination."""

    def test_valid_visa_no_alert(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override()},
                       visa_records=[_visa("ZZ")])

        response = handler(event, None)

        assert response["body"] == {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"}

    def test_expired_visa_is_critical(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override()},
                       visa_records=[_visa("ZZ", expiry="2020-01-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert "Visa for ZZ has expired" in _alerts()[0]["reasons"]

    def test_visa_expiring_on_travel_date_is_warning(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override()},
                       visa_records=[_visa("ZZ", expiry="2030-06-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"


class TestLayovers:
    """Layover segments only need a visa when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_skipped(self, endpoint_arn):
        segments = [
            _segment("YY", is_layover=True, flight_number="DL1"),
            _segment("ZZ", flight_number="DL2"),
        ]
        overrides = {"YY": _override(), "ZZ": _override(requires_visa=False)}
        event = _event(endpoint_arn, segments, overrides)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        segments = [
            _segment("YY", is_layover=True, flight_number="DL1"),
            _segment("ZZ", flight_number="DL2"),
        ]
        overrides = {"YY": _override(transit_visa_required=True),
                     "ZZ": _override(requires_visa=False)}
        event = _event(endpoint_arn, segments, overrides)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts()[0]["reasons"] == ["No visa on file for YY"]

    def test_is_layover_defaults_to_false_when_omitted(self, endpoint_arn):
        seg = _segment("YY")
        del seg["is_layover"]
        event = _event(endpoint_arn, [seg], {"YY": _override()})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestNotificationsAndPersistence:
    """Each alert type is saved to DynamoDB and pushed via SNS with the right payload."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override()},
                       passport_expiry="2030-05-01")

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2, "passport_status": "CRITICAL", "visa_status": "CRITICAL",
        }
        items = _alerts()
        assert sorted(i["alert_type"] for i in items) == ["PASSPORT", "VISA"]
        assert all(i["itinerary_ref"] == "DL-TEST" for i in items)
        assert all(i["status"] == "ACTIVE" for i in items)
        assert len(_sns_messages(endpoint_arn)) == 2

    def test_visa_push_payload_contents(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override()})

        handler(event, None)

        messages = _sns_messages(endpoint_arn)
        assert len(messages) == 1
        raw = json.dumps(messages[0])
        assert "Visa Alert" in raw
        assert "No visa on file for ZZ" in raw
        assert "DL-TEST" in raw

    def test_alert_ttl_is_30_days_after_last_arrival(self, endpoint_arn):
        segments = [
            _segment("ZZ", "2030-06-01", "2030-06-02", flight_number="DL1"),
            _segment("ZZ", "2030-06-10", "2030-06-11", flight_number="DL2"),
        ]
        event = _event(endpoint_arn, segments, {"ZZ": _override()})

        handler(event, None)

        expected = int((datetime(2030, 6, 11) + timedelta(days=30)).timestamp())
        assert int(_alerts()[0]["ttl"]) == expected


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new destinations."""

    def test_change_to_visa_free_destination_clears_alert(self, endpoint_arn):
        overrides = {"ZZ": _override(), "YY": _override(requires_visa=False)}
        original = _event(endpoint_arn, [_segment("ZZ")], overrides)
        changed = _event(endpoint_arn, [_segment("YY")], overrides)

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["visa_status"] == "CRITICAL"
        assert second["body"]["visa_status"] == "OK"
        assert second["body"]["alerts_sent"] == 0

    def test_change_to_later_date_beyond_passport_validity(self, endpoint_arn):
        overrides = {"ZZ": _override(requires_visa=False)}
        original = _event(endpoint_arn, [_segment("ZZ", "2030-01-01", "2030-01-02")],
                          overrides, passport_expiry="2031-01-01")
        changed = _event(endpoint_arn, [_segment("ZZ", "2030-12-01", "2030-12-02")],
                         overrides, passport_expiry="2031-01-01")

        first = handler(original, None)
        second = handler(changed, None)

        assert first["body"]["passport_status"] == "OK"
        assert second["body"]["passport_status"] == "CRITICAL"

    def test_added_segment_requiring_visa_triggers_alert(self, endpoint_arn):
        overrides = {"ZZ": _override(), "YY": _override(requires_visa=False)}
        changed = _event(endpoint_arn, [_segment("YY", flight_number="DL1"),
                                        _segment("ZZ", flight_number="DL2")], overrides)

        response = handler(changed, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestGroupDelegation:
    """Events with a ``travelers`` key are routed to the group handler."""

    def test_travelers_key_delegates_to_group_handler(self, endpoint_arn):
        traveler = _base_event(endpoint_arn, passport_expiry="2040-01-01")["profile"]
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": traveler["skymiles_number"],
            "travelers": [traveler],
            "segments": [_segment("ZZ")],
            "requirements_override": {"ZZ": _override(requires_visa=False)},
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert "traveler_results" in response["body"]


class TestEvaluateItinerary:
    """evaluate_itinerary is a thin wrapper around handler."""

    def test_returns_handler_response(self, endpoint_arn):
        event = _event(endpoint_arn, [_segment("ZZ")], {"ZZ": _override(requires_visa=False)})

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }


class TestComputeTtl:
    """TTL is 30 days after the latest arrival, or after today with no segments."""

    def test_uses_latest_arrival(self):
        segs = [
            FlightSegment("DL1", "ATL", "ZZ", date(2030, 1, 1), date(2030, 1, 5), False),
            FlightSegment("DL2", "ZZ", "ATL", date(2030, 1, 2), date(2030, 1, 3), False),
        ]

        ttl = _compute_ttl(Itinerary(confirmation_number="X", segments=segs))

        assert ttl == int((datetime(2030, 1, 5) + timedelta(days=30)).timestamp())

    def test_empty_segments_uses_today(self):
        ttl = _compute_ttl(Itinerary(confirmation_number="X", segments=[]))

        expected = int((datetime.combine(date.today(), datetime.min.time())
                        + timedelta(days=30)).timestamp())
        assert ttl == expected
