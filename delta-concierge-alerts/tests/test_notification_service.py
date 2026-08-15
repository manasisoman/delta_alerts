"""Tests for the SNS notification service."""

import json
from datetime import date

import boto3
import moto
import pytest

import src.services.notification_service as notif_mod
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertType,
    GroupEvaluationResult,
    GroupItinerary,
    NotificationPayload,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
    VisaRecord,
)
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)

from tests.conftest import make_profile, make_segment


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and patch the module-level SNS client for every test."""
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
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


def _published(message_id: str) -> dict:
    """Return the decoded SNS message body recorded by moto for a message id."""
    from moto.sns.models import sns_backends

    for account_backends in sns_backends.values():
        backend = account_backends["us-east-1"]
        for endpoint in backend.platform_endpoints.values():
            if message_id in endpoint.messages:
                return json.loads(endpoint.messages[message_id])
    raise AssertionError(f"No SNS message recorded for {message_id}")


def _alert_record(**overrides) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    fields = {
        "alert_id": "alert-1",
        "skymiles_number": "9999999",
        "alert_type": AlertType.VISA,
        "severity": AlertSeverity.CRITICAL,
        "reasons": ["No visa on file for CN"],
        "created_at": "2030-01-01T00:00:00Z",
        "itinerary_ref": "DL-TEST",
    }
    fields.update(overrides)
    return AlertRecord(**fields)


def _passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    """Build a PassportEvaluation directly, bypassing the evaluator."""
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=[make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _visa_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    """Build a VisaEvaluation directly, bypassing the evaluator."""
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=[make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _summary(
    first_name: str = "Jane",
    last_name: str = "Doe",
    passport_result: PassportEvaluation | None = None,
    visa_result: VisaEvaluation | None = None,
) -> TravelerAlertSummary:
    """Build a TravelerAlertSummary with clean results by default."""
    return TravelerAlertSummary(
        skymiles_number="9999999",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _passport_result(),
        visa_result=visa_result or _visa_result(),
    )


def _group_result(
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


class TestSendPushNotification:
    """A per-traveler payload is published as a multi-platform JSON message."""

    def test_publishes_apns_and_gcm_variants(self, endpoint_arn):
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_alert_record(),
            title="Visa required",
            body="You need a visa for CN",
            push_data={"alert_id": "alert-1", "severity": "CRITICAL"},
        )

        response = send_push_notification(payload)

        message = _published(response["MessageId"])
        assert message["default"] == "You need a visa for CN"
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Visa required",
            "body": "You need a visa for CN",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-1"
        gcm = json.loads(message["GCM"])
        assert gcm["notification"]["title"] == "Visa required"
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "CRITICAL"}

    def test_empty_push_data_still_publishes(self, endpoint_arn):
        payload = NotificationPayload(
            endpoint_arn=endpoint_arn,
            alert_record=_alert_record(alert_type=AlertType.PASSPORT),
            title="Passport expiring",
            body="Renew your passport",
            push_data={},
        )

        response = send_push_notification(payload)

        gcm = json.loads(_published(response["MessageId"])["GCM"])
        assert gcm["data"] == {}


class TestGroupNotificationAllClear:
    """With no actionable issues the summary reports every traveler cleared."""

    def test_all_travelers_cleared(self, endpoint_arn):
        group_result = _group_result(
            _summary(first_name="Jane"),
            _summary(first_name="Tim"),
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published(response["MessageId"])
        assert message["default"] == "All 2 travelers are cleared for travel."
        apns = json.loads(message["APNS"])
        assert apns["group_severity"] == "OK"
        assert apns["confirmation_number"] == "DL-GROUP"

    def test_empty_group_reports_zero_travelers(self, endpoint_arn):
        response = send_group_notification(endpoint_arn, _group_result())

        assert (
            _published(response["MessageId"])["default"]
            == "All 0 travelers are cleared for travel."
        )


class TestGroupNotificationActionItems:
    """Travelers with WARNING/CRITICAL issues are named in the summary body."""

    def test_single_traveler_needs_action(self, endpoint_arn):
        group_result = _group_result(
            _summary(
                first_name="Jane",
                last_name="Doe",
                visa_result=_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["No visa on file for CN"],
                ),
            ),
            _summary(first_name="Tim", last_name="Ray"),
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published(response["MessageId"])
        assert message["default"] == (
            "1 of 2 travelers need action: Jane Doe - No visa on file for CN"
        )
        gcm = json.loads(message["GCM"])
        assert gcm["data"]["group_severity"] == "CRITICAL"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"

    def test_passport_and_visa_issues_are_both_listed(self, endpoint_arn):
        group_result = _group_result(
            _summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport expires before departure", "Renew now"],
                ),
                visa_result=_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Visa expires on departure date"],
                ),
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        body = _published(response["MessageId"])["default"]
        assert body == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport expires before departure; Renew now, "
            "Visa expires on departure date"
        )

    def test_multiple_travelers_need_action(self, endpoint_arn):
        group_result = _group_result(
            _summary(
                first_name="Jane",
                last_name="Doe",
                passport_result=_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Passport has expired"],
                ),
            ),
            _summary(first_name="Tim", last_name="Ray"),
            _summary(
                first_name="Ann",
                last_name="Lee",
                visa_result=_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.WARNING,
                    reasons=["Visa expires on departure date"],
                ),
            ),
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        body = _published(response["MessageId"])["default"]
        assert body.startswith("2 of 3 travelers need action: ")
        assert "Jane Doe - Passport has expired" in body
        assert "Ann Lee - Visa expires on departure date" in body
        assert "Tim Ray" not in body


class TestGroupNotificationSeverityFiltering:
    """Only alert-required WARNING/CRITICAL results become action items."""

    def test_info_severity_is_not_an_action_item(self, endpoint_arn):
        group_result = _group_result(
            _summary(
                passport_result=_passport_result(
                    is_alert_required=True,
                    severity=AlertSeverity.INFO,
                    reasons=["Passport expires within 6 months of travel"],
                ),
                visa_result=_visa_result(
                    is_alert_required=True,
                    severity=AlertSeverity.INFO,
                    reasons=["Visa expires within 30 days of travel"],
                ),
            ),
            group_severity=AlertSeverity.INFO,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published(response["MessageId"])
        assert message["default"] == "All 1 travelers are cleared for travel."
        assert json.loads(message["APNS"])["group_severity"] == "INFO"

    def test_severity_without_alert_required_is_ignored(self, endpoint_arn):
        group_result = _group_result(
            _summary(
                visa_result=_visa_result(
                    is_alert_required=False,
                    severity=AlertSeverity.CRITICAL,
                    reasons=["Stale reason"],
                ),
            ),
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert (
            _published(response["MessageId"])["default"]
            == "All 1 travelers are cleared for travel."
        )

    def test_alert_required_without_severity_is_ignored(self, endpoint_arn):
        group_result = _group_result(
            _summary(
                passport_result=_passport_result(
                    is_alert_required=True, severity=None, reasons=["No severity"]
                ),
            ),
        )

        response = send_group_notification(endpoint_arn, group_result)

        assert (
            _published(response["MessageId"])["default"]
            == "All 1 travelers are cleared for travel."
        )


class TestGroupNotificationFromRealEvaluations:
    """End-to-end: real group evaluations feed the summary notification."""

    def test_expired_passport_and_layover_exemptions(
        self, endpoint_arn, base_requirements
    ):
        # Jane's passport is expired; Tim is US (visa-exempt for DE) and valid.
        jane = make_profile(passport_expiry=date(2020, 1, 1))
        jane.first_name, jane.last_name = "Jane", "Doe"
        tim = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        tim.first_name, tim.last_name = "Tim", "Ray"
        group = GroupItinerary(
            confirmation_number="DL-GROUP",
            segments=[
                # CN layover requires a transit visa but Tim is not exempt there,
                # so keep the group on the Schengen leg only.
                make_segment(destination="DE", departure=date(2030, 6, 1)),
            ],
            travelers=[jane, tim],
            primary_traveler=tim.skymiles_number,
        )

        group_result = evaluate_group_itinerary(group, base_requirements)
        response = send_group_notification(endpoint_arn, group_result)

        message = _published(response["MessageId"])
        assert message["default"].startswith("1 of 2 travelers need action: Jane Doe - ")
        assert "Tim Ray" not in message["default"]
        assert json.loads(message["APNS"])["group_severity"] == "CRITICAL"

    def test_layover_without_transit_visa_requirement_is_cleared(
        self, endpoint_arn, base_requirements
    ):
        # JP does not require a transit visa, so the layover is skipped entirely.
        traveler = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1))
        traveler.first_name, traveler.last_name = "Ann", "Lee"
        group = GroupItinerary(
            confirmation_number="DL-LAYOVER",
            segments=[
                make_segment(
                    destination="JP", departure=date(2030, 6, 1), is_layover=True
                ),
            ],
            travelers=[traveler],
            primary_traveler=traveler.skymiles_number,
        )

        group_result = evaluate_group_itinerary(group, base_requirements)
        response = send_group_notification(endpoint_arn, group_result)

        assert (
            _published(response["MessageId"])["default"]
            == "All 1 travelers are cleared for travel."
        )

    def test_itinerary_change_reevaluates_group_summary(
        self, endpoint_arn, base_requirements
    ):
        # Traveler holds a CN visa only; the original CN itinerary is clean.
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2035, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="CN",
                    visa_type="TOURIST",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2034, 1, 1),
                    visa_number="V1",
                )
            ],
        )
        traveler.first_name, traveler.last_name = "Ann", "Lee"
        group = GroupItinerary(
            confirmation_number="DL-CHANGE",
            segments=[make_segment(destination="CN", departure=date(2030, 6, 1))],
            travelers=[traveler],
            primary_traveler=traveler.skymiles_number,
        )

        before = send_group_notification(
            endpoint_arn, evaluate_group_itinerary(group, base_requirements)
        )
        # Itinerary is rebooked to Germany, where the traveler has no visa.
        group.segments = [make_segment(destination="DE", departure=date(2030, 6, 1))]
        after = send_group_notification(
            endpoint_arn, evaluate_group_itinerary(group, base_requirements)
        )

        assert (
            _published(before["MessageId"])["default"]
            == "All 1 travelers are cleared for travel."
        )
        assert "1 of 1 travelers need action: Ann Lee - No visa on file for DE" in (
            _published(after["MessageId"])["default"]
        )
