"""Tests for the SNS push notification service."""

import json
from datetime import date

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

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
def _mock_sns():
    """Activate moto SNS and patch the module-level client for every test."""
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


def _published_messages(endpoint_arn: str) -> list[dict]:
    """Return the decoded JSON messages moto recorded for an endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _base_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    """Build a valid NotificationPayload with optional field overrides."""
    fields = {
        "endpoint_arn": endpoint_arn,
        "alert_record": AlertRecord(
            alert_id="alert-1",
            skymiles_number="9999999",
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
            created_at="2030-01-01T00:00:00+00:00",
            itinerary_ref="DL-TEST",
        ),
        "title": "Passport Alert",
        "body": "Your passport has expired.",
        "push_data": {"alert_id": "alert-1", "severity": "CRITICAL"},
    }
    fields.update(overrides)
    return NotificationPayload(**fields)


def _summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport_alert: bool = False,
    passport_severity: AlertSeverity | None = None,
    passport_reasons: list[str] | None = None,
    visa_alert: bool = False,
    visa_severity: AlertSeverity | None = None,
    visa_reasons: list[str] | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary; defaults to a traveler with no issues."""
    profile = make_profile()
    segments = [make_segment(departure=date(2030, 9, 1), arrival=date(2030, 9, 2))]
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


def _group(
    *summaries: TravelerAlertSummary,
    group_severity: AlertSeverity | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult from traveler summaries."""
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


def _group_body(endpoint_arn: str) -> str:
    """Return the default body of the single group message sent to an endpoint."""
    (message,) = _published_messages(endpoint_arn)
    return message["default"]


class TestSendPushNotification:
    """A single-traveler push publishes one JSON message with APNS, GCM and default bodies."""

    def test_returns_sns_message_id(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        response = send_push_notification(payload)

        assert response["MessageId"]

    def test_publishes_to_payload_endpoint(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        assert len(_published_messages(endpoint_arn)) == 1

    def test_default_message_is_body(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        (message,) = _published_messages(endpoint_arn)
        assert message["default"] == "Your passport has expired."

    def test_apns_message_has_alert_sound_and_push_data(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        (message,) = _published_messages(endpoint_arn)
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Passport Alert",
            "body": "Your passport has expired.",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "CRITICAL"

    def test_gcm_message_has_notification_and_data(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        (message,) = _published_messages(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport has expired.",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "CRITICAL"}

    def test_empty_push_data(self, endpoint_arn):
        payload = _base_payload(endpoint_arn, push_data={})

        send_push_notification(payload)

        (message,) = _published_messages(endpoint_arn)
        assert set(json.loads(message["APNS"]).keys()) == {"aps"}
        assert json.loads(message["GCM"])["data"] == {}


class TestGroupAllClear:
    """When no traveler has a WARNING/CRITICAL issue, the group is reported as cleared."""

    def test_all_travelers_cleared(self, endpoint_arn):
        group = _group(_summary("Jane"), _summary("Tim"))

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        group = _group()

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 0 travelers are cleared for travel."

    def test_info_passport_alert_not_actionable(self, endpoint_arn):
        group = _group(
            _summary(
                passport_alert=True,
                passport_severity=AlertSeverity.INFO,
                passport_reasons=["Passport expires within 6 months"],
            )
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_info_visa_alert_not_actionable(self, endpoint_arn):
        group = _group(
            _summary(
                visa_alert=True,
                visa_severity=AlertSeverity.INFO,
                visa_reasons=["eVisa recommended"],
            )
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_passport_alert_without_severity_not_actionable(self, endpoint_arn):
        group = _group(
            _summary(passport_alert=True, passport_reasons=["Unknown"])
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_visa_alert_without_severity_not_actionable(self, endpoint_arn):
        group = _group(_summary(visa_alert=True, visa_reasons=["Unknown"]))

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_critical_severity_without_alert_flag_not_actionable(self, endpoint_arn):
        group = _group(
            _summary(
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["Visa required"],
            )
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."


class TestGroupActionItems:
    """Travelers with WARNING/CRITICAL passport or visa issues are listed in the summary."""

    def test_critical_passport_issue(self, endpoint_arn):
        group = _group(
            _summary(
                "Jane", "Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _summary("Tim", "Smith"),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 2 travelers need action: Jane Doe - Passport expired"
        )

    def test_warning_visa_issue(self, endpoint_arn):
        group = _group(
            _summary(
                "Tim", "Smith",
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa required for CN"],
            ),
            group_severity=AlertSeverity.WARNING,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Tim Smith - Visa required for CN"
        )

    def test_warning_passport_issue(self, endpoint_arn):
        group = _group(
            _summary(
                passport_alert=True,
                passport_severity=AlertSeverity.WARNING,
                passport_reasons=["Passport below minimum"],
            ),
            group_severity=AlertSeverity.WARNING,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - Passport below minimum"
        )

    def test_critical_visa_issue(self, endpoint_arn):
        group = _group(
            _summary(
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["Visa expired"],
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - Visa expired"
        )

    def test_passport_and_visa_issues_combined(self, endpoint_arn):
        group = _group(
            _summary(
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
                visa_alert=True,
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa required for CN"],
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: "
            "Jane Doe - Passport expired, Visa required for CN"
        )

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn):
        group = _group(
            _summary(
                passport_alert=True,
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired", "Passport missing pages"],
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: "
            "Jane Doe - Passport expired; Passport missing pages"
        )

    def test_multiple_travelers_need_action(self, endpoint_arn):
        group = _group(
            _summary(
                "Jane", "Doe",
                passport_alert=True,
                passport_severity=AlertSeverity.WARNING,
                passport_reasons=["passport expiring"],
            ),
            _summary(
                "Tim", "Smith",
                visa_alert=True,
                visa_severity=AlertSeverity.CRITICAL,
                visa_reasons=["visa required for China"],
            ),
            _summary("Ann", "Lee"),
            _summary("Bob", "Ray"),
            group_severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "2 of 4 travelers need action: "
            "Jane Doe - passport expiring; Tim Smith - visa required for China"
        )


class TestGroupMessageStructure:
    """Group pushes carry title, confirmation number and group severity for APNS and GCM."""

    def test_returns_sns_message_id(self, endpoint_arn):
        group = _group(_summary())

        response = send_group_notification(endpoint_arn, group)

        assert response["MessageId"]

    def test_apns_payload(self, endpoint_arn):
        group = _group(_summary(), group_severity=AlertSeverity.WARNING)

        send_group_notification(endpoint_arn, group)

        (message,) = _published_messages(endpoint_arn)
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Delta Concierge \u2014 Group Travel Alert",
            "body": message["default"],
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-GROUP"
        assert apns["group_severity"] == "WARNING"

    def test_gcm_payload(self, endpoint_arn):
        group = _group(_summary(), group_severity=AlertSeverity.CRITICAL)

        send_group_notification(endpoint_arn, group)

        (message,) = _published_messages(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {
            "title": "Delta Concierge \u2014 Group Travel Alert",
            "body": message["default"],
        }
        assert gcm["data"] == {
            "confirmation_number": "DL-GROUP",
            "group_severity": "CRITICAL",
        }

    def test_no_group_severity_reported_as_ok(self, endpoint_arn):
        group = _group(_summary(), group_severity=None)

        send_group_notification(endpoint_arn, group)

        (message,) = _published_messages(endpoint_arn)
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"
