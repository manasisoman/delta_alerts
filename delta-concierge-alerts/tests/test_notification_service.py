"""Tests for the SNS push notification service.

Covers the per-traveler push envelope (APNS/GCM/default), the group summary
notification's action-item filtering and body text, the group severity
payload, and group notifications driven by real evaluations (layovers,
per-country passport validity, visa limits, and itinerary changes).
"""

import dataclasses
import json
from datetime import date

import boto3
import moto
import pytest
from botocore.exceptions import ClientError
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


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and create an SNS platform endpoint for every test."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _published_messages(endpoint_arn: str) -> list[dict]:
    """Return every message moto recorded for the endpoint, JSON-decoded."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    endpoint = backend.platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _last_message(endpoint_arn: str) -> dict:
    """Return the most recent message published to the endpoint, with the
    nested APNS/GCM strings decoded."""
    message = _published_messages(endpoint_arn)[-1]
    message["APNS"] = json.loads(message["APNS"])
    message["GCM"] = json.loads(message["GCM"])
    return message


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    """Build a valid NotificationPayload with optional field overrides."""
    alert = AlertRecord(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=AlertType.VISA,
        severity=AlertSeverity.CRITICAL,
        reasons=["No visa on file for CN"],
        created_at="2030-01-01T00:00:00+00:00",
        itinerary_ref="DL-TEST",
    )
    fields = {
        "endpoint_arn": endpoint_arn,
        "alert_record": alert,
        "title": "Delta Concierge \u2014 Visa Alert",
        "body": "No visa on file for CN",
        "push_data": {"alert_id": "alert-1", "severity": "CRITICAL"},
    }
    fields.update(overrides)
    return NotificationPayload(**fields)


def _passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation; defaults to a clean (no-alert) result."""
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _visa_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    """Build a VisaEvaluation; defaults to a clean (no-alert) result."""
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport: PassportEvaluation | None = None,
    visa: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary; defaults to a clean traveler."""
    return TravelerAlertSummary(
        skymiles_number=f"SM-{first_name}",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport or _passport_result(),
        visa_result=visa or _visa_result(),
    )


def _group_result(
    *summaries: TravelerAlertSummary,
    confirmation: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult from traveler summaries."""
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


def _traveler(first_name: str, **profile_kwargs):
    """Build a named SkyMilesProfile for group itineraries."""
    profile = make_profile(**profile_kwargs)
    return dataclasses.replace(
        profile, skymiles_number=f"SM-{first_name}", first_name=first_name, last_name="Doe"
    )


def _critical_passport(reason: str = "Passport has expired") -> PassportEvaluation:
    return _passport_result(True, AlertSeverity.CRITICAL, [reason])


# ------------------------------------------------------------------
# send_push_notification
# ------------------------------------------------------------------

class TestPushNotificationEnvelope:
    """A single-traveler push is published as a JSON multi-platform message."""

    def test_returns_sns_message_id(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        response = send_push_notification(payload)

        assert response["MessageId"]
        assert len(_published_messages(endpoint_arn)) == 1

    def test_default_message_is_body(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        assert _last_message(endpoint_arn)["default"] == "No visa on file for CN"

    def test_apns_contains_alert_and_sound(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        aps = _last_message(endpoint_arn)["APNS"]["aps"]
        assert aps["alert"] == {
            "title": "Delta Concierge \u2014 Visa Alert",
            "body": "No visa on file for CN",
        }
        assert aps["sound"] == "default"

    def test_apns_merges_push_data_at_top_level(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        apns = _last_message(endpoint_arn)["APNS"]
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "CRITICAL"

    def test_gcm_contains_notification_and_data(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        gcm = _last_message(endpoint_arn)["GCM"]
        assert gcm["notification"] == {
            "title": "Delta Concierge \u2014 Visa Alert",
            "body": "No visa on file for CN",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "CRITICAL"}


class TestPushNotificationPushData:
    """push_data is optional and passed through unchanged."""

    def test_empty_push_data_leaves_only_aps(self, endpoint_arn):
        payload = _payload(endpoint_arn, push_data={})

        send_push_notification(payload)

        message = _last_message(endpoint_arn)
        assert list(message["APNS"].keys()) == ["aps"]
        assert message["GCM"]["data"] == {}

    def test_numeric_string_values_survive_round_trip(self, endpoint_arn):
        payload = _payload(endpoint_arn, push_data={"skymiles_number": "1234567890"})

        send_push_notification(payload)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["skymiles_number"] == "1234567890"
        assert message["GCM"]["data"]["skymiles_number"] == "1234567890"


class TestPushNotificationErrors:
    """SNS errors propagate to the caller."""

    def test_disabled_endpoint_raises(self, endpoint_arn):
        notif_mod.sns_client.set_endpoint_attributes(
            EndpointArn=endpoint_arn, Attributes={"Enabled": "false"}
        )
        payload = _payload(endpoint_arn)

        with pytest.raises(ClientError):
            send_push_notification(payload)


# ------------------------------------------------------------------
# send_group_notification — body text
# ------------------------------------------------------------------

class TestGroupAllCleared:
    """When no traveler has a WARNING/CRITICAL issue, the body says all are cleared."""

    def test_all_clean_travelers(self, endpoint_arn):
        group = _group_result(_summary("Jane"), _summary("Tim"))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        group = _group_result()

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 0 travelers are cleared for travel."


class TestGroupActionItemFiltering:
    """Only alert-required WARNING/CRITICAL results become action items."""

    def test_critical_passport_included(self, endpoint_arn):
        group = _group_result(_summary("Jane", passport=_critical_passport()))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport has expired"
        )

    def test_warning_passport_included(self, endpoint_arn):
        passport = _passport_result(True, AlertSeverity.WARNING, ["Passport expiring soon"])
        group = _group_result(_summary("Jane", passport=passport))

        send_group_notification(endpoint_arn, group)

        assert "Jane Doe - Passport expiring soon" in _last_message(endpoint_arn)["default"]

    def test_info_passport_excluded(self, endpoint_arn):
        passport = _passport_result(True, AlertSeverity.INFO, ["Consider renewing"])
        group = _group_result(_summary("Jane", passport=passport))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_passport_not_alert_required_excluded(self, endpoint_arn):
        passport = _passport_result(False, AlertSeverity.CRITICAL, ["Ignored"])
        group = _group_result(_summary("Jane", passport=passport))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_passport_without_severity_excluded(self, endpoint_arn):
        passport = _passport_result(True, None, ["Ignored"])
        group = _group_result(_summary("Jane", passport=passport))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_critical_visa_included(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        group = _group_result(_summary("Tim", visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Tim Doe - No visa on file for CN"
        )

    def test_warning_visa_included(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.WARNING, ["Visa for CN expires on date of travel"])
        group = _group_result(_summary("Tim", visa=visa))

        send_group_notification(endpoint_arn, group)

        assert "Tim Doe - Visa for CN expires on date of travel" in _last_message(endpoint_arn)["default"]

    def test_info_visa_excluded(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.INFO, ["Visa for CN expires within 30 days of travel"])
        group = _group_result(_summary("Tim", visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_visa_not_alert_required_excluded(self, endpoint_arn):
        visa = _visa_result(False, AlertSeverity.CRITICAL, ["Ignored"])
        group = _group_result(_summary("Tim", visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_visa_without_severity_excluded(self, endpoint_arn):
        visa = _visa_result(True, None, ["Ignored"])
        group = _group_result(_summary("Tim", visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."


class TestGroupActionItemFormatting:
    """Reasons, issues, and travelers are joined with distinct separators."""

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn):
        passport = _passport_result(
            True, AlertSeverity.CRITICAL, ["Reason A", "Reason B"]
        )
        group = _group_result(_summary("Jane", passport=passport))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"].endswith("Jane Doe - Reason A; Reason B")

    def test_passport_and_visa_issues_joined_with_comma(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        group = _group_result(_summary("Jane", passport=_critical_passport(), visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"].endswith(
            "Jane Doe - Passport has expired, No visa on file for CN"
        )

    def test_counts_only_travelers_needing_action(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        group = _group_result(
            _summary("Jane", passport=_critical_passport()),
            _summary("Tim", visa=visa),
            _summary("Ann"),
            _summary("Bob"),
        )

        send_group_notification(endpoint_arn, group)

        assert _last_message(endpoint_arn)["default"] == (
            "2 of 4 travelers need action: "
            "Jane Doe - Passport has expired; Tim Doe - No visa on file for CN"
        )


# ------------------------------------------------------------------
# send_group_notification — payload envelope
# ------------------------------------------------------------------

class TestGroupNotificationEnvelope:
    """The group push carries title, confirmation number, and group severity."""

    def test_returns_sns_message_id(self, endpoint_arn):
        group = _group_result(_summary())

        response = send_group_notification(endpoint_arn, group)

        assert response["MessageId"]
        assert len(_published_messages(endpoint_arn)) == 1

    def test_apns_contains_title_body_and_metadata(self, endpoint_arn):
        group = _group_result(
            _summary("Jane", passport=_critical_passport()),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        message = _last_message(endpoint_arn)
        apns = message["APNS"]
        assert apns["aps"]["alert"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-GROUP"
        assert apns["group_severity"] == "CRITICAL"

    def test_gcm_contains_title_body_and_metadata(self, endpoint_arn):
        group = _group_result(
            _summary("Jane", passport=_critical_passport()),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        message = _last_message(endpoint_arn)
        gcm = message["GCM"]
        assert gcm["notification"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert gcm["data"] == {
            "confirmation_number": "DL-GROUP",
            "group_severity": "CRITICAL",
        }

    def test_confirmation_number_passed_through(self, endpoint_arn):
        group = _group_result(_summary(), confirmation="DL-XYZ123")

        send_group_notification(endpoint_arn, group)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["confirmation_number"] == "DL-XYZ123"
        assert message["GCM"]["data"]["confirmation_number"] == "DL-XYZ123"


class TestGroupSeverityValue:
    """group_severity is serialized by value, or 'OK' when None."""

    @pytest.mark.parametrize(
        "severity, expected",
        [
            (AlertSeverity.CRITICAL, "CRITICAL"),
            (AlertSeverity.WARNING, "WARNING"),
            (AlertSeverity.INFO, "INFO"),
        ],
    )
    def test_severity_value(self, endpoint_arn, severity, expected):
        group = _group_result(_summary(), group_severity=severity)

        send_group_notification(endpoint_arn, group)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["group_severity"] == expected
        assert message["GCM"]["data"]["group_severity"] == expected

    def test_none_severity_serialized_as_ok(self, endpoint_arn):
        group = _group_result(_summary(), group_severity=None)

        send_group_notification(endpoint_arn, group)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["group_severity"] == "OK"
        assert message["GCM"]["data"]["group_severity"] == "OK"


class TestGroupNotificationErrors:
    """SNS errors propagate to the caller."""

    def test_disabled_endpoint_raises(self, endpoint_arn):
        notif_mod.sns_client.set_endpoint_attributes(
            EndpointArn=endpoint_arn, Attributes={"Enabled": "false"}
        )
        group = _group_result(_summary())

        with pytest.raises(ClientError):
            send_group_notification(endpoint_arn, group)


# ------------------------------------------------------------------
# send_group_notification — driven by real evaluations
# ------------------------------------------------------------------

def _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments):
    """Evaluate a group itinerary and send the group notification."""
    group = GroupItinerary(
        confirmation_number="DL-GROUP",
        segments=list(segments),
        travelers=list(travelers),
        primary_traveler=travelers[0].skymiles_number,
    )
    result = evaluate_group_itinerary(group, base_requirements)
    send_group_notification(endpoint_arn, result)
    return _last_message(endpoint_arn)


class TestGroupLayovers:
    """Layovers alert only when the country requires a transit visa."""

    def test_layover_in_transit_visa_country_needs_action(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Jane", nationality="IN", passport_expiry=date(2035, 1, 1))]
        segments = [
            make_segment("CN", date(2030, 6, 1), date(2030, 6, 2), is_layover=True),
            make_segment("JP", date(2030, 6, 2), date(2030, 6, 3), flight_number="DL200"),
        ]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - No visa on file for CN; No visa on file for JP"
        )

    def test_layover_without_transit_visa_requirement_cleared(self, endpoint_arn, base_requirements):
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2034, 1, 1), "V-CN")
        travelers = [
            _traveler("Jane", nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        ]
        segments = [
            make_segment("JP", date(2030, 6, 1), date(2030, 6, 2), is_layover=True),
            make_segment("CN", date(2030, 6, 2), date(2030, 6, 3), flight_number="DL200"),
        ]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == "All 1 travelers are cleared for travel."


class TestGroupPassportValidityPerCountry:
    """Passport validity is judged against each destination's required months."""

    def test_below_country_minimum_needs_action(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Jane", nationality="US", passport_expiry=date(2030, 7, 1))]
        segments = [make_segment("DE", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == (
            "1 of 1 travelers need action: "
            "Jane Doe - Passport does not meet DE's 3-month validity requirement for DL100"
        )
        assert message["APNS"]["group_severity"] == "CRITICAL"

    def test_meets_country_minimum_but_under_six_months_cleared(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Jane", nationality="US", passport_expiry=date(2030, 10, 1))]
        segments = [make_segment("DE", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == "All 1 travelers are cleared for travel."
        assert message["APNS"]["group_severity"] == "INFO"

    def test_same_passport_fails_stricter_country(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Jane", nationality="US", passport_expiry=date(2030, 10, 1))]
        segments = [make_segment("JP", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == (
            "1 of 1 travelers need action: "
            "Jane Doe - Passport does not meet JP's 6-month validity requirement for DL100"
        )


class TestGroupVisaLimits:
    """Visa expiry relative to travel date controls whether action is needed."""

    def test_visa_expiring_on_travel_date_needs_action(self, endpoint_arn, base_requirements):
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2030, 6, 1), "V-CN")
        travelers = [
            _traveler("Tim", nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        ]
        segments = [make_segment("CN", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == (
            "1 of 1 travelers need action: Tim Doe - Visa for CN expires on date of travel"
        )
        assert message["APNS"]["group_severity"] == "WARNING"

    def test_visa_expiring_within_warning_window_cleared(self, endpoint_arn, base_requirements):
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2030, 6, 15), "V-CN")
        travelers = [
            _traveler("Tim", nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        ]
        segments = [make_segment("CN", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == "All 1 travelers are cleared for travel."
        assert message["APNS"]["group_severity"] == "INFO"

    def test_visa_exempt_nationality_cleared(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Tim", nationality="US", passport_expiry=date(2035, 1, 1))]
        segments = [make_segment("DE", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == "All 1 travelers are cleared for travel."
        assert message["APNS"]["group_severity"] == "OK"

    def test_mixed_group_only_non_exempt_traveler_needs_action(self, endpoint_arn, base_requirements):
        travelers = [
            _traveler("Jane", nationality="US", passport_expiry=date(2035, 1, 1)),
            _traveler("Tim", nationality="IN", passport_expiry=date(2035, 1, 1)),
        ]
        segments = [make_segment("DE", date(2030, 6, 1), date(2030, 6, 2))]

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, segments)

        assert message["default"] == (
            "1 of 2 travelers need action: Tim Doe - No visa on file for DE"
        )


class TestGroupItineraryChange:
    """Re-notifying after an itinerary change reflects the new segments."""

    def test_added_segment_triggers_action(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Jane", nationality="US", passport_expiry=date(2035, 1, 1))]
        original = [make_segment("DE", date(2030, 6, 1), date(2030, 6, 2))]
        changed = original + [
            make_segment("CN", date(2030, 6, 5), date(2030, 6, 6), flight_number="DL200", origin="DE")
        ]
        _evaluate_and_notify(endpoint_arn, base_requirements, travelers, original)

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, changed)

        assert len(_published_messages(endpoint_arn)) == 2
        assert _published_messages(endpoint_arn)[0]["default"] == (
            "All 1 travelers are cleared for travel."
        )
        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - No visa on file for CN"
        )

    def test_later_departure_breaks_passport_validity(self, endpoint_arn, base_requirements):
        travelers = [_traveler("Jane", nationality="US", passport_expiry=date(2030, 10, 1))]
        original = [make_segment("DE", date(2030, 6, 1), date(2030, 6, 2))]
        changed = [make_segment("DE", date(2030, 8, 1), date(2030, 8, 2))]
        _evaluate_and_notify(endpoint_arn, base_requirements, travelers, original)

        message = _evaluate_and_notify(endpoint_arn, base_requirements, travelers, changed)

        assert _published_messages(endpoint_arn)[0]["default"] == (
            "All 1 travelers are cleared for travel."
        )
        assert message["default"] == (
            "1 of 1 travelers need action: "
            "Jane Doe - Passport does not meet DE's 3-month validity requirement for DL100"
        )
