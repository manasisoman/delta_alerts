"""Tests for the SNS push notification service."""

import json
from datetime import date

import boto3
import moto
import pytest
from moto.core import DEFAULT_ACCOUNT_ID
from moto.sns.models import sns_backends

import src.services.notification_service as notif_mod
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertType,
    FlightSegment,
    GroupEvaluationResult,
    GroupItinerary,
    NotificationPayload,
    PassportEvaluation,
    SkyMilesProfile,
    TravelDocRequirements,
    TravelerAlertSummary,
    VisaEvaluation,
    VisaRecord,
)
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)

GROUP_TITLE = "Delta Concierge \u2014 Group Travel Alert"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and create an SNS platform endpoint for every test."""
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


def _published_messages(endpoint_arn: str) -> list[dict]:
    """Return all messages moto recorded for an endpoint, JSON-decoded."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    endpoint = backend.platform_endpoints[endpoint_arn]
    return [json.loads(m) for m in endpoint.messages.values()]


def _last_message(endpoint_arn: str) -> dict:
    """Return the last published message with APNS/GCM bodies decoded."""
    message = _published_messages(endpoint_arn)[-1]
    return {
        "default": message["default"],
        "APNS": json.loads(message["APNS"]),
        "GCM": json.loads(message["GCM"]),
    }


def _profile(
    first_name: str = "Jane",
    last_name: str = "Doe",
    skymiles_number: str = "1111111111",
    nationality: str = "IN",
    passport_number: str | None = "P123456",
    passport_expiry: date | None = date(2035, 1, 1),
    visa_records: list[VisaRecord] | None = None,
) -> SkyMilesProfile:
    return SkyMilesProfile(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        nationality=nationality,
        passport_number=passport_number,
        passport_expiry=passport_expiry,
        visa_records=visa_records or [],
        endpoint_arn="arn:aws:sns:us-east-1:123456789012:endpoint/GCM/TestApp/x",
    )


def _passport_result(
    is_alert_required: bool = False,
    severity: AlertSeverity | None = None,
    reasons: list[str] | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=_profile(),
        segments_evaluated=[],
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
    return VisaEvaluation(
        profile=_profile(),
        segments_evaluated=[],
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
    return TravelerAlertSummary(
        skymiles_number="1111111111",
        first_name=first_name,
        last_name=last_name,
        passport_result=passport_result or _passport_result(),
        visa_result=visa_result or _visa_result(),
    )


def _group_result(
    traveler_summaries: list[TravelerAlertSummary] | None = None,
    confirmation_number: str = "DL-GROUP",
    group_severity: AlertSeverity | None = None,
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=traveler_summaries if traveler_summaries is not None else [_summary()],
        group_severity=group_severity,
    )


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    fields = {
        "endpoint_arn": endpoint_arn,
        "alert_record": AlertRecord(
            alert_id="alert-1",
            skymiles_number="1111111111",
            alert_type=AlertType.PASSPORT,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport has expired"],
            created_at="2030-01-01T00:00:00+00:00",
            itinerary_ref="DL-TEST",
        ),
        "title": "Delta Concierge Alert",
        "body": "Passport has expired",
        "push_data": {"alert_id": "alert-1", "severity": "CRITICAL"},
    }
    fields.update(overrides)
    return NotificationPayload(**fields)


def _segment(
    destination: str,
    departure_date: date = date(2030, 6, 1),
    is_layover: bool = False,
    flight_number: str = "DL100",
) -> FlightSegment:
    return FlightSegment(
        flight_number=flight_number,
        origin="JFK",
        destination=destination,
        departure_date=departure_date,
        arrival_date=departure_date,
        is_layover=is_layover,
    )


def _requirements() -> dict[str, TravelDocRequirements]:
    return {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB", "CA"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "JP": TravelDocRequirements(
            country_code="JP",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US", "GB"],
        ),
    }


class TestSendPushNotification:
    """Individual push notifications publish APNS, GCM and default bodies to the endpoint."""

    def test_publishes_to_endpoint_and_returns_message_id(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        response = send_push_notification(payload)

        assert "MessageId" in response
        assert len(_published_messages(endpoint_arn)) == 1

    def test_default_message_is_body(self, endpoint_arn):
        payload = _payload(endpoint_arn, body="Visa for CN has expired")

        send_push_notification(payload)

        assert _last_message(endpoint_arn)["default"] == "Visa for CN has expired"

    def test_apns_contains_alert_and_flattened_push_data(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        apns = _last_message(endpoint_arn)["APNS"]
        assert apns["aps"] == {
            "alert": {"title": "Delta Concierge Alert", "body": "Passport has expired"},
            "sound": "default",
        }
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "CRITICAL"

    def test_gcm_contains_notification_and_nested_push_data(self, endpoint_arn):
        payload = _payload(endpoint_arn)

        send_push_notification(payload)

        gcm = _last_message(endpoint_arn)["GCM"]
        assert gcm["notification"] == {
            "title": "Delta Concierge Alert",
            "body": "Passport has expired",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "CRITICAL"}

    def test_empty_push_data(self, endpoint_arn):
        payload = _payload(endpoint_arn, push_data={})

        send_push_notification(payload)

        message = _last_message(endpoint_arn)
        assert set(message["APNS"]) == {"aps"}
        assert message["GCM"]["data"] == {}


class TestGroupAllCleared:
    """When no traveler has a WARNING/CRITICAL issue the group is reported as cleared."""

    def test_single_clean_traveler(self, endpoint_arn):
        result = _group_result()

        response = send_group_notification(endpoint_arn, result)

        assert "MessageId" in response
        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_multiple_clean_travelers(self, endpoint_arn):
        result = _group_result([_summary("Jane"), _summary("Tim"), _summary("Ann")])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 3 travelers are cleared for travel."

    def test_empty_group(self, endpoint_arn):
        result = _group_result([])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 0 travelers are cleared for travel."


class TestGroupPassportIssues:
    """Passport issues are listed only when alert-required with WARNING or CRITICAL severity."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_severity_is_listed(self, endpoint_arn, severity):
        passport = _passport_result(True, severity, ["Passport has expired"])
        result = _group_result([_summary(passport_result=passport)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport has expired"
        )

    def test_info_severity_is_not_listed(self, endpoint_arn):
        passport = _passport_result(True, AlertSeverity.INFO, ["Passport expiring within 6 months"])
        result = _group_result([_summary(passport_result=passport)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_none_severity_is_not_listed(self, endpoint_arn):
        passport = _passport_result(True, None, ["Unclassified passport note"])
        result = _group_result([_summary(passport_result=passport)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_not_alert_required_is_not_listed(self, endpoint_arn):
        passport = _passport_result(False, AlertSeverity.CRITICAL, ["Stale reason"])
        result = _group_result([_summary(passport_result=passport)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_multiple_passport_reasons_joined_with_semicolon(self, endpoint_arn):
        passport = _passport_result(
            True,
            AlertSeverity.CRITICAL,
            [
                "Passport does not meet CN's 6-month validity requirement for DL100",
                "Passport does not meet JP's 6-month validity requirement for DL200",
            ],
        )
        result = _group_result([_summary(passport_result=passport)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport does not meet CN's 6-month validity requirement for DL100; "
            "Passport does not meet JP's 6-month validity requirement for DL200"
        )


class TestGroupVisaIssues:
    """Visa issues are listed only when alert-required with WARNING or CRITICAL severity."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_actionable_severity_is_listed(self, endpoint_arn, severity):
        visa = _visa_result(True, severity, ["No visa on file for CN"])
        result = _group_result([_summary("Tim", "Smith", visa_result=visa)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Tim Smith - No visa on file for CN"
        )

    def test_info_severity_is_not_listed(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.INFO, ["Visa for CN expires within 30 days of travel"])
        result = _group_result([_summary(visa_result=visa)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_none_severity_is_not_listed(self, endpoint_arn):
        visa = _visa_result(True, None, ["Unclassified visa note"])
        result = _group_result([_summary(visa_result=visa)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."

    def test_not_alert_required_is_not_listed(self, endpoint_arn):
        visa = _visa_result(False, AlertSeverity.CRITICAL, ["Stale reason"])
        result = _group_result([_summary(visa_result=visa)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == "All 1 travelers are cleared for travel."


class TestGroupActionItemFormatting:
    """Per-traveler issues are combined and counted correctly in the summary body."""

    def test_passport_and_visa_issues_joined_with_comma(self, endpoint_arn):
        passport = _passport_result(True, AlertSeverity.CRITICAL, ["Passport has expired"])
        visa = _visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        result = _group_result([_summary(passport_result=passport, visa_result=visa)])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == (
            "1 of 1 travelers need action: Jane Doe - Passport has expired, No visa on file for CN"
        )

    def test_only_travelers_with_issues_counted(self, endpoint_arn):
        jane = _summary(
            "Jane", "Doe",
            passport_result=_passport_result(True, AlertSeverity.WARNING, ["passport expiring"]),
        )
        tim = _summary(
            "Tim", "Smith",
            visa_result=_visa_result(True, AlertSeverity.CRITICAL, ["visa required for China"]),
        )
        result = _group_result([jane, _summary("Ann", "Lee"), tim, _summary("Bob", "Ray")])

        send_group_notification(endpoint_arn, result)

        assert _last_message(endpoint_arn)["default"] == (
            "2 of 4 travelers need action: "
            "Jane Doe - passport expiring; Tim Smith - visa required for China"
        )

    def test_body_is_identical_across_platforms(self, endpoint_arn):
        visa = _visa_result(True, AlertSeverity.CRITICAL, ["No visa on file for CN"])
        result = _group_result([_summary(visa_result=visa)])

        send_group_notification(endpoint_arn, result)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["aps"]["alert"]["body"] == message["default"]
        assert message["GCM"]["notification"]["body"] == message["default"]


class TestGroupPushData:
    """Group payloads carry title, confirmation number and group severity on both platforms."""

    def test_title_and_sound(self, endpoint_arn):
        result = _group_result()

        send_group_notification(endpoint_arn, result)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["aps"]["alert"]["title"] == GROUP_TITLE
        assert message["APNS"]["aps"]["sound"] == "default"
        assert message["GCM"]["notification"]["title"] == GROUP_TITLE

    @pytest.mark.parametrize("severity", list(AlertSeverity))
    def test_group_severity_value(self, endpoint_arn, severity):
        result = _group_result(group_severity=severity)

        send_group_notification(endpoint_arn, result)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["group_severity"] == severity.value
        assert message["GCM"]["data"]["group_severity"] == severity.value

    def test_missing_group_severity_reported_as_ok(self, endpoint_arn):
        result = _group_result(group_severity=None)

        send_group_notification(endpoint_arn, result)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["group_severity"] == "OK"
        assert message["GCM"]["data"]["group_severity"] == "OK"

    def test_confirmation_number(self, endpoint_arn):
        result = _group_result(confirmation_number="DL-XYZ789")

        send_group_notification(endpoint_arn, result)

        message = _last_message(endpoint_arn)
        assert message["APNS"]["confirmation_number"] == "DL-XYZ789"
        assert message["GCM"]["data"]["confirmation_number"] == "DL-XYZ789"


class TestGroupNotificationFromEvaluation:
    """End-to-end: real evaluator output drives the group notification body."""

    def _notify(self, endpoint_arn, segments, travelers, confirmation_number="DL-GROUP"):
        group = GroupItinerary(
            confirmation_number=confirmation_number,
            segments=segments,
            travelers=travelers,
            primary_traveler=travelers[0].skymiles_number,
        )
        result = evaluate_group_itinerary(group, _requirements())
        send_group_notification(endpoint_arn, result)
        return _last_message(endpoint_arn)

    def test_layover_requiring_transit_visa_is_listed(self, endpoint_arn):
        segments = [_segment("CN", is_layover=True), _segment("DE", flight_number="DL200")]

        message = self._notify(endpoint_arn, segments, [_profile(nationality="US")])

        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - No visa on file for CN"
        )
        assert message["APNS"]["group_severity"] == "CRITICAL"

    def test_layover_without_transit_visa_requirement_is_cleared(self, endpoint_arn):
        segments = [_segment("JP", is_layover=True), _segment("DE", flight_number="DL200")]

        message = self._notify(endpoint_arn, segments, [_profile(nationality="CA")])

        assert message["default"] == "All 1 travelers are cleared for travel."

    def test_passport_below_country_validity_requirement_is_listed(self, endpoint_arn):
        # 4 months of validity: enough for DE (3), not for CN (6)
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2035, 1, 1), "V1")
        traveler = _profile(passport_expiry=date(2030, 10, 1), visa_records=[visa])

        message = self._notify(endpoint_arn, [_segment("CN")], [traveler])

        assert message["default"] == (
            "1 of 1 travelers need action: Jane Doe - "
            "Passport does not meet CN's 6-month validity requirement for DL100"
        )

    def test_passport_meeting_country_validity_requirement_is_cleared(self, endpoint_arn):
        # 4 months of validity satisfies DE's 3-month rule; the 6-month INFO nudge is not actionable
        traveler = _profile(nationality="US", passport_expiry=date(2030, 10, 1))

        message = self._notify(endpoint_arn, [_segment("DE")], [traveler])

        assert message["default"] == "All 1 travelers are cleared for travel."
        assert message["APNS"]["group_severity"] == "INFO"

    def test_itinerary_change_sends_updated_summary(self, endpoint_arn):
        travelers = [
            _profile("Jane", "Doe", nationality="US"),
            _profile("Tim", "Smith", skymiles_number="2222222222", nationality="GB"),
        ]

        original = self._notify(endpoint_arn, [_segment("DE")], travelers, "DL-ORIG")
        changed = self._notify(endpoint_arn, [_segment("CN")], travelers, "DL-CHANGED")

        assert len(_published_messages(endpoint_arn)) == 2
        assert original["default"] == "All 2 travelers are cleared for travel."
        assert original["GCM"]["data"] == {"confirmation_number": "DL-ORIG", "group_severity": "OK"}
        assert changed["default"] == (
            "2 of 2 travelers need action: "
            "Jane Doe - No visa on file for CN; Tim Smith - No visa on file for CN"
        )
        assert changed["GCM"]["data"] == {
            "confirmation_number": "DL-CHANGED",
            "group_severity": "CRITICAL",
        }
