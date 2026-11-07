"""Branch-coverage tests for the single-traveler Lambda handler.

Uses made-up country codes (XA, XB, XC) via ``requirements_override`` and
2030+ dates so results are independent of production requirements data and
of today's date.
"""

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
from src.models.types import FlightSegment, Itinerary


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the alerts table + SNS endpoints, and swap module clients."""
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
                    "KeySchema": [{"AttributeName": "itinerary_ref", "KeyType": "HASH"}],
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
        endpoints = [
            sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"fake-token-{i}",
            )["EndpointArn"]
            for i in range(2)
        ]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoints
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    return _mock_aws[0]


@pytest.fixture
def second_endpoint_arn(_mock_aws):
    return _mock_aws[1]


def _override(**fields) -> dict:
    """Requirements override for a made-up country; no visa needed by default."""
    reqs = {
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    reqs.update(fields)
    return reqs


def _segment(**overrides) -> dict:
    seg = {
        "flight_number": "DL100",
        "origin": "JFK",
        "destination": "XA",
        "departure_date": "2030-06-01",
        "arrival_date": "2030-06-02",
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


def _profile(endpoint_arn: str, **overrides) -> dict:
    profile = {
        "skymiles_number": "1234567890",
        "first_name": "Test",
        "last_name": "User",
        "nationality": "US",
        "passport_number": "P999999",
        "passport_expiry": "2035-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    profile.update(overrides)
    return profile


def _visa(country_code: str = "XA", expiry_date: str = "2035-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2029-01-01",
        "expiry_date": expiry_date,
        "visa_number": "V123",
    }


def _base_event(endpoint_arn: str, segments=None, requirements_override=None, **profile_overrides) -> dict:
    """Clean single-traveler event: valid passport, no-visa country XA."""
    return {
        "profile": _profile(endpoint_arn, **profile_overrides),
        "itinerary": {
            "confirmation_number": "DL-COV",
            "segments": segments if segments is not None else [_segment()],
        },
        "requirements_override": (
            requirements_override if requirements_override is not None else {"XA": _override()}
        ),
    }


def _sns_messages(endpoint_arn: str) -> list[dict]:
    """Return decoded GCM payloads published to an endpoint via moto."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    raw = backend.platform_endpoints[endpoint_arn].messages
    decoded = []
    for message in raw.values():
        envelope = json.loads(message)
        decoded.append(json.loads(envelope["GCM"]))
    return decoded


def _alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


class TestCleanTraveler:
    """A compliant traveler gets OK statuses and no persisted alerts or pushes."""

    def test_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
        assert _alerts() == []
        assert _sns_messages(endpoint_arn) == []


class TestGroupDelegation:
    """Events with a ``travelers`` key are routed to the group handler."""

    def test_travelers_key_delegates_to_group_handler(self, endpoint_arn, second_endpoint_arn):
        event = {
            "confirmation_number": "DL-GRP",
            "primary_traveler": "111",
            "segments": [_segment()],
            "travelers": [
                _profile(endpoint_arn, skymiles_number="111"),
                _profile(second_endpoint_arn, skymiles_number="222", passport_expiry="2020-01-01"),
            ],
            "requirements_override": {"XA": _override()},
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert "traveler_results" in response["body"]
        alerted = {a["skymiles_number"] for a in _alerts()}
        assert alerted == {"222"}


class TestRequirementsOverride:
    """Overrides replace default rules; missing keys fall back to handler defaults."""

    def test_override_requires_visa_without_visa_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, requirements_override={"XA": _override(requires_visa=True)})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_empty_override_entry_defaults_to_visa_required(self, endpoint_arn):
        event = _base_event(endpoint_arn, requirements_override={"XA": {}})

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert "No visa on file for XA" in _alerts()[0]["reasons"]

    def test_visa_exempt_nationality_override(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            requirements_override={"XA": _override(requires_visa=True, visa_exempt_nationalities=["US"])},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_without_transit_visa_requirement_skipped(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(is_layover=True)],
            requirements_override={"XA": _override(requires_visa=True)},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(is_layover=True)],
            requirements_override={"XA": _override(requires_visa=True, transit_visa_required=True)},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_passport_validity_months_override(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2031-01-01",
            requirements_override={"XA": _override(passport_validity_months=12)},
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert "XA's 12-month" in _alerts()[0]["reasons"][0]

    def test_multiple_override_entries_applied(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(), _segment(flight_number="DL200", destination="XB")],
            requirements_override={"XA": _override(), "XB": _override(requires_visa=True)},
        )

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts()[0]["reasons"] == ["No visa on file for XB"]

    def test_empty_override_dict_uses_default_requirements(self, endpoint_arn):
        event = _base_event(endpoint_arn, requirements_override={})
        event["itinerary"]["segments"] = [_segment(destination="CN")]
        event["profile"]["nationality"] = "IN"

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_missing_override_key_uses_default_requirements(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["requirements_override"]
        event["itinerary"]["segments"] = [_segment(destination="CN")]
        event["profile"]["nationality"] = "IN"

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestPassportAlert:
    """Passport issues persist a PASSPORT alert and push a passport notification."""

    def test_passport_alert_record_and_push(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"] == {"alerts_sent": 1, "passport_status": "CRITICAL", "visa_status": "OK"}
        (alert,) = _alerts()
        assert alert["alert_type"] == "PASSPORT"
        assert alert["severity"] == "CRITICAL"
        assert alert["status"] == "ACTIVE"
        assert alert["itinerary_ref"] == "DL-COV"
        assert alert["skymiles_number"] == "1234567890"
        assert alert["reasons"] == ["Passport has expired"]
        (push,) = _sns_messages(endpoint_arn)
        assert push["notification"]["title"] == "Delta Concierge — Passport Alert"
        assert push["notification"]["body"] == "Passport has expired"
        assert push["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-COV",
        }

    def test_passport_info_severity_still_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2030-10-01",
            requirements_override={"XA": _override(passport_validity_months=3)},
        )

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"
        assert response["body"]["alerts_sent"] == 1
        assert _alerts()[0]["severity"] == "INFO"

    def test_passport_expires_before_departure(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-05-01")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert "expires before departure" in _alerts()[0]["reasons"][0]

    def test_multiple_passport_reasons_joined_in_push_body(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2030-07-01",
            segments=[_segment(), _segment(flight_number="DL200", destination="XB",
                                           departure_date="2030-08-01", arrival_date="2030-08-02")],
            requirements_override={"XA": _override(), "XB": _override()},
        )

        handler(event, None)

        (push,) = _sns_messages(endpoint_arn)
        reasons = _alerts()[0]["reasons"]
        assert len(reasons) == 2
        assert push["notification"]["body"] == "; ".join(reasons)


class TestVisaAlert:
    """Visa issues persist a VISA alert and push a visa notification."""

    def _visa_event(self, endpoint_arn, expiry_date):
        return _base_event(
            endpoint_arn,
            visa_records=[_visa(expiry_date=expiry_date)],
            requirements_override={"XA": _override(requires_visa=True)},
        )

    def test_visa_alert_record_and_push(self, endpoint_arn):
        event = _base_event(endpoint_arn, requirements_override={"XA": _override(requires_visa=True)})

        handler(event, None)

        (alert,) = _alerts()
        assert alert["alert_type"] == "VISA"
        assert alert["status"] == "ACTIVE"
        assert alert["itinerary_ref"] == "DL-COV"
        (push,) = _sns_messages(endpoint_arn)
        assert push["notification"]["title"] == "Delta Concierge — Visa Alert"
        assert push["notification"]["body"] == "No visa on file for XA"
        assert push["data"]["alert_type"] == "VISA"
        assert push["data"]["severity"] == "CRITICAL"

    def test_valid_visa_no_alert(self, endpoint_arn):
        event = self._visa_event(endpoint_arn, "2035-01-01")

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_visa_expiring_on_travel_date_is_warning(self, endpoint_arn):
        event = self._visa_event(endpoint_arn, "2030-06-01")

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"
        assert response["body"]["alerts_sent"] == 1

    def test_visa_expiring_within_window_is_info(self, endpoint_arn):
        event = self._visa_event(endpoint_arn, "2030-06-15")

        response = handler(event, None)

        assert response["body"]["visa_status"] == "INFO"
        assert _alerts()[0]["severity"] == "INFO"

    def test_visa_expiring_before_travel_is_critical(self, endpoint_arn):
        event = self._visa_event(endpoint_arn, "2030-05-01")

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert _alerts()[0]["reasons"] == ["Visa for XA expires before date of travel"]

    def test_visa_for_other_country_does_not_match(self, endpoint_arn):
        event = self._visa_event(endpoint_arn, "2035-01-01")
        event["profile"]["visa_records"] = [_visa(country_code="XB")]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestPassportAndVisaAlerts:
    """When both evaluations alert, two records and two pushes are produced."""

    def test_both_alerts_sent(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2020-01-01",
            requirements_override={"XA": _override(requires_visa=True)},
        )

        response = handler(event, None)

        assert response["body"] == {"alerts_sent": 2, "passport_status": "CRITICAL", "visa_status": "CRITICAL"}
        assert sorted(a["alert_type"] for a in _alerts()) == ["PASSPORT", "VISA"]
        titles = sorted(m["notification"]["title"] for m in _sns_messages(endpoint_arn))
        assert titles == ["Delta Concierge — Passport Alert", "Delta Concierge — Visa Alert"]
        assert len({a["alert_id"] for a in _alerts()}) == 2


class TestTtl:
    """TTL is 30 days after the latest arrival, or after today with no segments."""

    def test_ttl_persisted_from_last_arrival(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            passport_expiry="2020-01-01",
            segments=[
                _segment(arrival_date="2030-06-02"),
                _segment(flight_number="DL200", departure_date="2030-06-20", arrival_date="2030-06-21"),
            ],
        )

        handler(event, None)

        expected = int((datetime(2030, 6, 21) + timedelta(days=30)).timestamp())
        assert int(_alerts()[0]["ttl"]) == expected

    def test_ttl_uses_max_arrival_not_last_segment(self):
        itinerary = Itinerary(
            confirmation_number="DL-TTL",
            segments=[
                FlightSegment("DL1", "JFK", "XA", date(2030, 7, 1), date(2030, 7, 10), False),
                FlightSegment("DL2", "XA", "XB", date(2030, 6, 1), date(2030, 6, 2), False),
            ],
        )

        assert _compute_ttl(itinerary) == int((datetime(2030, 7, 10) + timedelta(days=30)).timestamp())

    def test_ttl_without_segments_uses_today(self):
        itinerary = Itinerary(confirmation_number="DL-EMPTY", segments=[])

        expected = int(
            (datetime.combine(date.today(), datetime.min.time()) + timedelta(days=30)).timestamp()
        )
        assert _compute_ttl(itinerary) == expected

    def test_handler_with_no_segments_and_expired_passport(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01", segments=[])

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert int(_alerts()[0]["ttl"]) > int(datetime.now().timestamp())


class TestEvaluateItinerary:
    """evaluate_itinerary is a thin wrapper over handler with no context."""

    def test_returns_handler_response(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = evaluate_itinerary(event)

        assert response["statusCode"] == 200
        assert response["body"]["passport_status"] == "CRITICAL"
        assert len(_alerts()) == 1


class TestParsing:
    """Event payload deserialization into profile and itinerary models."""

    def test_parse_profile_with_visa_records(self, endpoint_arn):
        data = _profile(endpoint_arn, visa_records=[_visa(), _visa(country_code="XB", expiry_date="2031-02-03")])

        profile = _parse_profile(data)

        assert profile.passport_expiry == date(2035, 1, 1)
        assert [v.country_code for v in profile.visa_records] == ["XA", "XB"]
        assert profile.visa_records[1].expiry_date == date(2031, 2, 3)
        assert profile.visa_records[0].issue_date == date(2029, 1, 1)
        assert profile.visa_records[0].visa_number == "V123"

    def test_parse_profile_optional_fields_absent(self, endpoint_arn):
        data = _profile(endpoint_arn)
        del data["visa_records"]
        del data["passport_number"]
        del data["passport_expiry"]

        profile = _parse_profile(data)

        assert profile.visa_records == []
        assert profile.passport_number is None
        assert profile.passport_expiry is None

    def test_parse_profile_empty_passport_expiry(self, endpoint_arn):
        profile = _parse_profile(_profile(endpoint_arn, passport_expiry=""))

        assert profile.passport_expiry is None

    def test_parse_itinerary_defaults_is_layover_false(self):
        seg = _segment()
        del seg["is_layover"]

        itinerary = _parse_itinerary({"confirmation_number": "DL-P", "segments": [seg]})

        assert itinerary.confirmation_number == "DL-P"
        assert itinerary.segments[0].is_layover is False
        assert itinerary.segments[0].departure_date == date(2030, 6, 1)

    def test_parse_itinerary_layover_flag(self):
        itinerary = _parse_itinerary(
            {"confirmation_number": "DL-P", "segments": [_segment(is_layover=True)]}
        )

        assert itinerary.segments[0].is_layover is True
