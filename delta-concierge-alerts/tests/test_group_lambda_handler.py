"""Critical-path tests for the group Lambda handler end-to-end flow."""

from datetime import date

import boto3
import moto
import pytest

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)


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


def _base_traveler(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group traveler with a far-future passport and DE visa."""
    traveler = {
        "skymiles_number": "1111111",
        "first_name": "Jane",
        "last_name": "Doe",
        "nationality": "IN",
        "passport_number": "P111",
        "passport_expiry": "2035-01-01",
        "visa_records": [
            {
                "country_code": "DE",
                "visa_type": "TOURIST",
                "issue_date": "2025-01-01",
                "expiry_date": "2035-01-01",
                "visa_number": "V111",
            }
        ],
        "endpoint_arn": endpoint_arn,
    }
    traveler.update(overrides)
    return traveler


def _base_event(endpoint_arn: str, travelers=None, **overrides) -> dict:
    """Build a valid group Lambda event; segment goes ATL -> DE in the far future."""
    event = {
        "confirmation_number": "GRP-001",
        "primary_traveler": "1111111",
        "segments": [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2030-09-01",
                "arrival_date": "2030-09-02",
                "is_layover": False,
            }
        ],
        "travelers": travelers if travelers is not None else [_base_traveler(endpoint_arn)],
    }
    event.update(overrides)
    return event


class TestCleanGroup:
    """A group where every traveler has valid documents produces no alerts."""

    def test_clean_traveler_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "GRP-001"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 1
        result = body["traveler_results"][0]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0

        # Nothing persisted for a clean group
        assert alert_store_mod.table.scan()["Items"] == []


class TestExpiredPassport:
    """A traveler with an expired passport yields a persisted CRITICAL alert."""

    def test_expired_passport_persisted_and_notified(self, endpoint_arn):
        traveler = _base_traveler(endpoint_arn, passport_expiry="2020-01-01")
        event = _base_event(endpoint_arn, travelers=[traveler])

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        result = body["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] >= 1

        items = alert_store_mod.table.scan()["Items"]
        passport_alerts = [i for i in items if i["alert_type"] == "PASSPORT"]
        assert len(passport_alerts) == 1
        assert passport_alerts[0]["skymiles_number"] == "1111111"
        assert passport_alerts[0]["severity"] == "CRITICAL"
        assert passport_alerts[0]["itinerary_ref"] == "GRP-001"
        assert passport_alerts[0]["status"] == "ACTIVE"


class TestMissingVisa:
    """A traveler with no visa on file for a visa-required country is flagged."""

    def test_missing_visa_persisted(self, endpoint_arn):
        traveler = _base_traveler(endpoint_arn, visa_records=[])
        event = _base_event(endpoint_arn, travelers=[traveler])

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

        items = alert_store_mod.table.scan()["Items"]
        visa_alerts = [i for i in items if i["alert_type"] == "VISA"]
        assert len(visa_alerts) == 1
        assert "No visa on file for DE" in visa_alerts[0]["reasons"]


class TestRequirementsOverride:
    """requirements_override replaces the default rules for a country."""

    def test_override_disables_visa_requirement(self, endpoint_arn):
        traveler = _base_traveler(endpoint_arn, visa_records=[])
        event = _base_event(
            endpoint_arn,
            travelers=[traveler],
            requirements_override={
                "DE": {
                    "country_code": "DE",
                    "requires_visa": False,
                    "transit_visa_required": False,
                    "passport_validity_months": 6,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        body = response["body"]
        # Without the override this traveler would get a CRITICAL visa alert.
        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["total_alerts_sent"] == 0
        visa_alerts = [
            i for i in alert_store_mod.table.scan()["Items"] if i["alert_type"] == "VISA"
        ]
        assert visa_alerts == []


class TestGroupSummaryNotification:
    """The primary traveler receives a group summary notification."""

    def test_primary_receives_group_summary(self, endpoint_arn, monkeypatch):
        calls = []
        monkeypatch.setattr(
            group_mod,
            "send_group_notification",
            lambda arn, result: calls.append((arn, result)),
        )
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        assert len(calls) == 1
        assert calls[0][0] == endpoint_arn
        assert calls[0][1].confirmation_number == "GRP-001"

    def test_unknown_primary_skips_summary(self, endpoint_arn, monkeypatch):
        calls = []
        monkeypatch.setattr(
            group_mod,
            "send_group_notification",
            lambda arn, result: calls.append((arn, result)),
        )
        event = _base_event(endpoint_arn, primary_traveler="0000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert calls == []


class TestFindTraveler:
    """_find_traveler resolves travelers by SkyMiles number."""

    def test_returns_matching_traveler(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        found = _find_traveler(group, "1111111")

        assert found is not None
        assert found.skymiles_number == "1111111"

    def test_returns_none_for_unknown(self, endpoint_arn):
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert _find_traveler(group, "9999999") is None


class TestParseGroupItinerary:
    """_parse_group_itinerary deserializes segments, travelers, and layover flags."""

    def test_parses_segments_and_layover_flag(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "CN",
                "departure_date": "2030-09-01",
                "arrival_date": "2030-09-02",
                "is_layover": True,
            },
            {
                "flight_number": "DL200",
                "origin": "CN",
                "destination": "DE",
                "departure_date": "2030-09-02",
                "arrival_date": "2030-09-03",
                "is_layover": False,
            },
        ]

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "GRP-001"
        assert group.primary_traveler == "1111111"
        assert len(group.segments) == 2
        assert group.segments[0].is_layover is True
        assert group.segments[1].is_layover is False
        assert group.segments[0].departure_date == date(2030, 9, 1)
        assert len(group.travelers) == 1
        assert group.travelers[0].skymiles_number == "1111111"

    def test_layover_defaults_to_false_when_absent(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["segments"] = [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2030-09-01",
                "arrival_date": "2030-09-02",
            }
        ]

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestMultipleTravelers:
    """A mixed group aggregates per-traveler results and highest severity."""

    def test_mixed_group_reports_each_traveler(self, endpoint_arn):
        clean = _base_traveler(endpoint_arn)
        expired = _base_traveler(
            endpoint_arn,
            skymiles_number="2222222",
            first_name="John",
            passport_expiry="2020-01-01",
            visa_records=[],
        )
        event = _base_event(endpoint_arn, travelers=[clean, expired])

        response = group_handler(event, None)

        body = response["body"]
        assert len(body["traveler_results"]) == 2
        assert body["group_severity"] == "CRITICAL"
        # Clean traveler contributes no alerts; the second contributes several.
        by_sm = {r["skymiles_number"]: r for r in body["traveler_results"]}
        assert by_sm["1111111"]["alerts_sent"] == 0
        assert by_sm["2222222"]["alerts_sent"] >= 1
        assert body["total_alerts_sent"] == by_sm["2222222"]["alerts_sent"]
