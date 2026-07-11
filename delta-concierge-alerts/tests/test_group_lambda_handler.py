"""Unit tests for the group itinerary Lambda handler.

Exercises parsing, requirements overrides, passport/visa alert and no-alert
paths, DynamoDB persistence, individual and group SNS notifications, traveler
lookup, and the response payload (per-traveler statuses + aggregate severity).
"""

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.models.types import GroupItinerary


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


def _traveler(endpoint_arn: str, **overrides) -> dict:
    """Build a valid traveler profile dict with sensible defaults."""
    traveler = {
        "skymiles_number": "1000001",
        "first_name": "Ann",
        "last_name": "Traveler",
        "nationality": "IN",
        "passport_number": "P100001",
        "passport_expiry": "2030-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    traveler.update(overrides)
    return traveler


def _base_event(endpoint_arn: str, travelers=None, **overrides) -> dict:
    """Build a valid group Lambda event with optional field overrides.

    Default itinerary: a single non-layover segment to CN (which requires a
    visa and has no exempt nationalities), so an IN traveler with no visa on
    file triggers a CRITICAL visa alert.
    """
    if travelers is None:
        travelers = [_traveler(endpoint_arn)]
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": travelers[0]["skymiles_number"],
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
        "travelers": travelers,
        "requirements_override": None,
    }
    event.update(overrides)
    return event


class TestParsing:
    """_parse_group_itinerary deserializes the event into a GroupItinerary."""

    def test_parses_segments_and_travelers(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        group = _parse_group_itinerary(event)

        assert isinstance(group, GroupItinerary)
        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1000001"
        assert len(group.segments) == 1
        assert group.segments[0].destination == "CN"
        assert len(group.travelers) == 1
        assert group.travelers[0].skymiles_number == "1000001"

    def test_is_layover_defaults_to_false(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["segments"][0]["is_layover"]

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False

    def test_parses_multiple_segments_and_travelers(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, skymiles_number="1000001"),
                _traveler(endpoint_arn, skymiles_number="1000002"),
            ],
        )
        event["segments"].append({
            "flight_number": "DL200",
            "origin": "PEK",
            "destination": "JP",
            "departure_date": "2026-09-20",
            "arrival_date": "2026-09-21",
            "is_layover": True,
        })

        group = _parse_group_itinerary(event)

        assert len(group.segments) == 2
        assert group.segments[1].is_layover is True
        assert len(group.travelers) == 2


class TestRequirementsOverride:
    """requirements_override merges into the default requirements dict."""

    def test_override_makes_destination_visa_exempt(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": ["IN"],
            }
        }

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["total_alerts_sent"] == 0

    def test_override_disabling_visa_clears_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {
            "CN": {"requires_visa": False}
        }

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "OK"


class TestPassportAlerts:
    """Passport problems produce a persisted alert and CRITICAL status."""

    def test_expired_passport_triggers_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="US", passport_expiry="2020-01-01")],
        )
        event["segments"][0]["destination"] = "DE"

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] >= 1

    def test_missing_passport_triggers_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn, nationality="US",
                    passport_number=None, passport_expiry=None,
                )
            ],
        )
        event["segments"][0]["destination"] = "DE"

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"


class TestVisaAlerts:
    """Missing visa for a required destination yields a CRITICAL visa alert."""

    def test_missing_visa_triggers_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] >= 1


class TestNoAlerts:
    """A clean traveler produces no alerts and OK statuses."""

    def test_clean_traveler_has_no_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="US")],
        )
        event["segments"][0]["destination"] = "DE"

        response = group_handler(event, None)

        body = response["body"]
        result = body["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"


class TestPersistence:
    """Alerts are written to DynamoDB with the expected shape."""

    def test_alert_persisted_to_dynamodb(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) == 1
        assert visa_alerts[0]["skymiles_number"] == "1000001"
        assert visa_alerts[0]["itinerary_ref"] == "DL-GROUP"
        assert visa_alerts[0]["severity"] == "CRITICAL"
        assert "No visa on file for CN" in visa_alerts[0]["reasons"]
        assert "ttl" in visa_alerts[0]

    def test_both_passport_and_visa_alerts_persisted(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2020-01-01")],
        )

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        types = {i["alert_type"] for i in items}
        assert types == {"PASSPORT", "VISA"}


class TestNotifications:
    """Individual and group SNS notifications are dispatched."""

    def test_individual_and_group_notifications_sent(self, endpoint_arn, monkeypatch):
        sent_individual = []
        sent_group = []
        monkeypatch.setattr(
            "src.handlers.group_lambda_handler.send_push_notification",
            lambda payload: sent_individual.append(payload),
        )
        monkeypatch.setattr(
            "src.handlers.group_lambda_handler.send_group_notification",
            lambda arn, result: sent_group.append((arn, result)),
        )
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        assert len(sent_individual) == 1
        assert sent_individual[0].push_data["alert_type"] == "VISA"
        assert len(sent_group) == 1
        assert sent_group[0][0] == endpoint_arn

    def test_group_notification_skipped_when_primary_absent(self, endpoint_arn, monkeypatch):
        sent_group = []
        monkeypatch.setattr(
            "src.handlers.group_lambda_handler.send_group_notification",
            lambda arn, result: sent_group.append(arn),
        )
        event = _base_event(endpoint_arn, primary_traveler="9999999")

        group_handler(event, None)

        assert sent_group == []


class TestTravelerLookup:
    """_find_traveler returns the matching profile or None."""

    def test_finds_matching_traveler(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        found = _find_traveler(group, "1000001")

        assert found is not None
        assert found.skymiles_number == "1000001"

    def test_returns_none_for_unknown_skymiles(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert _find_traveler(group, "does-not-exist") is None


class TestResponseAndAggregateSeverity:
    """Response payload reports per-traveler statuses and aggregate severity."""

    def test_response_shape(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert set(body["traveler_results"][0].keys()) == {
            "skymiles_number", "first_name", "last_name",
            "passport_status", "visa_status", "alerts_sent",
        }

    def test_group_severity_is_highest_across_travelers(self, endpoint_arn):
        # To DE: US traveler is visa-exempt (OK), IN traveler needs a visa and
        # has none on file (CRITICAL).
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, skymiles_number="1000001", nationality="US"),
                _traveler(endpoint_arn, skymiles_number="1000002", nationality="IN"),
            ],
        )
        event["segments"][0]["destination"] = "DE"

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        statuses = {r["skymiles_number"]: r["visa_status"] for r in body["traveler_results"]}
        assert statuses["1000001"] == "OK"
        assert statuses["1000002"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
