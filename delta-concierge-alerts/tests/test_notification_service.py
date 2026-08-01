"""Unit tests for the SNS push notification service.

Exercises both ``send_push_notification`` (single traveler) and
``send_group_notification`` (group summary), covering every branch that
changes the published message body or SNS call.

AWS SNS is mocked with ``moto`` and the module-level ``sns_client`` is
monkey-patched to a recording wrapper, matching the pattern established in
``tests/test_lambda_handler.py``.
"""

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


class _RecordingSNS:
    """Wraps a real (moto) SNS client to capture publish kwargs."""

    def __init__(self, real):
        self._real = real
        self.calls: list[dict] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return self._real.publish(**kwargs)


@pytest.fixture
def sns_env():
    """Activate moto, create an SNS endpoint, patch the module-level client.

    Yields a tuple ``(recorder, endpoint_arn)`` where ``recorder`` captures
    every ``publish`` call made through ``notif_mod.sns_client``.
    """
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        recorder = _RecordingSNS(sns)
        orig_client = notif_mod.sns_client
        notif_mod.sns_client = recorder
        try:
            yield recorder, endpoint_arn
        finally:
            notif_mod.sns_client = orig_client


def _make_alert_record(**overrides) -> AlertRecord:
    """Build a complete AlertRecord, overriding fields by keyword."""
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="1234567890",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-01T00:00:00Z",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _make_payload(**overrides) -> NotificationPayload:
    """Build a complete NotificationPayload, overriding fields by keyword."""
    defaults = dict(
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
        alert_record=_make_alert_record(),
        title="Passport Alert",
        body="Your passport needs attention.",
        push_data={"alert_id": "alert-1", "type": "PASSPORT"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _make_passport_eval(
    is_alert_required: bool = False,
    severity=None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation varying only alert-relevant fields."""
    return PassportEvaluation(
        profile=None,
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons if reasons is not None else [],
        validation_errors=[],
    )


def _make_visa_eval(
    is_alert_required: bool = False,
    severity=None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    """Build a VisaEvaluation varying only alert-relevant fields."""
    return VisaEvaluation(
        profile=None,
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons if reasons is not None else [],
        validation_errors=[],
    )


def _make_summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport_result: PassportEvaluation | None = None,
    visa_result: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary, defaulting to no-alert results."""
    return TravelerAlertSummary(
        skymiles_number="9999999",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _make_passport_eval(),
        visa_result=visa_result or _make_visa_eval(),
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    group_severity=None,
    confirmation_number: str = "DL-GROUP",
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult, overriding fields by keyword."""
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries if summaries is not None else [],
        group_severity=group_severity,
    )


class TestSendPushNotification:
    """send_push_notification publishes a platform-structured SNS message."""

    def test_publishes_to_endpoint_arn_as_json_structure(self, sns_env):
        recorder, endpoint_arn = sns_env
        payload = _make_payload(endpoint_arn=endpoint_arn)

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response
        assert len(recorder.calls) == 1
        call = recorder.calls[0]
        assert call["TargetArn"] == endpoint_arn
        assert call["MessageStructure"] == "json"

    def test_message_contains_apns_gcm_and_default(self, sns_env):
        recorder, endpoint_arn = sns_env
        payload = _make_payload(
            endpoint_arn=endpoint_arn,
            title="My Title",
            body="My Body",
            push_data={"k": "v"},
        )

        notif_mod.send_push_notification(payload)

        message = json.loads(recorder.calls[0]["Message"])
        assert message["default"] == "My Body"
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["aps"]["alert"] == {"title": "My Title", "body": "My Body"}
        assert apns["aps"]["sound"] == "default"
        assert gcm["notification"] == {"title": "My Title", "body": "My Body"}

    def test_push_data_merged_into_apns_but_nested_in_gcm(self, sns_env):
        recorder, endpoint_arn = sns_env
        payload = _make_payload(
            endpoint_arn=endpoint_arn, push_data={"alert_id": "abc", "deep_link": "x"}
        )

        notif_mod.send_push_notification(payload)

        message = json.loads(recorder.calls[0]["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["alert_id"] == "abc"
        assert apns["deep_link"] == "x"
        assert gcm["data"] == {"alert_id": "abc", "deep_link": "x"}


class TestGroupNotificationAllClear:
    """When no traveler needs action, the body reports everyone is cleared."""

    def test_all_travelers_cleared_body(self, sns_env):
        recorder, endpoint_arn = sns_env
        group = _make_group_result(summaries=[_make_summary(), _make_summary()])

        notif_mod.send_group_notification(endpoint_arn, group)

        message = json.loads(recorder.calls[0]["Message"])
        assert message["default"] == "All 2 travelers are cleared for travel."

    def test_publishes_to_primary_endpoint(self, sns_env):
        recorder, endpoint_arn = sns_env
        group = _make_group_result(summaries=[_make_summary()])

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
        call = recorder.calls[0]
        assert call["TargetArn"] == endpoint_arn
        assert call["MessageStructure"] == "json"


class TestGroupNotificationActionItems:
    """Travelers with WARNING/CRITICAL alerts appear in the action summary."""

    def test_single_traveler_with_passport_issue(self, sns_env):
        recorder, endpoint_arn = sns_env
        summary = _make_summary(
            first_name="Jane",
            last_name="Smith",
            passport_result=_make_passport_eval(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        group = _make_group_result(summaries=[summary, _make_summary()])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(recorder.calls[0]["Message"])["default"]
        assert body == "1 of 2 travelers need action: Jane Smith - Passport expired"

    def test_visa_issue_included(self, sns_env):
        recorder, endpoint_arn = sns_env
        summary = _make_summary(
            first_name="Tim",
            last_name="Lee",
            visa_result=_make_visa_eval(
                is_alert_required=True,
                severity=AlertSeverity.WARNING,
                reasons=["Visa required for China"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        body = json.loads(
            _publish_and_get(notif_mod.send_group_notification, endpoint_arn, group, recorder)
        )["default"]

        assert body == "1 of 1 travelers need action: Tim Lee - Visa required for China"

    def test_both_passport_and_visa_issues_joined(self, sns_env):
        recorder, endpoint_arn = sns_env
        summary = _make_summary(
            first_name="Ann",
            last_name="Cho",
            passport_result=_make_passport_eval(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expiring", "Renew soon"],
            ),
            visa_result=_make_visa_eval(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["No visa on file"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(recorder.calls[0]["Message"])["default"]
        assert (
            body
            == "1 of 1 travelers need action: "
            "Ann Cho - Passport expiring; Renew soon, No visa on file"
        )

    def test_multiple_travelers_need_action(self, sns_env):
        recorder, endpoint_arn = sns_env
        s1 = _make_summary(
            first_name="Jane",
            last_name="Doe",
            passport_result=_make_passport_eval(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        s2 = _make_summary(
            first_name="Tim",
            last_name="Roe",
            visa_result=_make_visa_eval(
                is_alert_required=True,
                severity=AlertSeverity.WARNING,
                reasons=["Visa required"],
            ),
        )
        group = _make_group_result(summaries=[s1, s2, _make_summary()])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(recorder.calls[0]["Message"])["default"]
        assert body == (
            "2 of 3 travelers need action: "
            "Jane Doe - Passport expired; Tim Roe - Visa required"
        )


class TestGroupNotificationSeverityFiltering:
    """Only WARNING/CRITICAL alerts that are required count as action items."""

    def test_info_severity_is_ignored(self, sns_env):
        recorder, endpoint_arn = sns_env
        summary = _make_summary(
            passport_result=_make_passport_eval(
                is_alert_required=True,
                severity=AlertSeverity.INFO,
                reasons=["Advisory only"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(recorder.calls[0]["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_alert_not_required_is_ignored(self, sns_env):
        recorder, endpoint_arn = sns_env
        summary = _make_summary(
            visa_result=_make_visa_eval(
                is_alert_required=False,
                severity=AlertSeverity.CRITICAL,
                reasons=["Should be ignored"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(recorder.calls[0]["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_none_severity_is_ignored(self, sns_env):
        recorder, endpoint_arn = sns_env
        summary = _make_summary(
            passport_result=_make_passport_eval(
                is_alert_required=True,
                severity=None,
                reasons=["No severity"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(recorder.calls[0]["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."


class TestGroupNotificationSeverityMetadata:
    """The published payload carries confirmation number and group severity."""

    def test_group_severity_value_when_set(self, sns_env):
        recorder, endpoint_arn = sns_env
        group = _make_group_result(
            summaries=[_make_summary()],
            group_severity=AlertSeverity.CRITICAL,
            confirmation_number="DL-XYZ",
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        message = json.loads(recorder.calls[0]["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "CRITICAL"
        assert apns["confirmation_number"] == "DL-XYZ"
        assert gcm["data"]["group_severity"] == "CRITICAL"
        assert gcm["data"]["confirmation_number"] == "DL-XYZ"

    def test_group_severity_defaults_to_ok_when_none(self, sns_env):
        recorder, endpoint_arn = sns_env
        group = _make_group_result(summaries=[_make_summary()], group_severity=None)

        notif_mod.send_group_notification(endpoint_arn, group)

        message = json.loads(recorder.calls[0]["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"

    def test_title_is_group_travel_alert(self, sns_env):
        recorder, endpoint_arn = sns_env
        group = _make_group_result(summaries=[_make_summary()])

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = json.loads(json.loads(recorder.calls[0]["Message"])["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"


def _publish_and_get(fn, endpoint_arn, group, recorder):
    """Call ``fn`` and return the Message string it published."""
    fn(endpoint_arn, group)
    return recorder.calls[-1]["Message"]
