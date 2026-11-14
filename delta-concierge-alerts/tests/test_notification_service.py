"""Tests for the SNS push notification service (single and group alerts)."""

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


@pytest.fixture(autouse=True)
def sns():
    """Activate moto and point the module-level SNS client at it."""
    with moto.mock_aws():
        client = boto3.client("sns", region_name="us-east-1")
        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = client
        try:
            yield client
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def app_arn(sns):
    return sns.create_platform_application(
        Name="TestApp", Platform="GCM",
        Attributes={"PlatformCredential": "fake"},
    )["PlatformApplicationArn"]


@pytest.fixture
def endpoint_arn(sns, app_arn):
    return _create_endpoint(sns, app_arn, "primary-token")


def _create_endpoint(sns, app_arn: str, token: str) -> str:
    return sns.create_platform_endpoint(
        PlatformApplicationArn=app_arn, Token=token,
    )["EndpointArn"]


def _published_messages(arn: str) -> list[dict]:
    """Return decoded JSON message bodies published to a moto endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _segment() -> FlightSegment:
    return FlightSegment(
        flight_number="DL100",
        origin="ATL",
        destination="CN",
        departure_date=date(2030, 9, 1),
        arrival_date=date(2030, 9, 2),
        is_layover=False,
    )


def _profile(first_name: str = "Jane", last_name: str = "Doe") -> SkyMilesProfile:
    return SkyMilesProfile(
        skymiles_number="1111111",
        first_name=first_name,
        last_name=last_name,
        nationality="US",
        passport_number="P123",
        passport_expiry=date(2031, 1, 1),
        visa_records=[],
        endpoint_arn="",
    )


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
        skymiles_number="1111111",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport or _passport(),
        visa_result=visa or _visa(),
    )


def _group(
    summaries: list[TravelerAlertSummary] | None = None,
    group_severity: AlertSeverity | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries if summaries is not None else [_summary()],
        group_severity=group_severity,
    )


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    fields = {
        "endpoint_arn": endpoint_arn,
        "alert_record": AlertRecord(
            alert_id="alert-1",
            skymiles_number="1111111",
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


def _group_body(arn: str) -> str:
    (message,) = _published_messages(arn)
    return message["default"]


class TestSendPushNotification:
    """A single-traveler alert is published once as a multi-platform JSON message."""

    def test_returns_sns_message_id(self, endpoint_arn):
        response = send_push_notification(_payload(endpoint_arn))

        assert "MessageId" in response

    def test_publishes_exactly_one_message_to_target(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        assert len(_published_messages(endpoint_arn)) == 1

    def test_default_message_is_body(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn, body="Custom body"))

        (message,) = _published_messages(endpoint_arn)
        assert message["default"] == "Custom body"

    def test_apns_message_contains_alert_sound_and_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        apns = json.loads(_published_messages(endpoint_arn)[0]["APNS"])
        assert apns["aps"] == {
            "alert": {"title": "Passport Alert", "body": "Your passport has expired."},
            "sound": "default",
        }
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "CRITICAL"

    def test_gcm_message_contains_notification_and_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        gcm = json.loads(_published_messages(endpoint_arn)[0]["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport has expired.",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "CRITICAL"}

    def test_empty_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn, push_data={}))

        message = _published_messages(endpoint_arn)[0]
        assert set(json.loads(message["APNS"])) == {"aps"}
        assert json.loads(message["GCM"])["data"] == {}

    def test_only_target_endpoint_receives_message(self, sns, app_arn, endpoint_arn):
        other_arn = _create_endpoint(sns, app_arn, "other-token")

        send_push_notification(_payload(endpoint_arn))

        assert len(_published_messages(endpoint_arn)) == 1
        assert _published_messages(other_arn) == []


class TestGroupAllClear:
    """When no traveler has an actionable issue, the body says everyone is cleared."""

    def test_single_clean_traveler(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group())

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_multiple_clean_travelers(self, endpoint_arn):
        group = _group([_summary("A", "One"), _summary("B", "Two"), _summary("C", "Three")])

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == "All 3 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group(summaries=[]))

        assert _group_body(endpoint_arn) == "All 0 travelers are cleared for travel."


class TestGroupPassportIssues:
    """Only passport alerts that are required and WARNING/CRITICAL are actionable."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_passport_issue_listed(self, endpoint_arn, severity):
        passport = _passport(True, severity, ["Passport expiring"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - Passport expiring"
        )

    def test_info_passport_issue_not_actionable(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.INFO, ["Passport expires soon"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_passport_none_severity_not_actionable(self, endpoint_arn):
        passport = _passport(True, None, ["Unknown"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_passport_alert_not_required_not_actionable(self, endpoint_arn):
        passport = _passport(False, AlertSeverity.CRITICAL, ["Ignored"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_multiple_passport_reasons_joined_with_semicolon(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.CRITICAL, ["Expired", "Missing number"])

        send_group_notification(endpoint_arn, _group([_summary(passport=passport)]))

        assert _group_body(endpoint_arn).endswith("Jane Doe - Expired; Missing number")


class TestGroupVisaIssues:
    """Only visa alerts that are required and WARNING/CRITICAL are actionable."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_visa_issue_listed(self, endpoint_arn, severity):
        visa = _visa(True, severity, ["Visa required for China"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - Visa required for China"
        )

    def test_info_visa_issue_not_actionable(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.INFO, ["eVisa available"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_visa_none_severity_not_actionable(self, endpoint_arn):
        visa = _visa(True, None, ["Unknown"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_visa_alert_not_required_not_actionable(self, endpoint_arn):
        visa = _visa(False, AlertSeverity.CRITICAL, ["Ignored"])

        send_group_notification(endpoint_arn, _group([_summary(visa=visa)]))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."


class TestGroupMixedTravelers:
    """Action items aggregate per traveler and count only travelers with issues."""

    def test_passport_and_visa_issues_combined_for_one_traveler(self, endpoint_arn):
        summary = _summary(
            passport=_passport(True, AlertSeverity.WARNING, ["Passport expiring"]),
            visa=_visa(True, AlertSeverity.CRITICAL, ["Visa required"]),
        )

        send_group_notification(endpoint_arn, _group([summary]))

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - Passport expiring, Visa required"
        )

    def test_counts_only_travelers_needing_action(self, endpoint_arn):
        summaries = [
            _summary("Jane", "Doe", passport=_passport(True, AlertSeverity.WARNING, ["Passport expiring"])),
            _summary("Tim", "Lee", visa=_visa(True, AlertSeverity.CRITICAL, ["Visa required for China"])),
            _summary("Ann", "Kim"),
            _summary("Bob", "Ray", passport=_passport(True, AlertSeverity.INFO, ["FYI"])),
        ]

        send_group_notification(endpoint_arn, _group(summaries, AlertSeverity.CRITICAL))

        assert _group_body(endpoint_arn) == (
            "2 of 4 travelers need action: Jane Doe - Passport expiring; "
            "Tim Lee - Visa required for China"
        )


class TestGroupMessageStructure:
    """Group messages carry the title, confirmation number and group severity per platform."""

    def test_returns_sns_message_id(self, endpoint_arn):
        response = send_group_notification(endpoint_arn, _group())

        assert "MessageId" in response

    def test_apns_payload(self, endpoint_arn):
        send_group_notification(
            endpoint_arn, _group(group_severity=AlertSeverity.WARNING, confirmation_number="DL-ABC")
        )

        message = _published_messages(endpoint_arn)[0]
        apns = json.loads(message["APNS"])
        assert apns["aps"] == {
            "alert": {
                "title": "Delta Concierge \u2014 Group Travel Alert",
                "body": message["default"],
            },
            "sound": "default",
        }
        assert apns["confirmation_number"] == "DL-ABC"
        assert apns["group_severity"] == "WARNING"

    def test_gcm_payload(self, endpoint_arn):
        send_group_notification(
            endpoint_arn, _group(group_severity=AlertSeverity.CRITICAL, confirmation_number="DL-ABC")
        )

        message = _published_messages(endpoint_arn)[0]
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {
            "title": "Delta Concierge \u2014 Group Travel Alert",
            "body": message["default"],
        }
        assert gcm["data"] == {"confirmation_number": "DL-ABC", "group_severity": "CRITICAL"}

    def test_none_group_severity_reported_as_ok(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group(group_severity=None))

        message = _published_messages(endpoint_arn)[0]
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    def test_sent_only_to_primary_endpoint(self, sns, app_arn, endpoint_arn):
        other_arn = _create_endpoint(sns, app_arn, "other-token")

        send_group_notification(endpoint_arn, _group())

        assert len(_published_messages(endpoint_arn)) == 1
        assert _published_messages(other_arn) == []
