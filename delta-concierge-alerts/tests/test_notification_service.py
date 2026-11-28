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
def endpoint_arn(_mock_sns):
    """Expose the moto SNS endpoint ARN created by _mock_sns."""
    return _mock_sns


def _published_messages(endpoint_arn: str) -> list[dict]:
    """Return every message published to the endpoint, JSON-decoded."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _only_message(endpoint_arn: str) -> dict:
    messages = _published_messages(endpoint_arn)
    assert len(messages) == 1
    return messages[0]


def _base_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    """Build a valid NotificationPayload with optional field overrides."""
    fields = {
        "endpoint_arn": endpoint_arn,
        "alert_record": AlertRecord(
            alert_id="alert-1",
            skymiles_number="9999999",
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.WARNING,
            reasons=["Passport expires soon"],
            created_at="2030-01-01T00:00:00Z",
            itinerary_ref="DL-TEST",
        ),
        "title": "Passport Alert",
        "body": "Your passport expires soon.",
        "push_data": {"alert_id": "alert-1", "severity": "WARNING"},
    }
    fields.update(overrides)
    return NotificationPayload(**fields)


def _passport_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=make_profile(passport_expiry=date(2035, 1, 1)),
        segments_evaluated=[make_segment(departure=date(2030, 9, 1), arrival=date(2030, 9, 2))],
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
        profile=make_profile(passport_expiry=date(2035, 1, 1)),
        segments_evaluated=[make_segment(departure=date(2030, 9, 1), arrival=date(2030, 9, 2))],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _traveler(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport: PassportEvaluation | None = None,
    visa: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number=f"SM-{first_name}",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport or _passport_eval(),
        visa_result=visa or _visa_eval(),
    )


def _group(
    *travelers: TravelerAlertSummary,
    group_severity: AlertSeverity | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=list(travelers),
        group_severity=group_severity,
    )


class TestSendPushNotification:
    """Individual pushes publish a JSON message with default, APNS and GCM variants."""

    def test_returns_sns_message_id(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_default_message_is_body(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        assert _only_message(endpoint_arn)["default"] == "Your passport expires soon."

    def test_apns_message_has_alert_and_push_data(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        apns = json.loads(_only_message(endpoint_arn)["APNS"])
        assert apns["aps"] == {
            "alert": {"title": "Passport Alert", "body": "Your passport expires soon."},
            "sound": "default",
        }
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "WARNING"

    def test_gcm_message_has_notification_and_data(self, endpoint_arn):
        payload = _base_payload(endpoint_arn)

        send_push_notification(payload)

        gcm = json.loads(_only_message(endpoint_arn)["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport expires soon.",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "WARNING"}

    def test_empty_push_data(self, endpoint_arn):
        payload = _base_payload(endpoint_arn, push_data={})

        send_push_notification(payload)

        message = _only_message(endpoint_arn)
        assert set(json.loads(message["APNS"])) == {"aps"}
        assert json.loads(message["GCM"])["data"] == {}

    def test_publishes_to_payload_endpoint(self, endpoint_arn):
        sns = notif_mod.sns_client
        app_arn = sns.list_platform_applications()["PlatformApplications"][0]["PlatformApplicationArn"]
        other_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="other-token",
        )["EndpointArn"]
        payload = _base_payload(other_arn)

        send_push_notification(payload)

        assert len(_published_messages(other_arn)) == 1
        assert _published_messages(endpoint_arn) == []


class TestGroupAllClear:
    """When no traveler has a WARNING/CRITICAL issue, the body says everyone is cleared."""

    def test_all_travelers_cleared(self, endpoint_arn):
        group = _group(_traveler("Jane"), _traveler("Tim"))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        group = _group()

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 0 travelers are cleared for travel."

    def test_info_passport_alert_not_actionable(self, endpoint_arn):
        passport = _passport_eval(True, AlertSeverity.INFO, ["Passport renew soon"])
        group = _group(_traveler(passport=passport))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_info_visa_alert_not_actionable(self, endpoint_arn):
        visa = _visa_eval(True, AlertSeverity.INFO, ["Visa note"])
        group = _group(_traveler(visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_passport_alert_without_severity_not_actionable(self, endpoint_arn):
        passport = _passport_eval(True, None, ["Unknown"])
        group = _group(_traveler(passport=passport))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_visa_alert_without_severity_not_actionable(self, endpoint_arn):
        visa = _visa_eval(True, None, ["Unknown"])
        group = _group(_traveler(visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_severity_without_alert_required_not_actionable(self, endpoint_arn):
        passport = _passport_eval(False, AlertSeverity.CRITICAL, ["Stale"])
        visa = _visa_eval(False, AlertSeverity.CRITICAL, ["Stale"])
        group = _group(_traveler(passport=passport, visa=visa))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."


class TestGroupActionItems:
    """WARNING/CRITICAL passport or visa issues become per-traveler action items."""

    def test_passport_warning(self, endpoint_arn):
        passport = _passport_eval(True, AlertSeverity.WARNING, ["passport expiring"])
        group = _group(_traveler("Jane", "Doe", passport=passport), _traveler("Tim", "Roe"))

        send_group_notification(endpoint_arn, group)

        assert (
            _only_message(endpoint_arn)["default"]
            == "1 of 2 travelers need action: Jane Doe - passport expiring"
        )

    def test_visa_critical(self, endpoint_arn):
        visa = _visa_eval(True, AlertSeverity.CRITICAL, ["visa required for China"])
        group = _group(_traveler("Tim", "Roe", visa=visa))

        send_group_notification(endpoint_arn, group)

        assert (
            _only_message(endpoint_arn)["default"]
            == "1 of 1 travelers need action: Tim Roe - visa required for China"
        )

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn):
        passport = _passport_eval(True, AlertSeverity.CRITICAL, ["expired", "no blank pages"])
        group = _group(_traveler("Jane", "Doe", passport=passport))

        send_group_notification(endpoint_arn, group)

        assert (
            _only_message(endpoint_arn)["default"]
            == "1 of 1 travelers need action: Jane Doe - expired; no blank pages"
        )

    def test_passport_and_visa_issues_joined_with_comma(self, endpoint_arn):
        passport = _passport_eval(True, AlertSeverity.WARNING, ["passport expiring"])
        visa = _visa_eval(True, AlertSeverity.CRITICAL, ["visa missing"])
        group = _group(_traveler("Jane", "Doe", passport=passport, visa=visa))

        send_group_notification(endpoint_arn, group)

        assert (
            _only_message(endpoint_arn)["default"]
            == "1 of 1 travelers need action: Jane Doe - passport expiring, visa missing"
        )

    def test_multiple_travelers_need_action(self, endpoint_arn):
        jane = _traveler(
            "Jane", "Doe",
            passport=_passport_eval(True, AlertSeverity.WARNING, ["passport expiring"]),
        )
        tim = _traveler(
            "Tim", "Roe",
            visa=_visa_eval(True, AlertSeverity.CRITICAL, ["visa required for China"]),
        )
        group = _group(jane, _traveler("Ann", "Poe"), tim, _traveler("Bob", "Loe"))

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == (
            "2 of 4 travelers need action: Jane Doe - passport expiring; "
            "Tim Roe - visa required for China"
        )


class TestGroupPayloadStructure:
    """Group pushes carry title, confirmation number and group severity for APNS and GCM."""

    def test_returns_sns_message_id(self, endpoint_arn):
        group = _group(_traveler())

        response = send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_apns_and_gcm_structure(self, endpoint_arn):
        passport = _passport_eval(True, AlertSeverity.CRITICAL, ["expired"])
        group = _group(
            _traveler("Jane", "Doe", passport=passport),
            group_severity=AlertSeverity.CRITICAL,
            confirmation_number="DL-ABC123",
        )

        send_group_notification(endpoint_arn, group)

        message = _only_message(endpoint_arn)
        body = "1 of 1 travelers need action: Jane Doe - expired"
        title = "Delta Concierge \u2014 Group Travel Alert"
        assert json.loads(message["APNS"]) == {
            "aps": {"alert": {"title": title, "body": body}, "sound": "default"},
            "confirmation_number": "DL-ABC123",
            "group_severity": "CRITICAL",
        }
        assert json.loads(message["GCM"]) == {
            "notification": {"title": title, "body": body},
            "data": {"confirmation_number": "DL-ABC123", "group_severity": "CRITICAL"},
        }

    def test_group_severity_none_reports_ok(self, endpoint_arn):
        group = _group(_traveler(), group_severity=None)

        send_group_notification(endpoint_arn, group)

        message = _only_message(endpoint_arn)
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    def test_group_severity_warning(self, endpoint_arn):
        group = _group(_traveler(), group_severity=AlertSeverity.WARNING)

        send_group_notification(endpoint_arn, group)

        message = _only_message(endpoint_arn)
        assert json.loads(message["APNS"])["group_severity"] == "WARNING"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "WARNING"
