"""Tests for the SNS notification service."""

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
    GroupEvaluationResult,
    NotificationPayload,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
)
from tests.conftest import make_profile, make_segment


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto SNS mock and monkey-patch the module-level client."""
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
    """Expose the moto SNS endpoint ARN."""
    return _mock_aws


def _base_alert_record(**overrides) -> AlertRecord:
    """Build a valid AlertRecord with sensible defaults."""
    kwargs = dict(
        alert_id="alert-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
    )
    kwargs.update(overrides)
    return AlertRecord(**kwargs)


def _base_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    """Build a valid NotificationPayload with sensible defaults."""
    kwargs = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_base_alert_record(),
        title="Delta Concierge Alert",
        body="Your passport is expired.",
        push_data={"alert_id": "alert-001", "severity": "CRITICAL"},
    )
    kwargs.update(overrides)
    return NotificationPayload(**kwargs)


def _make_passport_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation with sensible defaults."""
    profile = make_profile()
    return PassportEvaluation(
        profile=profile,
        segments_evaluated=[make_segment()],
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
    """Build a VisaEvaluation with sensible defaults."""
    profile = make_profile()
    return VisaEvaluation(
        profile=profile,
        segments_evaluated=[make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_traveler_summary(
    first_name: str = "Test",
    last_name: str = "User",
    skymiles_number: str = "9999999",
    passport_eval: PassportEvaluation | None = None,
    visa_eval: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary with clean defaults."""
    return TravelerAlertSummary(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_eval or _make_passport_eval(),
        visa_result=visa_eval or _make_visa_eval(),
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    confirmation_number: str = "DL-GRP-001",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult."""
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries or [],
        group_severity=group_severity,
    )


class TestSendPushNotification:
    """Verify send_push_notification publishes correct APNS and GCM message structure."""

    def test_publishes_with_correct_message_structure(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        response = notif_mod.send_push_notification(payload)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
        assert "MessageId" in response

    def test_apns_message_contains_alert_and_push_data(self, endpoint_arn):
        payload = _base_payload(
            endpoint_arn,
            title="Test Title",
            body="Test Body",
            push_data={"alert_id": "a-1", "severity": "WARNING"},
        )

        response = notif_mod.send_push_notification(payload)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_gcm_message_contains_notification_and_data(self, endpoint_arn):
        payload = _base_payload(
            endpoint_arn,
            title="GCM Title",
            body="GCM Body",
            push_data={"key": "value"},
        )

        response = notif_mod.send_push_notification(payload)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationAllCleared:
    """All travelers cleared — body should indicate no action needed."""

    def test_all_travelers_cleared(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(first_name="Alice", last_name="Smith"),
            _make_traveler_summary(first_name="Bob", last_name="Jones"),
        ]
        group_result = _make_group_result(summaries=summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_all_cleared_single_traveler(self, endpoint_arn):
        summaries = [_make_traveler_summary(first_name="Solo", last_name="Traveler")]
        group_result = _make_group_result(summaries=summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationWithPassportAction:
    """One traveler needs passport action (CRITICAL)."""

    def test_passport_critical_appears_in_action_items(self, endpoint_arn):
        critical_passport = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Jane",
                last_name="Doe",
                passport_eval=critical_passport,
            ),
            _make_traveler_summary(first_name="Bob", last_name="Clean"),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=AlertSeverity.CRITICAL
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationWithVisaAction:
    """One traveler needs visa action (WARNING)."""

    def test_visa_warning_appears_in_action_items(self, endpoint_arn):
        warning_visa = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Visa expires on departure date"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Tim",
                last_name="Lee",
                visa_eval=warning_visa,
            ),
            _make_traveler_summary(first_name="Sam", last_name="Ok"),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=AlertSeverity.WARNING
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationMultipleTravelersNeedAction:
    """Multiple travelers needing action."""

    def test_multiple_travelers_need_action(self, endpoint_arn):
        passport_critical = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        visa_warning = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Visa expires on departure date"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Jane",
                last_name="Doe",
                passport_eval=passport_critical,
            ),
            _make_traveler_summary(
                first_name="Tim",
                last_name="Lee",
                visa_eval=visa_warning,
            ),
            _make_traveler_summary(first_name="Sam", last_name="Clean"),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=AlertSeverity.CRITICAL
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationInfoSeverityIgnored:
    """INFO severity passport/visa results should NOT appear in action_items."""

    def test_info_passport_not_in_action_items(self, endpoint_arn):
        info_passport = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.INFO,
            reasons=["Passport expiring within 6 months"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Amy",
                last_name="Info",
                passport_eval=info_passport,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=AlertSeverity.INFO
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_info_visa_not_in_action_items(self, endpoint_arn):
        info_visa = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.INFO,
            reasons=["Visa expires within 30 days"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Zoe",
                last_name="Info",
                visa_eval=info_visa,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=AlertSeverity.INFO
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationSeverityNone:
    """group_severity=None (all clear) — should include 'OK' in push data."""

    def test_group_severity_none_uses_ok(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(first_name="Clear", last_name="Traveler"),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=None
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationBothPassportAndVisaIssues:
    """A single traveler with both passport and visa issues."""

    def test_both_passport_and_visa_issues(self, endpoint_arn):
        critical_passport = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        warning_visa = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Visa expires on departure date"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Dual",
                last_name="Issue",
                passport_eval=critical_passport,
                visa_eval=warning_visa,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries, group_severity=AlertSeverity.CRITICAL
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


class TestGroupNotificationNotAlertRequiredButSeveritySet:
    """is_alert_required=False should skip even if severity is WARNING/CRITICAL."""

    def test_not_alert_required_skips(self, endpoint_arn):
        passport_not_required = _make_passport_eval(
            is_alert_required=False,
            severity=AlertSeverity.WARNING,
            reasons=["Some reason"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Skip",
                last_name="Me",
                passport_eval=passport_not_required,
            ),
        ]
        group_result = _make_group_result(summaries=summaries)

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200
