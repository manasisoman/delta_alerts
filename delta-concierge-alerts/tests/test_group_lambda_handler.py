"""Critical-path tests for the group itinerary Lambda handler.

Covers per-traveler passport/visa alert persistence and push delivery,
country-specific passport validity and visa limits, layover/transit rules,
requirements overrides, group severity aggregation, the primary-traveler
group summary, and re-evaluation after itinerary changes.
"""

import json
from datetime import datetime, timedelta

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.handlers.group_lambda_handler as group_mod
import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import group_handler
from src.handlers.lambda_handler import handler

PRIMARY_SM = "1000000001"
COMPANION_SM = "1000000002"
DEPARTURE = "2031-06-01"
ARRIVAL = "2031-06-02"


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------

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
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arns = {
            name: sns.create_platform_endpoint(
                PlatformApplicationArn=app_arn,
                Token=f"fake-token-{name}",
            )["EndpointArn"]
            for name in ("primary", "companion")
        }

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arns
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoints(_mock_aws):
    """Expose the moto SNS endpoint ARNs keyed by traveler role."""
    return _mock_aws


def _traveler(skymiles_number: str, first: str, last: str, endpoint_arn: str, **overrides) -> dict:
    """Build a clean US-national traveler payload with optional overrides."""
    traveler = {
        "skymiles_number": skymiles_number,
        "first_name": first,
        "last_name": last,
        "nationality": "US",
        "passport_number": "P123456",
        "passport_expiry": "2040-01-01",
        "endpoint_arn": endpoint_arn,
        "visa_records": [],
    }
    traveler.update(overrides)
    return traveler


def _segment(**overrides) -> dict:
    """Build a non-layover ATL -> DE segment payload with optional overrides."""
    segment = {
        "flight_number": "DL100",
        "origin": "ATL",
        "destination": "DE",
        "departure_date": DEPARTURE,
        "arrival_date": ARRIVAL,
        "is_layover": False,
    }
    segment.update(overrides)
    return segment


def _visa(country_code: str, expiry_date: str = "2040-01-01") -> dict:
    """Build a visa record payload."""
    return {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2030-01-01",
        "expiry_date": expiry_date,
        "visa_number": f"V-{country_code}",
    }


def _base_event(endpoints: dict, **overrides) -> dict:
    """Build a valid two-traveler group event where nobody needs an alert."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY_SM,
        "travelers": [
            _traveler(PRIMARY_SM, "Ann", "Lee", endpoints["primary"]),
            _traveler(COMPANION_SM, "Ben", "Kim", endpoints["companion"]),
        ],
        "segments": [_segment()],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _messages(endpoint_arn: str) -> list[dict]:
    """Return the decoded SNS messages moto recorded for an endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _titles(endpoint_arn: str) -> list[str]:
    """Return the APNS alert titles of messages sent to an endpoint."""
    return [
        json.loads(m["APNS"])["aps"]["alert"]["title"] for m in _messages(endpoint_arn)
    ]


def _stored_alerts() -> list[dict]:
    """Return every alert item persisted in DynamoDB."""
    return alert_store_mod.table.scan()["Items"]


def _result_for(response: dict, skymiles_number: str) -> dict:
    """Return the traveler_results entry for a SkyMiles number."""
    return next(
        r for r in response["body"]["traveler_results"]
        if r["skymiles_number"] == skymiles_number
    )


def _expected_ttl(arrival: str) -> int:
    """Epoch timestamp 30 days after the given arrival date."""
    return int((datetime.fromisoformat(arrival) + timedelta(days=30)).timestamp())


# ------------------------------------------------------------------
# Tests
# ------------------------------------------------------------------

class TestCleanGroup:
    """A group with valid documents returns OK everywhere and stores no alerts."""

    def test_response_shape_for_clean_group(self, endpoints):
        event = _base_event(endpoints)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert body["traveler_results"] == [
            {
                "skymiles_number": PRIMARY_SM,
                "first_name": "Ann",
                "last_name": "Lee",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            },
            {
                "skymiles_number": COMPANION_SM,
                "first_name": "Ben",
                "last_name": "Kim",
                "passport_status": "OK",
                "visa_status": "OK",
                "alerts_sent": 0,
            },
        ]

    def test_no_alerts_persisted(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        assert _stored_alerts() == []

    def test_primary_receives_all_clear_summary_only(self, endpoints):
        event = _base_event(endpoints)

        group_handler(event, None)

        primary_msgs = _messages(endpoints["primary"])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == "All 2 travelers are cleared for travel."
        assert _messages(endpoints["companion"]) == []

    def test_lambda_handler_delegates_group_events(self, endpoints):
        """The single-traveler handler routes events with a travelers key here."""
        event = _base_event(endpoints)

        response = handler(event, None)

        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert len(response["body"]["traveler_results"]) == 2


class TestPerTravelerAlerts:
    """Each traveler's actionable results are persisted and pushed to that traveler."""

    def test_visa_alert_persisted_for_affected_traveler(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [_visa("CN")]

        response = group_handler(event, None)

        alerts = _stored_alerts()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert["skymiles_number"] == COMPANION_SM
        assert alert["alert_type"] == "VISA"
        assert alert["severity"] == "CRITICAL"
        assert alert["status"] == "ACTIVE"
        assert alert["itinerary_ref"] == "DL-GROUP"
        assert alert["reasons"] == ["No visa on file for CN"]
        assert response["body"]["total_alerts_sent"] == 1

    def test_visa_alert_pushed_only_to_affected_traveler(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [_visa("CN")]

        group_handler(event, None)

        companion_msgs = _messages(endpoints["companion"])
        assert len(companion_msgs) == 1
        assert companion_msgs[0]["default"] == "No visa on file for CN"
        apns = json.loads(companion_msgs[0]["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Visa Alert"
        assert apns["alert_type"] == "VISA"
        assert apns["severity"] == "CRITICAL"
        assert apns["confirmation_number"] == "DL-GROUP"
        assert _titles(endpoints["primary"]) == ["Delta Concierge \u2014 Group Travel Alert"]

    def test_passport_alert_persisted_and_pushed(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        alerts = _stored_alerts()
        assert [(a["skymiles_number"], a["alert_type"]) for a in alerts] == [
            (COMPANION_SM, "PASSPORT")
        ]
        assert alerts[0]["reasons"] == ["Passport has expired"]
        apns = json.loads(_messages(endpoints["companion"])[0]["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Passport Alert"
        assert apns["alert_type"] == "PASSPORT"
        assert apns["severity"] == "CRITICAL"
        result = _result_for(response, COMPANION_SM)
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "OK"
        assert result["alerts_sent"] == 1

    def test_passport_and_visa_alerts_for_same_traveler(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [_visa("CN")]
        event["travelers"][1]["passport_number"] = None

        response = group_handler(event, None)

        result = _result_for(response, COMPANION_SM)
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "CRITICAL"
        assert result["alerts_sent"] == 2
        assert response["body"]["total_alerts_sent"] == 2
        assert sorted(_titles(endpoints["companion"])) == [
            "Delta Concierge \u2014 Passport Alert",
            "Delta Concierge \u2014 Visa Alert",
        ]

    def test_alerts_counted_across_travelers(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        assert _result_for(response, PRIMARY_SM)["alerts_sent"] == 1
        assert _result_for(response, COMPANION_SM)["alerts_sent"] == 1

    def test_alert_ttl_is_30_days_after_arrival(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        assert int(_stored_alerts()[0]["ttl"]) == _expected_ttl(ARRIVAL)


class TestPassportValidityByCountry:
    """Passport validity is checked against each destination's required months."""

    def test_passport_below_schengen_3_month_minimum_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2031-08-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == [
            "Passport does not meet DE's 3-month validity requirement for DL100"
        ]

    def test_passport_meeting_schengen_minimum_but_under_6_months_is_info(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2031-10-01"

        response = group_handler(event, None)

        result = _result_for(response, COMPANION_SM)
        assert result["passport_status"] == "INFO"
        assert result["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "INFO"

    def test_same_passport_fails_japan_6_month_requirement(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="JP")])
        event["travelers"][1]["passport_expiry"] = "2031-10-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == [
            "Passport does not meet JP's 6-month validity requirement for DL100"
        ]

    def test_passport_expiring_before_departure_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2031-05-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == [
            "Passport expires before departure on DL100 to DE"
        ]

    def test_missing_passport_expiry_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = None

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == ["Passport information is incomplete"]


class TestVisaLimits:
    """Visa status reflects the visa on file versus the country's requirement."""

    def test_visa_exempt_nationality_needs_no_visa(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="JP")])

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "OK"

    def test_non_exempt_nationality_without_visa_is_critical(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == ["No visa on file for DE"]

    def test_valid_visa_clears_requirement(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        for traveler in event["travelers"]:
            traveler["visa_records"] = [_visa("CN")]

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_visa_expiring_on_departure_is_warning(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [_visa("CN")]
        event["travelers"][1]["visa_records"] = [_visa("CN", expiry_date=DEPARTURE)]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"

    def test_visa_expiring_before_departure_is_critical(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [_visa("CN")]
        event["travelers"][1]["visa_records"] = [_visa("CN", expiry_date="2031-01-01")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == [
            "Visa for CN expires before date of travel"
        ]

    def test_visa_expiring_within_30_days_of_travel_is_info(self, endpoints):
        event = _base_event(endpoints, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [_visa("CN")]
        event["travelers"][1]["visa_records"] = [_visa("CN", expiry_date="2031-06-20")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "INFO"


class TestLayovers:
    """Layovers only require a visa where the country demands a transit visa."""

    def test_layover_in_transit_visa_country_requires_visa(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(flight_number="DL100", destination="CN", is_layover=True),
                _segment(flight_number="DL200", origin="CN", destination="JP"),
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "CRITICAL"
        assert response["body"]["group_severity"] == "CRITICAL"

    def test_layover_without_transit_visa_requirement_skips_visa(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(flight_number="DL100", destination="DE", is_layover=True),
                _segment(flight_number="DL200", origin="DE", destination="JP"),
            ],
        )
        event["travelers"][1]["nationality"] = "IN"
        event["travelers"][1]["visa_records"] = [_visa("JP")]

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "OK"

    def test_segment_without_layover_flag_is_treated_as_destination(self, endpoints):
        segment = _segment()
        del segment["is_layover"]
        event = _base_event(endpoints, segments=[segment])
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["visa_status"] == "CRITICAL"

    def test_layover_still_checks_passport_validity(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(flight_number="DL100", destination="DE", is_layover=True),
                _segment(flight_number="DL200", origin="DE", destination="GB"),
            ],
        )
        event["travelers"][1]["passport_expiry"] = "2031-08-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert "Passport does not meet DE's 3-month validity requirement for DL100" in (
            _stored_alerts()[0]["reasons"]
        )


class TestRequirementsOverride:
    """requirements_override replaces default country rules; missing keys use defaults."""

    def test_override_can_waive_visa_requirement(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[_segment(destination="CN")],
            requirements_override={"CN": {"requires_visa": False}},
        )

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "OK"
        assert _stored_alerts() == []

    def test_override_with_partial_fields_applies_defaults(self, endpoints):
        """Omitted fields fall back to visa required, 6 months, and no exemptions."""
        event = _base_event(endpoints, requirements_override={"DE": {}})
        event["travelers"][1]["passport_expiry"] = "2031-10-01"

        response = group_handler(event, None)

        result = _result_for(response, COMPANION_SM)
        assert result["passport_status"] == "CRITICAL"
        assert result["visa_status"] == "CRITICAL"
        reasons = {
            a["alert_type"]: a["reasons"]
            for a in _stored_alerts()
            if a["skymiles_number"] == COMPANION_SM
        }
        assert reasons == {
            "PASSPORT": ["Passport does not meet DE's 6-month validity requirement for DL100"],
            "VISA": ["No visa on file for DE"],
        }

    def test_override_can_raise_passport_validity_months(self, endpoints):
        event = _base_event(
            endpoints,
            requirements_override={
                "DE": {
                    "passport_validity_months": 12,
                    "visa_exempt_nationalities": ["US"],
                }
            },
        )
        event["travelers"][1]["passport_expiry"] = "2032-01-01"

        response = group_handler(event, None)

        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert _stored_alerts()[0]["reasons"] == [
            "Passport does not meet DE's 12-month validity requirement for DL100"
        ]

    def test_empty_override_uses_default_requirements(self, endpoints):
        event = _base_event(endpoints, requirements_override={})

        response = group_handler(event, None)

        assert response["body"]["group_severity"] == "OK"


class TestGroupSeverity:
    """group_severity is the highest severity across all travelers."""

    def test_highest_severity_wins_across_travelers(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = "2031-10-01"
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY_SM)["passport_status"] == "INFO"
        assert _result_for(response, COMPANION_SM)["passport_status"] == "CRITICAL"
        assert response["body"]["group_severity"] == "CRITICAL"


class TestGroupSummaryNotification:
    """The primary traveler receives a group summary when present in the group."""

    def test_summary_lists_travelers_needing_action(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        primary_msgs = _messages(endpoints["primary"])
        assert len(primary_msgs) == 1
        assert primary_msgs[0]["default"] == (
            "1 of 2 travelers need action: Ben Kim - Passport has expired"
        )
        assert json.loads(primary_msgs[0]["APNS"])["group_severity"] == "CRITICAL"

    def test_primary_with_own_alert_gets_alert_and_summary(self, endpoints):
        event = _base_event(endpoints)
        event["travelers"][0]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        assert _titles(endpoints["primary"]) == [
            "Delta Concierge \u2014 Passport Alert",
            "Delta Concierge \u2014 Group Travel Alert",
        ]

    def test_no_summary_when_primary_not_in_group(self, endpoints):
        event = _base_event(endpoints, primary_traveler="9999999999")
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        assert _messages(endpoints["primary"]) == []
        assert _titles(endpoints["companion"]) == ["Delta Concierge \u2014 Passport Alert"]
        assert response["body"]["total_alerts_sent"] == 1


class TestUnmatchedTravelerSummary:
    """Alerts for a summary with no matching traveler are stored but not pushed."""

    @pytest.fixture
    def unmatched_summaries(self, monkeypatch):
        """Rewrite every evaluated summary to an unknown SkyMiles number."""
        real_evaluate = group_mod.evaluate_group_itinerary

        def _evaluate(group, requirements):
            result = real_evaluate(group, requirements)
            for summary in result.traveler_summaries:
                summary.skymiles_number = "0000000000"
            return result

        monkeypatch.setattr(group_mod, "evaluate_group_itinerary", _evaluate)

    def test_alerts_saved_without_per_traveler_push(self, endpoints, unmatched_summaries):
        event = _base_event(
            endpoints,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(
                    PRIMARY_SM, "Ann", "Lee", endpoints["primary"],
                    passport_expiry="2031-10-01",
                )
            ],
        )

        response = group_handler(event, None)

        alerts = _stored_alerts()
        assert sorted(a["alert_type"] for a in alerts) == ["PASSPORT", "VISA"]
        assert {a["skymiles_number"] for a in alerts} == {"0000000000"}
        assert response["body"]["total_alerts_sent"] == 2
        assert _titles(endpoints["primary"]) == ["Delta Concierge \u2014 Group Travel Alert"]


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_destination_change_surfaces_new_visa_alert(self, endpoints):
        original = _base_event(endpoints)
        changed = _base_event(endpoints, segments=[_segment(destination="CN")])

        before = group_handler(original, None)
        after = group_handler(changed, None)

        assert before["body"]["group_severity"] == "OK"
        assert after["body"]["group_severity"] == "CRITICAL"
        assert {a["alert_type"] for a in _stored_alerts()} == {"VISA"}

    def test_later_departure_breaks_passport_validity(self, endpoints):
        original = _base_event(endpoints)
        original["travelers"][1]["passport_expiry"] = "2031-10-01"
        changed = _base_event(
            endpoints,
            segments=[_segment(departure_date="2031-08-01", arrival_date="2031-08-02")],
        )
        changed["travelers"][1]["passport_expiry"] = "2031-10-01"

        before = group_handler(original, None)
        after = group_handler(changed, None)

        assert _result_for(before, COMPANION_SM)["passport_status"] == "INFO"
        assert _result_for(after, COMPANION_SM)["passport_status"] == "CRITICAL"

    def test_added_transit_layover_requires_visa(self, endpoints):
        original = _base_event(endpoints, segments=[_segment(destination="JP")])
        changed = _base_event(
            endpoints,
            segments=[
                _segment(flight_number="DL100", destination="CN", is_layover=True),
                _segment(flight_number="DL200", origin="CN", destination="JP"),
            ],
        )

        before = group_handler(original, None)
        after = group_handler(changed, None)

        assert _result_for(before, COMPANION_SM)["visa_status"] == "OK"
        assert _result_for(after, COMPANION_SM)["visa_status"] == "CRITICAL"

    def test_ttl_follows_latest_arrival_across_segments(self, endpoints):
        event = _base_event(
            endpoints,
            segments=[
                _segment(flight_number="DL100", destination="DE"),
                _segment(
                    flight_number="DL200", origin="DE", destination="GB",
                    departure_date="2031-06-20", arrival_date="2031-06-21",
                ),
            ],
        )
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        group_handler(event, None)

        assert int(_stored_alerts()[0]["ttl"]) == _expected_ttl("2031-06-21")
