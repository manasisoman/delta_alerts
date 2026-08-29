"""Critical-path tests for the group itinerary Lambda handler end-to-end flow."""

from datetime import date, datetime, timedelta

import boto3
import moto
import pytest

import src.services.alert_store as alert_store_mod
import src.services.notification_service as notif_mod
from src.handlers.group_lambda_handler import group_handler


class _RecordingSnsClient:
    """Delegating SNS client that records every publish call."""

    def __init__(self, client):
        self._client = client
        self.publishes: list[dict] = []

    def publish(self, **kwargs):
        self.publishes.append(kwargs)
        return self._client.publish(**kwargs)


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and set up DynamoDB table + SNS endpoint for every test."""
    with moto.mock_aws():
        # DynamoDB
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

        # SNS
        sns = boto3.client("sns", region_name="us-east-1")
        app_arn = sns.create_platform_application(
            Name="TestApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-token",
        )["EndpointArn"]
        recording_sns = _RecordingSnsClient(sns)

        # Save original module-level boto3 clients
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        # Patch module-level boto3 clients to use the moto context
        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = recording_sns

        try:
            yield endpoint_arn, recording_sns
        finally:
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws[0]


@pytest.fixture
def sns_spy(_mock_aws):
    """Expose the recording SNS client patched in by _mock_aws."""
    return _mock_aws[1]


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = "1000000001",
    first_name: str = "Ann",
    last_name: str = "Traveler",
    nationality: str = "US",
    passport_number: str | None = "P100001",
    passport_expiry: str | None = "2035-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler payload dict with sensible defaults."""
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
    departure_date: str = "2030-06-15",
    arrival_date: str = "2030-06-16",
    flight_number: str = "DL100",
    origin: str = "ATL",
    is_layover: bool = False,
) -> dict:
    """Build a flight segment payload dict with sensible defaults."""
    return {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
        "is_layover": is_layover,
    }


def _visa(
    country_code: str = "CN",
    expiry_date: str = "2032-01-01",
    issue_date: str = "2029-01-01",
    visa_type: str = "TOURIST",
    visa_number: str = "V100001",
) -> dict:
    """Build a visa record payload dict with sensible defaults."""
    return {
        "country_code": country_code,
        "visa_type": visa_type,
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": visa_number,
    }


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1000000001",
        "segments": [_segment()],
        "travelers": [_traveler(endpoint_arn)],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


class TestGroupHandlerHappyPath:
    """A group of compliant travelers produces no alerts but still gets a summary."""

    def test_clean_group_returns_ok(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Bob",
                    passport_number="P100002",
                ),
            ],
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert [r["passport_status"] for r in body["traveler_results"]] == ["OK", "OK"]
        assert [r["visa_status"] for r in body["traveler_results"]] == ["OK", "OK"]
        assert [r["alerts_sent"] for r in body["traveler_results"]] == [0, 0]
        assert alert_store_mod.table.scan()["Items"] == []

    def test_clean_group_still_sends_group_summary(self, endpoint_arn, sns_spy):
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        assert len(sns_spy.publishes) == 1
        assert "All 1 travelers are cleared for travel." in sns_spy.publishes[0]["Message"]

    def test_traveler_names_are_echoed_in_results(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, first_name="Jane", last_name="Doe")],
        )

        response = group_handler(event, None)

        result = response["body"]["traveler_results"][0]
        assert result["skymiles_number"] == "1000000001"
        assert result["first_name"] == "Jane"
        assert result["last_name"] == "Doe"


class TestMixedSeverities:
    """Group severity is the highest severity found across all travelers."""

    def test_one_traveler_missing_visa_escalates_group_to_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa()]),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Bob",
                    passport_number="P100002",
                ),
            ],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
        first, second = body["traveler_results"]
        assert first["visa_status"] == "OK"
        assert first["alerts_sent"] == 0
        assert second["visa_status"] == "CRITICAL"
        assert second["alerts_sent"] == 1

    def test_info_only_group_severity(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2030-11-01")],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["group_severity"] == "INFO"
        assert body["traveler_results"][0]["passport_status"] == "INFO"
        assert body["total_alerts_sent"] == 1

    def test_passport_and_visa_alerts_counted_per_traveler(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(endpoint_arn, nationality="IN", passport_expiry="2020-01-01"),
            ],
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert body["traveler_results"][0]["alerts_sent"] == 2
        assert body["total_alerts_sent"] == 2

    def test_missing_passport_information_is_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, passport_number=None, passport_expiry=None),
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["passport_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert [i["reasons"] for i in items] == [["Passport information is incomplete"]]


class TestAlertPersistence:
    """Every actionable alert is written to DynamoDB with the shared itinerary TTL."""

    def test_alert_record_fields(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(items) == 1
        item = items[0]
        assert item["skymiles_number"] == "1000000001"
        assert item["alert_type"] == "VISA"
        assert item["severity"] == "CRITICAL"
        assert item["status"] == "ACTIVE"
        assert item["itinerary_ref"] == "DL-GROUP"
        assert item["reasons"] == ["No visa on file for CN"]
        expected_ttl = int(
            (
                datetime.combine(date(2030, 6, 16), datetime.min.time())
                + timedelta(days=30)
            ).timestamp()
        )
        assert int(item["ttl"]) == expected_ttl

    def test_ttl_uses_latest_arrival_across_segments(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="CN"),
                _segment(
                    destination="CN",
                    flight_number="DL200",
                    departure_date="2030-07-01",
                    arrival_date="2030-07-05",
                ),
            ],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )

        group_handler(event, None)

        item = alert_store_mod.table.scan()["Items"][0]
        expected_ttl = int(
            (
                datetime.combine(date(2030, 7, 5), datetime.min.time())
                + timedelta(days=30)
            ).timestamp()
        )
        assert int(item["ttl"]) == expected_ttl

    def test_alerts_persisted_for_each_traveler(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(endpoint_arn, nationality="IN"),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Bob",
                    nationality="IN",
                    passport_number="P100002",
                ),
            ],
        )

        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert sorted(i["skymiles_number"] for i in items) == [
            "1000000001",
            "1000000002",
        ]

    def test_reevaluating_changed_itinerary_adds_new_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE")],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )
        group_handler(event, None)
        first_pass = alert_store_mod.table.scan()["Items"]

        event["segments"] = [_segment(destination="CN")]
        group_handler(event, None)

        items = alert_store_mod.table.scan()["Items"]
        assert len(first_pass) == 1
        assert len(items) == 2
        assert sorted(i["reasons"][0] for i in items) == [
            "No visa on file for CN",
            "No visa on file for DE",
        ]


class TestNotificationFanOut:
    """Each traveler alert publishes its own push, plus one group summary."""

    def test_per_traveler_and_group_notifications(self, endpoint_arn, sns_spy):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(endpoint_arn, nationality="IN", passport_expiry="2020-01-01"),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Bob",
                    nationality="IN",
                    passport_number="P100002",
                    visa_records=[_visa()],
                ),
            ],
        )

        group_handler(event, None)

        # 2 alerts for the first traveler + 1 missing-visa alert absent for the
        # second (valid visa) + 1 group summary
        assert len(sns_spy.publishes) == 3
        bodies = [p["Message"] for p in sns_spy.publishes]
        assert any("Passport Alert" in b for b in bodies)
        assert any("Visa Alert" in b for b in bodies)
        assert any("1 of 2 travelers need action" in b for b in bodies)

    def test_group_summary_targets_primary_traveler(self, endpoint_arn, sns_spy):
        event = _base_event(
            endpoint_arn,
            primary_traveler="1000000002",
            travelers=[
                _traveler(endpoint_arn),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Bob",
                    passport_number="P100002",
                ),
            ],
        )

        group_handler(event, None)

        assert len(sns_spy.publishes) == 1
        assert sns_spy.publishes[0]["TargetArn"] == endpoint_arn

    def test_unknown_primary_traveler_skips_group_summary(self, endpoint_arn, sns_spy):
        event = _base_event(endpoint_arn, primary_traveler="9999999999")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert sns_spy.publishes == []


class TestRequirementsOverride:
    """Per-request requirement overrides replace the default country data."""

    def test_override_removes_visa_exemption(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "DE": {
                    "requires_visa": True,
                    "passport_validity_months": 3,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        body = response["body"]
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert body["group_severity"] == "CRITICAL"

    def test_override_waiving_visa_clears_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
            requirements_override={
                "CN": {"requires_visa": False, "passport_validity_months": 6}
            },
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_override_defaults_applied_for_sparse_payload(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="ZZ")],
            travelers=[_traveler(endpoint_arn, passport_expiry="2030-08-01")],
            requirements_override={"ZZ": {}},
        )

        response = group_handler(event, None)

        body = response["body"]
        # Defaults: requires_visa=True with no exemptions and 6-month validity
        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"


class TestLayoverSegments:
    """Layovers only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE", is_layover=True)],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_layover_requiring_transit_visa_alerts(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", is_layover=True)],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert response["body"]["total_alerts_sent"] == 1

    def test_layover_defaults_to_false_when_absent(self, endpoint_arn):
        segment = _segment(destination="CN")
        del segment["is_layover"]
        event = _base_event(
            endpoint_arn,
            segments=[segment],
            travelers=[_traveler(endpoint_arn, nationality="IN")],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"


class TestVisaSelection:
    """When several visas match a destination, the latest expiry wins."""

    def test_valid_visa_preferred_over_expired_duplicate(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(
                    endpoint_arn,
                    nationality="IN",
                    visa_records=[
                        _visa(expiry_date="2020-01-01", visa_number="V-OLD"),
                        _visa(expiry_date="2032-01-01", visa_number="V-NEW"),
                    ],
                )
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN")],
            travelers=[
                _traveler(
                    endpoint_arn,
                    nationality="IN",
                    visa_records=[_visa(expiry_date="2030-01-01")],
                )
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"
        items = alert_store_mod.table.scan()["Items"]
        assert items[0]["reasons"] == ["Visa for CN expires before date of travel"]


class TestInvalidPayloads:
    """Malformed events surface as errors rather than silent success."""

    def test_missing_segments_key_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["segments"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_travelers_key_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["travelers"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_primary_traveler_key_raises(self, endpoint_arn):
        event = _base_event(endpoint_arn)
        del event["primary_traveler"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_traveler_field_raises(self, endpoint_arn):
        traveler = _traveler(endpoint_arn)
        del traveler["nationality"]
        event = _base_event(endpoint_arn, travelers=[traveler])

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_malformed_departure_date_raises(self, endpoint_arn):
        event = _base_event(
            endpoint_arn, segments=[_segment(departure_date="15-06-2030")]
        )

        with pytest.raises(ValueError):
            group_handler(event, None)
