"""Branch-coverage tests for lambda_handler: overrides, group delegation, TTL, layovers."""

from datetime import date, datetime, timedelta

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import _compute_ttl, evaluate_itinerary, handler
from tests.conftest import make_itinerary, make_segment

_XA_NO_VISA = {
    "XA": {
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": [],
    }
}
_XB_VISA_REQUIRED = {
    "XB": {
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
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


def _segment(destination: str = "XA", **overrides) -> dict:
    seg = {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": destination,
        "departure_date": "2031-03-01",
        "arrival_date": "2031-03-02",
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


def _base_event(endpoint_arn: str, **profile_overrides) -> dict:
    """Valid single-traveler event to a made-up no-visa country with a far-future passport."""
    profile = {
        "skymiles_number": "1234567890",
        "first_name": "Test",
        "last_name": "User",
        "nationality": "IN",
        "passport_number": "P999999",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    profile.update(profile_overrides)
    return {
        "profile": profile,
        "itinerary": {"confirmation_number": "DL-PATHS", "segments": [_segment()]},
        "requirements_override": dict(_XA_NO_VISA),
    }


def _sns_messages(endpoint_arn: str) -> list:
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    return list(backend.platform_endpoints[endpoint_arn].messages.values())


class TestHappyPath:
    """A traveler with a valid passport to a no-visa country gets no alerts."""

    def test_no_alerts_persisted_or_pushed(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }
        assert alert_store_mod.table.scan()["Items"] == []
        assert _sns_messages(endpoint_arn) == []


class TestRequirementsOverride:
    """requirements_override entries replace or add country rules for the request."""

    def test_override_marks_country_visa_free(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_requiring_visa_triggers_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("XB")]
        event["requirements_override"] = dict(_XB_VISA_REQUIRED)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_override_defaults_require_visa(self, endpoint_arn):
        """An empty override dict falls back to requires_visa=True."""
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("XB")]
        event["requirements_override"] = {"XB": {}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["No visa on file for XB"]

    def test_override_defaults_six_month_passport_rule(self, endpoint_arn):
        """Default passport_validity_months=6 flags a passport expiring 4 months after departure."""
        event = _base_event(endpoint_arn, passport_expiry="2031-07-01")
        event["itinerary"]["segments"] = [_segment("XB")]
        event["requirements_override"] = {"XB": {"requires_visa": False}}

        response = handler(event, None)

        assert response["body"]["passport_status"] != "OK"
        assert response["body"]["visa_status"] == "OK"

    def test_override_exempt_nationality_skips_visa(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("XB")]
        event["requirements_override"] = {
            "XB": {**_XB_VISA_REQUIRED["XB"], "visa_exempt_nationalities": ["IN"]}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0


class TestValidVisaRecord:
    """Visa records in the event payload are parsed and satisfy visa requirements."""

    def test_valid_visa_clears_visa_alert(self, endpoint_arn):
        visa = {
            "country_code": "XB",
            "visa_type": "TOURIST",
            "issue_date": "2030-01-01",
            "expiry_date": "2035-01-01",
            "visa_number": "V123",
        }
        event = _base_event(endpoint_arn, visa_records=[visa])
        event["itinerary"]["segments"] = [_segment("XB")]
        event["requirements_override"] = dict(_XB_VISA_REQUIRED)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0


class TestLayovers:
    """Layover segments only require a visa when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_skipped(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("XB", is_layover=True), _segment("XA")]
        event["requirements_override"] = {**_XA_NO_VISA, **_XB_VISA_REQUIRED}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["itinerary"]["segments"] = [_segment("XB", is_layover=True), _segment("XA")]
        event["requirements_override"] = {
            **_XA_NO_VISA,
            "XB": {**_XB_VISA_REQUIRED["XB"], "transit_visa_required": True},
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestBothAlerts:
    """Passport and visa issues each persist an alert and send a push."""

    def test_passport_and_visa_alerts_both_sent(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")
        event["itinerary"]["segments"] = [_segment("XB")]
        event["requirements_override"] = dict(_XB_VISA_REQUIRED)

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2,
            "passport_status": "CRITICAL",
            "visa_status": "CRITICAL",
        }
        items = alert_store_mod.table.scan()["Items"]
        assert sorted(i["alert_type"] for i in items) == ["PASSPORT", "VISA"]
        assert all(i["itinerary_ref"] == "DL-PATHS" for i in items)
        assert all(i["status"] == "ACTIVE" for i in items)
        assert len(_sns_messages(endpoint_arn)) == 2


class TestTtl:
    """Alert TTL is 30 days after the latest arrival date across all segments."""

    def test_ttl_uses_latest_arrival(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")
        event["itinerary"]["segments"] = [
            _segment("XA", departure_date="2031-03-10", arrival_date="2031-03-11"),
            _segment("XA", departure_date="2031-03-01", arrival_date="2031-03-02"),
        ]

        handler(event, None)

        expected = int(datetime(2031, 4, 10).timestamp())
        items = alert_store_mod.table.scan()["Items"]
        assert int(items[0]["ttl"]) == expected

    def test_compute_ttl_empty_segments_uses_today(self):
        itinerary = make_itinerary()

        ttl = _compute_ttl(itinerary)

        expected = datetime.combine(date.today(), datetime.min.time()) + timedelta(days=30)
        assert ttl == int(expected.timestamp())

    def test_compute_ttl_single_segment(self):
        itinerary = make_itinerary(
            make_segment(departure=date(2031, 6, 1), arrival=date(2031, 6, 2))
        )

        ttl = _compute_ttl(itinerary)

        assert ttl == int(datetime(2031, 7, 2).timestamp())


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new destinations."""

    def test_changed_destination_produces_new_result(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {**_XA_NO_VISA, **_XB_VISA_REQUIRED}
        first = handler(event, None)

        event["itinerary"]["segments"] = [_segment("XB")]
        second = handler(event, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"


class TestEvaluateItinerary:
    """evaluate_itinerary delegates to handler with the same event."""

    def test_matches_handler_response(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = evaluate_itinerary(event)

        assert response == handler(event, None)


class TestGroupDelegation:
    """Events with a travelers key are routed to the group handler."""

    def test_travelers_key_routes_to_group_handler(self, endpoint_arn):
        traveler = _base_event(endpoint_arn)["profile"]
        event = {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": traveler["skymiles_number"],
            "travelers": [traveler],
            "segments": [_segment("XB")],
            "requirements_override": dict(_XB_VISA_REQUIRED),
        }

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 1
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert "alerts_sent" not in body
