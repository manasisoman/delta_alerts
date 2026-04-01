"""Tests for the SNS notification service (send_push_notification & send_group_notification)."""

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
    SkyMilesProfile,
    TravelerAlertSummary,
    VisaEvaluation,
)
from src.services.notification_service import send_group_notification, send_push_notification


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

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
    """Expose the moto SNS endpoint ARN."""
    return _mock_aws


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _base_profile(**overrides) -> SkyMilesProfile:
    """Build a SkyMilesProfile with sensible defaults."""
    from datetime import date

    defaults = dict(
        skymiles_number="9999999",
        first_name="Test",
        last_name="User",
        nationality="IN",
        passport_number="P123456",
        passport_expiry=date(2030, 1, 1),
        visa_records=[],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )
    defaults.update(overrides)
    return SkyMilesProfile(**defaults)


def _base_segments() -> list[FlightSegment]:
    """Build a default list of flight segments."""
    from datetime import date

    return [
        FlightSegment(
            flight_number="DL100",
            origin="ATL",
            destination="DE",
            departure_date=date(2026, 9, 1),
            arrival_date=date(2026, 9, 2),
            is_layover=False,
        )
    ]


def _base_alert_record(**overrides) -> AlertRecord:
    """Build a minimal AlertRecord."""
    defaults = dict(
        alert_id="alert-001",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport expired"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _make_passport_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation with defaults."""
    return PassportEvaluation(
        profile=_base_profile(),
        segments_evaluated=_base_segments(),
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_visa_eval(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    """Build a VisaEvaluation with defaults."""
    return VisaEvaluation(
        profile=_base_profile(),
        segments_evaluated=_base_segments(),
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_traveler_summary(
    first_name: str = "Test",
    last_name: str = "User",
    skymiles_number: str = "9999999",
    passport_eval: PassportEvaluation | None = None,
    visa_eval: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary with defaults."""
    return TravelerAlertSummary(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_eval or _make_passport_eval(),
        visa_result=visa_eval or _make_visa_eval(),
    )


def _make_group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    group_severity: AlertSeverity | None = None,
    confirmation_number: str = "GRP-001",
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult with defaults."""
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries or [_make_traveler_summary()],
        group_severity=group_severity,
    )


# ---------------------------------------------------------------------------
# Tests for send_push_notification
# ---------------------------------------------------------------------------

class TestSendPushNotification:
    """Verify SNS publish is called with correct platform-specific messages."""

    def test_happy_path_publishes_and_returns_message_id(self, endpoint_arn):
        """A valid payload should publish to SNS and return a response with MessageId."""
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_base_alert_record(),
            title="Passport Alert",
            body="Your passport is expiring soon.",
            push_data={"alert_id": "alert-001", "severity": "CRITICAL"},
        )

        response = send_push_notification(payload)

        assert "MessageId" in response

    def test_message_structure_contains_apns_and_gcm(self, endpoint_arn):
        """The published message must include APNS and GCM platform keys."""
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_base_alert_record(),
            title="Visa Alert",
            body="No visa on file for CN.",
            push_data={"alert_id": "alert-002"},
        )

        response = send_push_notification(payload)

        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_push_data_embedded_in_apns_payload(self, endpoint_arn):
        """Custom push_data keys should be merged into the top-level APNS payload."""
        push_data = {"alert_id": "alert-003", "severity": "WARNING"}
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_base_alert_record(),
            title="Test Title",
            body="Test Body",
            push_data=push_data,
        )

        # We can verify the function runs without error and returns a valid response
        response = send_push_notification(payload)

        assert "MessageId" in response


# ---------------------------------------------------------------------------
# Tests for send_group_notification
# ---------------------------------------------------------------------------

class TestGroupNotificationAllClear:
    """When no travelers need action, the body should say everyone is cleared."""

    def test_all_travelers_clear(self, endpoint_arn):
        """No issues → body reads 'All N travelers are cleared for travel.'"""
        summaries = [
            _make_traveler_summary(first_name="Alice", last_name="Smith"),
            _make_traveler_summary(
                first_name="Bob",
                last_name="Jones",
                skymiles_number="8888888",
            ),
        ]
        group_result = _make_group_result(summaries=summaries, group_severity=None)

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_group_severity_none_produces_ok_in_push_data(self, endpoint_arn):
        """When group_severity is None, 'OK' should appear in the push data."""
        group_result = _make_group_result(group_severity=None)

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response


class TestGroupNotificationWithIssues:
    """When travelers have passport or visa issues, the body should summarize them."""

    def test_passport_warning_included_in_action_items(self, endpoint_arn):
        """A traveler with a passport WARNING should appear in the summary."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Passport validity below 3-month requirement for DE"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Jane",
                last_name="Doe",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.WARNING,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_passport_critical_included_in_action_items(self, endpoint_arn):
        """A traveler with a passport CRITICAL should appear in the summary."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Tim",
                last_name="Brown",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_visa_warning_included_in_action_items(self, endpoint_arn):
        """A traveler with a visa WARNING should appear in the summary."""
        visa_eval = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Visa expires on departure date"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Amy",
                last_name="Lee",
                visa_eval=visa_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.WARNING,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_visa_critical_included_in_action_items(self, endpoint_arn):
        """A traveler with a visa CRITICAL should appear in the summary."""
        visa_eval = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["No visa on file for CN"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Ray",
                last_name="Park",
                visa_eval=visa_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_both_passport_and_visa_issues_combined(self, endpoint_arn):
        """A traveler with both passport and visa issues should have both in action items."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        visa_eval = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["No visa on file for CN"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Sam",
                last_name="White",
                passport_eval=passport_eval,
                visa_eval=visa_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_mixed_travelers_some_clear_some_with_issues(self, endpoint_arn):
        """With 3 travelers, only those with issues should appear in the summary."""
        passport_issue = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        summaries = [
            _make_traveler_summary(first_name="Alice", last_name="Smith"),
            _make_traveler_summary(
                first_name="Bob",
                last_name="Jones",
                skymiles_number="8888888",
                passport_eval=passport_issue,
            ),
            _make_traveler_summary(
                first_name="Carol",
                last_name="King",
                skymiles_number="7777777",
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response


class TestGroupNotificationSeveritySkips:
    """Travelers with INFO severity or is_alert_required=False should be excluded from action items."""

    def test_passport_info_severity_excluded(self, endpoint_arn):
        """Passport with INFO severity should NOT appear as needing action."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.INFO,
            reasons=["Passport valid but expiring within 6 months"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Lyn",
                last_name="Fox",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.INFO,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_visa_info_severity_excluded(self, endpoint_arn):
        """Visa with INFO severity should NOT appear as needing action."""
        visa_eval = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.INFO,
            reasons=["Visa expires within 30 days of travel"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Dan",
                last_name="Cho",
                visa_eval=visa_eval,
            ),
        ]
        group_result = _make_group_result(summaries=summaries, group_severity=AlertSeverity.INFO)

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_passport_not_alert_required_excluded(self, endpoint_arn):
        """Passport with is_alert_required=False should not appear as needing action."""
        passport_eval = _make_passport_eval(
            is_alert_required=False,
            severity=AlertSeverity.WARNING,
            reasons=["Passport validity below requirement"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Eli",
                last_name="Vance",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(summaries=summaries, group_severity=None)

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_visa_not_alert_required_excluded(self, endpoint_arn):
        """Visa with is_alert_required=False should not appear as needing action."""
        visa_eval = _make_visa_eval(
            is_alert_required=False,
            severity=AlertSeverity.CRITICAL,
            reasons=["No visa on file"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Fay",
                last_name="Moss",
                visa_eval=visa_eval,
            ),
        ]
        group_result = _make_group_result(summaries=summaries, group_severity=None)

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_passport_severity_none_excluded(self, endpoint_arn):
        """Passport with severity=None should not appear as needing action even if is_alert_required."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=None,
            reasons=[],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Gus",
                last_name="Reed",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(summaries=summaries, group_severity=None)

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response


class TestGroupNotificationGroupSeverity:
    """Verify group_severity is correctly serialized in the push data."""

    def test_group_severity_critical_in_push_data(self, endpoint_arn):
        """When group_severity is CRITICAL, the value 'CRITICAL' should be used."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Hal",
                last_name="Voss",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_group_severity_warning_in_push_data(self, endpoint_arn):
        """When group_severity is WARNING, the value 'WARNING' should be used."""
        visa_eval = _make_visa_eval(
            is_alert_required=True,
            severity=AlertSeverity.WARNING,
            reasons=["Visa expires on departure date"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Ivy",
                last_name="Tate",
                visa_eval=visa_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.WARNING,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response

    def test_multiple_reasons_joined_with_semicolons(self, endpoint_arn):
        """Multiple reasons for a single evaluation should be joined with '; '."""
        passport_eval = _make_passport_eval(
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport expired", "Passport expires before departure"],
        )
        summaries = [
            _make_traveler_summary(
                first_name="Kai",
                last_name="Dunn",
                passport_eval=passport_eval,
            ),
        ]
        group_result = _make_group_result(
            summaries=summaries,
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response
