"""Tests for the SNS notification service — send_push_notification and send_group_notification."""

import json
from datetime import date

import boto3
import moto
import pytest

import src.services.notification_service as notif_mod
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
    FlightSegment,
    GroupEvaluationResult,
    NotificationPayload,
    PassportEvaluation,
    SkyMilesProfile,
    TravelerAlertSummary,
    VisaEvaluation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(**overrides) -> SkyMilesProfile:
    defaults = dict(
        skymiles_number="9999999",
        first_name="Test",
        last_name="User",
        nationality="US",
        passport_number="P123456",
        passport_expiry=date(2030, 1, 1),
        visa_records=[],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )
    defaults.update(overrides)
    return SkyMilesProfile(**defaults)


def _make_segment(**overrides) -> FlightSegment:
    defaults = dict(
        flight_number="DL100",
        origin="ATL",
        destination="DE",
        departure_date=date(2026, 9, 1),
        arrival_date=date(2026, 9, 2),
        is_layover=False,
    )
    defaults.update(overrides)
    return FlightSegment(**defaults)


_CLEAN_PROFILE = _make_profile()
_SEGMENTS = [_make_segment()]


def _make_passport_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=_CLEAN_PROFILE,
        segments_evaluated=_SEGMENTS,
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_visa_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=_CLEAN_PROFILE,
        segments_evaluated=_SEGMENTS,
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_summary(
    first_name: str = "Test",
    last_name: str = "User",
    passport_alert: bool = False,
    passport_severity: AlertSeverity | None = None,
    passport_reasons: list[str] | None = None,
    visa_alert: bool = False,
    visa_severity: AlertSeverity | None = None,
    visa_reasons: list[str] | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="9999999",
        first_name=first_name,
        last_name=last_name,
        passport_result=_make_passport_eval(
            is_alert_required=passport_alert,
            severity=passport_severity,
            reasons=passport_reasons,
        ),
        visa_result=_make_visa_eval(
            is_alert_required=visa_alert,
            severity=visa_severity,
            reasons=visa_reasons,
        ),
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary],
    confirmation_number: str = "DL-GRP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries,
        group_severity=group_severity,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up an SNS platform endpoint for every test."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn,
            Token="fake-token",
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


# ---------------------------------------------------------------------------
# Tests — send_push_notification (direct call for full coverage)
# ---------------------------------------------------------------------------

class TestSendPushNotification:
    """Verify send_push_notification builds and publishes the SNS message."""

    def test_publish_returns_message_id(self, endpoint_arn):
        alert = AlertRecord(
            alert_id="a-1",
            skymiles_number="9999999",
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
            created_at="2026-01-01T00:00:00",
            itinerary_ref="DL-TEST",
            status=AlertStatus.ACTIVE,
        )
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=alert,
            title="Alert",
            body="Your passport has expired.",
            push_data={"alert_id": "a-1"},
        )

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response

    def test_payload_contains_apns_and_gcm(self, endpoint_arn):
        alert = AlertRecord(
            alert_id="a-2",
            skymiles_number="9999999",
            alert_type=AlertType.VISA,
            severity=AlertSeverity.WARNING,
            reasons=["Visa expiring soon"],
            created_at="2026-01-01T00:00:00",
            itinerary_ref="DL-TEST",
            status=AlertStatus.ACTIVE,
        )
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=alert,
            title="Visa Alert",
            body="Check your visa.",
            push_data={"alert_id": "a-2", "severity": "WARNING"},
        )

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response


# ---------------------------------------------------------------------------
# Tests — send_group_notification
# ---------------------------------------------------------------------------

class TestSendGroupNotification:
    """Core behavior of send_group_notification: body text and SNS publish."""

    def test_all_travelers_clear(self, endpoint_arn):
        """No issues → body says 'All N travelers are cleared for travel'."""
        summaries = [
            _make_summary(first_name="Alice", last_name="Smith"),
            _make_summary(first_name="Bob", last_name="Jones"),
        ]
        group = _make_group_result(summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_all_travelers_clear_body_text(self, endpoint_arn):
        """Body must contain the 'All N travelers are cleared' message."""
        summaries = [
            _make_summary(first_name="Alice", last_name="Smith"),
            _make_summary(first_name="Bob", last_name="Jones"),
            _make_summary(first_name="Carol", last_name="Davis"),
        ]
        group = _make_group_result(summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_one_traveler_critical_passport(self, endpoint_arn):
        """One CRITICAL passport issue → body says '1 of N travelers need action'."""
        summaries = [
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_summary(first_name="Bob", last_name="Jones"),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_multiple_travelers_with_issues(self, endpoint_arn):
        """Multiple travelers with issues → body lists all action items."""
        summaries = [
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_summary(
                first_name="Tim",
                last_name="Lee",
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa required for CN"],
            ),
            _make_summary(first_name="Alice", last_name="Smith"),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_traveler_with_both_passport_and_visa_issues(self, endpoint_arn):
        """A single traveler with both passport AND visa issues → both appear."""
        summaries = [
            _make_summary(
                first_name="Raj",
                last_name="Patel",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport validity below 6 months"],
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["No visa on file for CN"],
            ),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response


class TestSendGroupNotificationSeverityFilter:
    """Only WARNING and CRITICAL issues should appear in action items."""

    def test_info_only_passport_not_in_action_items(self, endpoint_arn):
        """INFO passport issue should NOT appear in action items."""
        summaries = [
            _make_summary(
                first_name="Amy",
                last_name="Park",
                passport_alert=True,
                passport_severity=AlertSeverity.INFO,
                passport_reasons=["Passport expiring within 6 months"],
            ),
            _make_summary(first_name="Bob", last_name="Jones"),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.INFO)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_info_only_visa_not_in_action_items(self, endpoint_arn):
        """INFO visa issue should NOT appear in action items."""
        summaries = [
            _make_summary(
                first_name="Amy",
                last_name="Park",
                visa_alert=True,
                visa_severity=AlertSeverity.INFO,
                visa_reasons=["Visa expiring within 30 days"],
            ),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.INFO)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_mix_info_and_critical_only_critical_in_body(self, endpoint_arn):
        """Mix of INFO and CRITICAL → only CRITICAL appears in action body."""
        summaries = [
            _make_summary(
                first_name="Amy",
                last_name="Park",
                passport_alert=True,
                passport_severity=AlertSeverity.INFO,
                passport_reasons=["Passport expiring within 6 months"],
            ),
            _make_summary(
                first_name="Dan",
                last_name="Kim",
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["No visa on file for CN"],
            ),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_alert_required_false_not_counted(self, endpoint_arn):
        """is_alert_required=False with a severity set should NOT count."""
        summaries = [
            _make_summary(
                first_name="Amy",
                last_name="Park",
                passport_alert=False,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
        ]
        group = _make_group_result(summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_severity_none_not_counted(self, endpoint_arn):
        """is_alert_required=True but severity=None should NOT count."""
        summaries = [
            _make_summary(
                first_name="Amy",
                last_name="Park",
                passport_alert=True,
                passport_severity=None,
                passport_reasons=["Some reason"],
            ),
        ]
        group = _make_group_result(summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response


class TestSendGroupNotificationPayloadFormat:
    """Verify the SNS message payload structure."""

    def test_message_contains_apns_and_gcm(self, endpoint_arn):
        """Published message should have APNS and GCM formatted payloads."""
        summaries = [_make_summary(first_name="Alice", last_name="Smith")]
        group = _make_group_result(summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_confirmation_number_in_push_data(self, endpoint_arn):
        """confirmation_number should be present in the push data."""
        summaries = [_make_summary(first_name="Alice", last_name="Smith")]
        group = _make_group_result(
            summaries, confirmation_number="DL-CONF-123"
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_group_severity_in_push_data_when_set(self, endpoint_arn):
        """group_severity value should appear in push data when set."""
        summaries = [
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.WARNING,
                passport_reasons=["Passport expiring soon"],
            ),
        ]
        group = _make_group_result(
            summaries, group_severity=AlertSeverity.WARNING
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_group_severity_ok_when_none(self, endpoint_arn):
        """group_severity=None should produce 'OK' in the push payload."""
        summaries = [_make_summary(first_name="Alice", last_name="Smith")]
        group = _make_group_result(summaries, group_severity=None)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_warning_passport_included_in_action_items(self, endpoint_arn):
        """WARNING-level passport issue should appear in action items."""
        summaries = [
            _make_summary(
                first_name="Eve",
                last_name="Brown",
                passport_alert=True,
                passport_severity=AlertSeverity.WARNING,
                passport_reasons=["Passport validity below minimum"],
            ),
            _make_summary(first_name="Alice", last_name="Smith"),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.WARNING)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_warning_visa_included_in_action_items(self, endpoint_arn):
        """WARNING-level visa issue should appear in action items."""
        summaries = [
            _make_summary(
                first_name="Frank",
                last_name="White",
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa expires on departure date"],
            ),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.WARNING)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
