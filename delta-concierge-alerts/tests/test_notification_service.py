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
from tests.conftest import make_profile


@pytest.fixture(autouse=True)
def _mock_sns():
    """Activate moto SNS mock, create an endpoint, and patch the module client."""
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
def endpoint_arn(_mock_sns):
    """Expose the moto SNS endpoint ARN created by _mock_sns."""
    return _mock_sns


class _CapturingClient:
    """Stand-in SNS client that records publish kwargs and returns a fixed response."""

    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "captured-message-id"}


@pytest.fixture
def capture_publish():
    """Replace the module SNS client with a recorder to inspect the published message."""
    orig = notif_mod.sns_client
    client = _CapturingClient()
    notif_mod.sns_client = client
    try:
        yield client
    finally:
        notif_mod.sns_client = orig


def _make_alert_record(**overrides) -> AlertRecord:
    fields = dict(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.WARNING,
        reasons=["Passport expiring soon"],
        created_at="2026-09-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )
    fields.update(overrides)
    return AlertRecord(**fields)


def _make_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    fields = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_make_alert_record(),
        title="Passport Alert",
        body="Your passport expires soon.",
        push_data={"alert_id": "alert-1", "alert_type": "PASSPORT"},
    )
    fields.update(overrides)
    return NotificationPayload(**fields)


def _make_passport_result(
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


def _make_visa_result(
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


def _make_summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport_result: PassportEvaluation | None = None,
    visa_result: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="1111111",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _make_passport_result(),
        visa_result=visa_result or _make_visa_result(),
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    group_severity: AlertSeverity | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries if summaries is not None else [_make_summary()],
        group_severity=group_severity,
    )


def _decode_message(call: dict) -> tuple[dict, dict, dict]:
    """Return (outer, apns, gcm) dicts decoded from a captured publish call."""
    outer = json.loads(call["Message"])
    return outer, json.loads(outer["APNS"]), json.loads(outer["GCM"])


class TestSendPushNotification:
    """Individual push notifications publish a JSON-structured message to the endpoint."""

    def test_publishes_to_moto_endpoint(self, endpoint_arn):
        payload = _make_payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_message_targets_endpoint_with_json_structure(self, capture_publish):
        payload = _make_payload("arn:aws:sns:us-east-1:000:endpoint/abc")

        send_push_notification(payload)

        call = capture_publish.calls[0]
        assert call["TargetArn"] == "arn:aws:sns:us-east-1:000:endpoint/abc"
        assert call["MessageStructure"] == "json"

    def test_message_contains_title_body_and_push_data(self, capture_publish):
        payload = _make_payload(
            "arn:endpoint",
            title="Visa Alert",
            body="No visa on file for CN",
            push_data={"alert_id": "alert-9", "severity": "CRITICAL"},
        )

        send_push_notification(payload)

        outer, apns, gcm = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "No visa on file for CN"
        assert apns["aps"]["alert"] == {"title": "Visa Alert", "body": "No visa on file for CN"}
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-9"
        assert apns["severity"] == "CRITICAL"
        assert gcm["notification"] == {"title": "Visa Alert", "body": "No visa on file for CN"}
        assert gcm["data"] == {"alert_id": "alert-9", "severity": "CRITICAL"}

    def test_returns_publish_response(self, capture_publish):
        payload = _make_payload("arn:endpoint")

        response = send_push_notification(payload)

        assert response == {"MessageId": "captured-message-id"}


class TestGroupNotificationAllCleared:
    """When no traveler has a WARNING/CRITICAL alert, the body reports all cleared."""

    def test_no_alerts_reports_all_cleared(self, capture_publish):
        group = _make_group_result(summaries=[_make_summary(), _make_summary()])

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "All 2 travelers are cleared for travel."

    def test_info_severity_is_not_an_action_item(self, capture_publish):
        passport = _make_passport_result(True, AlertSeverity.INFO, ["Renew soon"])
        group = _make_group_result(summaries=[_make_summary(passport_result=passport)])

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "All 1 travelers are cleared for travel."

    def test_alert_required_without_severity_is_ignored(self, capture_publish):
        visa = _make_visa_result(True, None, ["Unknown"])
        group = _make_group_result(summaries=[_make_summary(visa_result=visa)])

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "All 1 travelers are cleared for travel."

    def test_severity_without_alert_required_is_ignored(self, capture_publish):
        passport = _make_passport_result(False, AlertSeverity.CRITICAL, ["Expired"])
        group = _make_group_result(summaries=[_make_summary(passport_result=passport)])

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "All 1 travelers are cleared for travel."

    def test_none_group_severity_encoded_as_ok(self, capture_publish):
        group = _make_group_result(group_severity=None)

        send_group_notification("arn:primary", group)

        _, apns, gcm = _decode_message(capture_publish.calls[0])
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"


class TestGroupNotificationActionItems:
    """Travelers with WARNING/CRITICAL passport or visa issues are listed by name."""

    def test_passport_warning_is_listed(self, capture_publish):
        passport = _make_passport_result(True, AlertSeverity.WARNING, ["Passport expiring"])
        group = _make_group_result(summaries=[_make_summary(passport_result=passport)])

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "1 of 1 travelers need action: Jane Doe - Passport expiring"

    def test_visa_critical_is_listed(self, capture_publish):
        visa = _make_visa_result(True, AlertSeverity.CRITICAL, ["Visa required for CN"])
        group = _make_group_result(
            summaries=[_make_summary(first_name="Tim", last_name="Lee", visa_result=visa)]
        )

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == "1 of 1 travelers need action: Tim Lee - Visa required for CN"

    def test_passport_and_visa_issues_joined_with_comma(self, capture_publish):
        passport = _make_passport_result(True, AlertSeverity.WARNING, ["Passport expiring"])
        visa = _make_visa_result(True, AlertSeverity.CRITICAL, ["Visa required for CN"])
        group = _make_group_result(
            summaries=[_make_summary(passport_result=passport, visa_result=visa)]
        )

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport expiring, Visa required for CN"
        )

    def test_multiple_reasons_joined_with_semicolon(self, capture_publish):
        passport = _make_passport_result(
            True, AlertSeverity.CRITICAL, ["Passport expired", "Passport number missing"]
        )
        group = _make_group_result(summaries=[_make_summary(passport_result=passport)])

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport expired; Passport number missing"
        )

    def test_only_flagged_travelers_counted(self, capture_publish):
        passport = _make_passport_result(True, AlertSeverity.WARNING, ["Passport expiring"])
        visa = _make_visa_result(True, AlertSeverity.CRITICAL, ["Visa required for CN"])
        group = _make_group_result(
            summaries=[
                _make_summary(first_name="Jane", passport_result=passport),
                _make_summary(first_name="Tim", visa_result=visa),
                _make_summary(first_name="Clean"),
                _make_summary(first_name="AlsoClean"),
            ]
        )

        send_group_notification("arn:primary", group)

        outer, _, _ = _decode_message(capture_publish.calls[0])
        assert outer["default"] == (
            "2 of 4 travelers need action: "
            "Jane Doe - Passport expiring; Tim Doe - Visa required for CN"
        )

    def test_group_severity_value_encoded(self, capture_publish):
        visa = _make_visa_result(True, AlertSeverity.CRITICAL, ["Visa required"])
        group = _make_group_result(
            summaries=[_make_summary(visa_result=visa)],
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification("arn:primary", group)

        _, apns, gcm = _decode_message(capture_publish.calls[0])
        assert apns["group_severity"] == "CRITICAL"
        assert gcm["data"]["group_severity"] == "CRITICAL"


class TestGroupNotificationEnvelope:
    """The group message envelope carries title, confirmation number, and SNS settings."""

    def test_publishes_to_moto_endpoint(self, endpoint_arn):
        response = send_group_notification(endpoint_arn, _make_group_result())

        assert "MessageId" in response

    def test_targets_primary_endpoint_with_json_structure(self, capture_publish):
        send_group_notification("arn:primary", _make_group_result())

        call = capture_publish.calls[0]
        assert call["TargetArn"] == "arn:primary"
        assert call["MessageStructure"] == "json"

    def test_title_and_confirmation_number_in_both_platforms(self, capture_publish):
        group = _make_group_result(confirmation_number="ABC123")

        send_group_notification("arn:primary", group)

        _, apns, gcm = _decode_message(capture_publish.calls[0])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "ABC123"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["data"]["confirmation_number"] == "ABC123"

    def test_returns_publish_response(self, capture_publish):
        response = send_group_notification("arn:primary", _make_group_result())

        assert response == {"MessageId": "captured-message-id"}
