"""Tests for the SNS notification service."""

import json

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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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

        orig_sns = notif_mod.sns_client
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            notif_mod.sns_client = orig_sns


@pytest.fixture
def endpoint_arn(_mock_aws):
    return _mock_aws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_alert_record(**overrides) -> AlertRecord:
    defaults = dict(
        alert_id="a-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _make_eval(
    *,
    is_alert: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
    passport: bool = True,
):
    """Build a PassportEvaluation or VisaEvaluation with minimal boilerplate."""
    profile = make_profile()
    segments = [make_segment()]
    kwargs = dict(
        profile=profile,
        segments_evaluated=segments,
        is_alert_required=is_alert,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )
    if passport:
        return PassportEvaluation(**kwargs)
    return VisaEvaluation(**kwargs)


def _make_traveler_summary(
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
        passport_result=_make_eval(
            is_alert=passport_alert,
            severity=passport_severity,
            reasons=passport_reasons,
            passport=True,
        ),
        visa_result=_make_eval(
            is_alert=visa_alert,
            severity=visa_severity,
            reasons=visa_reasons,
            passport=False,
        ),
    )


# ---------------------------------------------------------------------------
# send_push_notification
# ---------------------------------------------------------------------------

class TestSendPushNotification:
    """Direct coverage of send_push_notification message construction and SNS publish."""

    def test_publishes_and_returns_message_id(self, endpoint_arn):
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_base_alert_record(),
            title="Alert",
            body="Your passport is expired.",
            push_data={"alert_id": "a-001"},
        )

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response

    def test_message_contains_apns_and_gcm(self, endpoint_arn):
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_base_alert_record(),
            title="Travel Alert",
            body="Visa required for CN",
            push_data={"alert_id": "a-002", "severity": "CRITICAL"},
        )

        response = notif_mod.send_push_notification(payload)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


# ---------------------------------------------------------------------------
# send_group_notification — all travelers clear
# ---------------------------------------------------------------------------

class TestGroupAllClear:
    """When no traveler has WARNING or CRITICAL issues, the body reports all clear."""

    def test_all_travelers_cleared(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(first_name="Alice", last_name="Smith"),
            _make_traveler_summary(first_name="Bob", last_name="Jones"),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-001",
            traveler_summaries=summaries,
            group_severity=None,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_info_severity_excluded_from_action_items(self, endpoint_arn):
        """Traveler with only INFO severity should NOT appear in action_items."""
        summaries = [
            _make_traveler_summary(
                first_name="Carol",
                last_name="Davis",
                passport_alert=True,
                passport_severity=AlertSeverity.INFO,
                passport_reasons=["Passport expiring within 6 months"],
            ),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-002",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.INFO,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response


# ---------------------------------------------------------------------------
# send_group_notification — travelers with issues
# ---------------------------------------------------------------------------

class TestGroupWithIssues:
    """When one or more travelers have WARNING/CRITICAL issues, the body lists them."""

    def test_one_traveler_passport_critical(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="Jane",
                last_name="Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_traveler_summary(first_name="John", last_name="Doe"),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-003",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_one_traveler_visa_warning(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="Tim",
                last_name="Lee",
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa expires on departure date"],
            ),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-004",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.WARNING,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_mixed_group_multiple_action_items(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="Alice",
                last_name="A",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_traveler_summary(first_name="Bob", last_name="B"),
            _make_traveler_summary(
                first_name="Carol",
                last_name="C",
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa expires on departure date"],
            ),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-005",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_both_passport_and_visa_issues_same_traveler(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="Dana",
                last_name="Fox",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa expires on departure date"],
            ),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-006",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response


# ---------------------------------------------------------------------------
# send_group_notification — message body content verification
# ---------------------------------------------------------------------------

class TestGroupNotificationBody:
    """Verify the actual message body content sent to SNS."""

    def test_cleared_body_text(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(first_name="A", last_name="B"),
            _make_traveler_summary(first_name="C", last_name="D"),
            _make_traveler_summary(first_name="E", last_name="F"),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-100",
            traveler_summaries=summaries,
            group_severity=None,
        )

        # Spy on the message by capturing the publish call
        published = {}
        original_publish = notif_mod.sns_client.publish

        def spy_publish(**kwargs):
            published.update(kwargs)
            return original_publish(**kwargs)

        notif_mod.sns_client.publish = spy_publish

        notif_mod.send_group_notification(endpoint_arn, group_result)

        msg = json.loads(published["Message"])
        assert msg["default"] == "All 3 travelers are cleared for travel."

    def test_action_needed_body_text(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="Jane",
                last_name="Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_traveler_summary(first_name="John", last_name="Doe"),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-GRP-101",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        published = {}
        original_publish = notif_mod.sns_client.publish

        def spy_publish(**kwargs):
            published.update(kwargs)
            return original_publish(**kwargs)

        notif_mod.sns_client.publish = spy_publish

        notif_mod.send_group_notification(endpoint_arn, group_result)

        msg = json.loads(published["Message"])
        body = msg["default"]
        assert body.startswith("1 of 2 travelers need action: ")
        assert "Jane Doe - Passport expired" in body

    def test_apns_contains_confirmation_and_severity(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="X",
                last_name="Y",
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["No visa on file"],
            ),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-CONF-99",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        published = {}
        original_publish = notif_mod.sns_client.publish

        def spy_publish(**kwargs):
            published.update(kwargs)
            return original_publish(**kwargs)

        notif_mod.sns_client.publish = spy_publish

        notif_mod.send_group_notification(endpoint_arn, group_result)

        msg = json.loads(published["Message"])
        apns = json.loads(msg["APNS"])
        assert apns["confirmation_number"] == "DL-CONF-99"
        assert apns["group_severity"] == "CRITICAL"
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"

    def test_gcm_data_contains_confirmation_and_severity(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(first_name="Z", last_name="W"),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-CONF-77",
            traveler_summaries=summaries,
            group_severity=None,
        )

        published = {}
        original_publish = notif_mod.sns_client.publish

        def spy_publish(**kwargs):
            published.update(kwargs)
            return original_publish(**kwargs)

        notif_mod.sns_client.publish = spy_publish

        notif_mod.send_group_notification(endpoint_arn, group_result)

        msg = json.loads(published["Message"])
        gcm = json.loads(msg["GCM"])
        assert gcm["data"]["confirmation_number"] == "DL-CONF-77"
        assert gcm["data"]["group_severity"] == "OK"

    def test_multiple_passport_reasons_joined_with_semicolon(self, endpoint_arn):
        summaries = [
            _make_traveler_summary(
                first_name="Multi",
                last_name="Reason",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired", "Below 6-month minimum"],
            ),
        ]
        group_result = GroupEvaluationResult(
            confirmation_number="DL-MULTI",
            traveler_summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        published = {}
        original_publish = notif_mod.sns_client.publish

        def spy_publish(**kwargs):
            published.update(kwargs)
            return original_publish(**kwargs)

        notif_mod.sns_client.publish = spy_publish

        notif_mod.send_group_notification(endpoint_arn, group_result)

        msg = json.loads(published["Message"])
        body = msg["default"]
        assert "Passport expired; Below 6-month minimum" in body
