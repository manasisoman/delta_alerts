"""Tests for the SNS push notification service."""

import json
from datetime import date

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.services.notification_service as notif_mod
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertType,
    GroupEvaluationResult,
    GroupItinerary,
    NotificationPayload,
    PassportEvaluation,
    SkyMilesProfile,
    TravelerAlertSummary,
    VisaEvaluation,
    VisaRecord,
)
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)

from tests.conftest import make_profile, make_segment


GROUP_TITLE = "Delta Concierge \u2014 Group Travel Alert"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto SNS with a platform application and patch the module client."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]

        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = sns

        try:
            yield sns, app_arn
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def make_endpoint(_mock_aws):
    """Factory creating a distinct moto platform endpoint per traveler/device."""
    sns, app_arn = _mock_aws

    def _make(token: str = "device-token") -> str:
        return sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token=token,
        )["EndpointArn"]

    return _make


@pytest.fixture
def endpoint_arn(make_endpoint):
    return make_endpoint("primary-token")


def _published(arn: str) -> list[dict]:
    """Return decoded SNS messages that moto recorded for an endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _only_message(arn: str) -> dict:
    messages = _published(arn)
    assert len(messages) == 1
    return messages[0]


def _alert_record(**overrides) -> AlertRecord:
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport has expired"],
        created_at="2030-01-01T00:00:00+00:00",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    defaults = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_alert_record(),
        title="Passport Alert",
        body="Your passport has expired.",
        push_data={"alert_id": "alert-1", "severity": "CRITICAL"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _traveler(first: str = "Jane", last: str = "Doe", skymiles: str = "1000001", **kwargs) -> SkyMilesProfile:
    profile = make_profile(**kwargs)
    profile.first_name = first
    profile.last_name = last
    profile.skymiles_number = skymiles
    return profile


def _passport(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _visa(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _summary(
    first: str = "Jane",
    last: str = "Doe",
    passport: PassportEvaluation | None = None,
    visa: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="1000001",
        first_name=first,
        last_name=last,
        passport_result=passport or _passport(),
        visa_result=visa or _visa(),
    )


def _group_result(
    *summaries: TravelerAlertSummary,
    confirmation: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


def _body(arn: str) -> str:
    return _only_message(arn)["default"]


# ---------------------------------------------------------------------------
# send_push_notification
# ---------------------------------------------------------------------------


class TestPushNotificationPublish:
    """A single-traveler push publishes one JSON-structured message to its endpoint."""

    def test_returns_sns_message_id(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_default_message_is_body(self, endpoint_arn):
        payload = _payload(endpoint_arn, body="Visa required for CN")

        send_push_notification(payload)

        assert _only_message(endpoint_arn)["default"] == "Visa required for CN"

    def test_only_target_endpoint_receives_message(self, endpoint_arn, make_endpoint):
        other_arn = make_endpoint("other-token")
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        assert len(_published(endpoint_arn)) == 1
        assert _published(other_arn) == []


class TestPushNotificationPlatformFormats:
    """APNS and GCM sub-messages carry the title, body and push data."""

    def test_apns_alert_title_body_and_sound(self, endpoint_arn):
        payload = _payload(endpoint_arn, title="T", body="B")

        send_push_notification(payload)

        apns = json.loads(_only_message(endpoint_arn)["APNS"])
        assert apns["aps"]["alert"] == {"title": "T", "body": "B"}
        assert apns["aps"]["sound"] == "default"

    def test_apns_merges_push_data_at_top_level(self, endpoint_arn):
        payload = _payload(endpoint_arn, push_data={"alert_id": "a-9", "itinerary_ref": "DL-X"})

        send_push_notification(payload)

        apns = json.loads(_only_message(endpoint_arn)["APNS"])
        assert apns["alert_id"] == "a-9"
        assert apns["itinerary_ref"] == "DL-X"

    def test_gcm_notification_and_data(self, endpoint_arn):
        push_data = {"alert_id": "a-9", "severity": "WARNING"}
        payload = _payload(endpoint_arn, title="T", body="B", push_data=push_data)

        send_push_notification(payload)

        gcm = json.loads(_only_message(endpoint_arn)["GCM"])
        assert gcm["notification"] == {"title": "T", "body": "B"}
        assert gcm["data"] == push_data

    def test_empty_push_data(self, endpoint_arn):
        payload = _payload(endpoint_arn, push_data={})

        send_push_notification(payload)

        message = _only_message(endpoint_arn)
        assert set(json.loads(message["APNS"]).keys()) == {"aps"}
        assert json.loads(message["GCM"])["data"] == {}

    def test_numeric_string_push_data_survives_round_trip(self, endpoint_arn):
        payload = _payload(endpoint_arn, push_data={"skymiles_number": "9999999"})

        send_push_notification(payload)

        gcm = json.loads(_only_message(endpoint_arn)["GCM"])
        assert gcm["data"]["skymiles_number"] == "9999999"


class TestPushNotificationInvalidEndpoint:
    """Publishing to an unknown endpoint surfaces the SNS error."""

    def test_unknown_endpoint_raises(self, _mock_aws):
        sns, _ = _mock_aws
        payload = _payload("arn:aws:sns:us-east-1:123456789012:endpoint/GCM/TestApp/missing")

        with pytest.raises(sns.exceptions.ClientError):
            send_push_notification(payload)


# ---------------------------------------------------------------------------
# send_group_notification — summary body
# ---------------------------------------------------------------------------


class TestGroupAllCleared:
    """When no traveler has a WARNING/CRITICAL issue, the body says everyone is cleared."""

    def test_all_travelers_cleared(self, endpoint_arn):
        result = _group_result(_summary("Jane"), _summary("Tim"))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        result = _group_result()

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 0 travelers are cleared for travel."

    def test_returns_sns_message_id(self, endpoint_arn):
        result = _group_result(_summary())

        response = send_group_notification(endpoint_arn, result)

        assert "MessageId" in response


class TestGroupActionItems:
    """Travelers with WARNING/CRITICAL passport or visa issues are listed as action items."""

    def test_passport_critical_listed(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        result = _group_result(_summary("Jane", "Doe", passport=passport), _summary("Tim", "Roe"))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == (
            "1 of 2 travelers need action: Jane Doe - Passport has expired"
        )

    def test_visa_warning_listed(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.WARNING, ["Visa for CN expires on date of travel"])
        result = _group_result(_summary("Tim", "Roe", visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == (
            "1 of 1 travelers need action: Tim Roe - Visa for CN expires on date of travel"
        )

    def test_passport_and_visa_issues_joined_with_comma(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        visa = _visa(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        result = _group_result(_summary("Jane", "Doe", passport=passport, visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn).endswith(
            "Jane Doe - Passport has expired, No visa on file for CN"
        )

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.CRITICAL, ["No visa on file for CN", "No visa on file for IN"])
        result = _group_result(_summary("Jane", "Doe", visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn).endswith(
            "Jane Doe - No visa on file for CN; No visa on file for IN"
        )

    def test_multiple_travelers_listed_in_order(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        visa = _visa(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        result = _group_result(
            _summary("Jane", "Doe", passport=passport),
            _summary("Ann", "Lee"),
            _summary("Tim", "Roe", visa=visa),
        )

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == (
            "2 of 3 travelers need action: Jane Doe - Passport has expired; "
            "Tim Roe - No visa on file for CN"
        )


class TestGroupIssueFiltering:
    """Only alert-required results with WARNING/CRITICAL severity count as action items."""

    def test_info_passport_excluded(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.INFO, ["Passport expiring within 6 months"])
        result = _group_result(_summary(passport=passport))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_info_visa_excluded(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.INFO, ["Visa for CN expires within 30 days of travel"])
        result = _group_result(_summary(visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_passport_not_alert_required_excluded(self, endpoint_arn):
        passport = _passport(False, AlertSeverity.CRITICAL, ["stale reason"])
        result = _group_result(_summary(passport=passport))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_visa_not_alert_required_excluded(self, endpoint_arn):
        visa = _visa(False, AlertSeverity.CRITICAL, ["stale reason"])
        result = _group_result(_summary(visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_passport_none_severity_excluded(self, endpoint_arn):
        passport = _passport(True, None, ["Passport information is incomplete"])
        result = _group_result(_summary(passport=passport))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_visa_none_severity_excluded(self, endpoint_arn):
        visa = _visa(True, None, ["unknown"])
        result = _group_result(_summary(visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_info_visa_dropped_but_critical_passport_kept(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        visa = _visa(True, AlertSeverity.INFO, ["Visa for CN expires within 30 days of travel"])
        result = _group_result(_summary("Jane", "Doe", passport=passport, visa=visa))

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - Passport has expired"
        )


# ---------------------------------------------------------------------------
# send_group_notification — message structure
# ---------------------------------------------------------------------------


class TestGroupMessageStructure:
    """Group APNS/GCM payloads carry the title, confirmation number and group severity."""

    def test_apns_payload(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        result = _group_result(
            _summary(visa=visa), confirmation="DL-ABC", group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, result)

        message = _only_message(endpoint_arn)
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-ABC"
        assert apns["group_severity"] == "CRITICAL"

    def test_gcm_payload(self, endpoint_arn):
        result = _group_result(
            _summary(), confirmation="DL-ABC", group_severity=AlertSeverity.WARNING,
        )

        send_group_notification(endpoint_arn, result)

        message = _only_message(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert gcm["data"] == {"confirmation_number": "DL-ABC", "group_severity": "WARNING"}

    def test_none_group_severity_reported_as_ok(self, endpoint_arn):
        result = _group_result(_summary(), group_severity=None)

        send_group_notification(endpoint_arn, result)

        message = _only_message(endpoint_arn)
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    def test_sent_only_to_primary_endpoint(self, endpoint_arn, make_endpoint):
        companion_arn = make_endpoint("companion-token")
        result = _group_result(_summary())

        send_group_notification(endpoint_arn, result)

        assert len(_published(endpoint_arn)) == 1
        assert _published(companion_arn) == []


# ---------------------------------------------------------------------------
# send_group_notification — driven by the real group evaluator
# ---------------------------------------------------------------------------


@pytest.fixture
def requirements(base_requirements):
    return base_requirements


def _group(*segments, travelers, confirmation: str = "DL-GROUP") -> GroupItinerary:
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=list(segments),
        travelers=list(travelers),
        primary_traveler=travelers[0].skymiles_number,
    )


class TestGroupPassportValidityLimits:
    """Passport validity against each country's minimum drives the group summary."""

    def test_below_jp_6_month_requirement_needs_action(self, endpoint_arn, requirements):
        jane = _traveler("Jane", nationality="US", passport_expiry=date(2030, 9, 1))
        segment = make_segment(destination="JP", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[jane]), requirements)

        send_group_notification(endpoint_arn, result)

        assert "Jane Doe - Passport does not meet JP's 6-month validity requirement" in _body(endpoint_arn)

    def test_meets_de_3_month_minimum_info_only_is_cleared(self, endpoint_arn, requirements):
        # 4 months of validity: meets DE's 3-month rule, only an INFO renewal hint
        jane = _traveler("Jane", nationality="US", passport_expiry=date(2030, 10, 1))
        segment = make_segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[jane]), requirements)

        send_group_notification(endpoint_arn, result)

        assert result.group_severity == AlertSeverity.INFO
        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."
        assert json.loads(_only_message(endpoint_arn)["APNS"])["group_severity"] == "INFO"

    def test_expired_passport_needs_action(self, endpoint_arn, requirements):
        jane = _traveler("Jane", nationality="US", passport_expiry=date(2020, 1, 1))
        segment = make_segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[jane]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "1 of 1 travelers need action: Jane Doe - Passport has expired"


class TestGroupVisaLimits:
    """Visa requirements, exemptions and expiry windows drive the group summary."""

    def test_visa_exempt_and_non_exempt_travelers(self, endpoint_arn, requirements):
        jane = _traveler("Jane", "Doe", "1000001", nationality="US", passport_expiry=date(2035, 1, 1))
        tim = _traveler("Tim", "Roe", "1000002", nationality="IN", passport_expiry=date(2035, 1, 1))
        segment = make_segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[jane, tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "1 of 2 travelers need action: Tim Roe - No visa on file for DE"

    def test_valid_visa_cleared(self, endpoint_arn, requirements):
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2035, 1, 1), "V1")
        tim = _traveler("Tim", "Roe", nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_visa_expiring_on_travel_date_is_warning(self, endpoint_arn, requirements):
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2030, 6, 1), "V1")
        tim = _traveler("Tim", "Roe", nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == (
            "1 of 1 travelers need action: Tim Roe - Visa for CN expires on date of travel"
        )
        assert json.loads(_only_message(endpoint_arn)["APNS"])["group_severity"] == "WARNING"

    def test_visa_expiring_within_window_is_info_and_cleared(self, endpoint_arn, requirements):
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2030, 6, 10), "V1")
        tim = _traveler("Tim", "Roe", nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        result = evaluate_group_itinerary(_group(segment, travelers=[tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert result.group_severity == AlertSeverity.INFO
        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."


class TestGroupLayovers:
    """Layovers only produce visa action items when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_cleared(self, endpoint_arn, requirements):
        tim = _traveler("Tim", "Roe", nationality="IN", passport_expiry=date(2035, 1, 1))
        layover = make_segment(destination="JP", departure=date(2030, 6, 1), arrival=date(2030, 6, 2), is_layover=True)
        result = evaluate_group_itinerary(_group(layover, travelers=[tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_layover_requiring_transit_visa_needs_action(self, endpoint_arn, requirements):
        tim = _traveler("Tim", "Roe", nationality="IN", passport_expiry=date(2035, 1, 1))
        layover = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2), is_layover=True)
        result = evaluate_group_itinerary(_group(layover, travelers=[tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == "1 of 1 travelers need action: Tim Roe - No visa on file for CN"

    def test_layover_and_final_destination_reasons_combined(self, endpoint_arn, requirements):
        tim = _traveler("Tim", "Roe", nationality="IN", passport_expiry=date(2035, 1, 1))
        layover = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2), is_layover=True, flight_number="DL1")
        final = make_segment(destination="DE", departure=date(2030, 6, 2), arrival=date(2030, 6, 3), flight_number="DL2")
        result = evaluate_group_itinerary(_group(layover, final, travelers=[tim]), requirements)

        send_group_notification(endpoint_arn, result)

        assert _body(endpoint_arn) == (
            "1 of 1 travelers need action: Tim Roe - No visa on file for CN; No visa on file for DE"
        )


class TestGroupItineraryChange:
    """Re-notifying after an itinerary change reflects the new destinations."""

    def test_change_to_visa_country_flips_to_needs_action(self, endpoint_arn, requirements):
        jane = _traveler("Jane", "Doe", nationality="US", passport_expiry=date(2035, 1, 1))
        original = make_segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        changed = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))

        send_group_notification(endpoint_arn, evaluate_group_itinerary(_group(original, travelers=[jane]), requirements))
        send_group_notification(endpoint_arn, evaluate_group_itinerary(_group(changed, travelers=[jane]), requirements))

        bodies = [m["default"] for m in _published(endpoint_arn)]
        assert bodies == [
            "All 1 travelers are cleared for travel.",
            "1 of 1 travelers need action: Jane Doe - No visa on file for CN",
        ]

    def test_date_change_past_passport_expiry_flips_to_needs_action(self, endpoint_arn, requirements):
        jane = _traveler("Jane", "Doe", nationality="US", passport_expiry=date(2031, 1, 1))
        original = make_segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        rescheduled = make_segment(destination="DE", departure=date(2031, 2, 1), arrival=date(2031, 2, 2))

        send_group_notification(endpoint_arn, evaluate_group_itinerary(_group(original, travelers=[jane]), requirements))
        send_group_notification(endpoint_arn, evaluate_group_itinerary(_group(rescheduled, travelers=[jane]), requirements))

        bodies = [m["default"] for m in _published(endpoint_arn)]
        assert bodies[0] == "All 1 travelers are cleared for travel."
        assert bodies[1] == (
            "1 of 1 travelers need action: Jane Doe - Passport expires before departure on DL100 to DE"
        )

    def test_change_away_from_visa_country_clears_group(self, endpoint_arn, requirements):
        jane = _traveler("Jane", "Doe", nationality="US", passport_expiry=date(2035, 1, 1))
        original = make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        changed = make_segment(destination="JP", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))

        send_group_notification(endpoint_arn, evaluate_group_itinerary(_group(original, travelers=[jane]), requirements))
        send_group_notification(endpoint_arn, evaluate_group_itinerary(_group(changed, travelers=[jane]), requirements))

        bodies = [m["default"] for m in _published(endpoint_arn)]
        assert bodies == [
            "1 of 1 travelers need action: Jane Doe - No visa on file for CN",
            "All 1 travelers are cleared for travel.",
        ]
