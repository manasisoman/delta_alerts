"""Tests for the SNS push notification service."""

import json

import boto3
import moto
import pytest

import src.services.notification_service as notif_mod
from src.models.types import (
    AlertRecord,
    AlertSeverity,
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
    """Activate moto mocks and set up an SNS endpoint for every test."""
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


def make_alert_record(**overrides) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expires before departure"],
        created_at="2026-01-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def make_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    """Build a NotificationPayload with sensible defaults."""
    defaults = dict(
        endpoint_arn=endpoint_arn,
        alert_record=make_alert_record(),
        title="Delta Concierge",
        body="Your passport needs attention.",
        push_data={"alert_id": "alert-1", "severity": "CRITICAL"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def make_passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation with sensible defaults."""
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=[make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def make_visa_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    """Build a VisaEvaluation with sensible defaults."""
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=[make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def make_summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    skymiles_number: str = "1111111",
    passport_result: PassportEvaluation | None = None,
    visa_result: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary with sensible defaults (no issues)."""
    return TravelerAlertSummary(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or make_passport_result(),
        visa_result=visa_result or make_visa_result(),
    )


def make_group_result(
    *summaries: TravelerAlertSummary,
    confirmation_number: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult from traveler summaries."""
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


def published_message(payload_message: str) -> dict:
    """Decode a published SNS json-structured message into its parts."""
    outer = json.loads(payload_message)
    return {
        "default": outer["default"],
        "apns": json.loads(outer["APNS"]),
        "gcm": json.loads(outer["GCM"]),
    }


class TestSendPushNotification:
    """send_push_notification publishes a json-structured APNS/GCM message."""

    def test_publishes_and_returns_message_id(self, endpoint_arn):
        payload = make_payload(endpoint_arn)

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response

    def test_builds_apns_and_gcm_payloads(self, endpoint_arn, monkeypatch):
        captured: dict = {}

        def fake_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "fake-id"}

        monkeypatch.setattr(notif_mod.sns_client, "publish", fake_publish)
        payload = make_payload(endpoint_arn)

        notif_mod.send_push_notification(payload)

        parts = published_message(captured["Message"])
        assert captured["TargetArn"] == endpoint_arn
        assert captured["MessageStructure"] == "json"
        assert parts["default"] == payload.body
        assert parts["apns"]["aps"]["alert"] == {
            "title": payload.title,
            "body": payload.body,
        }
        assert parts["apns"]["aps"]["sound"] == "default"
        assert parts["apns"]["alert_id"] == "alert-1"
        assert parts["gcm"]["notification"] == {
            "title": payload.title,
            "body": payload.body,
        }
        assert parts["gcm"]["data"] == payload.push_data

    def test_empty_push_data_still_publishes(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        payload = make_payload(endpoint_arn, push_data={})

        notif_mod.send_push_notification(payload)

        parts = published_message(captured["Message"])
        assert parts["apns"] == {
            "aps": {
                "alert": {"title": payload.title, "body": payload.body},
                "sound": "default",
            }
        }
        assert parts["gcm"]["data"] == {}


class TestGroupNotificationNoIssues:
    """A clean group produces an all-cleared body and no action items."""

    def test_all_travelers_cleared(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(make_summary(), make_summary(first_name="Tim"))

        notif_mod.send_group_notification(endpoint_arn, group)

        parts = published_message(captured["Message"])
        assert parts["default"] == "All 2 travelers are cleared for travel."
        assert parts["apns"]["group_severity"] == "OK"
        assert parts["gcm"]["data"]["group_severity"] == "OK"

    def test_empty_group_reports_zero_travelers(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result()

        notif_mod.send_group_notification(endpoint_arn, group)

        assert (
            published_message(captured["Message"])["default"]
            == "All 0 travelers are cleared for travel."
        )

    def test_info_severity_is_not_an_action_item(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                passport_result=make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.INFO,
                    reasons=["Passport expiring within 6 months"],
                ),
                visa_result=make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.INFO,
                    reasons=["Visa expiring within 30 days"],
                ),
            ),
            group_severity=AlertSeverity.INFO,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        parts = published_message(captured["Message"])
        assert parts["default"] == "All 1 travelers are cleared for travel."
        assert parts["apns"]["group_severity"] == "INFO"

    def test_alert_not_required_is_ignored_despite_severity(
        self, endpoint_arn, monkeypatch
    ):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                passport_result=make_passport_result(
                    is_alert_required=False,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Stale reason"],
                ),
                visa_result=make_visa_result(
                    is_alert_required=False,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Stale reason"],
                ),
            )
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        assert (
            published_message(captured["Message"])["default"]
            == "All 1 travelers are cleared for travel."
        )

    def test_alert_required_without_severity_is_ignored(
        self, endpoint_arn, monkeypatch
    ):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                passport_result=make_passport_result(
                    is_alert_required=True, severity=None, reasons=["No severity"]
                ),
                visa_result=make_visa_result(
                    is_alert_required=True, severity=None, reasons=["No severity"]
                ),
            )
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        assert (
            published_message(captured["Message"])["default"]
            == "All 1 travelers are cleared for travel."
        )


class TestGroupNotificationWithIssues:
    """Travelers with WARNING/CRITICAL results become action items."""

    def test_single_passport_issue(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                passport_result=make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expired"],
                )
            ),
            make_summary(first_name="Tim", last_name="Roe"),
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        parts = published_message(captured["Message"])
        assert parts["default"] == (
            "1 of 2 travelers need action: Jane Doe - Passport expired"
        )
        assert parts["apns"]["group_severity"] == "CRITICAL"

    def test_warning_visa_issue_counts_as_action(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                first_name="Tim",
                last_name="Roe",
                visa_result=make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Visa expires on departure date"],
                ),
            ),
            group_severity=AlertSeverity.WARNING,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        assert published_message(captured["Message"])["default"] == (
            "1 of 1 travelers need action: Tim Roe - Visa expires on departure date"
        )

    def test_passport_and_visa_issues_are_comma_joined(
        self, endpoint_arn, monkeypatch
    ):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                passport_result=make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expired", "Passport below CN minimum"],
                ),
                visa_result=make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Visa required for CN"],
                ),
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        assert published_message(captured["Message"])["default"] == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport expired; Passport below CN minimum, Visa required for CN"
        )

    def test_multiple_travelers_are_semicolon_joined(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(
                passport_result=make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Passport expiring"],
                )
            ),
            make_summary(
                first_name="Tim",
                last_name="Roe",
                visa_result=make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Visa required for China"],
                ),
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        assert published_message(captured["Message"])["default"] == (
            "2 of 2 travelers need action: Jane Doe - Passport expiring; "
            "Tim Roe - Visa required for China"
        )


class TestGroupNotificationEnvelope:
    """The published envelope carries the group title, confirmation, and structure."""

    def test_envelope_fields(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client,
            "publish",
            lambda **kwargs: captured.update(kwargs) or {"MessageId": "fake-id"},
        )
        group = make_group_result(
            make_summary(), confirmation_number="DL-ABC123"
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        parts = published_message(captured["Message"])
        title = "Delta Concierge \u2014 Group Travel Alert"
        assert captured["TargetArn"] == endpoint_arn
        assert captured["MessageStructure"] == "json"
        assert parts["apns"]["aps"]["alert"]["title"] == title
        assert parts["apns"]["aps"]["sound"] == "default"
        assert parts["apns"]["confirmation_number"] == "DL-ABC123"
        assert parts["gcm"]["notification"]["title"] == title
        assert parts["gcm"]["data"]["confirmation_number"] == "DL-ABC123"
        assert parts["gcm"]["notification"]["body"] == parts["default"]

    def test_publishes_through_sns_and_returns_response(self, endpoint_arn):
        group = make_group_result(make_summary())

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
