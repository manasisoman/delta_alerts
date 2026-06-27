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


class _CapturingSNS:
    """Thin wrapper around a moto SNS client that records publish() calls."""

    def __init__(self, real_client):
        self._real = real_client
        self.last_publish_message: dict | None = None

    def publish(self, **kwargs):
        self.last_publish_message = json.loads(kwargs["Message"])
        return self._real.publish(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)


def _parse_body(spy: _CapturingSNS) -> str:
    """Extract the notification body text from the last captured publish."""
    return spy.last_publish_message["default"]


def _parse_apns(spy: _CapturingSNS) -> dict:
    return json.loads(spy.last_publish_message["APNS"])


def _parse_gcm(spy: _CapturingSNS) -> dict:
    return json.loads(spy.last_publish_message["GCM"])


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

        spy = _CapturingSNS(sns)
        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = spy

        try:
            yield endpoint_arn, spy
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN."""
    return _mock_aws[0]


@pytest.fixture
def sns_spy(_mock_aws):
    """Expose the capturing SNS spy."""
    return _mock_aws[1]


# ---------------------------------------------------------------------------
# Tests — send_push_notification
# ---------------------------------------------------------------------------

class TestSendPushNotification:
    """Verify send_push_notification builds correct APNS/GCM payloads."""

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

    def test_payload_contains_apns_and_gcm(self, endpoint_arn, sns_spy):
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

        notif_mod.send_push_notification(payload)

        apns = _parse_apns(sns_spy)
        gcm = _parse_gcm(sns_spy)
        assert apns["aps"]["alert"]["title"] == "Visa Alert"
        assert apns["aps"]["alert"]["body"] == "Check your visa."
        assert gcm["notification"]["title"] == "Visa Alert"
        assert gcm["data"]["alert_id"] == "a-2"


# ---------------------------------------------------------------------------
# Tests — send_group_notification
# ---------------------------------------------------------------------------

class TestSendGroupNotification:
    """Core behavior: body text varies based on traveler issue counts."""

    def test_all_travelers_clear(self, endpoint_arn, sns_spy):
        """No issues → body says 'All N travelers are cleared for travel'."""
        summaries = [
            _make_summary(first_name="Alice", last_name="Smith"),
            _make_summary(first_name="Bob", last_name="Jones"),
        ]
        group = _make_group_result(summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
        body = _parse_body(sns_spy)
        assert "All 2 travelers are cleared for travel" in body

    def test_all_travelers_clear_body_text_three(self, endpoint_arn, sns_spy):
        """Three clean travelers → body contains 'All 3 travelers are cleared'."""
        summaries = [
            _make_summary(first_name="Alice", last_name="Smith"),
            _make_summary(first_name="Bob", last_name="Jones"),
            _make_summary(first_name="Carol", last_name="Davis"),
        ]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "All 3 travelers are cleared for travel" in body

    def test_one_traveler_critical_passport(self, endpoint_arn, sns_spy):
        """One CRITICAL passport issue → body says '1 of 2 travelers need action'."""
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "1 of 2 travelers need action" in body
        assert "Jane Doe" in body
        assert "Passport expired" in body

    def test_multiple_travelers_with_issues(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "2 of 3 travelers need action" in body
        assert "Jane Doe - Passport expired" in body
        assert "Tim Lee - Visa required for CN" in body

    def test_traveler_with_both_passport_and_visa_issues(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "1 of 1 travelers need action" in body
        assert "Raj Patel" in body
        assert "Passport validity below 6 months" in body
        assert "No visa on file for CN" in body


class TestSendGroupNotificationSeverityFilter:
    """Only WARNING and CRITICAL issues should appear in action items."""

    def test_info_only_passport_not_in_action_items(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "All 2 travelers are cleared" in body
        assert "Amy Park" not in body

    def test_info_only_visa_not_in_action_items(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "All 1 travelers are cleared" in body
        assert "Amy Park" not in body

    def test_mix_info_and_critical_only_critical_in_body(self, endpoint_arn, sns_spy):
        """Mix of INFO and CRITICAL → only CRITICAL traveler appears in body."""
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "1 of 2 travelers need action" in body
        assert "Dan Kim" in body
        assert "No visa on file for CN" in body
        assert "Amy Park" not in body

    def test_alert_required_false_not_counted(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "All 1 travelers are cleared" in body
        assert "Amy Park" not in body

    def test_severity_none_not_counted(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "All 1 travelers are cleared" in body
        assert "Amy Park" not in body


class TestSendGroupNotificationPayloadFormat:
    """Verify the SNS message contains correctly structured APNS/GCM payloads."""

    def test_message_contains_apns_and_gcm(self, endpoint_arn, sns_spy):
        """Published message should have APNS and GCM formatted payloads."""
        summaries = [_make_summary(first_name="Alice", last_name="Smith")]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        msg = sns_spy.last_publish_message
        assert "APNS" in msg
        assert "GCM" in msg
        assert "default" in msg

    def test_confirmation_number_in_push_data(self, endpoint_arn, sns_spy):
        """confirmation_number should be present in both APNS and GCM push data."""
        summaries = [_make_summary(first_name="Alice", last_name="Smith")]
        group = _make_group_result(
            summaries, confirmation_number="DL-CONF-123"
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = _parse_apns(sns_spy)
        gcm = _parse_gcm(sns_spy)
        assert apns["confirmation_number"] == "DL-CONF-123"
        assert gcm["data"]["confirmation_number"] == "DL-CONF-123"

    def test_group_severity_in_push_data_when_set(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = _parse_apns(sns_spy)
        gcm = _parse_gcm(sns_spy)
        assert apns["group_severity"] == "WARNING"
        assert gcm["data"]["group_severity"] == "WARNING"

    def test_group_severity_ok_when_none(self, endpoint_arn, sns_spy):
        """group_severity=None should produce 'OK' in the push payload."""
        summaries = [_make_summary(first_name="Alice", last_name="Smith")]
        group = _make_group_result(summaries, group_severity=None)

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = _parse_apns(sns_spy)
        gcm = _parse_gcm(sns_spy)
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"

    def test_warning_passport_included_in_action_items(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "1 of 2 travelers need action" in body
        assert "Eve Brown" in body
        assert "Passport validity below minimum" in body

    def test_warning_visa_included_in_action_items(self, endpoint_arn, sns_spy):
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

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _parse_body(sns_spy)
        assert "1 of 1 travelers need action" in body
        assert "Frank White" in body
        assert "Visa expires on departure date" in body
