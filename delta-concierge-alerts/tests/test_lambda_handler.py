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


def _segment(**overrides) -> dict:
    """Build a valid far-future segment dict with optional field overrides."""
    seg = {
        "flight_number": "DL300",
        "origin": "ATL",
        "destination": "DE",
        "departure_date": "2030-06-01",
        "arrival_date": "2030-06-02",
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


class TestRequirementsOverride:
    """requirements_override replaces default country rules for the request."""

    def test_override_marks_visa_exempt_nationality_clean(self, endpoint_arn):
        """IN → CN normally CRITICAL; override exempting IN yields OK."""
        event = _base_event(endpoint_arn, passport_expiry="2035-01-01")
        event["itinerary"]["segments"] = [_segment(destination="CN")]
        event["requirements_override"] = {
            "CN": {"requires_visa": True, "visa_exempt_nationalities": ["IN"]}
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["passport_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_override_defaults_when_fields_omitted(self, endpoint_arn):
        """An empty override entry falls back to requires_visa=True / 6 months."""
        event = _base_event(endpoint_arn, nationality="US", passport_expiry="2035-01-01")
        event["itinerary"]["segments"] = [_segment(destination="DE")]
        event["requirements_override"] = {"DE": {}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1

    def test_override_raises_passport_validity_requirement(self, endpoint_arn):
        """Passport valid 7 months past departure fails a 12-month override."""
        event = _base_event(endpoint_arn, nationality="US", passport_expiry="2031-01-01")
        event["itinerary"]["segments"] = [_segment(destination="DE")]
        event["requirements_override"] = {
            "DE": {"passport_validity_months": 12, "visa_exempt_nationalities": ["US"]}
        }

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert response["body"]["visa_status"] == "OK"
        items = alert_store_mod.table.scan()["Items"]
        assert any(
            "12-month validity requirement" in r
            for i in items for r in i["reasons"]
        )

    def test_override_adds_unknown_country_code(self, endpoint_arn):
        """Override for a code not in COUNTRY_REQUIREMENTS uses code as country_code."""
        event = _base_event(endpoint_arn, nationality="US", passport_expiry="2035-01-01")
        event["itinerary"]["segments"] = [_segment(destination="ZZ")]
        event["requirements_override"] = {
            "ZZ": {"requires_visa": False, "passport_validity_months": 1}
        }

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 0,
            "passport_status": "OK",
            "visa_status": "OK",
        }


class TestLayoverSegments:
    """Layover segments only demand a visa when transit_visa_required is set."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        """IN traveler transiting DE (no transit visa) to a visa-free ZZ → OK."""
        event = _base_event(endpoint_arn, passport_expiry="2035-01-01")
        event["itinerary"]["segments"] = [
            _segment(destination="DE", is_layover=True),
            _segment(
                flight_number="DL301", origin="DE", destination="ZZ",
                departure_date="2030-06-02", arrival_date="2030-06-03",
            ),
        ]
        event["requirements_override"] = {
            "DE": {"transit_visa_required": False, "passport_validity_months": 3},
            "ZZ": {"requires_visa": False},
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"
        assert response["body"]["alerts_sent"] == 0

    def test_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        """Same itinerary but DE demands a transit visa → CRITICAL."""
        event = _base_event(endpoint_arn, passport_expiry="2035-01-01")
        event["itinerary"]["segments"] = [
            _segment(destination="DE", is_layover=True),
            _segment(
                flight_number="DL301", origin="DE", destination="ZZ",
                departure_date="2030-06-02", arrival_date="2030-06-03",
            ),
        ]
        event["requirements_override"] = {
            "DE": {"transit_visa_required": True, "passport_validity_months": 3},
            "ZZ": {"requires_visa": False},
        }

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        assert response["body"]["alerts_sent"] == 1
        items = alert_store_mod.table.scan()["Items"]
        assert "No visa on file for DE" in items[0]["reasons"]


class TestItineraryChange:
    """Re-evaluating a changed itinerary produces alerts matching the new segments."""

    def test_added_destination_introduces_visa_alert(self, endpoint_arn):
        """US → DE is clean; adding a CN leg to the same booking → CRITICAL."""
        event = _base_event(endpoint_arn, nationality="US", passport_expiry="2035-01-01")
        event["itinerary"]["segments"] = [_segment(destination="DE")]
        first = handler(event, None)

        event["itinerary"]["segments"].append(
            _segment(
                flight_number="DL302", origin="DE", destination="CN",
                departure_date="2030-06-05", arrival_date="2030-06-06",
            )
        )
        second = handler(event, None)

        assert first["body"]["visa_status"] == "OK"
        assert second["body"]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["itinerary_ref"] == "DL-TEST"
        assert "No visa on file for CN" in items[0]["reasons"]

    def test_later_departure_pushes_passport_into_validity_window(self, endpoint_arn):
        """Passport OK for a 2030 trip becomes INFO when the trip moves later."""
        event = _base_event(endpoint_arn, nationality="US", passport_expiry="2031-01-01")
        event["itinerary"]["segments"] = [_segment(destination="DE")]
        first = handler(event, None)

        event["itinerary"]["segments"] = [
            _segment(destination="DE", departure_date="2030-09-01", arrival_date="2030-09-02")
        ]
        second = handler(event, None)

        assert first["body"]["passport_status"] == "OK"
        assert second["body"]["passport_status"] == "INFO"
        assert second["body"]["alerts_sent"] == 1


class TestGroupDelegation:
    """Events with a ``travelers`` key are routed to the group handler."""

    def _group_event(self, endpoint_arn: str) -> dict:
        return {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "1111111111",
            "segments": [_segment(destination="CN")],
            "travelers": [
                {
                    "skymiles_number": "1111111111",
                    "first_name": "Primary",
                    "last_name": "Traveler",
                    "nationality": "IN",
                    "passport_number": "P111",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [
                        {
                            "country_code": "CN",
                            "visa_type": "TOURIST",
                            "issue_date": "2029-01-01",
                            "expiry_date": "2035-01-01",
                            "visa_number": "V111",
                        }
                    ],
                },
                {
                    "skymiles_number": "2222222222",
                    "first_name": "Second",
                    "last_name": "Traveler",
                    "nationality": "IN",
                    "passport_number": "P222",
                    "passport_expiry": "2035-01-01",
                    "endpoint_arn": endpoint_arn,
                    "visa_records": [],
                },
            ],
        }

    def test_group_event_returns_group_response_shape(self, endpoint_arn):
        """Group payload returns per-traveler results instead of single statuses."""
        event = self._group_event(endpoint_arn)

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert "passport_status" not in body
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
        statuses = {t["skymiles_number"]: t["visa_status"] for t in body["traveler_results"]}
        assert statuses == {"1111111111": "OK", "2222222222": "CRITICAL"}

    def test_group_event_persists_alert_for_affected_traveler_only(self, endpoint_arn):
        """Only the traveler missing a visa gets a DynamoDB record."""
        event = self._group_event(endpoint_arn)

        handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        assert items[0]["skymiles_number"] == "2222222222"
        assert items[0]["itinerary_ref"] == "DL-GROUP"

    def test_empty_travelers_key_still_delegates(self, endpoint_arn):
        """An empty travelers list is still a group payload → 0 alerts, OK."""
        event = self._group_event(endpoint_arn)
        event["travelers"] = []

        response = handler(event, None)

        assert response["body"]["group_severity"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"] == []
