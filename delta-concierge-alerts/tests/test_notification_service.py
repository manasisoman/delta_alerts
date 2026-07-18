"""Tests for the SNS push notification service.

Covers ``send_push_notification`` (single-traveler APNS/GCM publish) and
``send_group_notification`` (aggregated group summary), including the
happy paths plus the error/edge branches: missing/invalid endpoint ARNs,
publish exceptions, empty action lists, and severity filtering.

Uses the project's established moto pattern: an ``autouse`` fixture that
sets up SNS inside ``moto.mock_aws()`` and monkey-patches the module-level
``sns_client``, restoring the original in a ``try/finally``.
"""

import json

import boto3
import moto
import pytest
from botocore.exceptions import ClientError

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


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_sns():
    """Activate moto SNS and monkey-patch the module-level client."""
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
def endpoint_arn(_mock_sns):
    """Expose the moto SNS endpoint ARN."""
    return _mock_sns


# ------------------------------------------------------------------
# Factory helpers
# ------------------------------------------------------------------

def _make_alert_record(
    alert_id: str = "alert-001",
    alert_type: AlertType = AlertType.PASSPORT,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id=alert_id,
        skymiles_number="9999999",
        alert_type=alert_type,
        severity=severity,
        reasons=["Passport expiring soon"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-TEST",
    )


def _make_payload(
    endpoint_arn: str,
    title: str = "Delta Concierge Alert",
    body: str = "Your passport needs attention.",
    push_data: dict | None = None,
) -> NotificationPayload:
    """Build a NotificationPayload with sensible defaults."""
    return NotificationPayload(
        endpoint_arn=endpoint_arn,
        alert_record=_make_alert_record(),
        title=title,
        body=body,
        push_data=push_data if push_data is not None else {"alert_id": "alert-001"},
    )


def _make_passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation with sensible defaults."""
    return PassportEvaluation(
        profile=None,
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
    """Build a VisaEvaluation with sensible defaults."""
    return VisaEvaluation(
        profile=None,
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
    """Build a TravelerAlertSummary with sensible defaults (no issues)."""
    return TravelerAlertSummary(
        skymiles_number="9999999",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _make_passport_result(),
        visa_result=visa_result or _make_visa_result(),
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    confirmation_number: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult with sensible defaults."""
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries if summaries is not None else [_make_summary()],
        group_severity=group_severity,
    )


# ------------------------------------------------------------------
# send_push_notification
# ------------------------------------------------------------------

class TestSendPushNotification:
    """Single-traveler publish builds a valid multi-platform SNS message."""

    def test_publish_returns_message_id(self, endpoint_arn):
        payload = _make_payload(endpoint_arn)

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response

    def test_publish_uses_json_message_structure(self, endpoint_arn, monkeypatch):
        captured: dict = {}

        def fake_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "mid-1"}

        monkeypatch.setattr(notif_mod.sns_client, "publish", fake_publish)
        payload = _make_payload(endpoint_arn)

        notif_mod.send_push_notification(payload)

        assert captured["MessageStructure"] == "json"
        assert captured["TargetArn"] == endpoint_arn

    def test_message_contains_apns_and_gcm_payloads(self, endpoint_arn, monkeypatch):
        captured: dict = {}

        def fake_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "mid-2"}

        monkeypatch.setattr(notif_mod.sns_client, "publish", fake_publish)
        payload = _make_payload(
            endpoint_arn,
            title="Passport Alert",
            body="Renew your passport.",
            push_data={"alert_id": "abc", "deep_link": "app://alerts/abc"},
        )

        notif_mod.send_push_notification(payload)

        message = json.loads(captured["Message"])
        assert message["default"] == "Renew your passport."

        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"]["title"] == "Passport Alert"
        assert apns["aps"]["alert"]["body"] == "Renew your passport."
        assert apns["aps"]["sound"] == "default"
        assert apns["deep_link"] == "app://alerts/abc"

        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == "Passport Alert"
        assert gcm["data"]["alert_id"] == "abc"

    def test_empty_push_data_still_publishes(self, endpoint_arn, monkeypatch):
        captured: dict = {}

        def fake_publish(**kwargs):
            captured.update(kwargs)
            return {"MessageId": "mid-3"}

        monkeypatch.setattr(notif_mod.sns_client, "publish", fake_publish)
        payload = _make_payload(endpoint_arn, push_data={})

        notif_mod.send_push_notification(payload)

        apns = json.loads(json.loads(captured["Message"])["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge Alert"

    def test_invalid_endpoint_arn_raises(self, endpoint_arn):
        payload = _make_payload("arn:aws:sns:us-east-1:000000000000:endpoint/GCM/x/does-not-exist")

        with pytest.raises(ClientError):
            notif_mod.send_push_notification(payload)

    def test_publish_exception_propagates(self, endpoint_arn, monkeypatch):
        def boom(**kwargs):
            raise RuntimeError("SNS unavailable")

        monkeypatch.setattr(notif_mod.sns_client, "publish", boom)
        payload = _make_payload(endpoint_arn)

        with pytest.raises(RuntimeError, match="SNS unavailable"):
            notif_mod.send_push_notification(payload)


# ------------------------------------------------------------------
# send_group_notification — all-clear path
# ------------------------------------------------------------------

class TestGroupAllClear:
    """When no traveler needs action the body reports everyone cleared."""

    def test_all_travelers_cleared_body(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-1"},
        )
        group = _make_group_result(
            summaries=[_make_summary(), _make_summary(first_name="Tim")],
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "All 2 travelers are cleared for travel."

    def test_all_clear_group_severity_defaults_to_ok(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-2"},
        )
        group = _make_group_result(group_severity=None)

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = json.loads(json.loads(captured["Message"])["APNS"])
        assert apns["group_severity"] == "OK"
        assert apns["confirmation_number"] == "DL-GROUP"


# ------------------------------------------------------------------
# send_group_notification — action-needed path
# ------------------------------------------------------------------

class TestGroupNeedsAction:
    """Travelers with WARNING/CRITICAL issues are summarized in the body."""

    def test_passport_issue_included(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-3"},
        )
        summary = _make_summary(
            first_name="Jane",
            last_name="Doe",
            passport_result=_make_passport_result(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "1 of 1 travelers need action: Jane Doe - Passport expired"

    def test_visa_issue_included(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-4"},
        )
        summary = _make_summary(
            first_name="Tim",
            last_name="Lee",
            visa_result=_make_visa_result(
                is_alert_required=True,
                severity=AlertSeverity.WARNING,
                reasons=["Visa required for China"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "1 of 1 travelers need action: Tim Lee - Visa required for China"

    def test_both_passport_and_visa_issues_joined(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-5"},
        )
        summary = _make_summary(
            first_name="Ann",
            last_name="Kim",
            passport_result=_make_passport_result(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired", "Below validity window"],
            ),
            visa_result=_make_visa_result(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["No visa on file"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert (
            body
            == "1 of 1 travelers need action: "
            "Ann Kim - Passport expired; Below validity window, No visa on file"
        )

    def test_partial_group_counts_only_affected(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-6"},
        )
        affected = _make_summary(
            first_name="Jane",
            last_name="Doe",
            visa_result=_make_visa_result(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["No visa on file"],
            ),
        )
        clear = _make_summary(first_name="Ok", last_name="Traveler")
        group = _make_group_result(summaries=[affected, clear, _make_summary()])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body.startswith("1 of 3 travelers need action: ")

    def test_group_severity_value_included(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-7"},
        )
        summary = _make_summary(
            visa_result=_make_visa_result(
                is_alert_required=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["No visa on file"],
            ),
        )
        group = _make_group_result(
            summaries=[summary], group_severity=AlertSeverity.CRITICAL
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        message = json.loads(captured["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "CRITICAL"
        assert gcm["data"]["group_severity"] == "CRITICAL"


# ------------------------------------------------------------------
# send_group_notification — severity filtering
# ------------------------------------------------------------------

class TestGroupSeverityFiltering:
    """Only WARNING/CRITICAL alerts count; INFO / None / not-required skip."""

    def test_info_severity_not_counted(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-8"},
        )
        summary = _make_summary(
            passport_result=_make_passport_result(
                is_alert_required=True,
                severity=AlertSeverity.INFO,
                reasons=["Passport expiring within 6 months"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_alert_required_but_severity_none_not_counted(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-9"},
        )
        summary = _make_summary(
            visa_result=_make_visa_result(
                is_alert_required=True,
                severity=None,
                reasons=["ambiguous"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_severity_set_but_not_alert_required_not_counted(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-10"},
        )
        summary = _make_summary(
            passport_result=_make_passport_result(
                is_alert_required=False,
                severity=AlertSeverity.CRITICAL,
                reasons=["stale"],
            ),
        )
        group = _make_group_result(summaries=[summary])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "All 1 travelers are cleared for travel."


# ------------------------------------------------------------------
# send_group_notification — empty group + publish plumbing
# ------------------------------------------------------------------

class TestGroupEdgeCases:
    """Empty traveler list and SNS publish plumbing for the group flow."""

    def test_empty_group_reports_zero_cleared(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-11"},
        )
        group = _make_group_result(summaries=[])

        notif_mod.send_group_notification(endpoint_arn, group)

        body = json.loads(captured["Message"])["default"]
        assert body == "All 0 travelers are cleared for travel."

    def test_group_publish_returns_message_id(self, endpoint_arn):
        group = _make_group_result()

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_group_uses_json_structure_and_target(self, endpoint_arn, monkeypatch):
        captured: dict = {}
        monkeypatch.setattr(
            notif_mod.sns_client, "publish",
            lambda **kw: captured.update(kw) or {"MessageId": "g-12"},
        )
        group = _make_group_result()

        notif_mod.send_group_notification(endpoint_arn, group)

        assert captured["MessageStructure"] == "json"
        assert captured["TargetArn"] == endpoint_arn

    def test_group_invalid_endpoint_arn_raises(self, endpoint_arn):
        group = _make_group_result()

        with pytest.raises(ClientError):
            notif_mod.send_group_notification(
                "arn:aws:sns:us-east-1:000000000000:endpoint/GCM/x/missing", group
            )
