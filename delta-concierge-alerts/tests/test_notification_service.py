"""Tests for the SNS push notification service."""

import json
from datetime import date

import boto3
import moto
import pytest

import src.services.notification_service as notif_mod
from src.models.types import (
    AlertSeverity,
    AlertType,
    AlertRecord,
    AlertStatus,
    FlightSegment,
    GroupEvaluationResult,
    NotificationPayload,
    PassportEvaluation,
    SkyMilesProfile,
    TravelerAlertSummary,
    VisaEvaluation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_profile(**overrides) -> SkyMilesProfile:
    defaults = dict(
        skymiles_number="9999999",
        first_name="Test",
        last_name="User",
        nationality="US",
        passport_number="P123456",
        passport_expiry=date(2030, 1, 1),
        visa_records=[],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )
    defaults.update(overrides)
    return SkyMilesProfile(**defaults)


def _make_segment(**overrides) -> FlightSegment:
    defaults = dict(
        flight_number="DL100",
        origin="ATL",
        destination="DE",
        departure_date=date(2026, 9, 1),
        arrival_date=date(2026, 9, 2),
        is_layover=False,
    )
    defaults.update(overrides)
    return FlightSegment(**defaults)


def _clean_eval(profile: SkyMilesProfile | None = None) -> tuple[PassportEvaluation, VisaEvaluation]:
    """Return passport and visa evaluations with no alerts."""
    p = profile or _make_profile()
    seg = [_make_segment()]
    passport = PassportEvaluation(
        profile=p, segments_evaluated=seg,
        is_alert_required=False, severity=None,
        reasons=[], validation_errors=[],
    )
    visa = VisaEvaluation(
        profile=p, segments_evaluated=seg,
        is_alert_required=False, severity=None,
        reasons=[], validation_errors=[],
    )
    return passport, visa


def _alert_eval(
    profile: SkyMilesProfile | None = None,
    passport_severity: AlertSeverity | None = None,
    passport_reasons: list[str] | None = None,
    visa_severity: AlertSeverity | None = None,
    visa_reasons: list[str] | None = None,
) -> tuple[PassportEvaluation, VisaEvaluation]:
    """Return evaluations with configurable alert states."""
    p = profile or _make_profile()
    seg = [_make_segment()]
    passport = PassportEvaluation(
        profile=p, segments_evaluated=seg,
        is_alert_required=passport_severity is not None,
        severity=passport_severity,
        reasons=passport_reasons or [],
        validation_errors=[],
    )
    visa = VisaEvaluation(
        profile=p, segments_evaluated=seg,
        is_alert_required=visa_severity is not None,
        severity=visa_severity,
        reasons=visa_reasons or [],
        validation_errors=[],
    )
    return passport, visa


def _make_summary(
    first_name: str = "Test",
    last_name: str = "User",
    passport_severity: AlertSeverity | None = None,
    passport_reasons: list[str] | None = None,
    visa_severity: AlertSeverity | None = None,
    visa_reasons: list[str] | None = None,
) -> TravelerAlertSummary:
    profile = _make_profile(first_name=first_name, last_name=last_name)
    if passport_severity or visa_severity:
        pp, vv = _alert_eval(
            profile=profile,
            passport_severity=passport_severity,
            passport_reasons=passport_reasons,
            visa_severity=visa_severity,
            visa_reasons=visa_reasons,
        )
    else:
        pp, vv = _clean_eval(profile)
    return TravelerAlertSummary(
        skymiles_number=profile.skymiles_number,
        first_name=first_name,
        last_name=last_name,
        passport_result=pp,
        visa_result=vv,
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary],
    group_severity: AlertSeverity | None = None,
    confirmation: str = "DL-GRP-001",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=summaries,
        group_severity=group_severity,
    )


class PublishSpy:
    """Wraps an SNS client to record publish calls while still executing them."""

    def __init__(self, real_client):
        self._real = real_client
        self.calls: list[dict] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return self._real.publish(**kwargs)

    def __getattr__(self, name):
        return getattr(self._real, name)

    @property
    def last_message(self) -> dict:
        """Parse the JSON Message from the most recent publish call."""
        return json.loads(self.calls[-1]["Message"])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto SNS mock, create a platform endpoint, and monkey-patch."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        spy = PublishSpy(sns)
        orig_sns = notif_mod.sns_client
        notif_mod.sns_client = spy

        try:
            yield endpoint_arn, spy
        finally:
            notif_mod.sns_client = orig_sns


@pytest.fixture
def endpoint_arn(_mock_aws):
    return _mock_aws[0]


@pytest.fixture
def publish_spy(_mock_aws):
    return _mock_aws[1]


# ---------------------------------------------------------------------------
# Tests — send_group_notification: all travelers cleared
# ---------------------------------------------------------------------------

class TestGroupAllCleared:
    """When every traveler has clean evaluations the body confirms clearance."""

    def test_two_travelers_cleared(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(first_name="Alice", last_name="Smith"),
            _make_summary(first_name="Bob", last_name="Jones"),
        ]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert body == "All 2 travelers are cleared for travel."

    def test_single_traveler_cleared(self, endpoint_arn, publish_spy):
        summaries = [_make_summary(first_name="Solo", last_name="Traveler")]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert body == "All 1 travelers are cleared for travel."


# ---------------------------------------------------------------------------
# Tests — send_group_notification: CRITICAL passport issue
# ---------------------------------------------------------------------------

class TestGroupPassportCritical:
    """A traveler with a CRITICAL passport issue appears in action items."""

    def test_critical_passport_in_action_items(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(
                first_name="Jane", last_name="Doe",
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_summary(first_name="Bob", last_name="Clean"),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert "1 of 2 travelers need action" in body
        assert "Jane Doe - Passport expired" in body


# ---------------------------------------------------------------------------
# Tests — send_group_notification: WARNING visa issue
# ---------------------------------------------------------------------------

class TestGroupVisaWarning:
    """A traveler with a WARNING visa issue appears in action items."""

    def test_warning_visa_in_action_items(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(
                first_name="Tim", last_name="Lee",
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa expiring on departure date"],
            ),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.WARNING)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert "1 of 1 travelers need action" in body
        assert "Tim Lee - Visa expiring on departure date" in body


# ---------------------------------------------------------------------------
# Tests — send_group_notification: INFO-only NOT included
# ---------------------------------------------------------------------------

class TestGroupInfoExcluded:
    """INFO-level issues do not generate action items."""

    def test_info_passport_not_included(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(
                first_name="Ava", last_name="Green",
                passport_severity=AlertSeverity.INFO,
                passport_reasons=["Passport expiring within 6 months"],
            ),
        ]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert body == "All 1 travelers are cleared for travel."

    def test_info_visa_not_included(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(
                first_name="Liam", last_name="Blue",
                visa_severity=AlertSeverity.INFO,
                visa_reasons=["Visa expiring within 30 days"],
            ),
        ]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert body == "All 1 travelers are cleared for travel."


# ---------------------------------------------------------------------------
# Tests — send_group_notification: multiple travelers need action
# ---------------------------------------------------------------------------

class TestGroupMultipleActionNeeded:
    """Correct count when multiple travelers need action."""

    def test_multiple_travelers_action_count(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(
                first_name="Alice", last_name="A",
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Passport expired"],
            ),
            _make_summary(first_name="Bob", last_name="B"),
            _make_summary(
                first_name="Carol", last_name="C",
                visa_severity=AlertSeverity.WARNING,
                visa_reasons=["Visa required for CN"],
            ),
            _make_summary(first_name="Dan", last_name="D"),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        notif_mod.send_group_notification(endpoint_arn, group)

        body = publish_spy.last_message["default"]
        assert "2 of 4 travelers need action" in body
        assert "Alice A - Passport expired" in body
        assert "Carol C - Visa required for CN" in body


# ---------------------------------------------------------------------------
# Tests — send_group_notification: APNS and GCM structure
# ---------------------------------------------------------------------------

class TestGroupMessageStructure:
    """The published SNS message contains correctly structured APNS and GCM payloads."""

    def test_apns_structure(self, endpoint_arn, publish_spy):
        summaries = [_make_summary(first_name="Eve", last_name="X")]
        group = _make_group_result(summaries, confirmation="DL-STRUCT")

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = json.loads(publish_spy.last_message["APNS"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert "cleared" in apns["aps"]["alert"]["body"]
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-STRUCT"

    def test_gcm_structure(self, endpoint_arn, publish_spy):
        summaries = [_make_summary(first_name="Eve", last_name="X")]
        group = _make_group_result(summaries, confirmation="DL-STRUCT")

        notif_mod.send_group_notification(endpoint_arn, group)

        gcm = json.loads(publish_spy.last_message["GCM"])
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert "cleared" in gcm["notification"]["body"]
        assert gcm["data"]["confirmation_number"] == "DL-STRUCT"

    def test_message_structure_is_json(self, endpoint_arn, publish_spy):
        summaries = [_make_summary()]
        group = _make_group_result(summaries)

        notif_mod.send_group_notification(endpoint_arn, group)

        call = publish_spy.calls[-1]
        assert call["MessageStructure"] == "json"
        assert call["TargetArn"] == endpoint_arn


# ---------------------------------------------------------------------------
# Tests — send_group_notification: group_severity=None → "OK"
# ---------------------------------------------------------------------------

class TestGroupSeverityNone:
    """When group_severity is None, APNS/GCM push data renders it as 'OK'."""

    def test_group_severity_none_renders_ok(self, endpoint_arn, publish_spy):
        summaries = [_make_summary()]
        group = _make_group_result(summaries, group_severity=None)

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = json.loads(publish_spy.last_message["APNS"])
        gcm = json.loads(publish_spy.last_message["GCM"])
        assert apns["group_severity"] == "OK"
        assert gcm["data"]["group_severity"] == "OK"

    def test_group_severity_critical_renders_value(self, endpoint_arn, publish_spy):
        summaries = [
            _make_summary(
                passport_severity=AlertSeverity.CRITICAL,
                passport_reasons=["Expired"],
            ),
        ]
        group = _make_group_result(summaries, group_severity=AlertSeverity.CRITICAL)

        notif_mod.send_group_notification(endpoint_arn, group)

        apns = json.loads(publish_spy.last_message["APNS"])
        gcm = json.loads(publish_spy.last_message["GCM"])
        assert apns["group_severity"] == "CRITICAL"
        assert gcm["data"]["group_severity"] == "CRITICAL"


# ---------------------------------------------------------------------------
# Tests — send_push_notification (existing function, extend coverage)
# ---------------------------------------------------------------------------

class TestSendPushNotification:
    """Verify send_push_notification publishes correctly formatted messages."""

    def test_publish_returns_message_id(self, endpoint_arn, publish_spy):
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=AlertRecord(
                alert_id="A001",
                skymiles_number="9999999",
                alert_type=AlertType.PASSPORT,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
                created_at="2026-01-01T00:00:00Z",
                itinerary_ref="DL-TEST",
                status=AlertStatus.ACTIVE,
            ),
            title="Passport Alert",
            body="Your passport has expired.",
            push_data={"alert_id": "A001", "severity": "CRITICAL"},
        )

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response

    def test_apns_and_gcm_in_message(self, endpoint_arn, publish_spy):
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=AlertRecord(
                alert_id="A002",
                skymiles_number="9999999",
                alert_type=AlertType.VISA,
                severity=AlertSeverity.WARNING,
                reasons=["Visa expiring soon"],
                created_at="2026-01-01T00:00:00Z",
                itinerary_ref="DL-TEST",
                status=AlertStatus.ACTIVE,
            ),
            title="Visa Alert",
            body="Your visa is expiring soon.",
            push_data={"alert_id": "A002", "severity": "WARNING"},
        )

        notif_mod.send_push_notification(payload)

        msg = publish_spy.last_message
        apns = json.loads(msg["APNS"])
        gcm = json.loads(msg["GCM"])
        assert apns["aps"]["alert"]["title"] == "Visa Alert"
        assert apns["aps"]["alert"]["body"] == "Your visa is expiring soon."
        assert apns["alert_id"] == "A002"
        assert gcm["notification"]["title"] == "Visa Alert"
        assert gcm["data"]["alert_id"] == "A002"
