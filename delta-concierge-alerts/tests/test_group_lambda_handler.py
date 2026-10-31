"""Critical-path tests for the group itinerary Lambda handler."""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import moto.core
import pytest
from moto.sns.models import sns_backends

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.models.types import (
    AlertSeverity,
    GroupEvaluationResult,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
)

PRIMARY = "1000000001"
COMPANION = "1000000002"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the alerts table and one SNS endpoint per traveler."""
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
            sm: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"token-{sm}",
            )["EndpointArn"]
            for sm in (PRIMARY, COMPANION)
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
    """Map of skymiles_number -> moto SNS endpoint ARN."""
    return _mock_aws


def _messages(endpoint_arn: str) -> list[dict]:
    """Return decoded SNS messages published to an endpoint."""
    backend = sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


def _stored_alerts(skymiles_number: str) -> list[dict]:
    return alert_store_mod.get_alerts_by_member(skymiles_number)


def _traveler(skymiles_number: str, endpoint_arn: str, **overrides) -> dict:
    traveler = {
        "skymiles_number": skymiles_number,
        "first_name": f"First{skymiles_number[-1]}",
        "last_name": "Traveler",
        "nationality": "US",
        "passport_number": f"P{skymiles_number}",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    traveler.update(overrides)
    return traveler


def _segment(**overrides) -> dict:
    segment = {
        "flight_number": "DL200",
        "origin": "ATL",
        "destination": "XA",
        "departure_date": "2031-06-01",
        "arrival_date": "2031-06-02",
    }
    segment.update(overrides)
    return segment


def _override(**overrides) -> dict:
    reqs = {
        "country_code": "XA",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": ["US"],
    }
    reqs.update(overrides)
    return {"XA": reqs}


def _base_event(endpoints: dict, **overrides) -> dict:
    """Build a valid, alert-free two-traveler group event."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(COMPANION, endpoints[COMPANION]),
        ],
        "requirements_override": _override(),
    }
    event.update(overrides)
    return event


def _results_by_member(response: dict) -> dict:
    return {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}


class TestCleanGroup:
    """A fully compliant group produces no alerts, only an all-clear summary to the primary."""

    def test_returns_ok_statuses(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [
            {
                "skymiles_number": PRIMARY,
                "first_name": "First1",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            },
            {
                "skymiles_number": COMPANION,
                "first_name": "First2",
                "last_name": "Traveler",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            },
        ]

    def test_no_alerts_persisted(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert _stored_alerts(PRIMARY) == []
        assert _stored_alerts(COMPANION) == []

    def test_primary_gets_all_clear_summary_only(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == "All 2 travelers are cleared for travel."
        assert _messages(endpoints[COMPANION]) == []


class TestPassportAlerts:
    """A passport issue for one traveler is persisted and pushed only to that traveler."""

    def test_expired_passport_alerts_that_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"
        assert results[COMPANION]["passport_status"] == "CRITICAL"
        assert results[COMPANION]["alerts_sent"] == 1
        assert results[PRIMARY]["passport_status"] == "OK"
        assert results[PRIMARY]["alerts_sent"] == 0

    def test_passport_alert_persisted(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        alerts = _stored_alerts(COMPANION)
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["alert_type"] == "PASSPORT"
        assert alert["severity"] == "CRITICAL"
        assert alert["reasons"] == ["Passport has expired"]
        assert alert["itinerary_ref"] == "DL-GROUP"
        assert alert["status"] == "ACTIVE"
        assert _stored_alerts(PRIMARY) == []

    def test_passport_push_sent_to_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        msgs = _messages(endpoints[COMPANION])
        assert len(msgs) == 1
        gcm = json.loads(msgs[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert gcm["notification"]["body"] == "Passport has expired"
        assert gcm["data"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-GROUP",
        }

    def test_primary_summary_lists_traveler_needing_action(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == (
            "1 of 2 travelers need action: First2 Traveler - Passport has expired"
        )

    def test_missing_passport_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = None

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["passport_status"] == "CRITICAL"
        assert _stored_alerts(COMPANION)[0]["reasons"] == ["Passport information is incomplete"]

    def test_below_country_validity_window(self, endpoints):
        event = _base_event(endpoints, requirements_override=_override(passport_validity_months=6))
        event["travelers"][1]["passport_expiry"] = "2031-09-01"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["passport_status"] == "CRITICAL"
        assert "6-month validity requirement" in _stored_alerts(COMPANION)[0]["reasons"][0]

    def test_within_country_window_but_under_six_months_is_info(self, endpoints):
        event = _base_event(endpoints, requirements_override=_override(passport_validity_months=3))
        event["travelers"][1]["passport_expiry"] = "2031-10-01"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert results[COMPANION]["passport_status"] == "INFO"
        assert response["body"]["group_severity"] == "INFO"
        assert _stored_alerts(COMPANION)[0]["severity"] == "INFO"


class TestVisaAlerts:
    """A visa issue for one traveler is persisted and pushed only to that traveler."""

    def test_missing_visa_alerts_that_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert response["body"]["total_alerts_sent"] == 1
        assert results[COMPANION]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["passport_status"] == "OK"
        assert results[COMPANION]["alerts_sent"] == 1
        assert results[PRIMARY]["visa_status"] == "OK"

    def test_visa_alert_persisted(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        group_handler(event, None)

        alerts = _stored_alerts(COMPANION)
        assert len(alerts) == 1
        assert alerts[0]["alert_type"] == "VISA"
        assert alerts[0]["severity"] == "CRITICAL"
        assert alerts[0]["reasons"] == ["No visa on file for XA"]

    def test_visa_push_sent_to_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        group_handler(event, None)

        msgs = _messages(endpoints[COMPANION])
        assert len(msgs) == 1
        gcm = json.loads(msgs[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for XA"
        assert gcm["data"]["alert_type"] == "VISA"
        assert gcm["data"]["severity"] == "CRITICAL"

    def test_valid_visa_record_clears_alert(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["visa_records"] = [{
            "country_code": "XA",
            "visa_type": "TOURIST",
            "issue_date": "2030-01-01",
            "expiry_date": "2035-01-01",
            "visa_number": "V1",
        }]

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_expired_visa_record_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["visa_records"] = [{
            "country_code": "XA",
            "visa_type": "TOURIST",
            "issue_date": "2019-01-01",
            "expiry_date": "2020-01-01",
            "visa_number": "V1",
        }]

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "CRITICAL"
        assert _stored_alerts(COMPANION)[0]["reasons"] == ["Visa for XA has expired"]

    def test_visa_expiring_on_travel_date_is_warning(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["visa_records"] = [{
            "country_code": "XA",
            "visa_type": "TOURIST",
            "issue_date": "2030-01-01",
            "expiry_date": "2031-06-01",
            "visa_number": "V1",
        }]

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"


class TestLayovers:
    """Layovers only trigger visa alerts when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(is_layover=True)])
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_with_transit_visa_requirement_alerts(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[_segment(is_layover=True)],
            requirements_override=_override(transit_visa_required=True),
        )
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1


class TestCombinedAlerts:
    """Passport and visa alerts are counted independently per traveler and per group."""

    def test_both_alerts_for_one_traveler(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        result = _results_by_member(response)[COMPANION]
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        assert sorted(a["alert_type"] for a in _stored_alerts(COMPANION)) == ["PASSPORT", "VISA"]
        assert len(_messages(endpoints[COMPANION])) == 2

    def test_alerts_across_multiple_travelers(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert response["body"]["total_alerts_sent"] == 2
        assert results[PRIMARY]["alerts_sent"] == 1
        assert results[COMPANION]["alerts_sent"] == 1
        # primary receives its personal alert plus the group summary
        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 2
        assert primary_msgs[-1]["default"].startswith("2 of 2 travelers need action:")


class TestRequirementsOverride:
    """Overrides replace defaults; missing override fields fall back to handler defaults."""

    def test_override_defaults_require_visa(self, endpoints):
        event = _base_event(endpoints, requirements_override={"XA": {}})

        response = group_handler(event, None)

        # requires_visa defaults True with no exemptions -> US travelers need a visa
        results = _results_by_member(response)
        assert results[PRIMARY]["visa_status"] == "CRITICAL"
        assert results[COMPANION]["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 2

    def test_override_visa_not_required(self, endpoints):
        event = _base_event(endpoints, requirements_override=_override(requires_visa=False))
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["visa_status"] == "OK"

    def test_without_override_uses_default_requirements(self, endpoints):
        event = _base_event(endpoints, requirements_override=None)

        response = group_handler(event, None)

        # unknown country "XA" falls back to the default (visa-required) requirements
        results = _results_by_member(response)
        assert results[PRIMARY]["passport_status"] == "OK"
        assert response["statusCode"] == 200

    def test_override_absent_key(self, endpoints):
        event = _base_event(endpoints)
        del event["requirements_override"]
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert _results_by_member(response)[COMPANION]["passport_status"] == "CRITICAL"


class TestPrimaryTraveler:
    """The group summary is only sent when the primary traveler is in the group."""

    def test_unknown_primary_skips_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []
        assert _messages(endpoints[COMPANION]) == []

    def test_summary_goes_to_designated_primary(self, endpoints):
        event = _base_event(endpoints, primary_traveler=COMPANION)

        group_handler(event, None)

        assert len(_messages(endpoints[COMPANION])) == 1
        assert _messages(endpoints[PRIMARY]) == []


class TestItineraryChange:
    """TTL tracks the latest arrival, so a changed itinerary yields a later expiry."""

    def _ttl_for(self, endpoints, segments) -> int:
        event = _base_event(endpoints, segments=segments)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"
        group_handler(event, None)
        alerts = sorted(_stored_alerts(COMPANION), key=lambda a: a["created_at"])
        return int(alerts[-1]["ttl"])

    def test_ttl_is_30_days_after_last_arrival(self, endpoints):
        segments = [
            _segment(arrival_date="2031-06-02"),
            _segment(flight_number="DL201", origin="XA", destination="ATL",
                     departure_date="2031-06-10", arrival_date="2031-06-11"),
        ]

        ttl = self._ttl_for(endpoints, segments)

        expected = int((datetime(2031, 6, 11) + timedelta(days=30)).timestamp())
        assert ttl == expected

    def test_changed_itinerary_extends_ttl(self, endpoints):
        original = self._ttl_for(endpoints, [_segment(arrival_date="2031-06-02")])

        changed = self._ttl_for(
            endpoints,
            [_segment(departure_date="2031-07-01", arrival_date="2031-07-02")],
        )

        assert changed > original


class TestMissingTravelerForSummary:
    """Alerts are still persisted and counted when the summary's traveler can't be found."""

    def test_alerts_saved_without_push_for_unknown_traveler(self, endpoints):
        event = _base_event(endpoints)
        ghost = "5555555555"
        group = _parse_group_itinerary(event)
        profile = group.travelers[0]
        segments = group.segments
        passport = PassportEvaluation(
            profile=profile, segments_evaluated=segments, is_alert_required=True,
            severity=AlertSeverity.CRITICAL, reasons=["Passport has expired"],
            validation_errors=[],
        )
        visa = VisaEvaluation(
            profile=profile, segments_evaluated=segments, is_alert_required=True,
            severity=AlertSeverity.WARNING, reasons=["Visa for XA expires on date of travel"],
            validation_errors=[],
        )
        fake_result = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number=ghost, first_name="Ghost", last_name="Traveler",
                passport_result=passport, visa_result=visa,
            )],
            group_severity=AlertSeverity.CRITICAL,
        )
        orig_eval = group_mod.evaluate_group_itinerary
        group_mod.evaluate_group_itinerary = lambda group, requirements: fake_result

        try:
            response = group_handler(event, None)
        finally:
            group_mod.evaluate_group_itinerary = orig_eval

        body = response["body"]
        assert body["total_alerts_sent"] == 2
        assert body["group_severity"] == "CRITICAL"
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "WARNING"
        assert sorted(a["alert_type"] for a in _stored_alerts(ghost)) == ["PASSPORT", "VISA"]
        # only the group summary reaches the primary; no personal pushes were sent
        assert len(_messages(endpoints[PRIMARY])) == 1
        assert _messages(endpoints[COMPANION]) == []

    def test_alert_required_without_severity_is_skipped(self, endpoints):
        event = _base_event(endpoints)
        group = _parse_group_itinerary(event)
        profile = group.travelers[1]
        no_sev = dict(
            profile=profile, segments_evaluated=group.segments, is_alert_required=True,
            severity=None, reasons=["odd"], validation_errors=[],
        )
        fake_result = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number=COMPANION, first_name="First2", last_name="Traveler",
                passport_result=PassportEvaluation(**no_sev),
                visa_result=VisaEvaluation(**no_sev),
            )],
            group_severity=None,
        )
        orig_eval = group_mod.evaluate_group_itinerary
        group_mod.evaluate_group_itinerary = lambda group, requirements: fake_result

        try:
            response = group_handler(event, None)
        finally:
            group_mod.evaluate_group_itinerary = orig_eval

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["traveler_results"][0]["passport_status"] == "OK"
        assert _stored_alerts(COMPANION) == []


class TestParseGroupItinerary:
    """Event payloads deserialize into a GroupItinerary with typed fields."""

    def test_parses_segments_and_travelers(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(), _segment(is_layover=True)])

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert [t.skymiles_number for t in group.travelers] == [PRIMARY, COMPANION]
        assert group.travelers[0].passport_expiry == date(2040, 1, 1)
        assert group.segments[0].departure_date == date(2031, 6, 1)
        assert group.segments[0].arrival_date == date(2031, 6, 2)
        assert group.segments[0].is_layover is False
        assert group.segments[1].is_layover is True


class TestFindTraveler:
    """Traveler lookup by SkyMiles number returns the match or None."""

    def test_finds_matching_traveler(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        found = _find_traveler(group, COMPANION)

        assert found is group.travelers[1]

    def test_returns_none_when_absent(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, "0000000000") is None

    def test_returns_none_for_empty_group(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints, travelers=[]))

        assert _find_traveler(group, PRIMARY) is None
