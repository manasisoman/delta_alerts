"""Tests for the SNS push notification service."""

import json
from datetime import date

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


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto SNS, create a platform endpoint, and patch the module client."""
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


def _alert_record(**overrides) -> AlertRecord:
    """Build a valid AlertRecord with optional field overrides."""
    fields = {
        "alert_id": "alert-1",
        "skymiles_number": "9999999",
        "alert_type": AlertType.PASSPORT,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["Passport expired"],
        "created_at": "2030-01-01T00:00:00Z",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    """Build a valid NotificationPayload with optional field overrides."""
    fields = {
        "endpoint_arn": endpoint_arn,
        "alert_record": _alert_record(),
        "title": "Passport Alert",
        "body": "Your passport expires soon.",
        "push_data": {"alert_id": "alert-1", "severity": "CRITICAL"},
    }
    fields.update(overrides)
    return NotificationPayload(**fields)


def _passport_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
    segments=None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=segments if segments is not None else [make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _visa_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
    segments=None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=segments if segments is not None else [make_segment()],
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
    *summaries: TravelerAlertSummary,
    confirmation: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


def _capture_publish(monkeypatch) -> dict:
    """Wrap the patched SNS client so the publish kwargs can be inspected."""
    captured: dict = {}
    real_publish = notif_mod.sns_client.publish

    def _publish(**kwargs):
        captured.update(kwargs)
        return real_publish(**kwargs)

    monkeypatch.setattr(notif_mod.sns_client, "publish", _publish)
    return captured


class TestSendPushNotification:
    """Per-traveler push builds an APNS/GCM multi-platform message for the endpoint."""

    def test_happy_path_returns_message_id(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_publishes_to_payload_endpoint_with_json_structure(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        assert captured["TargetArn"] == endpoint_arn
        assert captured["MessageStructure"] == "json"

    def test_default_message_is_body(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        payload = _payload(endpoint_arn, body="Visa required for CN")

        send_push_notification(payload)

        message = json.loads(captured["Message"])
        assert message["default"] == "Visa required for CN"

    def test_apns_message_contains_alert_and_push_data(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        payload = _payload(
            endpoint_arn,
            title="Visa Alert",
            body="Apply now",
            push_data={"alert_id": "a-9", "deep_link": "delta://alerts/a-9"},
        )

        send_push_notification(payload)

        apns = json.loads(json.loads(captured["Message"])["APNS"])
        assert apns["aps"]["alert"] == {"title": "Visa Alert", "body": "Apply now"}
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "a-9"
        assert apns["deep_link"] == "delta://alerts/a-9"

    def test_gcm_message_contains_notification_and_data(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        push_data = {"alert_id": "a-9", "severity": "WARNING"}
        payload = _payload(
            endpoint_arn, title="Visa Alert", body="Apply now", push_data=push_data
        )

        send_push_notification(payload)

        gcm = json.loads(json.loads(captured["Message"])["GCM"])
        assert gcm["notification"] == {"title": "Visa Alert", "body": "Apply now"}
        assert gcm["data"] == push_data

    def test_empty_push_data_still_publishes(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        payload = _payload(endpoint_arn, push_data={})

        response = send_push_notification(payload)

        assert "MessageId" in response
        message = json.loads(captured["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert set(apns) == {"aps"}
        assert gcm["data"] == {}


class TestGroupNotificationAllClear:
    """When no traveler has a WARNING/CRITICAL alert, the group is reported cleared."""

    def test_all_clear_body_counts_travelers(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        group = _group_result(_summary(), _summary(first_name="Tim"), _summary(first_name="Sue"))

        send_group_notification(endpoint_arn, group)

        message = json.loads(captured["Message"])
        assert message["default"] == "All 3 travelers are cleared for travel."

    def test_empty_group_is_cleared(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)

        response = send_group_notification(endpoint_arn, _group_result())

        assert "MessageId" in response
        assert json.loads(captured["Message"])["default"] == (
            "All 0 travelers are cleared for travel."
        )

    def test_none_group_severity_serialized_as_ok(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        group = _group_result(_summary(), group_severity=None)

        send_group_notification(endpoint_arn, group)

        message = json.loads(captured["Message"])
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    def test_info_severity_alert_is_not_an_action_item(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        info_passport = _passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.INFO,
            reasons=["Passport renewal recommended"],
        )
        group = _group_result(_summary(passport_result=info_passport))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "All 1 travelers are cleared for travel."
        )

    def test_alert_required_without_severity_is_ignored(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        no_severity = _visa_eval(
            is_alert_required=True, severity=None, reasons=["Unknown"]
        )
        group = _group_result(_summary(visa_result=no_severity))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "All 1 travelers are cleared for travel."
        )

    def test_severity_without_alert_required_is_ignored(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        not_required = _passport_eval(
            is_alert_required=False,
            severity=AlertSeverity.CRITICAL,
            reasons=["stale"],
        )
        group = _group_result(_summary(passport_result=not_required))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "All 1 travelers are cleared for travel."
        )


class TestGroupNotificationActionItems:
    """Travelers with WARNING/CRITICAL passport or visa alerts are listed by name."""

    def test_critical_passport_issue_listed(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        expired = _passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        group = _group_result(
            _summary(first_name="Jane", last_name="Doe", passport_result=expired),
            _summary(first_name="Tim", last_name="Roe"),
        )

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "1 of 2 travelers need action: Jane Doe - Passport expired"
        )

    def test_warning_visa_issue_listed(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        visa_warn = _visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Visa for CN expires before date of travel"],
        )
        group = _group_result(_summary(first_name="Tim", last_name="Roe", visa_result=visa_warn))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "1 of 1 travelers need action: Tim Roe - Visa for CN expires before date of travel"
        )

    def test_passport_and_visa_issues_joined_with_comma(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        passport = _passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Passport validity below 6 months for CN"],
        )
        visa = _visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["No visa on file for CN"],
        )
        group = _group_result(_summary(passport_result=passport, visa_result=visa))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport validity below 6 months for CN, No visa on file for CN"
        )

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        layover_segments = [
            make_segment(destination="CN", is_layover=True, arrival=date(2030, 3, 1)),
            make_segment(destination="JP", departure=date(2030, 3, 1), arrival=date(2030, 3, 2)),
        ]
        visa = _visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Transit visa required for layover in CN", "No visa on file for JP"],
            segments=layover_segments,
        )
        group = _group_result(_summary(visa_result=visa))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "1 of 1 travelers need action: Jane Doe - "
            "Transit visa required for layover in CN; No visa on file for JP"
        )

    def test_multiple_travelers_with_issues(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        jane = _summary(
            first_name="Jane",
            last_name="Doe",
            passport_result=_passport_eval(
                is_alert_required=True,
                severity=AlertSeverity.WARNING,
                reasons=["Passport expiring"],
            ),
        )
        tim = _summary(
            first_name="Tim",
            last_name="Roe",
            visa_result=_visa_eval(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Visa required for China"],
            ),
        )
        group = _group_result(jane, _summary(first_name="Sue"), tim, _summary(first_name="Bob"))

        send_group_notification(endpoint_arn, group)

        assert json.loads(captured["Message"])["default"] == (
            "2 of 4 travelers need action: Jane Doe - Passport expiring; "
            "Tim Roe - Visa required for China"
        )


class TestGroupNotificationEnvelope:
    """Group push targets the primary endpoint and carries confirmation + severity."""

    def test_publishes_to_primary_endpoint(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)

        response = send_group_notification(endpoint_arn, _group_result(_summary()))

        assert "MessageId" in response
        assert captured["TargetArn"] == endpoint_arn
        assert captured["MessageStructure"] == "json"

    def test_apns_carries_title_confirmation_and_severity(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        group = _group_result(
            _summary(), confirmation="DL-XYZ", group_severity=AlertSeverity.CRITICAL
        )

        send_group_notification(endpoint_arn, group)

        apns = json.loads(json.loads(captured["Message"])["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-XYZ"
        assert apns["group_severity"] == "CRITICAL"

    def test_gcm_carries_title_confirmation_and_severity(
        self, endpoint_arn, monkeypatch
    ):
        captured = _capture_publish(monkeypatch)
        group = _group_result(
            _summary(), confirmation="DL-XYZ", group_severity=AlertSeverity.WARNING
        )

        send_group_notification(endpoint_arn, group)

        gcm = json.loads(json.loads(captured["Message"])["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["data"] == {"confirmation_number": "DL-XYZ", "group_severity": "WARNING"}

    def test_body_identical_across_platforms(self, endpoint_arn, monkeypatch):
        captured = _capture_publish(monkeypatch)
        group = _group_result(
            _summary(
                passport_result=_passport_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expired"],
                )
            )
        )

        send_group_notification(endpoint_arn, group)

        message = json.loads(captured["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert message["default"] == apns["aps"]["alert"]["body"] == gcm["notification"]["body"]
