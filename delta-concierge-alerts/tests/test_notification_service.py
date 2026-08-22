"""Tests for the SNS push notification service."""

import json

import boto3
import moto
import pytest
from botocore.exceptions import ClientError

import src.services.notification_service as notif_mod
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
    GroupEvaluationResult,
    NotificationPayload,
    PassportEvaluation,
    TravelerAlertSummary,
    VisaEvaluation,
)

from .conftest import make_itinerary, make_profile, make_segment


class RecordingSns:
    """SNS client wrapper that records publish kwargs and delegates to moto."""

    def __init__(self, delegate):
        self._delegate = delegate
        self.calls: list[dict] = []

    def publish(self, **kwargs):
        self.calls.append(kwargs)
        return self._delegate.publish(**kwargs)


class FailingSns:
    """SNS client stub that always raises a ClientError on publish."""

    def publish(self, **kwargs):
        raise ClientError(
            {
                "Error": {
                    "Code": "EndpointDisabled",
                    "Message": "Endpoint is disabled",
                }
            },
            "Publish",
        )


@pytest.fixture(autouse=True)
def _mock_sns():
    """Activate moto, create an SNS platform endpoint, patch the module client."""
    with moto.mock_aws():
        sns_client = boto3.client("sns", region_name="us-east-1")
        app_arn = sns_client.create_platform_application(
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns_client.create_platform_endpoint(
            PlatformApplicationArn=app_arn,
            Token="fake-token",
        )["EndpointArn"]

        orig_sns_client = notif_mod.sns_client
        recorder = RecordingSns(sns_client)
        notif_mod.sns_client = recorder

        try:
            yield endpoint_arn, recorder
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn_holder(_mock_sns):
    """Expose the moto SNS endpoint ARN."""
    return _mock_sns[0]


@pytest.fixture
def recorder(_mock_sns):
    """Expose the recording SNS client wrapper."""
    return _mock_sns[1]


def make_alert_record(
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    alert_type: AlertType = AlertType.PASSPORT,
    reasons: list[str] | None = None,
) -> AlertRecord:
    """Build an AlertRecord with sensible defaults."""
    return AlertRecord(
        alert_id="alert-1",
        skymiles_number="9999999",
        alert_type=alert_type,
        severity=severity,
        reasons=reasons or ["Passport expires before travel"],
        created_at="2026-01-01T00:00:00",
        itinerary_ref="DL-TEST",
        status=AlertStatus.ACTIVE,
    )


def make_payload(
    endpoint_arn: str,
    severity: AlertSeverity = AlertSeverity.CRITICAL,
    title: str = "Action required",
    body: str = "Your passport expires before your trip.",
    push_data: dict | None = None,
) -> NotificationPayload:
    """Build a NotificationPayload with sensible defaults."""
    record = make_alert_record(severity=severity)
    return NotificationPayload(
        endpoint_arn=endpoint_arn,
        alert_record=record,
        title=title,
        body=body,
        push_data=push_data if push_data is not None else {"alert_id": record.alert_id},
    )


def make_passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
    segments=None,
) -> PassportEvaluation:
    """Build a PassportEvaluation with sensible defaults."""
    return PassportEvaluation(
        profile=make_profile(),
        segments_evaluated=segments if segments is not None else [make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def make_visa_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
    segments=None,
) -> VisaEvaluation:
    """Build a VisaEvaluation with sensible defaults."""
    return VisaEvaluation(
        profile=make_profile(),
        segments_evaluated=segments if segments is not None else [make_segment()],
        is_alert_required=is_alert_required,
        severity=severity,
        reasons=reasons or [],
        validation_errors=[],
    )


def make_summary(
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
        passport_result=passport_result or make_passport_result(),
        visa_result=visa_result or make_visa_result(),
    )


def make_group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    group_severity: AlertSeverity | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupEvaluationResult:
    """Build a GroupEvaluationResult with sensible defaults."""
    return GroupEvaluationResult(
        confirmation_number=confirmation,
        traveler_summaries=summaries if summaries is not None else [make_summary()],
        group_severity=group_severity,
    )


def published(recorder: RecordingSns) -> dict:
    """Decode the outer JSON message of the most recent publish call."""
    return json.loads(recorder.calls[-1]["Message"])


class TestSendPushNotificationHappyPath:
    """A valid payload publishes a JSON-structured message to the endpoint ARN."""

    def test_publish_succeeds(self, endpoint_arn_holder, recorder):
        payload = make_payload(endpoint_arn_holder)

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response
        assert recorder.calls[-1]["TargetArn"] == endpoint_arn_holder
        assert recorder.calls[-1]["MessageStructure"] == "json"

    @pytest.mark.parametrize(
        "severity",
        [AlertSeverity.CRITICAL, AlertSeverity.WARNING, AlertSeverity.INFO],
    )
    def test_publish_succeeds_for_each_severity(
        self, endpoint_arn_holder, recorder, severity
    ):
        payload = make_payload(endpoint_arn_holder, severity=severity)

        response = notif_mod.send_push_notification(payload)

        assert "MessageId" in response
        assert payload.alert_record.severity is severity
        assert len(recorder.calls) == 1


class TestSendPushNotificationFormatting:
    """The published message carries default, APNS and GCM representations."""

    def test_default_body_and_platform_payloads(self, endpoint_arn_holder, recorder):
        payload = make_payload(
            endpoint_arn_holder,
            title="Visa required for CN",
            body="You need a visa for China.",
            push_data={"alert_id": "alert-9", "alert_type": "VISA"},
        )

        notif_mod.send_push_notification(payload)

        message = published(recorder)
        assert message["default"] == "You need a visa for China."
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Visa required for CN",
            "body": "You need a visa for China.",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-9"
        assert apns["alert_type"] == "VISA"
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {
            "title": "Visa required for CN",
            "body": "You need a visa for China.",
        }
        assert gcm["data"] == {"alert_id": "alert-9", "alert_type": "VISA"}

    def test_empty_push_data_still_publishes(self, endpoint_arn_holder, recorder):
        payload = make_payload(endpoint_arn_holder, push_data={})

        notif_mod.send_push_notification(payload)

        message = published(recorder)
        assert json.loads(message["APNS"]).keys() == {"aps"}
        assert json.loads(message["GCM"])["data"] == {}

    def test_passport_expiry_reason_reaches_body(self, endpoint_arn_holder, recorder):
        """Passport validity shortfall vs. country requirement is what users see."""
        payload = make_payload(
            endpoint_arn_holder,
            body="Passport expires 2030-01-01, CN requires 6 months validity.",
        )

        notif_mod.send_push_notification(payload)

        assert "CN requires 6 months validity" in published(recorder)["default"]

    def test_layover_reason_reaches_body(self, endpoint_arn_holder, recorder):
        """Transit-visa alerts for layover segments are published unchanged."""
        layover = make_segment(destination="CN", is_layover=True)
        itinerary = make_itinerary(make_segment(destination="JP"), layover)
        payload = make_payload(
            endpoint_arn_holder,
            body=f"Transit visa required for layover in {layover.destination} "
            f"({itinerary.confirmation_number}).",
        )

        notif_mod.send_push_notification(payload)

        assert "Transit visa required for layover in CN" in published(recorder)["default"]

    def test_itinerary_change_republishes_notification(
        self, endpoint_arn_holder, recorder
    ):
        """Re-evaluation after an itinerary change publishes a second message."""
        first = make_payload(endpoint_arn_holder, body="Cleared for DE on 2030-01-01.")
        notif_mod.send_push_notification(first)

        updated = make_payload(
            endpoint_arn_holder, body="Itinerary changed to CN - visa now required."
        )
        notif_mod.send_push_notification(updated)

        assert len(recorder.calls) == 2
        bodies = [json.loads(call["Message"])["default"] for call in recorder.calls]
        assert bodies == [
            "Cleared for DE on 2030-01-01.",
            "Itinerary changed to CN - visa now required.",
        ]


class TestSendPushNotificationErrors:
    """Publish failures propagate to the caller."""

    def test_missing_endpoint_arn_raises(self, recorder):
        payload = make_payload("")

        with pytest.raises(Exception):
            notif_mod.send_push_notification(payload)

    def test_unknown_endpoint_arn_raises_client_error(self):
        payload = make_payload("arn:aws:sns:us-east-1:000000000000:endpoint/GCM/App/na")

        with pytest.raises(ClientError):
            notif_mod.send_push_notification(payload)

    def test_publish_exception_propagates(self, endpoint_arn_holder):
        notif_mod.sns_client = FailingSns()
        payload = make_payload(endpoint_arn_holder)

        with pytest.raises(ClientError) as exc_info:
            notif_mod.send_push_notification(payload)

        assert exc_info.value.response["Error"]["Code"] == "EndpointDisabled"


class TestGroupNotificationCleanGroup:
    """A group with no actionable alerts gets an all-clear summary."""

    def test_all_travelers_cleared(self, endpoint_arn_holder, recorder):
        group = make_group_result(
            summaries=[make_summary(), make_summary(first_name="Tim")]
        )

        response = notif_mod.send_group_notification(endpoint_arn_holder, group)

        assert "MessageId" in response
        message = published(recorder)
        assert message["default"] == "All 2 travelers are cleared for travel."
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    def test_empty_group(self, endpoint_arn_holder, recorder):
        group = make_group_result(summaries=[])

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        assert published(recorder)["default"] == "All 0 travelers are cleared for travel."

    def test_info_severity_is_not_actionable(self, endpoint_arn_holder, recorder):
        group = make_group_result(
            summaries=[
                make_summary(
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=AlertSeverity.INFO,
                        reasons=["Visa on file expires after travel"],
                    )
                )
            ],
            group_severity=AlertSeverity.INFO,
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        assert published(recorder)["default"] == "All 1 travelers are cleared for travel."

    def test_severity_without_alert_required_is_ignored(
        self, endpoint_arn_holder, recorder
    ):
        group = make_group_result(
            summaries=[
                make_summary(
                    passport_result=make_passport_result(
                        is_alert_required=False,
                        severity=AlertSeverity.CRITICAL,
                        reasons=["Stale reason"],
                    )
                )
            ]
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        assert published(recorder)["default"] == "All 1 travelers are cleared for travel."

    def test_alert_required_without_severity_is_ignored(
        self, endpoint_arn_holder, recorder
    ):
        group = make_group_result(
            summaries=[
                make_summary(
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=None,
                        reasons=["Unclassified"],
                    )
                )
            ]
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        assert published(recorder)["default"] == "All 1 travelers are cleared for travel."


class TestGroupNotificationActionItems:
    """Travelers with WARNING/CRITICAL issues are named in the summary body."""

    def test_single_traveler_needs_action(self, endpoint_arn_holder, recorder):
        group = make_group_result(
            summaries=[
                make_summary(
                    passport_result=make_passport_result(
                        is_alert_required=True,
                        severity=AlertSeverity.WARNING,
                        reasons=["Passport expires within 6 months of entry to CN"],
                    )
                ),
                make_summary(first_name="Tim"),
            ],
            group_severity=AlertSeverity.WARNING,
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        body = published(recorder)["default"]
        assert body.startswith("1 of 2 travelers need action: ")
        assert "Jane Doe - Passport expires within 6 months of entry to CN" in body

    def test_passport_and_visa_issues_are_both_listed(
        self, endpoint_arn_holder, recorder
    ):
        group = make_group_result(
            summaries=[
                make_summary(
                    passport_result=make_passport_result(
                        is_alert_required=True,
                        severity=AlertSeverity.CRITICAL,
                        reasons=["Passport expired on 2020-01-01"],
                    ),
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=AlertSeverity.CRITICAL,
                        reasons=["No visa on file for CN"],
                    ),
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        body = published(recorder)["default"]
        assert (
            body == "1 of 1 travelers need action: Jane Doe - "
            "Passport expired on 2020-01-01, No visa on file for CN"
        )

    def test_multiple_reasons_are_semicolon_joined(self, endpoint_arn_holder, recorder):
        group = make_group_result(
            summaries=[
                make_summary(
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=AlertSeverity.CRITICAL,
                        reasons=[
                            "No visa on file for CN",
                            "Transit visa required for layover in CN",
                        ],
                        segments=[
                            make_segment(destination="CN", is_layover=True),
                            make_segment(destination="JP"),
                        ],
                    )
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        assert (
            "No visa on file for CN; Transit visa required for layover in CN"
            in published(recorder)["default"]
        )

    def test_multiple_travelers_need_action(self, endpoint_arn_holder, recorder):
        group = make_group_result(
            summaries=[
                make_summary(
                    passport_result=make_passport_result(
                        is_alert_required=True,
                        severity=AlertSeverity.CRITICAL,
                        reasons=["Passport expired on 2020-01-01"],
                    )
                ),
                make_summary(
                    first_name="Tim",
                    last_name="Smith",
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=AlertSeverity.WARNING,
                        reasons=["Visa expires before return from CN"],
                    ),
                ),
                make_summary(first_name="Ann", last_name="Lee"),
            ],
            group_severity=AlertSeverity.CRITICAL,
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        body = published(recorder)["default"]
        assert body.startswith("2 of 3 travelers need action: ")
        assert "Jane Doe - Passport expired on 2020-01-01" in body
        assert "Tim Smith - Visa expires before return from CN" in body
        assert "Ann Lee" not in body


class TestGroupNotificationMessageShape:
    """Group messages include the confirmation number and group severity."""

    def test_metadata_and_title(self, endpoint_arn_holder, recorder):
        group = make_group_result(
            summaries=[
                make_summary(
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=AlertSeverity.CRITICAL,
                        reasons=["No visa on file for CN"],
                    )
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
            confirmation="DL-ABC123",
        )

        notif_mod.send_group_notification(endpoint_arn_holder, group)

        call = recorder.calls[-1]
        assert call["TargetArn"] == endpoint_arn_holder
        assert call["MessageStructure"] == "json"
        message = json.loads(call["Message"])
        apns = json.loads(message["APNS"])
        gcm = json.loads(message["GCM"])
        assert apns["aps"]["alert"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert apns["confirmation_number"] == "DL-ABC123"
        assert apns["group_severity"] == "CRITICAL"
        assert gcm["notification"]["title"] == "Delta Concierge \u2014 Group Travel Alert"
        assert gcm["data"] == {
            "confirmation_number": "DL-ABC123",
            "group_severity": "CRITICAL",
        }
        assert message["default"] == apns["aps"]["alert"]["body"]

    def test_itinerary_change_re_evaluation_publishes_updated_summary(
        self, endpoint_arn_holder, recorder
    ):
        clean = make_group_result(summaries=[make_summary()])
        notif_mod.send_group_notification(endpoint_arn_holder, clean)

        rerouted = make_group_result(
            summaries=[
                make_summary(
                    visa_result=make_visa_result(
                        is_alert_required=True,
                        severity=AlertSeverity.CRITICAL,
                        reasons=["No visa on file for CN"],
                        segments=[make_segment(destination="CN")],
                    )
                )
            ],
            group_severity=AlertSeverity.CRITICAL,
        )
        notif_mod.send_group_notification(endpoint_arn_holder, rerouted)

        bodies = [json.loads(call["Message"])["default"] for call in recorder.calls]
        assert bodies[0] == "All 1 travelers are cleared for travel."
        assert bodies[1] == (
            "1 of 1 travelers need action: Jane Doe - No visa on file for CN"
        )


class TestGroupNotificationErrors:
    """Publish failures during group notification propagate to the caller."""

    def test_publish_exception_propagates(self, endpoint_arn_holder):
        notif_mod.sns_client = FailingSns()
        group = make_group_result()

        with pytest.raises(ClientError):
            notif_mod.send_group_notification(endpoint_arn_holder, group)

    def test_missing_endpoint_arn_raises(self):
        group = make_group_result()

        with pytest.raises(Exception):
            notif_mod.send_group_notification("", group)

