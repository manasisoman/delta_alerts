"""Tests for the group itinerary Lambda handler (group_lambda_handler)."""

import json
from datetime import date

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.handlers.lambda_handler import _compute_ttl
from src.models.types import (
    AlertSeverity,
    GroupEvaluationResult,
    Itinerary,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
)

PRIMARY = "1000000001"
SECOND = "1000000002"
THIRD = "1000000003"

DEPARTURE = "2030-06-01"
ARRIVAL = "2030-06-02"

# Made-up country codes isolate tests from production COUNTRY_REQUIREMENTS data.
DEFAULT_OVERRIDE = {
    "XA": {
        "country_code": "XA",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": ["US"],
    },
    "XB": {
        "country_code": "XB",
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": [],
    },
}


@pytest.fixture(autouse=True)
def aws():
    """Activate moto, create the DynamoDB table and an SNS platform app, and swap module clients."""
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
            Name="GroupTestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield {"sns": sns, "app_arn": app_arn, "table": ddb.Table("ConciergeAlerts")}
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoints(aws):
    """One SNS platform endpoint per traveler so pushes can be attributed."""
    return {
        sm: aws["sns"].create_platform_endpoint(
            PlatformApplicationArn=aws["app_arn"], Token=f"token-{sm}"
        )["EndpointArn"]
        for sm in (PRIMARY, SECOND, THIRD)
    }


def _messages(endpoint_arn: str) -> list[str]:
    """Return the default-text bodies of all messages pushed to an endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    bodies = []
    for raw in endpoint.messages.values():
        try:
            bodies.append(json.loads(raw).get("default", raw))
        except (ValueError, AttributeError):
            bodies.append(raw)
    return bodies


def _saved_alerts(table) -> list[dict]:
    return table.scan()["Items"]


def _traveler(
    skymiles_number: str,
    endpoint_arn: str,
    nationality: str = "US",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
    first_name: str = "Test",
    last_name: str = "Traveler",
) -> dict:
    data = {
        "skymiles_number": skymiles_number,
        "first_name": first_name,
        "last_name": last_name,
        "nationality": nationality,
        "passport_number": "P1234567",
        "passport_expiry": passport_expiry,
        "endpoint_arn": endpoint_arn,
        "visa_records": visa_records or [],
    }
    return data


def _segment(
    destination: str = "XA",
    departure: str = DEPARTURE,
    arrival: str = ARRIVAL,
    flight_number: str = "DL100",
    is_layover: bool | None = None,
) -> dict:
    seg = {
        "flight_number": flight_number,
        "origin": "ATL",
        "destination": destination,
        "departure_date": departure,
        "arrival_date": arrival,
    }
    if is_layover is not None:
        seg["is_layover"] = is_layover
    return seg


def _visa(country_code: str = "XA", expiry: str = "2035-01-01") -> dict:
    return {
        "country_code": country_code,
        "visa_type": "tourist",
        "issue_date": "2025-01-01",
        "expiry_date": expiry,
        "visa_number": "V0001",
    }


def _base_event(endpoints: dict, **overrides) -> dict:
    """A clean two-traveler group flying to XA; override any top-level field."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": [
            _traveler(PRIMARY, endpoints[PRIMARY], first_name="Pat", last_name="Primary"),
            _traveler(SECOND, endpoints[SECOND], first_name="Sam", last_name="Second"),
        ],
        "requirements_override": DEFAULT_OVERRIDE,
    }
    event.update(overrides)
    return event


def _results_by_member(response: dict) -> dict:
    return {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}


class TestCleanGroup:
    """A group with fully valid documents produces no alerts and an all-clear summary."""

    def test_returns_200_with_ok_summary(self, endpoints):
        response = group_handler(_base_event(endpoints), None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"

    def test_every_traveler_reports_ok(self, endpoints):
        response = group_handler(_base_event(endpoints), None)

        results = _results_by_member(response)
        assert set(results) == {PRIMARY, SECOND}
        for r in results.values():
            assert r["passport_status"] == "OK"
            assert r["visa_status"] == "OK"
            assert r["alerts_sent"] == 0

    def test_no_alerts_persisted(self, aws, endpoints):
        group_handler(_base_event(endpoints), None)

        assert _saved_alerts(aws["table"]) == []

    def test_primary_receives_all_clear_group_notification(self, endpoints):
        group_handler(_base_event(endpoints), None)

        assert _messages(endpoints[PRIMARY]) == ["All 2 travelers are cleared for travel."]
        assert _messages(endpoints[SECOND]) == []

    def test_traveler_results_preserve_names_and_order(self, endpoints):
        response = group_handler(_base_event(endpoints), None)

        results = response["body"]["traveler_results"]
        assert [r["skymiles_number"] for r in results] == [PRIMARY, SECOND]
        assert (results[0]["first_name"], results[0]["last_name"]) == ("Pat", "Primary")
        assert (results[1]["first_name"], results[1]["last_name"]) == ("Sam", "Second")


class TestPassportAlerts:
    """Passport problems are persisted as PASSPORT alerts and pushed to the affected traveler."""

    def _event_with_second(self, endpoints, **traveler_kwargs):
        return _base_event(
            endpoints,
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY]),
                _traveler(SECOND, endpoints[SECOND], **traveler_kwargs),
            ],
        )

    def test_expired_passport_is_critical(self, endpoints):
        event = self._event_with_second(endpoints, passport_expiry="2020-01-01")

        response = group_handler(event, None)

        result = _results_by_member(response)[SECOND]
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_expired_passport_alert_is_persisted(self, aws, endpoints):
        event = self._event_with_second(endpoints, passport_expiry="2020-01-01")

        group_handler(event, None)

        alerts = _saved_alerts(aws["table"])
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["skymiles_number"] == SECOND
        assert alert["alert_type"] == "PASSPORT"
        assert alert["severity"] == "CRITICAL"
        assert alert["status"] == "ACTIVE"
        assert alert["itinerary_ref"] == "DL-GROUP"
        assert alert["reasons"] == ["Passport has expired"]

    def test_expired_passport_pushes_to_affected_traveler(self, endpoints):
        event = self._event_with_second(endpoints, passport_expiry="2020-01-01")

        group_handler(event, None)

        assert _messages(endpoints[SECOND]) == ["Passport has expired"]

    def test_primary_group_summary_lists_affected_traveler(self, endpoints):
        event = self._event_with_second(
            endpoints, passport_expiry="2020-01-01", first_name="Jane", last_name="Doe"
        )

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0].startswith("1 of 2 travelers need action")
        assert "Jane Doe - Passport has expired" in primary_msgs[0]

    def test_missing_passport_expiry_is_critical(self, endpoints):
        event = self._event_with_second(endpoints, passport_expiry=None)

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["passport_status"] == "CRITICAL"
        assert _messages(endpoints[SECOND]) == ["Passport information is incomplete"]

    def test_passport_below_country_validity_requirement(self, endpoints):
        event = self._event_with_second(endpoints, passport_expiry="2030-09-01")

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["passport_status"] == "CRITICAL"
        assert "6-month validity requirement" in _messages(endpoints[SECOND])[0]

    def test_info_passport_alert_is_still_saved_and_pushed(self, aws, endpoints):
        # XB needs 3 months; 4 months of validity triggers the 6-month INFO nudge.
        event = self._event_with_second(endpoints, passport_expiry="2030-10-01")
        event["segments"] = [_segment(destination="XB")]

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["passport_status"] == "INFO"
        assert response["body"]["group_severity"] == "INFO"
        assert response["body"]["total_alerts_sent"] == 1
        assert [a["severity"] for a in _saved_alerts(aws["table"])] == ["INFO"]
        assert len(_messages(endpoints[SECOND])) == 1


class TestVisaAlerts:
    """Visa problems are persisted as VISA alerts and pushed to the affected traveler."""

    def _event_with_second(self, endpoints, **traveler_kwargs):
        return _base_event(
            endpoints,
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY]),
                _traveler(SECOND, endpoints[SECOND], **traveler_kwargs),
            ],
        )

    def test_missing_visa_is_critical(self, aws, endpoints):
        event = self._event_with_second(endpoints, nationality="IN")

        response = group_handler(event, None)

        result = _results_by_member(response)[SECOND]
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "OK"
        assert result["alerts_sent"] == 1
        alerts = _saved_alerts(aws["table"])
        assert [(a["alert_type"], a["severity"]) for a in alerts] == [("VISA", "CRITICAL")]
        assert _messages(endpoints[SECOND]) == ["No visa on file for XA"]

    def test_visa_expiring_on_travel_date_is_warning(self, endpoints):
        event = self._event_with_second(
            endpoints, nationality="IN", visa_records=[_visa(expiry=DEPARTURE)]
        )

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"
        assert _messages(endpoints[SECOND]) == ["Visa for XA expires on date of travel"]

    def test_valid_visa_produces_no_alert(self, aws, endpoints):
        event = self._event_with_second(endpoints, nationality="IN", visa_records=[_visa()])

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["visa_status"] == "OK"
        assert _saved_alerts(aws["table"]) == []

    def test_visa_exempt_nationality_produces_no_alert(self, endpoints):
        event = self._event_with_second(endpoints, nationality="US")

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["visa_status"] == "OK"

    def test_country_not_requiring_visa_produces_no_alert(self, endpoints):
        event = self._event_with_second(endpoints, nationality="IN")
        event["segments"] = [_segment(destination="XB")]

        response = group_handler(event, None)

        assert _results_by_member(response)[SECOND]["visa_status"] == "OK"


class TestCombinedAlerts:
    """Alert counts and group severity aggregate across documents and travelers."""

    def test_passport_and_visa_alerts_for_one_traveler(self, aws, endpoints):
        event = _base_event(
            endpoints,
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY]),
                _traveler(SECOND, endpoints[SECOND], nationality="IN", passport_expiry="2020-01-01"),
            ],
        )

        response = group_handler(event, None)

        result = _results_by_member(response)[SECOND]
        assert result["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        assert sorted(a["alert_type"] for a in _saved_alerts(aws["table"])) == ["PASSPORT", "VISA"]
        assert len(_messages(endpoints[SECOND])) == 2

    def test_alerts_across_multiple_travelers(self, endpoints):
        event = _base_event(
            endpoints,
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01"),
                _traveler(SECOND, endpoints[SECOND], nationality="IN"),
                _traveler(THIRD, endpoints[THIRD]),
            ],
        )

        response = group_handler(event, None)

        results = _results_by_member(response)
        assert results[PRIMARY]["alerts_sent"] == 1
        assert results[SECOND]["alerts_sent"] == 1
        assert results[THIRD]["alerts_sent"] == 0
        assert response["body"]["total_alerts_sent"] == 2
        assert _messages(endpoints[THIRD]) == []

    def test_primary_gets_personal_alert_and_group_summary(self, endpoints):
        event = _base_event(
            endpoints,
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01"),
                _traveler(SECOND, endpoints[SECOND]),
            ],
        )

        group_handler(event, None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 2
        assert primary_msgs[0] == "Passport has expired"
        assert primary_msgs[1].startswith("1 of 2 travelers need action")

    def test_group_severity_is_highest_across_travelers(self, endpoints):
        event = _base_event(
            endpoints,
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN", visa_records=[_visa(expiry=DEPARTURE)]),
                _traveler(SECOND, endpoints[SECOND], nationality="IN"),
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "CRITICAL"


class TestPrimaryTravelerNotification:
    """The group summary goes only to the primary traveler, and only if they are in the group."""

    def test_unknown_primary_skips_group_notification(self, endpoints):
        event = _base_event(endpoints, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []
        assert _messages(endpoints[SECOND]) == []

    def test_non_first_primary_receives_summary(self, endpoints):
        event = _base_event(endpoints, primary_traveler=SECOND)

        group_handler(event, None)

        assert _messages(endpoints[SECOND]) == ["All 2 travelers are cleared for travel."]
        assert _messages(endpoints[PRIMARY]) == []


class TestRequirementsOverride:
    """requirements_override entries are merged into the default country requirements."""

    def test_override_defaults_require_visa_and_six_months(self, endpoints):
        event = _base_event(
            endpoints,
            requirements_override={"XC": {}},
            segments=[_segment(destination="XC")],
            travelers=[
                _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN", passport_expiry="2030-09-01"),
            ],
        )

        response = group_handler(event, None)

        result = _results_by_member(response)[PRIMARY]
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "CRITICAL"

    def test_override_replaces_default_country_entry(self, endpoints):
        # Default DE data exempts US nationals; the override removes that exemption.
        event = _base_event(
            endpoints,
            requirements_override={"DE": {"requires_visa": True, "visa_exempt_nationalities": []}},
            segments=[_segment(destination="DE")],
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], nationality="US")],
        )

        response = group_handler(event, None)

        assert _results_by_member(response)[PRIMARY]["visa_status"] == "CRITICAL"

    def test_no_override_uses_default_country_requirements(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[_segment(destination="DE")],
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], nationality="US")],
        )
        del event["requirements_override"]

        response = group_handler(event, None)

        assert _results_by_member(response)[PRIMARY]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_empty_override_uses_default_country_requirements(self, endpoints):
        event = _base_event(
            endpoints,
            requirements_override={},
            segments=[_segment(destination="DE")],
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], nationality="US")],
        )

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "OK"


class TestLayovers:
    """Layover segments only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(destination="XA", is_layover=True, flight_number="DL1"),
                _segment(destination="XB", flight_number="DL2", departure="2030-06-02", arrival="2030-06-03"),
            ],
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")],
        )

        response = group_handler(event, None)

        assert _results_by_member(response)[PRIMARY]["visa_status"] == "OK"

    def test_layover_with_transit_visa_requirement_alerts(self, endpoints):
        override = {**DEFAULT_OVERRIDE, "XA": {**DEFAULT_OVERRIDE["XA"], "transit_visa_required": True}}
        event = _base_event(
            endpoints,
            requirements_override=override,
            segments=[
                _segment(destination="XA", is_layover=True, flight_number="DL1"),
                _segment(destination="XB", flight_number="DL2", departure="2030-06-02", arrival="2030-06-03"),
            ],
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")],
        )

        response = group_handler(event, None)

        assert _results_by_member(response)[PRIMARY]["visa_status"] == "CRITICAL"


class TestAlertTTL:
    """Persisted alerts expire 30 days after the group's last arrival."""

    def test_ttl_uses_latest_arrival_across_segments(self, aws, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(destination="XB", flight_number="DL1"),
                _segment(destination="XB", flight_number="DL2", departure="2030-06-10", arrival="2030-06-11"),
            ],
            travelers=[_traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2020-01-01")],
        )

        group_handler(event, None)

        expected = _compute_ttl(Itinerary(
            confirmation_number="DL-GROUP",
            segments=_parse_group_itinerary(event).segments,
        ))
        alerts = _saved_alerts(aws["table"])
        assert len(alerts) == 1
        assert int(alerts[0]["ttl"]) == expected


class TestParseGroupItinerary:
    """_parse_group_itinerary deserializes segments, travelers, and the primary traveler."""

    def test_parses_segments(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(destination="XA", is_layover=True, flight_number="DL1"),
                _segment(destination="XB", flight_number="DL2", departure="2030-06-02", arrival="2030-06-03"),
            ],
        )

        group = _parse_group_itinerary(event)

        assert [s.flight_number for s in group.segments] == ["DL1", "DL2"]
        assert group.segments[0].is_layover is True
        assert group.segments[1].is_layover is False
        assert group.segments[1].departure_date == date(2030, 6, 2)
        assert group.segments[1].arrival_date == date(2030, 6, 3)

    def test_parses_travelers_and_primary(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["visa_records"] = [_visa()]
        event["travelers"][1]["passport_expiry"] = None

        group = _parse_group_itinerary(event)

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert [t.skymiles_number for t in group.travelers] == [PRIMARY, SECOND]
        assert group.travelers[0].passport_expiry == date(2035, 1, 1)
        assert group.travelers[1].passport_expiry is None
        assert group.travelers[1].visa_records[0].expiry_date == date(2035, 1, 1)
        assert group.travelers[1].endpoint_arn == endpoints[SECOND]

    def test_missing_required_field_raises(self, endpoints):
        event = _base_event(endpoints)
        del event["primary_traveler"]

        with pytest.raises(KeyError):
            _parse_group_itinerary(event)


class TestFindTraveler:
    """_find_traveler returns the matching profile or None."""

    def test_returns_matching_traveler(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        traveler = _find_traveler(group, SECOND)

        assert traveler is not None
        assert traveler.skymiles_number == SECOND

    def test_returns_none_when_absent(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, "0000000000") is None

    def test_returns_none_for_empty_group(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints, travelers=[]))

        assert _find_traveler(group, PRIMARY) is None


def _passport_eval(required: bool, severity, reasons):
    return PassportEvaluation(
        profile=None, segments_evaluated=[], is_alert_required=required,
        severity=severity, reasons=reasons, validation_errors=[],
    )


def _visa_eval(required: bool, severity, reasons):
    return VisaEvaluation(
        profile=None, segments_evaluated=[], is_alert_required=required,
        severity=severity, reasons=reasons, validation_errors=[],
    )


@pytest.fixture
def stub_evaluator():
    """Swap the group evaluator for one returning canned summaries; restore afterwards."""
    original = group_mod.evaluate_group_itinerary
    holder: dict = {}

    def _fake(group, requirements):
        holder["requirements"] = requirements
        return holder["result"]

    group_mod.evaluate_group_itinerary = _fake
    try:
        yield holder
    finally:
        group_mod.evaluate_group_itinerary = original


class TestEvaluatorEdgeCases:
    """Handler behavior for evaluator outputs the real evaluators don't normally produce."""

    def test_summary_for_unknown_traveler_saves_alerts_without_push(self, aws, endpoints, stub_evaluator):
        stub_evaluator["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number="0000000000", first_name="Ghost", last_name="Traveler",
                passport_result=_passport_eval(True, AlertSeverity.CRITICAL, ["Passport has expired"]),
                visa_result=_visa_eval(True, AlertSeverity.CRITICAL, ["No visa on file for XA"]),
            )],
            group_severity=AlertSeverity.CRITICAL,
        )

        response = group_handler(_base_event(endpoints), None)

        assert response["body"]["total_alerts_sent"] == 2
        assert {a["skymiles_number"] for a in _saved_alerts(aws["table"])} == {"0000000000"}
        assert _messages(endpoints[SECOND]) == []
        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert "Ghost Traveler" in primary_msgs[0]

    def test_alert_required_without_severity_is_skipped(self, aws, endpoints, stub_evaluator):
        stub_evaluator["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number=PRIMARY, first_name="Pat", last_name="Primary",
                passport_result=_passport_eval(True, None, ["odd"]),
                visa_result=_visa_eval(True, None, ["odd"]),
            )],
            group_severity=None,
        )

        response = group_handler(_base_event(endpoints), None)

        result = _results_by_member(response)[PRIMARY]
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0
        assert _saved_alerts(aws["table"]) == []

    def test_severity_without_alert_required_is_reported_but_not_sent(self, aws, endpoints, stub_evaluator):
        stub_evaluator["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP",
            traveler_summaries=[TravelerAlertSummary(
                skymiles_number=PRIMARY, first_name="Pat", last_name="Primary",
                passport_result=_passport_eval(False, AlertSeverity.INFO, []),
                visa_result=_visa_eval(False, AlertSeverity.WARNING, []),
            )],
            group_severity=AlertSeverity.WARNING,
        )

        response = group_handler(_base_event(endpoints), None)

        result = _results_by_member(response)[PRIMARY]
        assert result["passport_status"] == "INFO"
        assert result["visa_status"] == "WARNING"
        assert result["alerts_sent"] == 0
        assert response["body"]["group_severity"] == "WARNING"
        assert _saved_alerts(aws["table"]) == []

    def test_merged_requirements_include_defaults_and_overrides(self, endpoints, stub_evaluator):
        stub_evaluator["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP", traveler_summaries=[], group_severity=None,
        )

        group_handler(_base_event(endpoints), None)

        reqs = stub_evaluator["requirements"]
        assert "DE" in reqs
        assert reqs["XA"].visa_exempt_nationalities == ["US"]
        assert reqs["XB"].requires_visa is False
        assert reqs["XB"].passport_validity_months == 3

    def test_override_country_code_defaults_to_key(self, endpoints, stub_evaluator):
        stub_evaluator["result"] = GroupEvaluationResult(
            confirmation_number="DL-GROUP", traveler_summaries=[], group_severity=None,
        )

        group_handler(_base_event(endpoints, requirements_override={"XC": {}}), None)

        xc = stub_evaluator["requirements"]["XC"]
        assert xc.country_code == "XC"
        assert xc.requires_visa is True
        assert xc.transit_visa_required is False
        assert xc.passport_validity_months == 6
        assert xc.visa_exempt_nationalities == []
