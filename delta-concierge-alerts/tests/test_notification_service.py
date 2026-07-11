"""Unit tests for the SNS push notification service.

Exercises individual APNS/GCM payload construction in
``send_push_notification`` and the group-summary logic in
``send_group_notification`` (no actionable travelers, passport-only,
visa-only, mixed issues, and severity metadata).

External SNS calls are mocked with moto and the module-level
``sns_client`` is monkey-patched, matching the project's existing
AWS test pattern (see ``tests/test_lambda_handler.py``).
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


class _CapturingSNS:
    """Wraps a boto3 SNS client, forwarding calls while recording publishes."""

    def __init__(self, client):
        self._client = client
        self.published: list[dict] = []

    def publish(self, **kwargs):
        self.published.append(kwargs)
        return self._client.publish(**kwargs)


@pytest.fixture(autouse=True)
def _mock_sns():
    """Activate moto SNS, create an endpoint, and patch the module client."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        capturing = _CapturingSNS(sns)
        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = capturing

        try:
            yield endpoint_arn, capturing
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_sns) -> str:
    """The moto SNS endpoint ARN created for the test."""
    return _mock_sns[0]


@pytest.fixture
def capturing(_mock_sns) -> _CapturingSNS:
    """The capturing SNS wrapper recording published messages."""
    return _mock_sns[1]


def _last_published(capturing: _CapturingSNS) -> dict:
    """Decode the outer json-structured Message of the last publish call."""
    kwargs = capturing.published[-1]
    return json.loads(kwargs["Message"])


def _make_alert_record(**overrides) -> AlertRecord:
    defaults = dict(
        alert_id="a-1",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-07-11T00:00:00Z",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _make_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    defaults = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_make_alert_record(),
        title="Passport Alert",
        body="Your passport needs attention.",
        push_data={"alert_id": "a-1", "alert_type": "PASSPORT"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _make_passport_eval(
    is_alert_required: bool = False,
    severity=None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=None,
        segments_evaluated=[],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_visa_eval(
    is_alert_required: bool = False,
    severity=None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
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
    return TravelerAlertSummary(
        skymiles_number="9999999",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _make_passport_eval(),
        visa_result=visa_result or _make_visa_eval(),
    )


def _make_group_result(
    *summaries: TravelerAlertSummary,
    confirmation: str = "DL-GROUP",
    group_severity=None,
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


class TestSendPushNotificationApns:
    """APNS payload nests title/body under aps.alert and merges push_data."""

    def test_apns_alert_structure(self, endpoint_arn, capturing):
        payload = _make_payload(endpoint_arn)

        send_push = notif_mod.send_push_notification
        send_push(payload)

        message = _last_published(capturing)
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Passport Alert",
            "body": "Your passport needs attention.",
        }
        assert apns["aps"]["sound"] == "default"

    def test_apns_merges_push_data_at_top_level(self, endpoint_arn, capturing):
        payload = _make_payload(
            endpoint_arn, push_data={"alert_id": "xyz", "deep_link": "app://x"}
        )

        notif_mod.send_push_notification(payload)

        apns = json.loads(_last_published(capturing)["APNS"])
        assert apns["alert_id"] == "xyz"
        assert apns["deep_link"] == "app://x"


class TestSendPushNotificationGcm:
    """GCM payload nests title/body under notification and push_data under data."""

    def test_gcm_notification_structure(self, endpoint_arn, capturing):
        payload = _make_payload(endpoint_arn)

        notif_mod.send_push_notification(payload)

        gcm = json.loads(_last_published(capturing)["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport needs attention.",
        }

    def test_gcm_data_carries_push_data(self, endpoint_arn, capturing):
        payload = _make_payload(
            endpoint_arn, push_data={"alert_id": "xyz", "alert_type": "VISA"}
        )

        notif_mod.send_push_notification(payload)

        gcm = json.loads(_last_published(capturing)["GCM"])
        assert gcm["data"] == {"alert_id": "xyz", "alert_type": "VISA"}


class TestSendPushNotificationEnvelope:
    """The outer message uses body as default and targets the endpoint ARN."""

    def test_default_and_target(self, endpoint_arn, capturing):
        payload = _make_payload(endpoint_arn, body="Fix your docs")

        response = notif_mod.send_push_notification(payload)

        message = _last_published(capturing)
        assert message["default"] == "Fix your docs"
        assert capturing.published[-1]["TargetArn"] == endpoint_arn
        assert capturing.published[-1]["MessageStructure"] == "json"
        assert "MessageId" in response


class TestGroupNoActionableTravelers:
    """When no traveler needs action, body reports all cleared and severity OK."""

    def test_all_cleared_body(self, endpoint_arn, capturing):
        group = _make_group_result(
            _make_summary(),
            _make_summary(first_name="Tim"),
            group_severity=None,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        message = _last_published(capturing)
        assert message["default"] == "All 2 travelers are cleared for travel."
        apns = json.loads(message["APNS"])
        assert apns["group_severity"] == "OK"

    def test_info_only_is_not_actionable(self, endpoint_arn, capturing):
        group = _make_group_result(
            _make_summary(
                passport_result=_make_passport_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.INFO,
                    reasons=["Passport expiring within 6 months"],
                )
            )
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        assert _last_published(capturing)["default"] == (
            "All 1 travelers are cleared for travel."
        )


class TestGroupPassportIssue:
    """A traveler with a passport alert is listed with the passport reasons."""

    def test_passport_reason_in_body(self, endpoint_arn, capturing):
        group = _make_group_result(
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_make_passport_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expired"],
                ),
            ),
            _make_summary(first_name="Tim", last_name="Lee"),
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _last_published(capturing)["default"]
        assert body == "1 of 2 travelers need action: Jane Doe - Passport expired"


class TestGroupVisaIssue:
    """A traveler with a visa alert is listed with the visa reasons."""

    def test_visa_reason_in_body(self, endpoint_arn, capturing):
        group = _make_group_result(
            _make_summary(
                first_name="Tim",
                last_name="Lee",
                visa_result=_make_visa_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Visa required for China"],
                ),
            ),
            group_severity=AlertSeverity.WARNING,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _last_published(capturing)["default"]
        assert body == (
            "1 of 1 travelers need action: Tim Lee - Visa required for China"
        )


class TestGroupMixedIssues:
    """Passport and visa issues for one traveler are joined with a comma."""

    def test_passport_and_visa_joined(self, endpoint_arn, capturing):
        group = _make_group_result(
            _make_summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_make_passport_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expired", "Passport invalid"],
                ),
                visa_result=_make_visa_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN"],
                ),
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn, group)

        body = _last_published(capturing)["default"]
        assert body == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport expired; Passport invalid, No visa on file for CN"
        )


class TestGroupSeverityMetadata:
    """Group severity value and confirmation number ride in the push metadata."""

    def test_severity_value_in_apns_and_gcm(self, endpoint_arn, capturing):
        group = _make_group_result(
            _make_summary(
                visa_result=_make_visa_eval(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN"],
                )
            ),
            confirmation="DL-XYZ",
            group_severity=AlertSeverity.CRITICAL,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        message = _last_published(capturing)
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "CRITICAL"
        assert apns["confirmation_number"] == "DL-XYZ"
        assert gcm["data"]["group_severity"] == "CRITICAL"
        assert gcm["data"]["confirmation_number"] == "DL-XYZ"
        assert capturing.published[-1]["TargetArn"] == endpoint_arn
        assert "MessageId" in response

    def test_title_is_group_alert(self, endpoint_arn, capturing):
        group = _make_group_result(_make_summary())

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = json.loads(_last_published(capturing)["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
