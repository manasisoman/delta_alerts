"""Branch-coverage tests for the single-traveler Lambda handler.

Uses made-up country codes (XA, XB, ...) supplied through
``requirements_override`` and 2030+ dates so results are independent of the
production ``COUNTRY_REQUIREMENTS`` data and of today's date.
"""

import json
from datetime import date, datetime, timedelta

import boto3
import moto
import moto.core
import pytest
from moto.sns.models import sns_backends

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.lambda_handler import (
    _compute_ttl,
    _parse_itinerary,
    _parse_profile,
    evaluate_itinerary,
    handler,
)
from src.models.types import FlightSegment, Itinerary

DEPARTURE = "2030-06-01"
ARRIVAL = "2030-06-02"
CLEAN_PASSPORT_EXPIRY = "2035-01-01"


@pytest.fixture(autouse=True)
def aws():
    """Moto DynamoDB table + SNS platform app, with module clients swapped in."""
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
                {"AttributeName": "itinerary_ref", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "itinerary-ref-index",
                    "KeySchema": [{"AttributeName": "itinerary_ref", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )

        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp",
            Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]

        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client
        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns
        try:
            yield {"sns": sns, "app_arn": app_arn}
        finally:
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


def _new_endpoint(aws, token: str) -> str:
    return aws["sns"].create_platform_endpoint(
        PlatformApplicationArn=aws["app_arn"], Token=token
    )["EndpointArn"]


@pytest.fixture
def endpoint_arn(aws):
    return _new_endpoint(aws, "primary-token")


def _sent_messages(arn: str) -> list[dict]:
    backend = sns_backends[moto.core.DEFAULT_ACCOUNT_ID]["us-east-1"]
    return [json.loads(m) for m in backend.platform_endpoints[arn].messages.values()]


def _gcm_notifications(arn: str) -> list[dict]:
    return [json.loads(m["GCM"]) for m in _sent_messages(arn)]


def _segment(destination: str = "XA", **overrides) -> dict:
    seg = {
        "flight_number": "DL100",
        "origin": "JFK",
        "destination": destination,
        "departure_date": DEPARTURE,
        "arrival_date": ARRIVAL,
        "is_layover": False,
    }
    seg.update(overrides)
    return seg


def _visa(country_code: str = "XA", expiry_date: str = "2035-01-01", **overrides) -> dict:
    record = {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": "2025-01-01",
        "expiry_date": expiry_date,
        "visa_number": "V123",
    }
    record.update(overrides)
    return record


def _base_event(endpoint_arn: str, **profile_overrides) -> dict:
    """Clean event: XA requires a visa (exempt for nobody) and the traveler holds one."""
    profile = {
        "skymiles_number": "1234567890",
        "first_name": "Test",
        "last_name": "User",
        "nationality": "IN",
        "passport_number": "P999999",
        "passport_expiry": CLEAN_PASSPORT_EXPIRY,
        "endpoint_arn": endpoint_arn,
        "visa_records": [_visa()],
    }
    profile.update(profile_overrides)
    return {
        "profile": profile,
        "itinerary": {"confirmation_number": "DL-COV", "segments": [_segment()]},
        "requirements_override": {
            "XA": {
                "requires_visa": True,
                "transit_visa_required": False,
                "passport_validity_months": 6,
                "visa_exempt_nationalities": [],
            },
            "XB": {"requires_visa": False, "passport_validity_months": 3},
        },
    }


def _stored_items() -> list[dict]:
    return alert_store_mod.table.scan()["Items"]


class TestHappyPath:
    """A fully documented traveler gets an OK response and nothing is sent or stored."""

    def test_clean_traveler_returns_ok(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = handler(event, None)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 0, "passport_status": "OK", "visa_status": "OK"},
        }

    def test_clean_traveler_stores_nothing(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert _stored_items() == []

    def test_clean_traveler_sends_no_push(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        handler(event, None)

        assert _sent_messages(endpoint_arn) == []


class TestGroupDelegation:
    """Events carrying ``travelers`` are routed to the group handler."""

    def _group_event(self, aws) -> dict:
        arn_a = _new_endpoint(aws, "token-a")
        arn_b = _new_endpoint(aws, "token-b")
        base = _base_event(arn_a)
        traveler_a = dict(base["profile"], skymiles_number="A1")
        traveler_b = dict(
            base["profile"], skymiles_number="B2", endpoint_arn=arn_b, visa_records=[]
        )
        return {
            "confirmation_number": "DL-GROUP",
            "primary_traveler": "A1",
            "segments": [_segment()],
            "travelers": [traveler_a, traveler_b],
            "requirements_override": base["requirements_override"],
        }

    def test_group_event_returns_group_response_shape(self, aws):
        event = self._group_event(aws)

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["confirmation_number"] == "DL-GROUP"
        assert "traveler_results" in response["body"]
        assert "alerts_sent" not in response["body"]

    def test_group_event_evaluates_each_traveler(self, aws):
        event = self._group_event(aws)

        response = handler(event, None)

        results = {r["skymiles_number"]: r for r in response["body"]["traveler_results"]}
        assert results["A1"]["visa_status"] == "OK"
        assert results["B2"]["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1

    def test_evaluate_itinerary_also_delegates_group_events(self, aws):
        event = self._group_event(aws)

        response = evaluate_itinerary(event)

        assert response["body"]["confirmation_number"] == "DL-GROUP"


class TestRequirementsOverride:
    """``requirements_override`` entries replace default rules, with per-field defaults."""

    def test_override_with_no_fields_defaults_to_visa_required(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["requirements_override"] = {"XA": {}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_override_with_no_fields_defaults_to_six_month_validity(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-10-01")
        event["requirements_override"] = {"XA": {}}

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"

    def test_override_visa_not_required(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [_segment("XB")]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_exempt_nationality_skips_visa(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["requirements_override"]["XA"]["visa_exempt_nationalities"] = ["IN"]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_shorter_validity_downgrades_passport_to_info(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2030-10-01")
        event["itinerary"]["segments"] = [_segment("XB")]

        response = handler(event, None)

        assert response["body"]["passport_status"] == "INFO"

    def test_override_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [_segment("XA", is_layover=True)]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_override_layover_with_transit_visa_requirement_alerts(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [_segment("XA", is_layover=True)]
        event["requirements_override"]["XA"]["transit_visa_required"] = True

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_override_takes_precedence_over_default_country_data(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [_segment("CN")]
        event["requirements_override"] = {"CN": {"requires_visa": False}}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_missing_override_key_uses_default_country_data(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [_segment("CN")]
        del event["requirements_override"]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_empty_override_dict_uses_default_country_data(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[], nationality="US")
        event["itinerary"]["segments"] = [_segment("DE")]
        event["requirements_override"] = {}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_unknown_country_without_override_falls_back_to_visa_required(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [_segment("ZZ")]
        event["requirements_override"] = None

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestVisaRecords:
    """Visa records from the event are parsed and evaluated against departure."""

    def test_expired_visa_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa(expiry_date="2020-01-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa(expiry_date="2030-05-01")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa(expiry_date=DEPARTURE)])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "WARNING"

    def test_visa_expiring_within_warning_window_is_info(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa(expiry_date="2030-06-20")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "INFO"

    def test_visa_for_other_country_does_not_count(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa(country_code="XC")])

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"

    def test_latest_matching_visa_is_used(self, endpoint_arn):
        records = [_visa(expiry_date="2020-01-01"), _visa(expiry_date="2035-01-01")]
        event = _base_event(endpoint_arn, visa_records=records)

        response = handler(event, None)

        assert response["body"]["visa_status"] == "OK"

    def test_missing_visa_records_key_treated_as_no_visas(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["profile"]["visa_records"]

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"


class TestPassportParsing:
    """Passport fields are optional in the event; blanks count as missing."""

    def test_empty_passport_expiry_string_is_missing(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="")

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"

    def test_absent_passport_keys_are_missing(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["profile"]["passport_number"]
        del event["profile"]["passport_expiry"]

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"

    def test_parse_profile_converts_dates(self, endpoint_arn):
        data = _base_event(endpoint_arn)["profile"]

        profile = _parse_profile(data)

        assert profile.passport_expiry == date(2035, 1, 1)
        assert profile.visa_records[0].expiry_date == date(2035, 1, 1)
        assert profile.visa_records[0].issue_date == date(2025, 1, 1)
        assert profile.endpoint_arn == endpoint_arn


class TestItineraryParsing:
    """Itinerary segments are parsed with ``is_layover`` defaulting to False."""

    def test_is_layover_defaults_to_false(self):
        seg = _segment()
        del seg["is_layover"]

        itinerary = _parse_itinerary({"confirmation_number": "DL-X", "segments": [seg]})

        assert itinerary.segments[0].is_layover is False

    def test_segment_dates_parsed(self):
        itinerary = _parse_itinerary(
            {"confirmation_number": "DL-X", "segments": [_segment()]}
        )

        assert itinerary.confirmation_number == "DL-X"
        assert itinerary.segments[0].departure_date == date(2030, 6, 1)
        assert itinerary.segments[0].arrival_date == date(2030, 6, 2)

    def test_layover_destination_with_transit_rule_alerts_in_multi_leg_trip(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa()])
        event["itinerary"]["segments"] = [
            _segment("XD", flight_number="DL1", is_layover=True),
            _segment("XA", flight_number="DL2"),
        ]
        event["requirements_override"]["XD"] = {"transit_visa_required": True}

        response = handler(event, None)

        assert response["body"]["visa_status"] == "CRITICAL"
        items = _stored_items()
        assert items[0]["reasons"] == ["No visa on file for XD"]


class TestAlertDispatch:
    """Each actionable evaluation persists one alert and sends one push."""

    def _both_alerts_event(self, endpoint_arn) -> dict:
        return _base_event(endpoint_arn, passport_expiry="2020-01-01", visa_records=[])

    def test_passport_and_visa_alerts_both_counted(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        response = handler(event, None)

        assert response["body"] == {
            "alerts_sent": 2,
            "passport_status": "CRITICAL",
            "visa_status": "CRITICAL",
        }

    def test_passport_and_visa_alerts_both_persisted(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        handler(event, None)

        by_type = {i["alert_type"]: i for i in _stored_items()}
        assert set(by_type) == {"PASSPORT", "VISA"}
        assert by_type["PASSPORT"]["reasons"] == ["Passport has expired"]
        assert by_type["VISA"]["reasons"] == ["No visa on file for XA"]

    def test_persisted_alert_fields(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        handler(event, None)

        for item in _stored_items():
            assert item["skymiles_number"] == "1234567890"
            assert item["itinerary_ref"] == "DL-COV"
            assert item["status"] == "ACTIVE"
            assert item["severity"] == "CRITICAL"
            datetime.fromisoformat(item["created_at"])

    def test_alert_ids_are_unique(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        handler(event, None)

        ids = [i["alert_id"] for i in _stored_items()]
        assert len(set(ids)) == 2

    def test_alerts_queryable_by_itinerary_ref(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        handler(event, None)

        alerts = alert_store_mod.get_alerts_by_itinerary("DL-COV")
        assert len(alerts) == 2

    def test_push_sent_for_each_alert(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        handler(event, None)

        titles = sorted(n["notification"]["title"] for n in _gcm_notifications(endpoint_arn))
        assert titles == [
            "Delta Concierge — Passport Alert",
            "Delta Concierge — Visa Alert",
        ]

    def test_push_data_carries_type_severity_and_confirmation(self, endpoint_arn):
        event = self._both_alerts_event(endpoint_arn)

        handler(event, None)

        data = {n["data"]["alert_type"]: n["data"] for n in _gcm_notifications(endpoint_arn)}
        assert data["PASSPORT"] == {
            "alert_type": "PASSPORT",
            "severity": "CRITICAL",
            "confirmation_number": "DL-COV",
        }
        assert data["VISA"]["severity"] == "CRITICAL"

    def test_push_body_joins_multiple_reasons(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])
        event["itinerary"]["segments"] = [
            _segment("XA", flight_number="DL1"),
            _segment("XC", flight_number="DL2"),
        ]
        event["requirements_override"]["XC"] = {}

        handler(event, None)

        bodies = [n["notification"]["body"] for n in _gcm_notifications(endpoint_arn)]
        assert bodies == ["No visa on file for XA; No visa on file for XC"]

    def test_info_severity_still_dispatched(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[_visa(expiry_date="2030-06-20")])

        response = handler(event, None)

        assert response["body"]["alerts_sent"] == 1
        assert _stored_items()[0]["severity"] == "INFO"
        assert len(_sent_messages(endpoint_arn)) == 1

    def test_only_visa_alert_sends_single_push(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])

        response = handler(event, None)

        assert response["body"]["alerts_sent"] == 1
        assert response["body"]["passport_status"] == "OK"
        assert [i["alert_type"] for i in _stored_items()] == ["VISA"]

    def test_only_passport_alert_sends_single_push(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_expiry="2020-01-01")

        response = handler(event, None)

        assert response["body"]["alerts_sent"] == 1
        assert response["body"]["visa_status"] == "OK"
        assert [i["alert_type"] for i in _stored_items()] == ["PASSPORT"]


class TestTTL:
    """Alert TTL is 30 days after the latest arrival (or today with no segments)."""

    @staticmethod
    def _epoch(d: date) -> int:
        return int(datetime.combine(d, datetime.min.time()).timestamp())

    def test_ttl_uses_latest_arrival(self):
        itinerary = Itinerary(
            confirmation_number="DL-X",
            segments=[
                FlightSegment("DL1", "JFK", "XA", date(2030, 6, 1), date(2030, 6, 2), False),
                FlightSegment("DL2", "XA", "XB", date(2030, 7, 1), date(2030, 7, 10), False),
                FlightSegment("DL3", "XB", "XC", date(2030, 6, 5), date(2030, 6, 6), False),
            ],
        )

        ttl = _compute_ttl(itinerary)

        assert ttl == self._epoch(date(2030, 8, 9))

    def test_ttl_without_segments_uses_today(self):
        itinerary = Itinerary(confirmation_number="DL-X", segments=[])

        ttl = _compute_ttl(itinerary)

        assert ttl == self._epoch(date.today() + timedelta(days=30))

    def test_persisted_alert_has_ttl(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])

        handler(event, None)

        assert int(_stored_items()[0]["ttl"]) == self._epoch(date(2030, 7, 2))

    def test_empty_itinerary_passport_missing_alert_uses_today_ttl(self, endpoint_arn):
        event = _base_event(endpoint_arn, passport_number=None)
        event["itinerary"]["segments"] = []

        response = handler(event, None)

        assert response["body"]["passport_status"] == "CRITICAL"
        assert int(_stored_items()[0]["ttl"]) == self._epoch(date.today() + timedelta(days=30))


class TestEvaluateItinerary:
    """``evaluate_itinerary`` is a thin wrapper returning the handler response."""

    def test_matches_handler_response(self, endpoint_arn):
        event = _base_event(endpoint_arn, visa_records=[])

        response = evaluate_itinerary(event)

        assert response == {
            "statusCode": 200,
            "body": {"alerts_sent": 1, "passport_status": "OK", "visa_status": "CRITICAL"},
        }

    def test_clean_event_is_ok(self, endpoint_arn):
        event = _base_event(endpoint_arn)

        response = evaluate_itinerary(event)

        assert response["body"]["alerts_sent"] == 0
