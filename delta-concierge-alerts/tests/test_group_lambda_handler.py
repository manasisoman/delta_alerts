"""Critical-path tests for the group itinerary Lambda handler."""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import group_handler
from src.models.types import (
    AlertSeverity,
    GroupEvaluationResult,
    PassportEvaluation,
    TravelDocRequirements,
    TravelerAlertSummary,
    VisaEvaluation,
)


PRIMARY = "1000000001"
COMPANION = "1000000002"
DEPARTURE = "2030-06-01"
ARRIVAL = "2030-06-02"
FAR_FUTURE_EXPIRY = "2035-01-01"

# Made-up country codes so tests are isolated from real COUNTRY_REQUIREMENTS.
XA_OVERRIDE = {
    "country_code": "XA",
    "requires_visa": True,
    "transit_visa_required": False,
    "passport_validity_months": 3,
    "visa_exempt_nationalities": ["US"],
}
XB_OVERRIDE = {
    "country_code": "XB",
    "requires_visa": False,
    "transit_visa_required": False,
    "passport_validity_months": 6,
    "visa_exempt_nationalities": [],
}


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks with a DynamoDB table and one SNS endpoint per traveler."""
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
        endpoints = {
            number: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"token-{number}",
            )["EndpointArn"]
            for number in (PRIMARY, COMPANION)
        }

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
def endpoints(_mock_aws):
    """Expose the per-traveler moto SNS endpoint ARNs keyed by SkyMiles number."""
    return _mock_aws


def _traveler(number: str, endpoint_arn: str, **overrides) -> dict:
    """Build a traveler profile dict that is clean for XA/XB travel by default."""
    traveler = {
        "skymiles_number": number,
        "first_name": f"First{number[-1]}",
        "last_name": f"Last{number[-1]}",
        "nationality": "US",
        "passport_number": f"P{number}",
        "passport_expiry": FAR_FUTURE_EXPIRY,
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    traveler.update(overrides)
    return traveler


def _segment(destination: str = "XA", **overrides) -> dict:
    """Build a flight segment dict with far-future dates."""
    segment = {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": destination,
        "departure_date": DEPARTURE,
        "arrival_date": ARRIVAL,
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


def _base_event(endpoints: dict, **overrides) -> dict:
    """Build a valid, clean two-traveler group event with optional overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION]),
        ],
        "requirements_override": {"XA": dict(XA_OVERRIDE), "XB": dict(XB_OVERRIDE)},
    }
    event.update(overrides)
    return event


def _messages(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages published to a moto platform endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


def _gcm(message: dict) -> dict:
    """Decode the GCM sub-message of a published SNS JSON message."""
    return json.loads(message["GCM"])


def _results_by_member(response: dict) -> dict:
    """Index traveler_results by SkyMiles number."""
    return {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}


def _saved_alerts() -> list[dict]:
    """Return every alert item persisted to the moto DynamoDB table."""
    return alert_store_mod.table.scan()["Items"]


def _swap_evaluator(result: GroupEvaluationResult):
    """Context helper: replace group_mod.evaluate_group_itinerary with a stub."""
    class _Swap:
        def __enter__(self):
            self.orig = group_mod.evaluate_group_itinerary
            group_mod.evaluate_group_itinerary = lambda group, requirements: result
            return self

        def __exit__(self, *exc):
            group_mod.evaluate_group_itinerary = self.orig
            return False

    return _Swap()


def _summary(
    number: str,
    passport_alert: bool = False,
    passport_severity: AlertSeverity | None = None,
    visa_alert: bool = False,
    visa_severity: AlertSeverity | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary with stubbed evaluation results."""
    return TravelerAlertSummary(
        skymiles_number=number,
        first_name="Ghost",
        last_name="Traveler",
        passport_result=PassportEvaluation(
            profile=None,
            segments_evaluated=[],
            is_alert_required=passport_alert,
            severity=passport_severity,
            reasons=["stub passport reason"] if passport_alert else [],
            validation_errors=[],
        ),
        visa_result=VisaEvaluation(
            profile=None,
            segments_evaluated=[],
            is_alert_required=visa_alert,
            severity=visa_severity,
            reasons=["stub visa reason"] if visa_alert else [],
            validation_errors=[],
        ),
    )


class TestCleanGroup:
    """A fully compliant group produces no alerts but still notifies the primary traveler."""

    def test_response_shape_for_clean_group(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 2
        for result in body["traveler_results"]:
            assert result["passport_status"] == "OK"
            assert result["visa_status"] == "OK"
            assert result["alerts_sent"] == 0

    def test_traveler_results_preserve_names_and_order(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        results = response["body"]["traveler_results"]
        assert [r["skymiles_number"] for r in results] == [PRIMARY, COMPANION]
        assert results[0]["first_name"] == "First1"
        assert results[1]["last_name"] == "Last2"

    def test_no_alerts_persisted(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert _saved_alerts() == []

    def test_primary_receives_cleared_group_summary_only(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == "All 2 travelers are cleared for travel."
        assert _gcm(primary_msgs[0])["data"]["group_severity"] == "OK"
        assert _messages(endpoints[COMPANION]) == []


class TestPassportAlerts:
    """Passport issues persist a PASSPORT alert and push it to the affected traveler."""

    def test_expired_passport_creates_critical_alert(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert results[COMPANION]["passport_status"] == "CRITICAL"
        assert results[COMPANION]["alerts_sent"] == 1
        assert results[PRIMARY]["passport_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_passport_alert_record_persisted(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        items = _saved_alerts()
        assert len(items) == 1
        item = items[0]
        assert item["skymiles_number"] == COMPANION
        assert item["alert_type"] == "PASSPORT"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-GROUP"
        assert item["reasons"] == ["Passport has expired"]

    def test_passport_push_sent_to_affected_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        companion_msgs = _messages(endpoints[COMPANION])
        assert len(companion_msgs) == 1
        gcm = _gcm(companion_msgs[0])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert gcm["notification"]["body"] == "Passport has expired"
        assert gcm["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_missing_passport_expiry_creates_critical_alert(self, endpoints):
        event = _base_event(endpoints)
        del event["travelers"][1]["passport_expiry"]

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["passport_status"] == "CRITICAL"
        assert _saved_alerts()[0]["reasons"] == ["Passport information is incomplete"]

    def test_passport_below_country_minimum_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2030-07-01"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["passport_status"] == "CRITICAL"

    def test_passport_within_six_months_is_info(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2030-10-01"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["passport_status"] == "INFO"
        assert response["body"]["group_severity"] == "INFO"
        assert response["body"]["total_alerts_sent"] == 1


class TestVisaAlerts:
    """Visa issues persist a VISA alert and push it to the affected traveler."""

    def test_missing_visa_creates_critical_alert(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert results[COMPANION]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["passport_status"] == "OK"
        assert results[COMPANION]["alerts_sent"] == 1

    def test_visa_alert_record_persisted(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        group_handler(event, None)

        items = _saved_alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "VISA"
        assert items[0]["severity"] == "CRITICAL"
        assert items[0]["reasons"] == ["No visa on file for XA"]

    def test_visa_push_sent_to_affected_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        group_handler(event, None)

        companion_msgs = _messages(endpoints[COMPANION])
        assert len(companion_msgs) == 1
        gcm = _gcm(companion_msgs[0])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for XA"
        assert gcm["data"]["alert_type"] == "VISA"
        assert gcm["data"]["severity"] == "CRITICAL"

    def test_valid_visa_record_is_parsed_and_clears_alert(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["visa_records"] = [{
            "country_code": "XA",
            "visa_type": "tourist",
            "issue_date": "2029-01-01",
            "expiry_date": "2032-01-01",
            "visa_number": "V123",
        }]

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_visa_expiring_on_departure_is_warning(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["visa_records"] = [{
            "country_code": "XA",
            "visa_type": "tourist",
            "issue_date": "2029-01-01",
            "expiry_date": DEPARTURE,
            "visa_number": "V123",
        }]

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"


class TestCombinedAlerts:
    """Passport and visa alerts are counted per traveler and summed across the group."""

    def test_passport_and_visa_alerts_for_same_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["alerts_sent"] == 2
        assert sorted(i["alert_type"] for i in _saved_alerts()) == ["PASSPORT", "VISA"]
        assert len(_messages(endpoints[COMPANION])) == 2

    def test_alerts_summed_across_travelers(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        results = _results_by_member(response)
        assert results[PRIMARY]["alerts_sent"] == 1
        assert results[COMPANION]["alerts_sent"] == 1

    def test_group_severity_is_highest_across_travelers(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = "2030-10-01"
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "CRITICAL"

    def test_primary_summary_lists_travelers_needing_action(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == (
            "1 of 2 travelers need action: First2 Last2 - No visa on file for XA"
        )
        assert _gcm(primary_msgs[0])["data"]["group_severity"] == "CRITICAL"


class TestLayovers:
    """Layover segments only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_skipped(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment("XA", flight_number="DL100", is_layover=True),
            _segment("XB", flight_number="DL200"),
        ])
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_with_transit_visa_requirement_alerts(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment("XA", flight_number="DL100", is_layover=True),
            _segment("XB", flight_number="DL200"),
        ])
        event["requirements_override"]["XA"]["transit_visa_required"] = True
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "CRITICAL"

    def test_is_layover_defaults_to_false(self, endpoints):
        segment = _segment("XA")
        del segment["is_layover"]
        event = _base_event(endpoints, segments=[segment])
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "CRITICAL"


class TestItineraryChanges:
    """Re-evaluating a changed group itinerary reflects the new destinations."""

    def test_rerouted_itinerary_triggers_new_visa_alert(self, endpoints):
        original = _base_event(endpoints, segments=[_segment("XB")])
        original["travelers"][1]["nationality"] = "IN"
        changed = _base_event(endpoints, segments=[_segment("XA")])
        changed["travelers"][1]["nationality"] = "IN"

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert first["body"]["total_alerts_sent"] == 0
        assert second["body"]["total_alerts_sent"] == 1
        assert _results_by_member(second)[COMPANION]["visa_status"] == "CRITICAL"

    def test_later_departure_pushes_passport_below_minimum(self, endpoints):
        original = _base_event(endpoints)
        original["travelers"][1]["passport_expiry"] = "2031-06-01"
        changed = _base_event(endpoints, segments=[
            _segment("XA", departure_date="2031-04-01", arrival_date="2031-04-02"),
        ])
        changed["travelers"][1]["passport_expiry"] = "2031-06-01"

        first = group_handler(original, None)
        second = group_handler(changed, None)

        assert _results_by_member(first)[COMPANION]["passport_status"] == "OK"
        assert _results_by_member(second)[COMPANION]["passport_status"] == "CRITICAL"


class TestRequirementsOverride:
    """requirements_override entries replace defaults; missing fields use handler defaults."""

    def test_override_field_defaults_applied(self, endpoints):
        event = _base_event(endpoints, requirements_override={"XC": {}}, segments=[_segment("XC")])
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["passport_expiry"] = "2030-10-01"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert results[COMPANION]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["passport_status"] == "CRITICAL"
        assert results[PRIMARY]["visa_status"] == "CRITICAL"

    def test_override_exempt_nationalities_respected(self, endpoints):
        event = _base_event(endpoints)
        event["requirements_override"]["XA"]["visa_exempt_nationalities"] = ["US", "IN"]
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"

    def test_without_override_uses_module_country_requirements(self, endpoints):
        event = _base_event(endpoints)
        del event["requirements_override"]
        event["travelers"][1]["nationality"] = "IN"
        orig = group_mod.COUNTRY_REQUIREMENTS
        group_mod.COUNTRY_REQUIREMENTS = {
            "XA": TravelDocRequirements(
                country_code="XA",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        try:
            response = group_handler(event, None)
        finally:
            group_mod.COUNTRY_REQUIREMENTS = orig

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_empty_override_uses_module_country_requirements(self, endpoints):
        event = _base_event(endpoints, requirements_override={})
        event["travelers"][1]["nationality"] = "IN"
        orig = group_mod.COUNTRY_REQUIREMENTS
        group_mod.COUNTRY_REQUIREMENTS = {
            "XA": TravelDocRequirements(
                country_code="XA",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        try:
            response = group_handler(event, None)
        finally:
            group_mod.COUNTRY_REQUIREMENTS = orig

        assert _results_by_member(response)[COMPANION]["visa_status"] == "CRITICAL"


class TestTtl:
    """Persisted alerts expire 30 days after the group's last arrival."""

    def test_ttl_based_on_last_arrival(self, endpoints):
        event = _base_event(endpoints, segments=[
            _segment("XB", flight_number="DL100"),
            _segment("XA", flight_number="DL200", departure_date="2030-06-10", arrival_date="2030-06-11"),
        ])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        expected = int((datetime.combine(date(2030, 6, 11), datetime.min.time()) + timedelta(days=30)).timestamp())
        assert int(_saved_alerts()[0]["ttl"]) == expected


class TestPrimaryTravelerLookup:
    """The group summary is only sent when the primary traveler is in the group."""

    def test_unknown_primary_skips_group_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []
        assert _messages(endpoints[COMPANION]) == []

    def test_group_summary_sent_to_non_first_primary(self, endpoints):
        event = _base_event(endpoints, primary_traveler=COMPANION)

        group_handler(event, None)

        assert len(_messages(endpoints[COMPANION])) == 1
        assert _messages(endpoints[PRIMARY]) == []


class TestUnmatchedTravelerSummary:
    """Alerts for a summary with no matching traveler are persisted but not pushed."""

    def test_passport_and_visa_alerts_saved_without_push(self, endpoints):
        event = _base_event(endpoints)
        stub = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[_summary(
                "5555555555",
                passport_alert=True, passport_severity=AlertSeverity.CRITICAL,
                visa_alert=True, visa_severity=AlertSeverity.WARNING,
            )],
            group_severity=AlertSeverity.CRITICAL,
        )

        with _swap_evaluator(stub):
            response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        result = response["body"]["traveler_results"][0]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "WARNING"
        assert sorted(i["alert_type"] for i in _saved_alerts()) == ["PASSPORT", "VISA"]
        assert _messages(endpoints[COMPANION]) == []
        assert len(_messages(endpoints[PRIMARY])) == 1

    def test_alert_required_without_severity_is_skipped(self, endpoints):
        event = _base_event(endpoints)
        stub = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[_summary(COMPANION, passport_alert=True, visa_alert=True)],
            group_severity=None,
        )

        with _swap_evaluator(stub):
            response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"][0]["passport_status"] == "OK"
        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert _saved_alerts() == []
        assert _messages(endpoints[COMPANION]) == []
