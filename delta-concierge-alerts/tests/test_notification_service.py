"""Critical-path tests for the SNS push notification service."""

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
    """Activate moto and swap the module-level SNS client for every test."""
    with moto.mock_aws():
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]

        orig_sns_client = notif_mod.sns_client
        notif_mod.sns_client = sns

        try:
            yield sns, app_arn
        finally:
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def make_endpoint(_mock_aws):
    """Factory creating one moto platform endpoint per device token."""
    sns, app_arn = _mock_aws

    def _make(token: str = "fake-token") -> str:
        return sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token=token,
        )["EndpointArn"]

    return _make


def _published(endpoint_arn: str) -> list[dict]:
    """Return decoded top-level JSON messages published to an endpoint."""
    backend = sns_backends[DEFAULT_ACCOUNT_ID]["us-east-1"]
    messages = backend.platform_endpoints[endpoint_arn].messages.values()
    return [json.loads(m) for m in messages]


def _only_message(endpoint_arn: str) -> dict:
    messages = _published(endpoint_arn)
    assert len(messages) == 1
    return messages[0]


def _alert_record(**overrides) -> AlertRecord:
    record = dict(
        alert_id="alert-1",
        skymiles_number="1234567890",
        alert_type=AlertType.PASSPORT,
        severity=AlertSeverity.WARNING,
        reasons=["Passport expires soon"],
        created_at="2030-01-01T00:00:00Z",
        itinerary_ref="DL-TEST",
    )
    record.update(overrides)
    return AlertRecord(**record)


def _payload(endpoint_arn: str, **overrides) -> NotificationPayload:
    payload = dict(
        endpoint_arn=endpoint_arn,
        alert_record=_alert_record(),
        title="Passport Alert",
        body="Your passport expires before your trip.",
        push_data={"alert_id": "alert-1", "severity": "WARNING"},
    )
    payload.update(overrides)
    return NotificationPayload(**payload)


def _profile(**overrides) -> SkyMilesProfile:
    profile = dict(
        skymiles_number="1111111111",
        first_name="Jane",
        last_name="Doe",
        nationality="US",
        passport_number="P123456",
        passport_expiry=date(2035, 1, 1),
        visa_records=[],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )
    profile.update(overrides)
    return SkyMilesProfile(**profile)


def _passport_eval(
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


def _visa_eval(
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
        passport_result=passport_result or _passport_eval(),
        visa_result=visa_result or _visa_eval(),
    )


def _group_result(
    summaries: list[TravelerAlertSummary] | None = None,
    group_severity: AlertSeverity | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupEvaluationResult:
    return GroupEvaluationResult(
        confirmation_number=confirmation_number,
        traveler_summaries=summaries if summaries is not None else [_summary()],
        group_severity=group_severity,
    )


def _segment(
    destination: str = "DE",
    departure: date = date(2030, 6, 1),
    is_layover: bool = False,
    flight_number: str = "DL100",
) -> FlightSegment:
    return FlightSegment(
        flight_number=flight_number,
        origin="ATL",
        destination=destination,
        departure_date=departure,
        arrival_date=departure,
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
    """Individual pushes publish a platform-specific JSON message to the payload's endpoint."""

    def test_publishes_to_payload_endpoint(self, make_endpoint):
        endpoint = make_endpoint()

        response = send_push_notification(_payload(endpoint))

        assert "MessageId" in response
        assert len(_published(endpoint)) == 1

    def test_default_message_is_body(self, make_endpoint):
        endpoint = make_endpoint()

        send_push_notification(_payload(endpoint, body="Check your visa."))

        assert _only_message(endpoint)["default"] == "Check your visa."

    def test_apns_message_has_title_body_sound_and_push_data(self, make_endpoint):
        endpoint = make_endpoint()

        send_push_notification(_payload(endpoint))

        apns = json.loads(_only_message(endpoint)["APNS"])
        assert apns["aps"]["alert"] == {
            "title": "Passport Alert",
            "body": "Your passport expires before your trip.",
        }
        assert apns["aps"]["sound"] == "default"
        assert apns["alert_id"] == "alert-1"
        assert apns["severity"] == "WARNING"

    def test_gcm_message_nests_push_data_under_data(self, make_endpoint):
        endpoint = make_endpoint()

        send_push_notification(_payload(endpoint))

        gcm = json.loads(_only_message(endpoint)["GCM"])
        assert gcm["notification"] == {
            "title": "Passport Alert",
            "body": "Your passport expires before your trip.",
        }
        assert gcm["data"] == {"alert_id": "alert-1", "severity": "WARNING"}

    def test_empty_push_data(self, make_endpoint):
        endpoint = make_endpoint()

        send_push_notification(_payload(endpoint, push_data={}))

        message = _only_message(endpoint)
        assert set(json.loads(message["APNS"]).keys()) == {"aps"}
        assert json.loads(message["GCM"])["data"] == {}

    def test_only_target_endpoint_receives_push(self, make_endpoint):
        target = make_endpoint("token-a")
        other = make_endpoint("token-b")

        send_push_notification(_payload(target))

        assert len(_published(target)) == 1
        assert _published(other) == []


class TestGroupAllCleared:
    """When nobody needs action, the summary says all travelers are cleared."""

    def test_single_clean_traveler(self, make_endpoint):
        endpoint = make_endpoint()

        send_group_notification(endpoint, _group_result())

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."

    def test_multiple_clean_travelers(self, make_endpoint):
        endpoint = make_endpoint()
        summaries = [_summary(first_name=n) for n in ("A", "B", "C")]

        send_group_notification(endpoint, _group_result(summaries=summaries))

        assert _only_message(endpoint)["default"] == "All 3 travelers are cleared for travel."

    def test_empty_group(self, make_endpoint):
        endpoint = make_endpoint()

        send_group_notification(endpoint, _group_result(summaries=[]))

        assert _only_message(endpoint)["default"] == "All 0 travelers are cleared for travel."


class TestGroupActionItemFiltering:
    """Only alert-required WARNING/CRITICAL results become action items."""

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_passport_warning_or_critical_included(self, make_endpoint, severity):
        endpoint = make_endpoint()
        summary = _summary(passport_result=_passport_eval(True, severity, ["passport expiring"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == (
            "1 of 1 travelers need action: Jane Doe - passport expiring"
        )

    @pytest.mark.parametrize("severity", [AlertSeverity.WARNING, AlertSeverity.CRITICAL])
    def test_visa_warning_or_critical_included(self, make_endpoint, severity):
        endpoint = make_endpoint()
        summary = _summary(visa_result=_visa_eval(True, severity, ["visa required for CN"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == (
            "1 of 1 travelers need action: Jane Doe - visa required for CN"
        )

    def test_passport_info_severity_excluded(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(passport_result=_passport_eval(True, AlertSeverity.INFO, ["fyi"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."

    def test_visa_info_severity_excluded(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(visa_result=_visa_eval(True, AlertSeverity.INFO, ["fyi"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."

    def test_passport_not_alert_required_excluded(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(
            passport_result=_passport_eval(False, AlertSeverity.CRITICAL, ["ignored"])
        )

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."

    def test_visa_not_alert_required_excluded(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(visa_result=_visa_eval(False, AlertSeverity.CRITICAL, ["ignored"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."

    def test_passport_none_severity_excluded(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(passport_result=_passport_eval(True, None, ["ignored"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."

    def test_visa_none_severity_excluded(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(visa_result=_visa_eval(True, None, ["ignored"]))

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == "All 1 travelers are cleared for travel."


class TestGroupActionItemFormatting:
    """Action items join reasons with '; ' and passport/visa issues with ', '."""

    def test_multiple_passport_reasons_joined_with_semicolon(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(
            passport_result=_passport_eval(True, AlertSeverity.WARNING, ["r1", "r2"])
        )

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == (
            "1 of 1 travelers need action: Jane Doe - r1; r2"
        )

    def test_passport_and_visa_issues_joined_with_comma(self, make_endpoint):
        endpoint = make_endpoint()
        summary = _summary(
            passport_result=_passport_eval(True, AlertSeverity.WARNING, ["passport expiring"]),
            visa_result=_visa_eval(True, AlertSeverity.CRITICAL, ["visa required"]),
        )

        send_group_notification(endpoint, _group_result(summaries=[summary]))

        assert _only_message(endpoint)["default"] == (
            "1 of 1 travelers need action: Jane Doe - passport expiring, visa required"
        )

    def test_mixed_group_counts_only_travelers_needing_action(self, make_endpoint):
        endpoint = make_endpoint()
        summaries = [
            _summary("Jane", "Doe", passport_result=_passport_eval(
                True, AlertSeverity.WARNING, ["passport expiring"])),
            _summary("Bob", "Clean"),
            _summary("Tim", "Lee", visa_result=_visa_eval(
                True, AlertSeverity.CRITICAL, ["visa required for China"])),
            _summary("Ann", "Info", visa_result=_visa_eval(
                True, AlertSeverity.INFO, ["fyi"])),
        ]

        send_group_notification(endpoint, _group_result(summaries=summaries))

        assert _only_message(endpoint)["default"] == (
            "2 of 4 travelers need action: Jane Doe - passport expiring; "
            "Tim Lee - visa required for China"
        )


class TestGroupMessageStructure:
    """APNS/GCM payloads carry title, body, confirmation number and group severity."""

    def test_apns_payload(self, make_endpoint):
        endpoint = make_endpoint()

        send_group_notification(
            endpoint, _group_result(group_severity=AlertSeverity.WARNING)
        )

        message = _only_message(endpoint)
        apns = json.loads(message["APNS"])
        assert apns["aps"]["alert"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert apns["aps"]["sound"] == "default"
        assert apns["confirmation_number"] == "DL-GROUP"
        assert apns["group_severity"] == "WARNING"

    def test_gcm_payload(self, make_endpoint):
        endpoint = make_endpoint()

        send_group_notification(
            endpoint, _group_result(group_severity=AlertSeverity.CRITICAL)
        )

        message = _only_message(endpoint)
        gcm = json.loads(message["GCM"])
        assert gcm["notification"] == {"title": GROUP_TITLE, "body": message["default"]}
        assert gcm["data"] == {"confirmation_number": "DL-GROUP", "group_severity": "CRITICAL"}

    def test_none_group_severity_reported_as_ok(self, make_endpoint):
        endpoint = make_endpoint()

        send_group_notification(endpoint, _group_result(group_severity=None))

        message = _only_message(endpoint)
        assert json.loads(message["APNS"])["group_severity"] == "OK"
        assert json.loads(message["GCM"])["data"]["group_severity"] == "OK"

    def test_only_primary_endpoint_receives_group_summary(self, make_endpoint):
        primary = make_endpoint("primary")
        companion = make_endpoint("companion")

        response = send_group_notification(primary, _group_result())

        assert "MessageId" in response
        assert len(_published(primary)) == 1
        assert _published(companion) == []


class TestGroupNotificationFromEvaluatedItinerary:
    """Summaries built from real evaluator output reflect layovers, country limits and itinerary changes."""

    def _notify(self, endpoint: str, segments: list[FlightSegment], travelers) -> str:
        group = GroupItinerary(
            confirmation_number="DL-GROUP",
            segments=segments,
            travelers=travelers,
            primary_traveler=travelers[0].skymiles_number,
        )
        result = evaluate_group_itinerary(group, _requirements())
        send_group_notification(endpoint, result)
        return _published(endpoint)[-1]

    def test_visa_exempt_travelers_cleared(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile(), _profile(skymiles_number="2", first_name="Tim")]

        message = self._notify(endpoint, [_segment("DE")], travelers)

        assert message["default"] == "All 2 travelers are cleared for travel."
        assert json.loads(message["APNS"])["group_severity"] == "OK"

    def test_layover_without_transit_visa_requirement_not_flagged(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile(nationality="IN")]

        message = self._notify(
            endpoint, [_segment("JP", is_layover=True), _segment("DE", flight_number="DL200")],
            travelers,
        )

        assert "JP" not in message["default"]
        assert message["default"].startswith("1 of 1 travelers need action: Jane Doe - ")
        assert "DE" in message["default"]

    def test_layover_requiring_transit_visa_flagged(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile()]

        message = self._notify(endpoint, [_segment("CN", is_layover=True)], travelers)

        assert message["default"].startswith("1 of 1 travelers need action: Jane Doe - ")
        assert "CN" in message["default"]
        assert json.loads(message["APNS"])["group_severity"] == "CRITICAL"

    def test_passport_within_schengen_limit_cleared(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile(passport_expiry=date(2030, 10, 1))]

        message = self._notify(endpoint, [_segment("DE", departure=date(2030, 6, 1))], travelers)

        assert message["default"] == "All 1 travelers are cleared for travel."

    def test_passport_below_country_limit_flagged(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile(passport_expiry=date(2030, 10, 1))]

        message = self._notify(endpoint, [_segment("JP", departure=date(2030, 6, 1))], travelers)

        assert message["default"].startswith("1 of 1 travelers need action: Jane Doe - ")

    def test_expired_passport_flagged_critical(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile(passport_expiry=date(2020, 1, 1))]

        message = self._notify(endpoint, [_segment("DE")], travelers)

        assert message["default"].startswith("1 of 1 travelers need action: Jane Doe - ")
        assert json.loads(message["APNS"])["group_severity"] == "CRITICAL"

    def test_valid_visa_on_file_cleared(self, make_endpoint):
        endpoint = make_endpoint()
        visa = VisaRecord("CN", "TOURIST", date(2029, 1, 1), date(2035, 1, 1), "V1")
        travelers = [_profile(visa_records=[visa])]

        message = self._notify(endpoint, [_segment("CN")], travelers)

        assert message["default"] == "All 1 travelers are cleared for travel."

    def test_itinerary_change_to_visa_country_triggers_action(self, make_endpoint):
        endpoint = make_endpoint()
        travelers = [_profile()]

        original = self._notify(endpoint, [_segment("DE")], travelers)
        changed = self._notify(endpoint, [_segment("CN")], travelers)

        assert original["default"] == "All 1 travelers are cleared for travel."
        assert changed["default"].startswith("1 of 1 travelers need action: Jane Doe - ")
        assert len(_published(endpoint)) == 2
