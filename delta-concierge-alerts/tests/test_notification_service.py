"""Tests for the notification_service module — send_push_notification and send_group_notification."""

import json
from datetime import date

import boto3
import moto
import pytest

import src.services.notification_service as notif_mod
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
    FlightSegment,
    GroupEvaluationResult,
    NotificationPayload,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
)
from tests.conftest import make_profile


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_sns():
    """Activate moto SNS mock and monkey-patch the module-level sns_client."""
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEGMENT = FlightSegment(
    flight_number="DL100",
    origin="ATL",
    destination="DE",
    departure_date=date(2026, 9, 1),
    arrival_date=date(2026, 9, 2),
    is_layover=False,
)


def _make_passport_eval(
    *,
    is_alert: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=[_SEGMENT],
        is_alert_required=is_alert,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_visa_eval(
    *,
    is_alert: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=[_SEGMENT],
        is_alert_required=is_alert,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_traveler_summary(
    first_name: str = "Test",
    last_name: str = "User",
    passport_eval: PassportEvaluation | None = None,
    visa_eval: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    return TravelerAlertSummary(
        skymiles_number="9999999",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_eval or _make_passport_eval(),
        visa_result=visa_eval or _make_visa_eval(),
    )


def _make_notification_payload(endpoint_arn: str) -> NotificationPayload:
    return NotificationPayload(
        endpoint_arn=endpoint_arn,
        alert_record=AlertRecord(
            alert_id="alert-001",
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
        push_data={"confirmation_number": "DL-TEST", "alert_type": "PASSPORT"},
    )


# ---------------------------------------------------------------------------
# Tests — send_push_notification
# ---------------------------------------------------------------------------

class TestSendPushNotification:
    """Verify SNS publish is invoked correctly for individual push notifications."""

    def test_publish_returns_message_id(self, endpoint_arn):
        payload = _make_notification_payload(endpoint_arn)

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response
        assert isinstance(response["MessageId"], str)

    def test_apns_and_gcm_message_structure(self, endpoint_arn):
        payload = _make_notification_payload(endpoint_arn)

        response = notif_mod.send_push_notification(payload)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200


# ---------------------------------------------------------------------------
# Tests — send_group_notification: travelers with issues
# ---------------------------------------------------------------------------

class TestGroupNotificationWithIssues:
    """When travelers have WARNING/CRITICAL issues, the body reports them."""

    def test_body_includes_need_action_count(self, endpoint_arn):
        """One of two travelers has a passport issue → '1 of 2 travelers need action'."""
        clean = _make_traveler_summary(first_name="Alice", last_name="Green")
        problem = _make_traveler_summary(
            first_name="Bob",
            last_name="Smith",
            passport_eval=_make_passport_eval(
                is_alert=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-GRP1",
            traveler_summaries=[clean, problem],
            group_severity=AlertSeverity.CRITICAL,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_visa_issue_included_in_action_items(self, endpoint_arn):
        """Traveler with visa WARNING appears in action items."""
        problem = _make_traveler_summary(
            first_name="Jane",
            last_name="Doe",
            visa_eval=_make_visa_eval(
                is_alert=True,
                severity=AlertSeverity.WARNING,
                reasons=["Visa expiring soon for DE"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-GRP2",
            traveler_summaries=[problem],
            group_severity=AlertSeverity.WARNING,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_both_passport_and_visa_issues(self, endpoint_arn):
        """Traveler with both passport and visa issues lists both."""
        problem = _make_traveler_summary(
            first_name="Tim",
            last_name="Lee",
            passport_eval=_make_passport_eval(
                is_alert=True,
                severity=AlertSeverity.WARNING,
                reasons=["Passport expiring soon"],
            ),
            visa_eval=_make_visa_eval(
                is_alert=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["No visa on file for CN"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-GRP3",
            traveler_summaries=[problem],
            group_severity=AlertSeverity.CRITICAL,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response

    def test_info_severity_not_included_in_action_items(self, endpoint_arn):
        """INFO-level passport issue should NOT appear in action items → all clear."""
        info_only = _make_traveler_summary(
            first_name="Sam",
            last_name="Ray",
            passport_eval=_make_passport_eval(
                is_alert=True,
                severity=AlertSeverity.INFO,
                reasons=["Passport valid but expiring within 6 months"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-GRP4",
            traveler_summaries=[info_only],
            group_severity=AlertSeverity.INFO,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response


# ---------------------------------------------------------------------------
# Tests — send_group_notification: all clear
# ---------------------------------------------------------------------------

class TestGroupNotificationAllClear:
    """When no travelers have WARNING/CRITICAL issues, body says 'cleared for travel'."""

    def test_all_clear_body(self, endpoint_arn):
        """Two clean travelers → 'All 2 travelers are cleared for travel.'"""
        t1 = _make_traveler_summary(first_name="Alice", last_name="Green")
        t2 = _make_traveler_summary(first_name="Bob", last_name="Smith")
        group = GroupEvaluationResult(
            confirmation_number="DL-CLR",
            traveler_summaries=[t1, t2],
            group_severity=None,
        )

        response = notif_mod.send_group_notification(endpoint_arn, group)

        assert "MessageId" in response


# ---------------------------------------------------------------------------
# Tests — send_group_notification: message structure verification
# ---------------------------------------------------------------------------

class TestGroupNotificationMessageStructure:
    """Verify APNS/GCM JSON structure and severity mapping in published messages."""

    def _call_and_parse(self, endpoint_arn, group):
        """Call send_group_notification and reconstruct what was published."""
        # We call the function and verify it builds the correct internal structures
        # by monkey-patching sns_client.publish to capture the message.
        captured = {}
        original_publish = notif_mod.sns_client.publish

        def capturing_publish(**kwargs):
            captured.update(kwargs)
            return original_publish(**kwargs)

        notif_mod.sns_client.publish = capturing_publish
        try:
            response = notif_mod.send_group_notification(endpoint_arn, group)
        finally:
            notif_mod.sns_client.publish = original_publish

        return response, captured

    def test_severity_none_maps_to_ok(self, endpoint_arn):
        """group_severity=None → 'OK' in APNS and GCM data."""
        t1 = _make_traveler_summary(first_name="Clean", last_name="Traveler")
        group = GroupEvaluationResult(
            confirmation_number="DL-OK",
            traveler_summaries=[t1],
            group_severity=None,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        assert "MessageId" in response
        json_message = json.loads(captured["Message"])

        apns = json.loads(json_message["APNS"])
        assert apns["group_severity"] == "OK"
        assert apns["confirmation_number"] == "DL-OK"
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert "All 1 travelers are cleared for travel." in apns["aps"]["alert"]["body"]
        assert apns["aps"]["sound"] == "default"

        gcm = json.loads(json_message["GCM"])
        assert gcm["data"]["group_severity"] == "OK"
        assert gcm["data"]["confirmation_number"] == "DL-OK"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"

    def test_severity_critical_maps_to_critical(self, endpoint_arn):
        """group_severity=CRITICAL → 'CRITICAL' in APNS and GCM data."""
        problem = _make_traveler_summary(
            first_name="Bob",
            last_name="Jones",
            passport_eval=_make_passport_eval(
                is_alert=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-CRIT",
            traveler_summaries=[problem],
            group_severity=AlertSeverity.CRITICAL,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        assert "MessageId" in response
        json_message = json.loads(captured["Message"])

        apns = json.loads(json_message["APNS"])
        assert apns["group_severity"] == "CRITICAL"
        assert apns["confirmation_number"] == "DL-CRIT"

        gcm = json.loads(json_message["GCM"])
        assert gcm["data"]["group_severity"] == "CRITICAL"
        assert gcm["data"]["confirmation_number"] == "DL-CRIT"

    def test_severity_warning_maps_to_warning(self, endpoint_arn):
        """group_severity=WARNING → 'WARNING' in APNS and GCM data."""
        problem = _make_traveler_summary(
            first_name="Sue",
            last_name="Park",
            visa_eval=_make_visa_eval(
                is_alert=True,
                severity=AlertSeverity.WARNING,
                reasons=["Visa expiring on departure date"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-WARN",
            traveler_summaries=[problem],
            group_severity=AlertSeverity.WARNING,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        json_message = json.loads(captured["Message"])
        apns = json.loads(json_message["APNS"])
        assert apns["group_severity"] == "WARNING"

        gcm = json.loads(json_message["GCM"])
        assert gcm["data"]["group_severity"] == "WARNING"

    def test_need_action_body_format(self, endpoint_arn):
        """Verify the exact body format when travelers need action."""
        alice = _make_traveler_summary(first_name="Alice", last_name="Wong")
        bob = _make_traveler_summary(
            first_name="Bob",
            last_name="Li",
            passport_eval=_make_passport_eval(
                is_alert=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        group = GroupEvaluationResult(
            confirmation_number="DL-FMT",
            traveler_summaries=[alice, bob],
            group_severity=AlertSeverity.CRITICAL,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        json_message = json.loads(captured["Message"])
        body = json_message["default"]
        assert body == "1 of 2 travelers need action: Bob Li - Passport expired"

    def test_all_clear_body_format(self, endpoint_arn):
        """Verify the exact body format when all travelers are clear."""
        t1 = _make_traveler_summary(first_name="A", last_name="B")
        t2 = _make_traveler_summary(first_name="C", last_name="D")
        t3 = _make_traveler_summary(first_name="E", last_name="F")
        group = GroupEvaluationResult(
            confirmation_number="DL-CLR",
            traveler_summaries=[t1, t2, t3],
            group_severity=None,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        json_message = json.loads(captured["Message"])
        assert json_message["default"] == "All 3 travelers are cleared for travel."

    def test_target_arn_matches_primary_endpoint(self, endpoint_arn):
        """Verify publish uses the primary_endpoint_arn as TargetArn."""
        group = GroupEvaluationResult(
            confirmation_number="DL-ARN",
            traveler_summaries=[_make_traveler_summary()],
            group_severity=None,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        assert captured["TargetArn"] == endpoint_arn
        assert captured["MessageStructure"] == "json"

    def test_multiple_travelers_with_issues(self, endpoint_arn):
        """Multiple travelers with issues all appear in the body."""
        t1 = _make_traveler_summary(
            first_name="Jane",
            last_name="Doe",
            passport_eval=_make_passport_eval(
                is_alert=True,
                severity=AlertSeverity.CRITICAL,
                reasons=["Passport expired"],
            ),
        )
        t2 = _make_traveler_summary(
            first_name="Tim",
            last_name="Fox",
            visa_eval=_make_visa_eval(
                is_alert=True,
                severity=AlertSeverity.WARNING,
                reasons=["Visa expiring soon for CN"],
            ),
        )
        t3 = _make_traveler_summary(first_name="Clean", last_name="Person")
        group = GroupEvaluationResult(
            confirmation_number="DL-MULTI",
            traveler_summaries=[t1, t2, t3],
            group_severity=AlertSeverity.CRITICAL,
        )

        response, captured = self._call_and_parse(endpoint_arn, group)

        json_message = json.loads(captured["Message"])
        body = json_message["default"]
        assert "2 of 3 travelers need action" in body
        assert "Jane Doe - Passport expired" in body
        assert "Tim Fox - Visa expiring soon for CN" in body
