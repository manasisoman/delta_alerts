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
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)

from tests.conftest import make_profile, make_segment


class _RecordingSnsClient:
    """Minimal SNS stand-in that records publish calls."""

    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, **kwargs) -> dict:
        self.calls.append(kwargs)
        return {"MessageId": "recorded-message-id"}


@pytest.fixture
def sns_recorder():
    """Monkey-patch the module-level SNS client with a recording stub."""
    original = notif_mod.sns_client
    recorder = _RecordingSnsClient()
    notif_mod.sns_client = recorder
    try:
        yield recorder
    finally:
        notif_mod.sns_client = original


@pytest.fixture
def moto_sns():
    """Activate moto and patch the module-level SNS client to a real endpoint."""
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

        original = notif_mod.sns_client
        notif_mod.sns_client = sns
        try:
            yield endpoint_arn
        finally:
            notif_mod.sns_client = original


def make_alert_record(
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    alert_type: AlertType = AlertType.PASSPORT,
    reasons: list[str] | None = None,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=alert_type,
        severity=severity,
        reasons=reasons or ["Passport expires before travel"],
        created_at="2026-08-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )


def make_payload(
    endpoint_arn: str = "arn:aws:sns:us-east-1:000:endpoint/test",
    title: str = "Passport action needed",
    body: str = "Your passport expires before your trip.",
    push_data: dict | None = None,
) -> NotificationPayload:
    """Build a NotificationPayload with sensible defaults."""
    return NotificationPayload(
        endpoint_arn=endpoint_arn,
        alert_record=make_alert_record(),
        title=title,
        body=body,
        push_data=push_data if push_data is not None else {"alert_id": "alert-1"},
    )


def make_summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport_alert: bool = False,
    passport_severity: AlertSeverity | None = None,
    passport_reasons: list[str] | None = None,
    visa_alert: bool = False,
    visa_severity: AlertSeverity | None = None,
    visa_reasons: list[str] | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary, defaulting to a clean traveler."""
    profile = make_profile()
    segments = [make_segment()]
    return TravelerAlertSummary(
        skymiles_number=profile.skymiles_number,
        first_name=first_name,
        last_name=last_name,
        passport_result=PassportEvaluation(
            profile=profile,
            segments_evaluated=segments,
            is_alert_required=passport_alert,
            severity=passport_severity,
            reasons=passport_reasons or [],
            validation_errors=[],
        ),
        visa_result=VisaEvaluation(
            profile=profile,
            segments_evaluated=segments,
            is_alert_required=visa_alert,
            severity=visa_severity,
            reasons=visa_reasons or [],
            validation_errors=[],
        ),
    )


def make_group_result(
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


class TestSendPushNotification:
    """Individual push notifications carry APNS and GCM variants of the alert."""

    def test_publishes_json_message_structure(self, sns_recorder):
        payload = make_payload()

        send_push_notification(payload)

        call = sns_recorder.calls[0]
        assert call["TargetArn"] == payload.endpoint_arn
        assert call["MessageStructure"] == "json"

    def test_apns_and_gcm_carry_title_and_body(self, sns_recorder):
        payload = make_payload(title="Visa required", body="China requires a visa.")

        send_push_notification(payload)

        message = json.loads(sns_recorder.calls[0]["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert message["default"] == "China requires a visa."
        assert apns["aps"]["alert"] == {
            "title": "Visa required",
            "body": "China requires a visa.",
        }
        assert apns["aps"]["sound"] == "default"
        assert gcm["notification"] == {
            "title": "Visa required",
            "body": "China requires a visa.",
        }

    def test_push_data_merged_into_apns_and_nested_in_gcm(self, sns_recorder):
        payload = make_payload(push_data={"alert_id": "alert-42", "type": "VISA"})

        send_push_notification(payload)

        message = json.loads(sns_recorder.calls[0]["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["alert_id"] == "alert-42"
        assert apns["type"] == "VISA"
        assert gcm["data"] == {"alert_id": "alert-42", "type": "VISA"}

    def test_empty_push_data_still_publishes(self, sns_recorder):
        payload = make_payload(push_data={})

        send_push_notification(payload)

        apns = json.loads(json.loads(sns_recorder.calls[0]["Message"])["APNS"])
        assert set(apns) == {"aps"}

    def test_returns_sns_publish_response(self, moto_sns):
        payload = make_payload(endpoint_arn=moto_sns)

        response = send_push_notification(payload)

        assert "MessageId" in response


class TestGroupNotificationClean:
    """A group with no actionable issues is reported as fully cleared."""

    def test_all_travelers_cleared(self, sns_recorder):
        group = make_group_result(make_summary(), make_summary(first_name="Tim"))

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        message = json.loads(sns_recorder.calls[0]["Message"])
        assert message["default"] == "All 2 travelers are cleared for travel."

    def test_empty_group_reports_zero_travelers(self, sns_recorder):
        group = make_group_result()

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        message = json.loads(sns_recorder.calls[0]["Message"])
        assert message["default"] == "All 0 travelers are cleared for travel."

    def test_group_severity_none_serialized_as_ok(self, sns_recorder):
        group = make_group_result(make_summary(), group_severity=None)

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        message = json.loads(sns_recorder.calls[0]["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"


class TestGroupNotificationActionItems:
    """Only WARNING/CRITICAL alerts become action items in the group summary."""

    def test_single_traveler_with_passport_issue(self, sns_recorder):
        group = make_group_result(
            make_summary(
                first_name="Jane",
                passport_alert=True,
                passport_severity=AlertSeverity.WARNING,
                passport_reasons=["Passport expiring soon"],
            ),
            make_summary(first_name="Tim"),
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        body = json.loads(sns_recorder.calls[0]["Message"])["default"]
        assert body == (
            "1 of 2 travelers need action: Jane Doe - Passport expiring soon"
        )

    def test_passport_and_visa_issues_for_same_traveler(self, sns_recorder):
        group = make_group_result(
            make_summary(
                first_name="Tim",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired", "Passport number missing"],
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["Visa required for China"],
            )
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        body = json.loads(sns_recorder.calls[0]["Message"])["default"]
        assert body == (
            "1 of 1 travelers need action: Tim Doe - "
            "Passport expired; Passport number missing, Visa required for China"
        )

    def test_multiple_travelers_need_action(self, sns_recorder):
        group = make_group_result(
            make_summary(
                first_name="Jane",
                passport_alert=True,
                passport_severity=AlertSeverity.WARNING,
                passport_reasons=["Passport expiring"],
            ),
            make_summary(
                first_name="Tim",
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["Visa required for China"],
            ),
            make_summary(first_name="Ann"),
            make_summary(first_name="Bob"),
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        body = json.loads(sns_recorder.calls[0]["Message"])["default"]
        assert body == (
            "2 of 4 travelers need action: Jane Doe - Passport expiring; "
            "Tim Doe - Visa required for China"
        )

    def test_info_severity_is_not_an_action_item(self, sns_recorder):
        group = make_group_result(
            make_summary(
                passport_alert=True,
                passport_severity=AlertSeverity.INFO,
                passport_reasons=["Entry form recommended"],
                visa_alert=True,
                visa_severity=AlertSeverity.INFO,
                visa_reasons=["Visa on arrival available"],
            )
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        body = json.loads(sns_recorder.calls[0]["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_alert_required_without_severity_is_ignored(self, sns_recorder):
        group = make_group_result(
            make_summary(
                passport_alert=True,
                passport_severity=None,
                passport_reasons=["Unclassified passport issue"],
                visa_alert=True,
                visa_severity=None,
                visa_reasons=["Unclassified visa issue"],
            )
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        body = json.loads(sns_recorder.calls[0]["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_severity_without_alert_required_is_ignored(self, sns_recorder):
        group = make_group_result(
            make_summary(
                passport_alert=False,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Resolved passport issue"],
                visa_alert=False,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["Resolved visa issue"],
            )
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        body = json.loads(sns_recorder.calls[0]["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."


class TestGroupNotificationEnvelope:
    """The group publish call carries the confirmation number and severity."""

    def test_envelope_fields(self, sns_recorder):
        group = make_group_result(
            make_summary(
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["Visa required for China"],
            ),
            confirmation="DL-XYZ99",
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification("arn:aws:sns:us-east-1:000:endpoint/primary", group)

        call = sns_recorder.calls[0]
        message = json.loads(call["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert call["TargetArn"] == "arn:aws:sns:us-east-1:000:endpoint/primary"
        assert call["MessageStructure"] == "json"
        assert apns["confirmation_number"] == "DL-XYZ99"
        assert apns["group_severity"] == "CRITICAL"
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["data"]["confirmation_number"] == "DL-XYZ99"

    def test_returns_sns_publish_response(self, moto_sns):
        group = make_group_result(make_summary())

        response = send_group_notification(moto_sns, group)

        assert "MessageId" in response
