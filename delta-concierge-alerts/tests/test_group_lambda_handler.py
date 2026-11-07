"""Tests for the group itinerary Lambda handler (group_lambda_handler)."""

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
SECOND = "1000000002"
THIRD = "1000000003"

# Made-up country codes so tests are isolated from production COUNTRY_REQUIREMENTS.
VISA_COUNTRY = {
    "XA": {
        "country_code": "XA",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": ["US"],
    }
}
NO_VISA_COUNTRY = {
    "XB": {
        "country_code": "XB",
        "requires_visa": False,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": [],
    }
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
        endpoints = {
            sm: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn, Token=f"token-{sm}",
            )["EndpointArn"]
            for sm in (PRIMARY, SECOND, THIRD)
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
    """Map of SkyMiles number -> moto SNS endpoint ARN."""
    return _mock_aws


def _traveler(skymiles_number: str, endpoint_arn: str, **overrides) -> dict:
    """Build a clean traveler payload (US national, far-future passport)."""
    data = {
        "skymiles_number": skymiles_number,
        "first_name": f"First{skymiles_number[-1]}",
        "last_name": f"Last{skymiles_number[-1]}",
        "nationality": "US",
        "passport_number": f"P{skymiles_number}",
        "passport_expiry": "2035-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    data.update(overrides)
    return data


def _segment(**overrides) -> dict:
    seg = {
        "flight_number": "DL200",
        "origin": "JFK",
        "destination": "XA",
        "departure_date": "2030-06-01",
        "arrival_date": "2030-06-02",
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


def _base_event(endpoints: dict, travelers: list[dict] | None = None, **overrides) -> dict:
    """Build a valid two-traveler group event with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": travelers if travelers is not None else [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND]),
        ],
        "requirements_override": dict(VISA_COUNTRY),
    }
    event.update(overrides)
    return event


def _messages(endpoint_arn: str) -> list[dict]:
    """Return decoded JSON messages published to a moto SNS endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[endpoint_arn].messages.values()]


def _alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


def _result_for(response: dict, skymiles_number: str) -> dict:
    return next(
        r for r in response["body"]["traveler_results"]
        if r["skymiles_number"] == skymiles_number
    )


class TestCleanGroup:
    """A group with valid documents produces no alerts and an OK summary."""

    def test_response_shape(self, endpoints):
        response = group_handler(_base_event(endpoints), None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 2

    def test_traveler_results_are_ok(self, endpoints):
        response = group_handler(_base_event(endpoints), None)

        result = _result_for(response, SECOND)
        assert result == {
            "skymiles_number": SECOND,
            "first_name": "First2",
            "last_name": "Last2",
            "passport_status": "OK",
            "visa_status": "OK",
            "alerts_sent": 0,
        }

    def test_no_alerts_persisted(self, endpoints):
        group_handler(_base_event(endpoints), None)

        assert _alerts() == []

    def test_primary_gets_cleared_summary_only(self, endpoints):
        group_handler(_base_event(endpoints), None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == "All 2 travelers are cleared for travel."
        assert _messages(endpoints[SECOND]) == []


class TestVisaAlerts:
    """Missing or problematic visas create VISA alerts and per-traveler pushes."""

    def test_missing_visa_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN"),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        result = _result_for(response, SECOND)
        assert result["visa_status"] == "CRITICAL"
        assert result["passport_status"] == "OK"
        assert result["alerts_sent"] == 1
        assert response["body"]["total_alerts_sent"] == 1
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_visa_alert_persisted(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN"),
        ]

        group_handler(_base_event(endpoints, travelers), None)

        items = _alerts()
        assert len(items) == 1
        assert items[0]["skymiles_number"] == SECOND
        assert items[0]["alert_type"] == "VISA"
        assert items[0]["severity"] == "CRITICAL"
        assert items[0]["itinerary_ref"] == "DL-GROUP"
        assert items[0]["status"] == "ACTIVE"
        assert "No visa on file for XA" in items[0]["reasons"]

    def test_visa_alert_pushed_to_affected_traveler(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN"),
        ]

        group_handler(_base_event(endpoints, travelers), None)

        msgs = _messages(endpoints[SECOND])
        assert len(msgs) == 1
        gcm = json.loads(msgs[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert gcm["notification"]["body"] == "No visa on file for XA"
        assert gcm["data"]["alert_type"] == "VISA"
        assert gcm["data"]["severity"] == "CRITICAL"
        assert gcm["data"]["confirmation_number"] == "DL-GROUP"

    def test_group_summary_names_traveler_needing_action(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN"),
        ]

        group_handler(_base_event(endpoints, travelers), None)

        primary_msgs = _messages(endpoints[PRIMARY])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"].startswith("1 of 2 travelers need action")
        assert "First2 Last2" in primary_msgs[0]["default"]

    def test_visa_expiring_on_travel_date_is_warning(self, endpoints):
        visa = {
            "country_code": "XA", "visa_type": "tourist",
            "issue_date": "2029-01-01", "expiry_date": "2030-06-01",
            "visa_number": "V1",
        }
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN", visa_records=[visa]),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        assert _result_for(response, SECOND)["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"

    def test_valid_visa_no_alert(self, endpoints):
        visa = {
            "country_code": "XA", "visa_type": "tourist",
            "issue_date": "2029-01-01", "expiry_date": "2034-01-01",
            "visa_number": "V1",
        }
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN", visa_records=[visa]),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        assert _result_for(response, SECOND)["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0


class TestPassportAlerts:
    """Passport problems create PASSPORT alerts and per-traveler pushes."""

    def test_expired_passport_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], passport_expiry="2020-01-01"),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        result = _result_for(response, SECOND)
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 1

    def test_passport_alert_persisted_and_pushed(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], passport_expiry="2020-01-01"),
        ]

        group_handler(_base_event(endpoints, travelers), None)

        items = _alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "PASSPORT"
        assert items[0]["reasons"] == ["Passport has expired"]
        msgs = _messages(endpoints[SECOND])
        assert len(msgs) == 1
        gcm = json.loads(msgs[0]["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert gcm["data"]["alert_type"] == "PASSPORT"
        assert gcm["data"]["severity"] == "CRITICAL"

    def test_missing_passport_expiry_is_critical(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], passport_expiry=None),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        assert _result_for(response, SECOND)["passport_status"] == "CRITICAL"
        assert _alerts()[0]["reasons"] == ["Passport information is incomplete"]

    def test_passport_below_country_validity_requirement(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], passport_expiry="2030-09-01"),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        assert _result_for(response, SECOND)["passport_status"] == "CRITICAL"
        assert "6-month validity requirement" in _alerts()[0]["reasons"][0]

    def test_passport_expiring_within_six_months_is_info(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], passport_expiry="2030-10-01"),
        ]

        response = group_handler(
            _base_event(endpoints, travelers, segments=[_segment(destination="XB")],
                        requirements_override=dict(NO_VISA_COUNTRY)),
            None,
        )

        result = _result_for(response, SECOND)
        assert result["passport_status"] == "INFO"
        assert result["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "INFO"
        assert _alerts()[0]["severity"] == "INFO"


class TestMultipleAlerts:
    """Alerts are counted per traveler and summed across the group."""

    def test_passport_and_visa_alerts_for_same_traveler(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY]),
            _traveler(SECOND, endpoints[SECOND], nationality="IN", passport_expiry="2020-01-01"),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        result = _result_for(response, SECOND)
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 2
        assert len(_messages(endpoints[SECOND])) == 2
        assert sorted(i["alert_type"] for i in _alerts()) == ["PASSPORT", "VISA"]

    def test_total_alerts_summed_across_travelers(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(SECOND, endpoints[SECOND], nationality="IN"),
            _traveler(THIRD, endpoints[THIRD]),
        ]

        response = group_handler(_base_event(endpoints, travelers), None)

        assert response["body"]["total_alerts_sent"] == 2
        assert _result_for(response, THIRD)["alerts_sent"] == 0
        # Primary receives its own visa alert plus the group summary.
        assert len(_messages(endpoints[PRIMARY])) == 2
        assert _messages(endpoints[THIRD]) == []

    def test_alerts_queryable_by_itinerary(self, endpoints):
        travelers = [
            _traveler(PRIMARY, endpoints[PRIMARY], nationality="IN"),
            _traveler(SECOND, endpoints[SECOND], nationality="IN"),
        ]

        group_handler(_base_event(endpoints, travelers), None)

        summary = alert_store_mod.get_group_alert_summary("DL-GROUP")
        assert set(summary) == {PRIMARY, SECOND}


class TestLayovers:
    """Layover segments only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_skipped(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]
        segments = [
            _segment(destination="XA", is_layover=True),
            _segment(flight_number="DL201", origin="XA", destination="XB",
                     departure_date="2030-06-02", arrival_date="2030-06-02"),
        ]
        overrides = {**VISA_COUNTRY, **NO_VISA_COUNTRY}

        response = group_handler(
            _base_event(endpoints, travelers, segments=segments, requirements_override=overrides),
            None,
        )

        assert _result_for(response, PRIMARY)["visa_status"] == "OK"
        assert _alerts() == []

    def test_layover_with_transit_visa_requirement_alerts(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]
        overrides = {"XA": {**VISA_COUNTRY["XA"], "transit_visa_required": True}}

        response = group_handler(
            _base_event(endpoints, travelers, segments=[_segment(is_layover=True)],
                        requirements_override=overrides),
            None,
        )

        assert _result_for(response, PRIMARY)["visa_status"] == "CRITICAL"


class TestRequirementsOverride:
    """requirements_override entries are converted to TravelDocRequirements with defaults."""

    def test_override_defaults_require_visa(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY])]

        response = group_handler(
            _base_event(endpoints, travelers, requirements_override={"XA": {}}),
            None,
        )

        # Defaults: requires_visa=True, no exempt nationalities -> US national needs a visa.
        assert _result_for(response, PRIMARY)["visa_status"] == "CRITICAL"

    def test_override_default_passport_validity_is_six_months(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], passport_expiry="2030-11-01")]

        response = group_handler(
            _base_event(endpoints, travelers,
                        requirements_override={"XA": {"requires_visa": False}}),
            None,
        )

        assert _result_for(response, PRIMARY)["passport_status"] == "CRITICAL"

    def test_override_no_visa_country(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]

        response = group_handler(
            _base_event(endpoints, travelers, segments=[_segment(destination="XB")],
                        requirements_override=dict(NO_VISA_COUNTRY)),
            None,
        )

        assert _result_for(response, PRIMARY)["visa_status"] == "OK"

    def test_override_replaces_default_country(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]
        overrides = {"CN": {"requires_visa": False}}

        response = group_handler(
            _base_event(endpoints, travelers, segments=[_segment(destination="CN")],
                        requirements_override=overrides),
            None,
        )

        assert _result_for(response, PRIMARY)["visa_status"] == "OK"

    @pytest.mark.parametrize("override", [None, {}])
    def test_no_override_uses_default_requirements(self, endpoints, override):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]

        response = group_handler(
            _base_event(endpoints, travelers, segments=[_segment(destination="CN")],
                        requirements_override=override),
            None,
        )

        assert _result_for(response, PRIMARY)["visa_status"] == "CRITICAL"
        assert "No visa on file for CN" in _alerts()[0]["reasons"]

    def test_missing_override_key_uses_default_requirements(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]
        event = _base_event(endpoints, travelers, segments=[_segment(destination="CN")])
        del event["requirements_override"]

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["visa_status"] == "CRITICAL"


class TestPrimaryTraveler:
    """The group summary is only sent when the primary traveler is in the group."""

    def test_primary_not_in_group_skips_summary(self, endpoints):
        travelers = [_traveler(SECOND, endpoints[SECOND])]

        response = group_handler(_base_event(endpoints, travelers), None)

        assert response["statusCode"] == 200
        assert _messages(endpoints[PRIMARY]) == []
        assert _messages(endpoints[SECOND]) == []

    def test_summary_sent_to_non_first_primary(self, endpoints):
        event = _base_event(endpoints, primary_traveler=SECOND)

        group_handler(event, None)

        assert _messages(endpoints[PRIMARY]) == []
        assert len(_messages(endpoints[SECOND])) == 1


class TestTTL:
    """Persisted alerts carry a TTL 30 days after the last arrival."""

    def test_ttl_uses_last_arrival(self, endpoints):
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], nationality="IN")]
        segments = [
            _segment(),
            _segment(flight_number="DL300", origin="XA", destination="XB",
                     departure_date="2030-06-10", arrival_date="2030-06-11"),
        ]
        overrides = {**VISA_COUNTRY, **NO_VISA_COUNTRY}

        group_handler(
            _base_event(endpoints, travelers, segments=segments, requirements_override=overrides),
            None,
        )

        expected = int((datetime(2030, 6, 11) + timedelta(days=30)).timestamp())
        assert int(_alerts()[0]["ttl"]) == expected


class TestUnknownTraveler:
    """Alerts for a summary with no matching traveler are saved but not pushed."""

    @staticmethod
    def _orphan_result(group, requirements):
        def _eval(cls, severity):
            return cls(
                profile=group.travelers[0],
                segments_evaluated=group.segments,
                is_alert_required=True,
                severity=severity,
                reasons=["reason"],
                validation_errors=[],
            )
        return GroupEvaluationResult(
            confirmation_number=group.confirmation_number,
            traveler_summaries=[
                TravelerAlertSummary(
                    skymiles_number="0000000000",
                    first_name="Ghost",
                    last_name="Traveler",
                    passport_result=_eval(PassportEvaluation, AlertSeverity.CRITICAL),
                    visa_result=_eval(VisaEvaluation, AlertSeverity.WARNING),
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

    def test_orphan_summary_saves_without_push(self, endpoints):
        original = group_mod.evaluate_group_itinerary
        group_mod.evaluate_group_itinerary = self._orphan_result
        try:
            response = group_handler(_base_event(endpoints), None)
        finally:
            group_mod.evaluate_group_itinerary = original

        assert response["body"]["total_alerts_sent"] == 2
        assert sorted(i["alert_type"] for i in _alerts()) == ["PASSPORT", "VISA"]
        assert all(i["skymiles_number"] == "0000000000" for i in _alerts())
        assert _messages(endpoints[SECOND]) == []
        # Primary only receives the group summary.
        assert len(_messages(endpoints[PRIMARY])) == 1


class TestAlertRequiredWithoutSeverity:
    """An alert flag without a severity is not persisted and reports OK."""

    @staticmethod
    def _no_severity_result(group, requirements):
        def _eval(cls):
            return cls(
                profile=group.travelers[0],
                segments_evaluated=group.segments,
                is_alert_required=True,
                severity=None,
                reasons=["reason"],
                validation_errors=[],
            )
        traveler = group.travelers[0]
        return GroupEvaluationResult(
            confirmation_number=group.confirmation_number,
            traveler_summaries=[
                TravelerAlertSummary(
                    skymiles_number=traveler.skymiles_number,
                    first_name=traveler.first_name,
                    last_name=traveler.last_name,
                    passport_result=_eval(PassportEvaluation),
                    visa_result=_eval(VisaEvaluation),
                )
            ],
            group_severity=None,
        )

    def test_no_alert_saved(self, endpoints):
        original = group_mod.evaluate_group_itinerary
        group_mod.evaluate_group_itinerary = self._no_severity_result
        try:
            response = group_handler(_base_event(endpoints), None)
        finally:
            group_mod.evaluate_group_itinerary = original

        result = _result_for(response, PRIMARY)
        assert result["passport_status"] == "OK"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"
        assert _alerts() == []


class TestParseGroupItinerary:
    """Event payloads deserialize into a GroupItinerary."""

    def test_parses_segments_and_travelers(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert len(group.travelers) == 2
        seg = group.segments[0]
        assert seg.departure_date == date(2030, 6, 1)
        assert seg.arrival_date == date(2030, 6, 2)
        assert seg.is_layover is False

    def test_is_layover_defaults_to_false(self, endpoints):
        seg = _segment()
        del seg["is_layover"]

        group = _parse_group_itinerary(_base_event(endpoints, segments=[seg]))

        assert group.segments[0].is_layover is False

    def test_parses_visa_records_and_missing_passport_expiry(self, endpoints):
        visa = {
            "country_code": "XA", "visa_type": "tourist",
            "issue_date": "2029-01-01", "expiry_date": "2034-01-01",
            "visa_number": "V1",
        }
        travelers = [_traveler(PRIMARY, endpoints[PRIMARY], passport_expiry=None,
                               visa_records=[visa])]

        group = _parse_group_itinerary(_base_event(endpoints, travelers))

        profile = group.travelers[0]
        assert profile.passport_expiry is None
        assert profile.visa_records[0].expiry_date == date(2034, 1, 1)

    def test_missing_required_field_raises(self, endpoints):
        event = _base_event(endpoints)
        del event["primary_traveler"]

        with pytest.raises(KeyError):
            _parse_group_itinerary(event)


class TestFindTraveler:
    """_find_traveler returns the matching profile or None."""

    def test_finds_traveler(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, SECOND).skymiles_number == SECOND

    def test_returns_none_when_missing(self, endpoints):
        group = _parse_group_itinerary(_base_event(endpoints))

        assert _find_traveler(group, "nope") is None
