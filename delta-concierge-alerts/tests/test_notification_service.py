"""Tests for the SNS push notification service."""

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
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)

from tests.conftest import make_profile, make_segment


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


@pytest.fixture
def published():
    """Record the kwargs of every publish call while still hitting moto."""
    calls: list[dict] = []
    real_client = notif_mod.sns_client

    class _RecordingClient:
        def publish(self, **kwargs):
            calls.append(kwargs)
            return real_client.publish(**kwargs)

    notif_mod.sns_client = _RecordingClient()
    try:
        yield calls
    finally:
        notif_mod.sns_client = real_client


def _make_passport_result(
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


def _make_visa_result(
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


def _make_summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    skymiles_number: str = "1111111",
    passport_result: PassportEvaluation | None = None,
    visa_result: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary, clean by default."""
    return TravelerAlertSummary(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _make_passport_result(),
        visa_result=visa_result or _make_visa_result(),
    )


def _make_group_result(
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


def _make_payload(
    endpoint_arn: str,
    title: str = "Passport expiring",
    body: str = "Your passport expires soon.",
    push_data: dict | None = None,
) -> NotificationPayload:
    """Build a NotificationPayload with sensible defaults."""
    record = AlertRecord(
        alert_id="alert-1",
        skymiles_number="1111111",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.WARNING,
        reasons=["Passport expires within 6 months of travel"],
        created_at="2030-01-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )
    return NotificationPayload(
        endpoint_arn=endpoint_arn,
        alert_record=record,
        title=title,
        body=body,
        push_data=push_data or {"alert_id": "alert-1"},
    )


def _default_body(published: list[dict]) -> str:
    """Return the default (plain-text) body of the published message."""
    return json.loads(published[0]["Message"])["default"]


class TestPushNotificationPublish:
    """A single-traveler payload publishes a multi-platform JSON message."""

    def test_publish_returns_message_id(self, endpoint_arn):
        payload = _make_payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_apns_and_gcm_carry_title_body_and_push_data(self, endpoint_arn, published):
        payload = _make_payload(
            endpoint_arn,
            title="Visa required",
            body="You need a visa for CN.",
            push_data={"alert_id": "alert-9", "severity": "CRITICAL"},
        )

        send_push_notification(payload)

        assert published[0]["MessageStructure"] == "json"
        assert published[0]["TargetArn"] == endpoint_arn
        message = json.loads(published[0]["Message"])
        assert message["default"] == "You need a visa for CN."
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Visa required",
            "body": "You need a visa for CN.",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["severity"] == "CRITICAL"
        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == "Visa required"
        assert gcm["data"] == {"alert_id": "alert-9", "severity": "CRITICAL"}


class TestPushNotificationErrors:
    """Publishing to an unusable endpoint ARN surfaces the boto3 ClientError."""

    def test_unknown_endpoint_arn_raises_client_error(self):
        payload = _make_payload(
            "arn:aws:sns:us-east-1:123456789012:endpoint/GCM/TestApp/deadbeef"
        )

        with pytest.raises(ClientError):
            send_push_notification(payload)

    def test_empty_endpoint_arn_raises_client_error(self):
        payload = _make_payload("")

        with pytest.raises(ClientError):
            send_push_notification(payload)


class TestGroupNotificationPublish:
    """A group summary publishes one JSON message to the primary endpoint."""

    def test_publish_returns_message_id(self, endpoint_arn):
        group_result = _make_group_result(_make_summary())

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_publish_targets_primary_endpoint_with_json_structure(
        self, endpoint_arn, published
    ):
        group_result = _make_group_result(_make_summary())

        send_group_notification(endpoint_arn, group_result)

        assert published[0]["TargetArn"] == endpoint_arn
        assert published[0]["MessageStructure"] == "json"


class TestGroupNotificationBody:
    """The summary body counts and names only travelers needing action."""

    def test_all_clear_when_no_traveler_has_alerts(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(first_name="Jane"),
            _make_summary(first_name="Tim", skymiles_number="2222222"),
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == "All 2 travelers are cleared for travel."

    def test_single_traveler_with_passport_issue_is_named(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Passport expires within 3 months of travel"],
                ),
            ),
            _make_summary(first_name="Tim", skymiles_number="2222222"),
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == (
            "1 of 2 travelers need action: "
            "Jane Doe - Passport expires within 3 months of travel"
        )

    def test_multiple_travelers_are_semicolon_separated(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Passport expiring"],
                ),
            ),
            _make_summary(
                first_name="Tim",
                last_name="Roe",
                skymiles_number="2222222",
                visa_result=_make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN"],
                ),
            ),
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == (
            "2 of 2 travelers need action: Jane Doe - Passport expiring; "
            "Tim Roe - No visa on file for CN"
        )

    def test_passport_and_visa_issues_are_comma_separated(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_make_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expired"],
                ),
                visa_result=_make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN"],
                ),
            )
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == (
            "1 of 1 travelers need action: "
            "Jane Doe - Passport expired, No visa on file for CN"
        )

    def test_multiple_reasons_are_joined_with_semicolons(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                visa_result=_make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN", "Transit visa required for CN"],
                )
            )
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert (
            "Jane Doe - No visa on file for CN; Transit visa required for CN" in body
        )

    def test_empty_group_reports_zero_travelers_cleared(self, endpoint_arn, published):
        group_result = _make_group_result()

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == "All 0 travelers are cleared for travel."


class TestGroupNotificationSeverityFiltering:
    """Only WARNING and CRITICAL alerts contribute action items."""

    def test_info_severity_is_not_an_action_item(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                visa_result=_make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.INFO,
                    reasons=["Visa expires shortly after return"],
                )
            )
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == "All 1 travelers are cleared for travel."

    def test_missing_severity_is_not_an_action_item(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                passport_result=_make_passport_result(
                    is_alert_required=True,
                    severity=None,
                    reasons=["Unclassified passport issue"],
                )
            )
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == "All 1 travelers are cleared for travel."

    def test_severity_without_alert_required_is_not_an_action_item(self, endpoint_arn, published):
        group_result = _make_group_result(
            _make_summary(
                passport_result=_make_passport_result(
                    is_alert_required=False,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Stale reason"],
                )
            )
        )

        send_group_notification(endpoint_arn, group_result)
        body = _default_body(published)

        assert body == "All 1 travelers are cleared for travel."


class TestGroupNotificationMessagePayload:
    """The APNS and GCM payloads carry the group title, confirmation, and severity."""

    def test_platform_payloads_include_confirmation_and_severity(
        self, endpoint_arn, published
    ):
        group_result = _make_group_result(
            _make_summary(
                visa_result=_make_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN"],
                )
            ),
            confirmation="DL-ABC123",
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group_result)

        message = json.loads(published[0]["Message"])
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert apns["aps"]["alert"]["body"] == message["default"]
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-ABC123"
        assert apns["group_severity"] == "CRITICAL"
        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["data"] == {
            "confirmation_number": "DL-ABC123",
            "group_severity": "CRITICAL",
        }

    def test_missing_group_severity_falls_back_to_ok(self, endpoint_arn, published):
        group_result = _make_group_result(_make_summary(), group_severity=None)

        send_group_notification(endpoint_arn, group_result)

        message = json.loads(published[0]["Message"])
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"


class TestGroupNotificationErrors:
    """Group publishes to an unusable endpoint ARN surface the ClientError."""

    def test_unknown_endpoint_arn_raises_client_error(self):
        group_result = _make_group_result(_make_summary())

        with pytest.raises(ClientError):
            send_group_notification(
                "arn:aws:sns:us-east-1:123456789012:endpoint/GCM/TestApp/deadbeef",
                group_result,
            )

    def test_empty_endpoint_arn_raises_client_error(self):
        group_result = _make_group_result(_make_summary())

        with pytest.raises(ClientError):
            send_group_notification("", group_result)
