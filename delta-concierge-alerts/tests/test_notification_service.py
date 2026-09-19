"""Unit tests for src/services/notification_service.py (SNS push notifications)."""

import json
from datetime import date

import boto3
import moto
import pytest
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

MOTO_ACCOUNT_ID = "123456789012"
REGION = "us-east-1"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create an SNS platform endpoint, and patch the module client."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name=REGION)
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


def _published_message(endpoint_arn: str, message_id: str) -> dict:
    """Fetch the raw message moto recorded for a platform endpoint and decode it."""
    backend = sns_backends[MOTO_ACCOUNT_ID][REGION]
    raw = backend.platform_endpoints[endpoint_arn].messages[message_id]
    return json.loads(raw)


def _make_alert_record(**overrides) -> AlertRecord:
    defaults = dict(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.CRITICAL,
        reasons=["Passport has expired"],
        created_at="2026-01-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )
    defaults.update(overrides)
    return AlertRecord(**defaults)


def _make_payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    defaults = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_make_alert_record(),
        title="Delta Concierge Alert",
        body="Your passport has expired.",
        push_data={"alert_id": "alert-1", "severity": "CRITICAL"},
    )
    defaults.update(overrides)
    return NotificationPayload(**defaults)


def _make_passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=[make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def _make_visa_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=[make_segment()],
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
        skymiles_number="1111111",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _make_passport_result(),
        visa_result=visa_result or _make_visa_result(),
    )


def _make_group_result(
    *summaries: TravelerAlertSummary,
    confirmation_number: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=list(summaries),
        group_severity=group_severity,
    )


class TestSendPushNotification:
    """send_push_notification publishes a multi-platform JSON message to the endpoint."""

    def test_returns_sns_publish_response(self, endpoint_arn):
        payload = _make_payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_message_contains_default_apns_and_gcm(self, endpoint_arn):
        payload = _make_payload(endpoint_arn)

        response = send_push_notification(payload)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert set(message.keys()) == {"default", "APNS", "GCM"}
        assert message["default"] == "Your passport has expired."

    def test_apns_payload_includes_alert_and_push_data(self, endpoint_arn):
        payload = _make_payload(endpoint_arn, push_data={"alert_id": "abc", "deep_link": "delta://x"})

        response = send_push_notification(payload)

        apns = json.loads(_published_message(endpoint_arn, response["MessageId"])["APNS"])
        assert apns["aps"]["alert"] == {"title": "Delta Concierge Alert", "body": "Your passport has expired."}
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "abc"
        assert apns["deep_link"] == "delta://x"

    def test_gcm_payload_nests_push_data_under_data(self, endpoint_arn):
        payload = _make_payload(endpoint_arn, push_data={"alert_id": "abc"})

        response = send_push_notification(payload)

        gcm = json.loads(_published_message(endpoint_arn, response["MessageId"])["GCM"])
        assert gcm["notification"] == {"title": "Delta Concierge Alert", "body": "Your passport has expired."}
        assert gcm["data"] == {"alert_id": "abc"}

    def test_empty_push_data_still_publishes(self, endpoint_arn):
        payload = _make_payload(endpoint_arn, push_data={})

        response = send_push_notification(payload)

        message = _published_message(endpoint_arn, response["MessageId"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert set(apns.keys()) == {"aps"}
        assert gcm["data"] == {}

    def test_custom_title_and_body_propagate(self, endpoint_arn):
        payload = _make_payload(endpoint_arn, title="Visa Alert", body="Visa for CN has expired")

        response = send_push_notification(payload)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "Visa for CN has expired"
        assert json.loads(message["APNS"])["aps"]["alert"]["title"] == "Visa Alert"
        assert json.loads(message["GCM"])["notification"]["title"] == "Visa Alert"

    def test_invalid_endpoint_arn_raises(self):
        payload = _make_payload("arn:aws:sns:us-east-1:123456789012:endpoint/GCM/TestApp/does-not-exist")

        with pytest.raises(Exception):
            send_push_notification(payload)


class TestGroupNotificationAllCleared:
    """When no traveler has a WARNING/CRITICAL issue the body reports everyone cleared."""

    def test_no_travelers_with_issues(self, endpoint_arn):
        group_result = _make_group_result(_make_summary(), _make_summary(first_name="Tim"))

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "All 2 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        group_result = _make_group_result()

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "All 0 travelers are cleared for travel."

    def test_info_severity_is_not_an_action_item(self, endpoint_arn):
        passport = _make_passport_result(True, AlertSeverity.INFO, ["Passport expires within 6 months"])
        visa = _make_visa_result(True, AlertSeverity.INFO, ["Visa expires within 30 days"])
        group_result = _make_group_result(
            _make_summary(passport_result=passport, visa_result=visa),
            group_severity=AlertSeverity.INFO,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "All 1 travelers are cleared for travel."

    def test_alert_required_but_severity_none_is_ignored(self, endpoint_arn):
        passport = _make_passport_result(True, None, ["Something"])
        visa = _make_visa_result(True, None, ["Something else"])
        group_result = _make_group_result(_make_summary(passport_result=passport, visa_result=visa))

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "All 1 travelers are cleared for travel."

    def test_critical_severity_without_alert_required_is_ignored(self, endpoint_arn):
        passport = _make_passport_result(False, AlertSeverity.CRITICAL, ["Passport has expired"])
        visa = _make_visa_result(False, AlertSeverity.CRITICAL, ["No visa"])
        group_result = _make_group_result(_make_summary(passport_result=passport, visa_result=visa))

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "All 1 travelers are cleared for travel."


class TestGroupNotificationActionItems:
    """Travelers with WARNING/CRITICAL passport or visa issues are listed by name."""

    def test_critical_passport_issue(self, endpoint_arn):
        passport = _make_passport_result(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        group_result = _make_group_result(
            _make_summary(passport_result=passport),
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == "1 of 1 travelers need action: Jane Doe - Passport has expired"

    def test_warning_visa_issue(self, endpoint_arn):
        visa = _make_visa_result(True, AlertSeverity.WARNING, ["Visa for CN expires on date of travel"])
        group_result = _make_group_result(
            _make_summary(visa_result=visa),
            group_severity=AlertSeverity.WARNING,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - Visa for CN expires on date of travel"
        )

    def test_passport_and_visa_issues_joined_with_comma(self, endpoint_arn):
        passport = _make_passport_result(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        visa = _make_visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        group_result = _make_group_result(
            _make_summary(passport_result=passport, visa_result=visa),
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport has expired, No visa on file for CN"
        )

    def test_multiple_reasons_joined_with_semicolon(self, endpoint_arn):
        passport = _make_passport_result(
            True, AlertSeverity.CRITICAL,
            ["Passport expires before date of travel", "Passport validity below 6 months for CN"],
        )
        group_result = _make_group_result(
            _make_summary(passport_result=passport),
            group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport expires before date of travel; Passport validity below 6 months for CN"
        )

    def test_only_travelers_with_issues_are_listed(self, endpoint_arn):
        jane = _make_summary(
            passport_result=_make_passport_result(True, AlertSeverity.CRITICAL, ["Passport has expired"]),
        )
        tim = _make_summary(
            first_name="Tim", last_name="Smith",
            visa_result=_make_visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"]),
        )
        clean = _make_summary(first_name="Ann", last_name="Lee")
        info_only = _make_summary(
            first_name="Bob", last_name="Ray",
            passport_result=_make_passport_result(True, AlertSeverity.INFO, ["Expiring soon"]),
        )
        group_result = _make_group_result(
            jane, tim, clean, info_only, group_severity=AlertSeverity.CRITICAL,
        )

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"] == (
            "2 of 4 travelers need action: "
            "Jane Doe - Passport has expired; Tim Smith - No visa on file for CN"
        )


class TestGroupNotificationMessageStructure:
    """The group message carries the confirmation number and severity on both platforms."""

    def test_title_and_platform_payloads(self, endpoint_arn):
        group_result = _make_group_result(_make_summary(), confirmation_number="DL-ABC123")

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert set(message.keys()) == {"default", "APNS", "GCM"}
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert apns["aps"]["alert"]["body"] == message["default"]
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-ABC123"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["notification"]["body"] == message["default"]
        assert gcm["data"]["confirmation_number"] == "DL-ABC123"

    def test_group_severity_none_maps_to_ok(self, endpoint_arn):
        group_result = _make_group_result(_make_summary(), group_severity=None)

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    @pytest.mark.parametrize(
        "severity", [AlertSeverity.INFO, AlertSeverity.WARNING, AlertSeverity.CRITICAL],
    )
    def test_group_severity_value_is_serialized(self, endpoint_arn, severity):
        group_result = _make_group_result(_make_summary(), group_severity=severity)

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert json.loads(message["APNS"])["group_severity"] == severity.value
        assert json.loads(message["GCM"])["data"]["group_severity"] == severity.value

    def test_returns_sns_publish_response(self, endpoint_arn):
        group_result = _make_group_result(_make_summary())

        response = send_group_notification(endpoint_arn, group_result)

        assert "MessageId" in response
        assert response["ResponseMetadata"]["HTTPStatusCode"] == 200

    def test_invalid_endpoint_arn_raises(self):
        group_result = _make_group_result(_make_summary())

        with pytest.raises(Exception):
            send_group_notification(
                "arn:aws:sns:us-east-1:123456789012:endpoint/GCM/TestApp/does-not-exist",
                group_result,
            )


class TestGroupNotificationFromEvaluators:
    """Real evaluator output (layover, validity window, itinerary change) feeds the summary."""

    def test_layover_and_validity_window_results_summarized(self, endpoint_arn, base_requirements):
        from src.evaluators.passport_evaluator import evaluate_passport_expiry
        from src.evaluators.visa_evaluator import evaluate_visa_requirements
        from tests.conftest import make_itinerary

        # Passport valid only ~2 months past travel: below CN's 6-month window.
        profile = make_profile(nationality="IN", passport_expiry=date(2026, 11, 1))
        layover_cn = make_segment(destination="CN", is_layover=True)  # CN requires transit visa
        final_jp = make_segment(destination="JP", departure=date(2026, 9, 3), arrival=date(2026, 9, 4))
        itinerary = make_itinerary(layover_cn, final_jp)
        passport = evaluate_passport_expiry(profile, itinerary, base_requirements)
        visa = evaluate_visa_requirements(profile, itinerary, base_requirements)
        summary = TravelerAlertSummary(
            skymiles_number=profile.skymiles_number,
            first_name=profile.first_name,
            last_name=profile.last_name,
            passport_result=passport,
            visa_result=visa,
        )
        group_result = _make_group_result(summary, group_severity=AlertSeverity.CRITICAL)

        response = send_group_notification(endpoint_arn, group_result)

        message = _published_message(endpoint_arn, response["MessageId"])
        assert message["default"].startswith("1 of 1 travelers need action: Test User - ")
        assert "; ".join(passport.reasons) in message["default"]
        assert "; ".join(visa.reasons) in message["default"]

    def test_itinerary_change_clears_action_items(self, endpoint_arn, base_requirements):
        from src.evaluators.passport_evaluator import evaluate_passport_expiry
        from src.evaluators.visa_evaluator import evaluate_visa_requirements
        from tests.conftest import make_itinerary

        profile = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        original = make_itinerary(make_segment(destination="CN"))  # US needs a CN visa
        changed = make_itinerary(make_segment(destination="DE"))  # US is visa-exempt for DE

        def _group(itinerary):
            return _make_group_result(
                TravelerAlertSummary(
                    skymiles_number=profile.skymiles_number,
                    first_name=profile.first_name,
                    last_name=profile.last_name,
                    passport_result=evaluate_passport_expiry(profile, itinerary, base_requirements),
                    visa_result=evaluate_visa_requirements(profile, itinerary, base_requirements),
                )
            )

        before = send_group_notification(endpoint_arn, _group(original))
        after = send_group_notification(endpoint_arn, _group(changed))

        assert _published_message(endpoint_arn, before["MessageId"])["default"].startswith(
            "1 of 1 travelers need action: Test User - "
        )
        assert _published_message(endpoint_arn, after["MessageId"])["default"] == (
            "All 1 travelers are cleared for travel."
        )
