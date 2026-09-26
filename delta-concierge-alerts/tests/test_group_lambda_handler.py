"""Critical-path tests for the group Lambda handler end-to-end flow."""

from datetime import date

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import (
    _find_traveler,
    _parse_group_itinerary,
    group_handler,
)
from src.models.types import GroupItinerary
from tests.conftest import make_profile, make_segment


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
    with moto.mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="ConciergeAlerts",
            KeySchema=[
                {"AttributeName": "skymiles_number", "KeyType": "HASH"},
                {"AttributeName": "alert_id", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "skymiles_number", "AttributeType": "S"},
                {"AttributeName": "alert_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


PRIMARY = "1000000001"
SECONDARY = "1000000002"

# Far-future travel dates so tests never drift.
DEPARTURE = "2030-06-01"
ARRIVAL = "2030-06-02"


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = PRIMARY,
    first_name: str = "Primary",
    last_name: str = "Traveler",
    nationality: str = "US",
    passport_number: str | None = "P111111",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler profile dict with sensible defaults."""
    return {
        "skymiles_number": skymiles_number,
        "first_name": first_name,
        "last_name": last_name,
        "nationality": nationality,
        "passport_number": passport_number,
        "passport_expiry": passport_expiry,
        "endpoint_arn": endpoint_arn,
        "visa_records": visa_records or [],
    }


def _segment(
    destination: str = "DE",
    departure_date: str = DEPARTURE,
    arrival_date: str = ARRIVAL,
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> dict:
    """Build a flight segment dict with sensible defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event (two clean US travelers to DE)."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": PRIMARY,
        "segments": [_segment()],
        "travelers": [
            _traveler(endpoint_arn),
            _traveler(
                endpoint_arn,
                skymiles_number=SECONDARY,
                first_name="Second",
                last_name="Traveler",
            ),
        ],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _result_for(response: dict, skymiles_number: str) -> dict:
    """Return the per-traveler result entry for the given SkyMiles number."""
    return next(
        r for r in response["body"]["traveler_results"]
        if r["skymiles_number"] == skymiles_number
    )


def _scan_alerts() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


class TestHappyPath:
    """Clean group → no alerts persisted, OK statuses, summary shape intact."""

    def test_clean_group_returns_ok_and_no_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert len(body["traveler_results"]) == 2
        for result in body["traveler_results"]:
            assert result["passport_status"] == "OK"
            assert result["visa_status"] == "OK"
            assert result["alerts_sent"] == 0
        assert _scan_alerts() == []

    def test_traveler_results_include_names(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        secondary = _result_for(response, SECONDARY)
        assert secondary["first_name"] == "Second"
        assert secondary["last_name"] == "Traveler"


class TestPassportAlerts:
    """Passport failures for one traveler persist a PASSPORT alert for only that traveler."""

    def test_expired_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][1]["passport_expiry"] = "2020-01-01"

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
        assert _result_for(response, PRIMARY)["passport_status"] == "OK"
        secondary = _result_for(response, SECONDARY)
        assert secondary["passport_status"] == "CRITICAL"
        assert secondary["alerts_sent"] == 1

        items = _scan_alerts()
        assert len(items) == 1
        assert items[0]["alert_type"] == "PASSPORT"
        assert items[0]["skymiles_number"] == SECONDARY
        assert items[0]["itinerary_ref"] == "DL-GROUP"
        assert items[0]["status"] == "ACTIVE"

    def test_missing_passport_persists_critical_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_number"] = None
        event["travelers"][0]["passport_expiry"] = None

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["passport_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1

    def test_passport_below_destination_minimum_is_critical(self, endpoint_arn):
        """JP requires 6 months; passport valid only ~4 months past travel."""
        event = _base_event(endpoint_arn, segments=[_segment(destination="JP")])
        event["travelers"][0]["passport_expiry"] = "2030-10-01"

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["passport_status"] == "CRITICAL"
        assert _result_for(response, SECONDARY)["passport_status"] == "OK"

    def test_passport_meets_schengen_minimum_but_expiring_soon_is_info(self, endpoint_arn):
        """DE requires 3 months; passport valid ~4 months past travel → INFO advisory."""
        event = _base_event(endpoint_arn)
        event["travelers"][0]["passport_expiry"] = "2030-10-01"

        response = group_handler(event, None)

        primary = _result_for(response, PRIMARY)
        assert primary["passport_status"] == "INFO"
        assert primary["alerts_sent"] == 1
        assert response["body"]["group_severity"] == "INFO"
        items = _scan_alerts()
        assert len(items) == 1
        assert items[0]["severity"] == "INFO"


class TestVisaAlerts:
    """Visa failures persist a VISA alert; exemptions and layover rules are honored."""

    def test_missing_visa_for_cn_persists_critical_alert(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 2  # US national also needs CN visa
        secondary = _result_for(response, SECONDARY)
        assert secondary["visa_status"] == "CRITICAL"
        assert secondary["passport_status"] == "OK"

        visa_alerts = [i for i in _scan_alerts() if i["alert_type"] == "VISA"]
        assert len(visa_alerts) == 2
        assert all("No visa on file for CN" in a["reasons"] for a in visa_alerts)

    def test_valid_visa_clears_traveler(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        for traveler in event["travelers"]:
            traveler["visa_records"] = [
                {
                    "country_code": "CN",
                    "visa_type": "TOURIST",
                    "issue_date": "2029-01-01",
                    "expiry_date": "2035-01-01",
                    "visa_number": "V123",
                }
            ]

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2030-05-01",
                "visa_number": "V123",
            }
        ]

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["visa_status"] == "CRITICAL"

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        event["travelers"][1]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": DEPARTURE,
                "visa_number": "V123",
            }
        ]

        response = group_handler(event, None)

        assert _result_for(response, SECONDARY)["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "CRITICAL"  # primary has no visa

    def test_exempt_nationality_skips_visa_check(self, endpoint_arn):
        """US nationals are visa-exempt for DE while IN nationals are not."""
        event = _base_event(endpoint_arn)
        event["travelers"][1]["nationality"] = "IN"

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["visa_status"] == "OK"
        assert _result_for(response, SECONDARY)["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1

    def test_both_passport_and_visa_alerts_for_one_traveler(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        event["travelers"][1]["passport_expiry"] = "2020-01-01"
        event["travelers"][0]["visa_records"] = [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2029-01-01",
                "expiry_date": "2035-01-01",
                "visa_number": "V123",
            }
        ]

        response = group_handler(event, None)

        secondary = _result_for(response, SECONDARY)
        assert secondary["passport_status"] == "CRITICAL"
        assert secondary["visa_status"] == "CRITICAL"
        assert secondary["alerts_sent"] == 2
        assert _result_for(response, PRIMARY)["alerts_sent"] == 0
        assert response["body"]["total_alerts_sent"] == 2
        types = sorted(i["alert_type"] for i in _scan_alerts())
        assert types == ["PASSPORT", "VISA"]


class TestLayovers:
    """Layover segments only require a visa when the country demands transit visas."""

    def test_layover_in_transit_visa_country_is_critical(self, endpoint_arn):
        """CN requires transit visas → layover without visa is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="CN", is_layover=True),
                _segment(
                    destination="JP",
                    departure_date="2030-06-02",
                    arrival_date="2030-06-03",
                    flight_number="DL101",
                    origin="PEK",
                ),
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, PRIMARY)["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 2

    def test_layover_without_transit_visa_requirement_is_ok(self, endpoint_arn):
        """DE does not require transit visas → IN national layover is skipped."""
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="DE", is_layover=True),
                _segment(
                    destination="US",
                    departure_date="2030-06-02",
                    arrival_date="2030-06-03",
                    flight_number="DL101",
                    origin="FRA",
                ),
            ],
        )
        event["travelers"][0]["nationality"] = "DE"
        event["travelers"][1]["nationality"] = "DE"

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"


class TestRequirementsOverride:
    """requirements_override replaces default country rules for the given codes."""

    def test_override_waives_visa_requirement(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        event["requirements_override"] = {"CN": {"requires_visa": False}}

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"

    def test_override_defaults_fill_missing_fields(self, endpoint_arn):
        """Empty override dict → requires_visa=True, 6-month passport minimum."""
        event = _base_event(endpoint_arn)
        event["requirements_override"] = {"DE": {}}
        event["travelers"][0]["passport_expiry"] = "2030-10-01"

        response = group_handler(event, None)

        primary = _result_for(response, PRIMARY)
        assert primary["passport_status"] == "CRITICAL"
        assert primary["visa_status"] == "CRITICAL"  # exemptions removed by override

    def test_override_adds_unknown_country(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="ZZ")])
        event["requirements_override"] = {
            "ZZ": {
                "country_code": "ZZ",
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 3,
                "visa_exempt_nationalities": ["US"],
            }
        }

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0

    def test_empty_override_dict_uses_defaults(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(destination="CN")])
        event["requirements_override"] = {}

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2


class TestGroupNotification:
    """A group summary push is sent to the primary traveler when they can be found."""

    def test_group_summary_sent_to_primary(self, endpoint_arn, monkeypatch):
        import src.handlers.group_lambda_handler as handler_mod

        calls = []
        monkeypatch.setattr(
            handler_mod,
            "send_group_notification",
            lambda arn, result: calls.append((arn, result)),
        )
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        assert len(calls) == 1
        assert calls[0][0] == endpoint_arn
        assert calls[0][1].confirmation_number == "DL-GROUP"

    def test_no_group_summary_when_primary_not_in_travelers(self, endpoint_arn, monkeypatch):
        import src.handlers.group_lambda_handler as handler_mod

        calls = []
        monkeypatch.setattr(
            handler_mod,
            "send_group_notification",
            lambda arn, result: calls.append((arn, result)),
        )
        event = _base_event(endpoint_arn, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert calls == []


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments in the result."""

    def test_adding_cn_segment_creates_new_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        first = group_handler(event, None)
        assert first["body"]["total_alerts_sent"] == 0

        event["segments"].append(
            _segment(
                destination="CN",
                departure_date="2030-06-05",
                arrival_date="2030-06-06",
                flight_number="DL300",
                origin="FRA",
            )
        )

        second = group_handler(event, None)

        assert second["body"]["total_alerts_sent"] == 2
        assert second["body"]["group_severity"] == "CRITICAL"
        assert len(_scan_alerts()) == 2

    def test_removing_problem_segment_clears_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(),
                _segment(
                    destination="CN",
                    departure_date="2030-06-05",
                    arrival_date="2030-06-06",
                    flight_number="DL300",
                    origin="FRA",
                ),
            ],
        )
        first = group_handler(event, None)
        assert first["body"]["group_severity"] == "CRITICAL"

        event["segments"] = [_segment()]

        second = group_handler(event, None)

        assert second["body"]["group_severity"] == "OK"
        assert second["body"]["total_alerts_sent"] == 0


class TestParseGroupItinerary:
    """_parse_group_itinerary deserializes segments and travelers with defaults."""

    def test_parses_all_fields(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["segments"][0]["is_layover"]

        group = _parse_group_itinerary(event)

        assert isinstance(group, GroupItinerary)
        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == PRIMARY
        assert len(group.travelers) == 2
        seg = group.segments[0]
        assert seg.departure_date == date(2030, 6, 1)
        assert seg.arrival_date == date(2030, 6, 2)
        assert seg.is_layover is False

    def test_parses_layover_flag(self, endpoint_arn):
        event = _base_event(endpoint_arn, segments=[_segment(is_layover=True)])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is True


class TestFindTraveler:
    """_find_traveler returns the matching profile or None."""

    def _group(self) -> GroupItinerary:
        first = make_profile()
        second = make_profile(nationality="US")
        second.skymiles_number = "8888888"
        return GroupItinerary(
            confirmation_number="DL-TEST",
            segments=[make_segment()],
            travelers=[first, second],
            primary_traveler="9999999",
        )

    def test_returns_matching_traveler(self):
        group = self._group()

        found = _find_traveler(group, "8888888")

        assert found is group.travelers[1]

    def test_returns_none_when_missing(self):
        group = self._group()

        found = _find_traveler(group, "0000000")

        assert found is None
