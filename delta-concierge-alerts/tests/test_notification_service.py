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
    """Activate moto, create an SNS platform endpoint, and swap the module client."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
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
    """Return decoded SNS messages delivered to the given endpoint."""
    endpoint = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"].platform_endpoints[arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _profile(first: str = "Jane", last: str = "Doe", skymiles: str = "1111111") -> SkyMilesProfile:
    return SkyMilesProfile(
        skymiles_number=skymiles,
        first_name=first,
        last_name=last,
        nationality="US",
        passport_number="P123456",
        passport_expiry=date(2030, 1, 1),
        visa_records=[],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )


def _segment() -> FlightSegment:
    return FlightSegment(
        flight_number="DL100",
        origin="ATL",
        destination="CN",
        departure_date=date(2030, 6, 1),
        arrival_date=date(2030, 6, 2),
        is_layover=False,
    )


def _passport(alert: bool = False, severity=None, reasons=None) -> PassportEvaluation:
    return PassportEvaluation(
        profile=_profile(),
        segments_evaluated=[_segment()],
        is_alert_required=alert,
        severity=severity,
        reasons=reasons if reasons is not None else [],
        validation_errors=[],
    )


def _visa(alert: bool = False, severity=None, reasons=None) -> VisaEvaluation:
    return VisaEvaluation(
        profile=_profile(),
        segments_evaluated=[_segment()],
        is_alert_required=alert,
        severity=severity,
        reasons=reasons if reasons is not None else [],
        validation_errors=[],
    )


def _summary(first: str = "Jane", last: str = "Doe", passport=None, visa=None) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="1111111",
        first_name=first,
        last_name=last,
        passport_result=passport or _passport(),
        visa_result=visa or _visa(),
    )


def _group(*summaries, severity=None, confirmation: str = "DL-GRP") -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=list(summaries),
        group_severity=severity,
    )


def _payload(arn: str, **overrides) -> NotificationPayload:
    record = AlertRecord(
        alert_id="alert-1",
        skymiles_number="1111111",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport has expired"],
        created_at="2030-01-01T00:00:00",
        itinerary_ref="DL-TEST",
    )
    fields = dict(
        endpoint_arn=arn,
        alert_record=record,
        title="Passport Alert",
        body="Your passport has expired.",
        push_data={"alert_id": "alert-1", "severity": "CRITICAL"},
    )
    fields.update(overrides)
    return NotificationPayload(**fields)


def _group_body(arn: str) -> str:
    messages = _published(arn)
    assert len(messages) == 1
    return messages[0]["default"]


class TestSendPushNotification:
    """Individual pushes publish an APNS/GCM/default JSON message to the endpoint."""

    def test_returns_message_id(self, endpoint_arn):
        response = send_push_notification(_payload(endpoint_arn))

        assert "MessageId" in response

    def test_default_message_is_body(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        assert _published(endpoint_arn)[0]["default"] == "Your passport has expired."

    def test_apns_message_includes_alert_and_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        apns = json.loads(_published(endpoint_arn)[0]["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Passport Alert",
            "body": "Your passport has expired.",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "CRITICAL"

    def test_gcm_message_nests_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn))

        gcm = json.loads(_published(endpoint_arn)[0]["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport has expired.",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "CRITICAL"}

    def test_empty_push_data(self, endpoint_arn):
        send_push_notification(_payload(endpoint_arn, push_data={}))

        message = _published(endpoint_arn)[0]
        assert json.loads(message["GCM"])["data"] == {}
        assert set(json.loads(message["APNS"])) == {"aps"}


class TestGroupAllClear:
    """When no traveler has an actionable issue, the group is reported as cleared."""

    def test_all_travelers_cleared(self, endpoint_arn):
        group = _group(_summary("Jane"), _summary("Tim"))

        response = send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
        assert _group_body(endpoint_arn) == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group())

        assert _group_body(endpoint_arn) == "All 0 travelers are cleared for travel."

    def test_group_severity_none_reported_as_ok(self, endpoint_arn):
        send_group_notification(endpoint_arn, _group(_summary(), severity=None))

        message = _published(endpoint_arn)[0]
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"


class TestGroupPassportIssues:
    """Only alert-required WARNING/CRITICAL passport results become action items."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_severity_included(self, endpoint_arn, severity):
        passport = _passport(True, severity, ["passport expiring"])
        group = _group(_summary("Jane", "Doe", passport=passport), _summary("Tim", "Roe"))

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "1 of 2 travelers need action: Jane Doe - passport expiring"
        )

    def test_info_severity_ignored(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.INFO, ["fyi"])

        send_group_notification(endpoint_arn, _group(_summary(passport=passport)))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_none_severity_ignored(self, endpoint_arn):
        passport = _passport(True, None, ["unknown"])

        send_group_notification(endpoint_arn, _group(_summary(passport=passport)))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_alert_not_required_ignored(self, endpoint_arn):
        passport = _passport(False, AlertSeverity.CRITICAL, ["stale"])

        send_group_notification(endpoint_arn, _group(_summary(passport=passport)))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn):
        passport = _passport(True, AlertSeverity.CRITICAL, ["expired", "missing number"])

        send_group_notification(endpoint_arn, _group(_summary(passport=passport)))

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - expired; missing number"
        )


class TestGroupVisaIssues:
    """Only alert-required WARNING/CRITICAL visa results become action items."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_severity_included(self, endpoint_arn, severity):
        visa = _visa(True, severity, ["visa required for China"])

        send_group_notification(endpoint_arn, _group(_summary("Tim", "Roe", visa=visa)))

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Tim Roe - visa required for China"
        )

    def test_info_severity_ignored(self, endpoint_arn):
        visa = _visa(True, AlertSeverity.INFO, ["fyi"])

        send_group_notification(endpoint_arn, _group(_summary(visa=visa)))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_none_severity_ignored(self, endpoint_arn):
        visa = _visa(True, None, ["unknown"])

        send_group_notification(endpoint_arn, _group(_summary(visa=visa)))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."

    def test_alert_not_required_ignored(self, endpoint_arn):
        visa = _visa(False, AlertSeverity.WARNING, ["stale"])

        send_group_notification(endpoint_arn, _group(_summary(visa=visa)))

        assert _group_body(endpoint_arn) == "All 1 travelers are cleared for travel."


class TestGroupCombinedIssues:
    """Passport and visa issues combine per traveler and across travelers."""

    def test_passport_and_visa_for_same_traveler(self, endpoint_arn):
        summary = _summary(
            passport=_passport(True, AlertSeverity.WARNING, ["passport expiring"]),
            visa=_visa(True, AlertSeverity.CRITICAL, ["visa required"]),
        )

        send_group_notification(endpoint_arn, _group(summary))

        assert _group_body(endpoint_arn) == (
            "1 of 1 travelers need action: Jane Doe - passport expiring, visa required"
        )

    def test_multiple_travelers_with_issues(self, endpoint_arn):
        group = _group(
            _summary("Jane", "Doe", passport=_passport(True, AlertSeverity.WARNING, ["passport expiring"])),
            _summary("Tim", "Roe", visa=_visa(True, AlertSeverity.CRITICAL, ["visa required for China"])),
            _summary("Ann", "Lee"),
            _summary("Bob", "Kim"),
            severity=AlertSeverity.CRITICAL,
        )

        send_group_notification(endpoint_arn, group)

        assert _group_body(endpoint_arn) == (
            "2 of 4 travelers need action: Jane Doe - passport expiring; "
            "Tim Roe - visa required for China"
        )


class TestGroupMessageStructure:
    """Group pushes carry title, confirmation number, and group severity per platform."""

    def test_apns_payload(self, endpoint_arn):
        group = _group(
            _summary(passport=_passport(True, AlertSeverity.CRITICAL, ["expired"])),
            severity=AlertSeverity.CRITICAL,
            confirmation="DL-ABC123",
        )

        send_group_notification(endpoint_arn, group)

        message = _published(endpoint_arn)[0]
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-ABC123"
        assert apns["group_severity"] == "CRITICAL"

    def test_gcm_payload(self, endpoint_arn):
        group = _group(_summary(), severity=AlertSeverity.WARNING, confirmation="DL-XYZ")

        send_group_notification(endpoint_arn, group)

        message = _published(endpoint_arn)[0]
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert gcm["data"] == {"confirmation_number": "DL-XYZ", "group_severity": "WARNING"}
