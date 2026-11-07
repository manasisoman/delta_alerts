"""Unit tests for the SNS push notification service."""

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
    FlightSegment,
    GroupEvaluationResult,
    NotificationPayload,
    PassportEvaluation,
    SkyMilesProfile,
    TravelerAlertSummary,
    VisaEvaluation,
)
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)

GROUP_TITLE = "Delta Concierge \u2014 Group Travel Alert"


@pytest.fixture(autouse=True)
def endpoint_arn():
    """Activate moto, create an SNS platform endpoint and swap the module client."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = sns
        try:
            yield arn
        finally:
            notif_mod.sns_client = orig_sns_client


def _published(arn: str) -> list[dict]:
    """Return decoded SNS messages published to the given moto endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _only_message(arn: str) -> dict:
    messages = _published(arn)
    assert len(messages) == 1
    return messages[0]


def _profile(first_name: str = "Jane", last_name: str = "Doe") -> SkyMilesProfile:
    return SkyMilesProfile(
        skymiles_number="1234567",
        first_name=first_name,
        last_name=last_name,
        nationality="US",
        passport_number="P123456",
        passport_expiry=date(2035, 1, 1),
        visa_records=[],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )


def _segment() -> FlightSegment:
    return FlightSegment(
        flight_number="DL100",
        origin="ATL",
        destination="CDG",
        departure_date=date(2030, 6, 1),
        arrival_date=date(2030, 6, 2),
        is_layover=False,
    )


def _alert_record(**overrides) -> AlertRecord:
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="1234567",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.WARNING,
        reasons=["Passport expires soon"],
        created_at="2030-01-01T00:00:00+00:00",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _payload(arn: str, **overrides) -> NotificationPayload:
    defaults = dict(
        endpoint_arn=arn,
        alert_record=_alert_record(),
        title="Passport Alert",
        body="Your passport expires soon.",
        push_data={"alert_id": "alert-1", "severity": "WARNING"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _passport(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=_profile(),
        segments_evaluated=[_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _visa(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=_profile(),
        segments_evaluated=[_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport: PassportEvaluation | None = None,
    visa: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="1234567",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport or _passport(),
        visa_result=visa or _visa(),
    )


def _group(
    summaries: list[TravelerAlertSummary],
    group_severity: AlertSeverity | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries,
        group_severity=group_severity,
    )


class TestSendPushNotification:
    """A single-traveler push publishes a JSON message with APNS and GCM variants."""

    def test_returns_sns_message_id(self, endpoint_arn):
        response = send_push_notification(_payload(endpoint_arn))

        assert "MessageId" in response

    def test_default_message_is_body(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        message = _only_message(endpoint_arn)
        assert message["default"] == "Your passport expires soon."

    def test_apns_message_contains_alert_sound_and_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        apns = json.loads(_only_message(endpoint_arn)["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Passport Alert",
            "body": "Your passport expires soon.",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "WARNING"

    def test_gcm_message_contains_notification_and_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        gcm = json.loads(_only_message(endpoint_arn)["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport expires soon.",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "WARNING"}

    def test_empty_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn, push_data={}))

        message = _only_message(endpoint_arn)
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert set(apns.keys()) == {"aps"}
        assert gcm["data"] == {}

    def test_publishes_to_payload_endpoint(self, endpoint_arn):
        sns = notif_mod.sns_client
        app_arn = sns.list_platform_applications()["PlatformApplications"][0][
            "PlatformApplicationArn"
        ]
        other_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="other-token",
        )["EndpointArn"]

        send_push_notification(_payload(other_arn))

        assert len(_published(other_arn)) == 1
        assert _published(endpoint_arn) == []


class TestGroupNotificationAllClear:
    """When no traveler has a WARNING/CRITICAL issue, the body says all are cleared."""

    def test_all_clear_body(self, endpoint_arn):
        group = _group([_summary(), _summary(first_name="Tim")])

        response = send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
        message = _only_message(endpoint_arn)
        assert message["default"] == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group([]))

        message = _only_message(endpoint_arn)
        assert message["default"] == "All 0 travelers are cleared for travel."

    def test_group_severity_none_reports_ok(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group([_summary()], group_severity=None))

        message = _only_message(endpoint_arn)
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"


class TestGroupPassportIssues:
    """Passport issues are only listed when an alert is required at WARNING or CRITICAL."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_passport_severity_listed(self, endpoint_arn, severity):
        passport = _passport(True, severity, ["Passport expiring", "Renew now"])
        group = _group([_summary(passport=passport)], group_severity=severity)

        send_group_notification(endpoint_arn, group)

        message = _only_message(endpoint_arn)
        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport expiring; Renew now"
        )

    def test_info_passport_not_listed(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.INFO, ["Passport fine"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _only_message(endpoint_arn)["default"] == (
            "All 1 travelers are cleared for travel."
        )

    def test_passport_none_severity_not_listed(self, endpoint_arn):
        passport = _passport(True, None, ["Unknown"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _only_message(endpoint_arn)["default"] == (
            "All 1 travelers are cleared for travel."
        )

    def test_passport_alert_not_required_not_listed(self, endpoint_arn):
        passport = _passport(False, AlertSeverity.CRITICAL, ["Ignored"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _only_message(endpoint_arn)["default"] == (
            "All 1 travelers are cleared for travel."
        )


class TestGroupVisaIssues:
    """Visa issues are only listed when an alert is required at WARNING or CRITICAL."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_visa_severity_listed(self, endpoint_arn, severity):
        visa = _visa(True, severity, ["Visa required for China"])
        group = _group([_summary(first_name="Tim", last_name="Lee", visa=visa)])

        send_group_notification(endpoint_arn, group)

        assert _only_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Tim Lee - Visa required for China"
        )

    def test_info_visa_not_listed(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.INFO, ["Visa on arrival"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _only_message(endpoint_arn)["default"] == (
            "All 1 travelers are cleared for travel."
        )

    def test_visa_none_severity_not_listed(self, endpoint_arn):
        visa = _visa(True, None, ["Unknown"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _only_message(endpoint_arn)["default"] == (
            "All 1 travelers are cleared for travel."
        )

    def test_visa_alert_not_required_not_listed(self, endpoint_arn):
        visa = _visa(False, AlertSeverity.WARNING, ["Ignored"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _only_message(endpoint_arn)["default"] == (
            "All 1 travelers are cleared for travel."
        )


class TestGroupCombinedIssues:
    """Multiple travelers and multiple issues are aggregated into one summary."""

    def test_passport_and_visa_issues_joined_with_comma(self, endpoint_arn):
        summary = _summary(
            passport=_passport(True, AlertSeverity.WARNING, ["Passport expiring"]),
            visa=_visa(True, AlertSeverity.CRITICAL, ["Visa required"]),
        )

        send_group_notification(endpoint_arn, _group([summary]))

        assert _only_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport expiring, Visa required"
        )

    def test_counts_only_travelers_needing_action(self, endpoint_arn):
        summaries = [
            _summary(
                first_name="Jane",
                passport=_passport(True, AlertSeverity.WARNING, ["passport expiring"]),
            ),
            _summary(first_name="Bob", last_name="Ray"),
            _summary(
                first_name="Tim",
                last_name="Lee",
                visa=_visa(True, AlertSeverity.CRITICAL, ["visa required for China"]),
            ),
            _summary(first_name="Ann", last_name="Kim"),
        ]

        send_group_notification(
            endpoint_arn, _group(summaries, group_severity=AlertSeverity.CRITICAL)
        )

        assert _only_message(endpoint_arn)["default"] == (
            "2 of 4 travelers need action: Jane Doe - passport expiring; "
            "Tim Lee - visa required for China"
        )


class TestGroupMessageStructure:
    """Group pushes carry title, sound, confirmation number and severity on both platforms."""

    def test_apns_payload(self, endpoint_arn):
        group = _group(
            [_summary(passport=_passport(True, AlertSeverity.CRITICAL, ["Expired"]))],
            group_severity=AlertSeverity.CRITICAL,
            confirmation_number="DL-ABC123",
        )

        send_group_notification(endpoint_arn, group)

        message = _only_message(endpoint_arn)
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"]["title"] == GROUP_TITLE
        assert apns["aps"]["alert"]["body"] == message["default"]
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-ABC123"
        assert apns["group_severity"] == AlertSeverity.CRITICAL.value

    def test_gcm_payload(self, endpoint_arn):
        group = _group(
            [_summary(visa=_visa(True, AlertSeverity.WARNING, ["Visa expiring"]))],
            group_severity=AlertSeverity.WARNING,
            confirmation_number="DL-XYZ789",
        )

        send_group_notification(endpoint_arn, group)

        message = _only_message(endpoint_arn)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == GROUP_TITLE
        assert gcm["notification"]["body"] == message["default"]
        assert gcm["data"] == {
            "confirmation_number": "DL-XYZ789",
            "group_severity": AlertSeverity.WARNING.value,
        }
