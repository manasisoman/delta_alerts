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
def _mock_aws():
    """Activate moto SNS mock and patch the module-level client for every test."""
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


class _CapturingClient:
    """Stand-in SNS client that records publish kwargs and returns a fake response."""

    def __init__(self):
        self.calls: list[dict] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return {"MessageId": "fake-message-id"}


@pytest.fixture
def capture():
    """Swap in a capturing client so tests can inspect the published message."""
    client = _CapturingClient()
    orig = notif_mod.sns_client
    notif_mod.sns_client = client
    try:
        yield client
    finally:
        notif_mod.sns_client = orig


def _alert_record(**overrides) -> AlertRecord:
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.WARNING,
        reasons=["Passport expiring soon"],
        created_at="2030-01-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    defaults = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_alert_record(),
        title="Passport Alert",
        body="Your passport expires soon.",
        push_data={"alert_id": "alert-1", "alert_type": "PASSPORT"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _passport_eval(
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


def _visa_eval(
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
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport_result: PassportEvaluation | None = None,
    visa_result: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="1111111",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _passport_eval(),
        visa_result=visa_result or _visa_eval(),
    )


def _group_result(
    summaries: list[TravelerAlertSummary],
    group_severity: AlertSeverity | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=summaries,
        group_severity=group_severity,
    )


def _decode(call: dict) -> tuple[dict, dict, dict]:
    """Return (outer, apns, gcm) dicts from a captured publish call."""
    outer = json.loads(call["Message"])
    return outer, json.loads(outer["APNS"]), json.loads(outer["GCM"])


class TestSendPushNotification:
    """send_push_notification publishes a multi-platform message to the endpoint."""

    def test_happy_path_publishes_to_sns(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_message_structure_and_target(self, capture):
        payload = _payload("arn:aws:sns:us-east-1:000:endpoint/abc")

        send_push_notification(payload)

        call = capture.calls[0]
        assert call["TargetArn"] == "arn:aws:sns:us-east-1:000:endpoint/abc"
        assert call["MessageStructure"] == "json"
        outer, apns, gcm = _decode(call)
        assert outer["default"] == "Your passport expires soon."
        assert apns["aps"]["alert"] == {
            "title": "Passport Alert",
            "body": "Your passport expires soon.",
        }
        assert apns["aps"]["sound"] == "default"
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport expires soon.",
        }

    def test_push_data_merged_into_apns_and_gcm(self, capture):
        payload = _payload(
            "arn:x", push_data={"alert_id": "a-9", "deep_link": "delta://alerts/a-9"}
        )

        send_push_notification(payload)

        _, apns, gcm = _decode(capture.calls[0])
        assert apns["alert_id"] == "a-9"
        assert apns["deep_link"] == "delta://alerts/a-9"
        assert gcm["data"] == {"alert_id": "a-9", "deep_link": "delta://alerts/a-9"}

    def test_empty_push_data(self, capture):
        payload = _payload("arn:x", push_data={})

        send_push_notification(payload)

        _, apns, gcm = _decode(capture.calls[0])
        assert set(apns.keys()) == {"aps"}
        assert gcm["data"] == {}

    def test_returns_sns_response(self, capture):
        payload = _payload("arn:x")

        response = send_push_notification(payload)

        assert response == {"MessageId": "fake-message-id"}


class TestGroupNotificationAllClear:
    """Group notification when no traveler requires action."""

    def test_no_travelers_with_issues(self, capture):
        result = _group_result([_summary(), _summary(first_name="Tim")])

        send_group_notification("arn:primary", result)

        outer, apns, gcm = _decode(capture.calls[0])
        assert outer["default"] == "All 2 travelers are cleared for travel."
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"
        assert capture.calls[0]["TargetArn"] == "arn:primary"

    def test_empty_group(self, capture):
        result = _group_result([])

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == "All 0 travelers are cleared for travel."

    def test_info_severity_does_not_count_as_action(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(True, AlertSeverity.INFO, ["fyi"]),
                    visa_result=_visa_eval(True, AlertSeverity.INFO, ["fyi"]),
                )
            ]
        )

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == "All 1 travelers are cleared for travel."

    def test_alert_required_without_severity_is_ignored(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(True, None, ["x"]),
                    visa_result=_visa_eval(True, None, ["y"]),
                )
            ]
        )

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == "All 1 travelers are cleared for travel."

    def test_severity_without_alert_required_is_ignored(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(False, AlertSeverity.CRITICAL, ["x"]),
                    visa_result=_visa_eval(False, AlertSeverity.WARNING, ["y"]),
                )
            ]
        )

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == "All 1 travelers are cleared for travel."

    def test_happy_path_publishes_to_real_moto_endpoint(self, endpoint_arn):
        result = _group_result([_summary()])

        response = send_group_notification(endpoint_arn, result)

        assert "MessageId" in response


class TestGroupNotificationActionItems:
    """Group notification body lists each traveler needing action."""

    def test_passport_warning_only(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(
                        True, AlertSeverity.WARNING, ["passport expiring"]
                    )
                ),
                _summary(first_name="Tim"),
            ],
            group_severity=AlertSeverity.WARNING,
        )

        send_group_notification("arn:primary", result)

        outer, apns, gcm = _decode(capture.calls[0])
        assert outer["default"] == (
            "1 of 2 travelers need action: Jane Doe - passport expiring"
        )
        assert apns["group_severity"] == "WARNING"
        assert gcm["data"]["group_severity"] == "WARNING"

    def test_visa_critical_only(self, capture):
        result = _group_result(
            [
                _summary(
                    first_name="Tim",
                    last_name="Lee",
                    visa_result=_visa_eval(
                        True, AlertSeverity.CRITICAL, ["visa required for China"]
                    ),
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification("arn:primary", result)

        outer, apns, _ = _decode(capture.calls[0])
        assert outer["default"] == (
            "1 of 1 travelers need action: Tim Lee - visa required for China"
        )
        assert apns["group_severity"] == "CRITICAL"

    def test_passport_and_visa_issues_joined_with_comma(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(
                        True, AlertSeverity.WARNING, ["passport expiring"]
                    ),
                    visa_result=_visa_eval(
                        True, AlertSeverity.CRITICAL, ["visa required"]
                    ),
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == (
            "1 of 1 travelers need action: Jane Doe - passport expiring, visa required"
        )

    def test_multiple_reasons_joined_with_semicolon(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(
                        True, AlertSeverity.CRITICAL, ["expired", "below minimum"]
                    )
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == (
            "1 of 1 travelers need action: Jane Doe - expired; below minimum"
        )

    def test_multiple_travelers_need_action(self, capture):
        result = _group_result(
            [
                _summary(
                    passport_result=_passport_eval(
                        True, AlertSeverity.WARNING, ["passport expiring"]
                    )
                ),
                _summary(
                    first_name="Tim",
                    visa_result=_visa_eval(
                        True, AlertSeverity.CRITICAL, ["visa required for China"]
                    ),
                ),
                _summary(first_name="Ann"),
                _summary(first_name="Bob"),
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification("arn:primary", result)

        outer, _, _ = _decode(capture.calls[0])
        assert outer["default"] == (
            "2 of 4 travelers need action: "
            "Jane Doe - passport expiring; Tim Doe - visa required for China"
        )

    def test_message_metadata(self, capture):
        result = _group_result(
            [_summary(passport_result=_passport_eval(True, AlertSeverity.WARNING, ["x"]))],
            group_severity=AlertSeverity.WARNING,
            confirmation="ABC123",
        )

        response = send_group_notification("arn:primary", result)

        call = capture.calls[0]
        assert call["MessageStructure"] == "json"
        outer, apns, gcm = _decode(call)
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert apns["aps"]["alert"]["body"] == outer["default"]
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "ABC123"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["data"]["confirmation_number"] == "ABC123"
        assert response == {"MessageId": "fake-message-id"}
