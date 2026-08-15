"""Tests for the group itinerary Lambda handler end-to-end flow."""

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
            Name="TestGroupApp", Platform="GCM",
            Attributes={"PlatformCredential": "fake"},
        )["PlatformApplicationArn"]
        endpoint_arn = sns.create_platform_endpoint(
            PlatformApplicationArn=app_arn, Token="fake-group-token",
        )["EndpointArn"]

        # Save original module-level boto3 clients
        orig_dynamodb = alert_store_mod.dynamodb
        orig_table = alert_store_mod.table
        orig_sns_client = notif_mod.sns_client

        # Patch module-level boto3 clients to use the moto context
        alert_store_mod.dynamodb = ddb
        alert_store_mod.table = ddb.Table("ConciergeAlerts")
        notif_mod.sns_client = sns

        try:
            yield endpoint_arn
        finally:
            # Restore original clients so other test modules aren't affected
            alert_store_mod.dynamodb = orig_dynamodb
            alert_store_mod.table = orig_table
            notif_mod.sns_client = orig_sns_client


@pytest.fixture
def endpoint_arn(_mock_aws):
    """Expose the moto SNS endpoint ARN created by _mock_aws."""
    return _mock_aws


def _traveler(
    endpoint_arn: str,
    skymiles_number: str = "1000000001",
    first_name: str = "Ada",
    last_name: str = "Traveler",
    nationality: str = "IN",
    passport_number: str | None = "P999999",
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
    destination: str = "CN",
    departure_date: str = "2030-06-01",
    arrival_date: str = "2030-06-02",
    flight_number: str = "DL100",
    origin: str = "ATL",
    is_layover: bool | None = False,
) -> dict:
    """Build a flight segment payload dict with sensible defaults."""
    segment = {
        "flight_number": flight_number,
        "origin": origin,
        "destination": destination,
        "departure_date": departure_date,
        "arrival_date": arrival_date,
    }
    if is_layover is not None:
        segment["is_layover"] = is_layover
    return segment


def _base_event(endpoint_arn: str, **overrides) -> dict:
    """Build a valid group Lambda event with optional top-level overrides."""
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1000000001",
        "travelers": [_traveler(endpoint_arn)],
        "segments": [_segment()],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _visa(
    country_code: str = "CN",
    expiry_date: str = "2035-01-01",
    issue_date: str = "2020-01-01",
    visa_type: str = "TOURIST",
    visa_number: str = "V123",
) -> dict:
    """Build a visa record payload dict with sensible defaults."""
    return {
        "country_code": country_code,
        "visa_type": visa_type,
        "issue_date": issue_date,
        "expiry_date": expiry_date,
        "visa_number": visa_number,
    }


def _stored_alerts(alert_type: str | None = None) -> list[dict]:
    """Return alert items currently persisted in the mocked DynamoDB table."""
    items = alert_store_mod.table.scan()["Items"]
    if alert_type is None:
        return items
    return [i for i in items if i["alert_type"] == alert_type]


class TestCleanGroup:
    """A group with fully compliant documents produces no alerts."""

    def test_no_alerts_for_clean_travelers(self, endpoint_arn):
        """US nationals to DE with far-future passports need no action."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, nationality="US"),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Grace",
                    nationality="US",
                ),
            ],
            segments=[_segment(destination="DE")],
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["total_alerts_sent"] == 0
        assert body["group_severity"] == "OK"
        assert [r["passport_status"] for r in body["traveler_results"]] == ["OK", "OK"]
        assert [r["visa_status"] for r in body["traveler_results"]] == ["OK", "OK"]
        assert _stored_alerts() == []

    def test_traveler_results_include_identity_and_counts(self, endpoint_arn):
        """Each traveler result carries name, SkyMiles number and alert count."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="US")],
            segments=[_segment(destination="DE")],
        )

        result = group_handler(event, None)["body"]["traveler_results"][0]

        assert result == {
            "skymiles_number": "1000000001",
            "first_name": "Ada",
            "last_name": "Traveler",
            "passport_status": "OK",
            "visa_status": "OK",
            "alerts_sent": 0,
        }


class TestPassportAlerts:
    """Passport severity rules surface per traveler and are persisted."""

    def test_expired_passport_is_critical(self, endpoint_arn):
        """A passport that expired in the past is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, nationality="US", passport_expiry="2020-01-01")
            ],
            segments=[_segment(destination="DE")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["group_severity"] == "CRITICAL"
        alerts = _stored_alerts("PASSPORT")
        assert len(alerts) == 1
        assert "Passport has expired" in alerts[0]["reasons"]

    def test_missing_passport_fields_is_critical(self, endpoint_arn):
        """Null passport number and expiry are CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    nationality="US",
                    passport_number=None,
                    passport_expiry=None,
                )
            ],
            segments=[_segment(destination="DE")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert "Passport information is incomplete" in _stored_alerts("PASSPORT")[0]["reasons"]

    def test_passport_expiring_before_departure_is_critical(self, endpoint_arn):
        """A passport expiring before the segment departure is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, nationality="US", passport_expiry="2030-05-01")
            ],
            segments=[_segment(destination="DE")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"

    def test_below_six_month_country_minimum_is_critical(self, endpoint_arn):
        """CN requires 6 months of validity at travel time."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    passport_expiry="2030-09-01",
                    visa_records=[_visa()],
                )
            ],
            segments=[_segment(destination="CN")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_schengen_three_month_minimum_met_yields_info(self, endpoint_arn):
        """DE only requires 3 months, so a 3-month passport is advisory INFO."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, nationality="US", passport_expiry="2030-09-01")
            ],
            segments=[_segment(destination="DE")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["passport_status"] == "INFO"
        assert body["group_severity"] == "INFO"
        assert len(_stored_alerts("PASSPORT")) == 1


class TestVisaAlerts:
    """Visa severity rules surface per traveler and are persisted."""

    def test_missing_visa_is_critical(self, endpoint_arn):
        """No visa on file for a visa-requiring destination is CRITICAL."""
        event = _base_event(endpoint_arn)

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert "No visa on file for CN" in _stored_alerts("VISA")[0]["reasons"]

    def test_expired_visa_is_critical(self, endpoint_arn):
        """A visa whose expiry is already in the past is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2020-01-01")])
            ],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert "Visa for CN has expired" in _stored_alerts("VISA")[0]["reasons"]

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        """A visa expiring before departure is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2030-05-01")])
            ],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        """A visa expiring exactly on the departure date is WARNING."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2030-06-01")])
            ],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "WARNING"
        assert body["group_severity"] == "WARNING"

    def test_visa_expiring_within_warning_window_is_info(self, endpoint_arn):
        """A visa expiring within 30 days of travel is INFO."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2030-06-20")])
            ],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "INFO"

    def test_latest_expiry_visa_record_wins(self, endpoint_arn):
        """With several CN visas, the one expiring latest is evaluated."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    visa_records=[
                        _visa(expiry_date="2020-01-01", visa_number="V-OLD"),
                        _visa(expiry_date="2035-01-01", visa_number="V-NEW"),
                    ],
                )
            ],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["total_alerts_sent"] == 0

    def test_visa_exempt_nationality_skips_check(self, endpoint_arn):
        """A visa-exempt nationality needs no visa for the destination."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="US")],
            segments=[_segment(destination="JP")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_destination_not_requiring_visa_skips_check(self, endpoint_arn):
        """CL does not require a visa at all."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CL")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "OK"


class TestLayoverSegments:
    """Layovers only need a visa when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        """A DE layover needs no transit visa, so no alert is raised."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE", is_layover=True)],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["total_alerts_sent"] == 0

    def test_layover_requiring_transit_visa_is_critical(self, endpoint_arn):
        """A CN layover requires a transit visa, so a missing visa is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="CN", is_layover=True),
                _segment(
                    destination="JP",
                    flight_number="DL200",
                    departure_date="2030-06-02",
                    arrival_date="2030-06-03",
                ),
            ],
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(country_code="JP")])
            ],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert "No visa on file for CN" in _stored_alerts("VISA")[0]["reasons"]


class TestRequirementsOverride:
    """Per-request requirement overrides replace the default country rules."""

    def test_override_can_waive_visa_requirement(self, endpoint_arn):
        """Setting requires_visa=False for CN suppresses the visa alert."""
        event = _base_event(
            endpoint_arn,
            requirements_override={"CN": {"requires_visa": False}},
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "OK"
        assert body["total_alerts_sent"] == 0

    def test_override_can_exempt_nationality(self, endpoint_arn):
        """Adding the traveler's nationality to the exempt list skips the visa."""
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "CN": {"visa_exempt_nationalities": ["IN"]},
            },
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_override_can_tighten_passport_validity(self, endpoint_arn):
        """A large passport_validity_months override makes a valid passport CRITICAL."""
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "DE": {
                    "passport_validity_months": 120,
                    "visa_exempt_nationalities": ["IN"],
                },
            },
            segments=[_segment(destination="DE")],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["passport_status"] == "CRITICAL"
        assert body["traveler_results"][0]["visa_status"] == "OK"

    def test_override_transit_visa_requirement_on_layover(self, endpoint_arn):
        """Overriding transit_visa_required makes a layover require a visa."""
        event = _base_event(
            endpoint_arn,
            requirements_override={
                "DE": {"transit_visa_required": True},
            },
            segments=[_segment(destination="DE", is_layover=True)],
        )

        body = group_handler(event, None)["body"]

        assert body["traveler_results"][0]["visa_status"] == "CRITICAL"


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across all travelers."""

    def test_group_severity_is_highest_across_travelers(self, endpoint_arn):
        """One CRITICAL traveler escalates the whole group to CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    visa_records=[_visa(expiry_date="2030-06-20")],
                ),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Grace",
                ),
            ],
        )

        body = group_handler(event, None)["body"]

        statuses = {r["skymiles_number"]: r["visa_status"] for r in body["traveler_results"]}
        assert statuses["1000000001"] == "INFO"
        assert statuses["1000000002"] == "CRITICAL"
        assert body["group_severity"] == "CRITICAL"

    def test_alerts_counted_per_traveler_and_in_total(self, endpoint_arn):
        """A traveler with both passport and visa issues sends two alerts."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, passport_expiry="2020-01-01"),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    first_name="Grace",
                    visa_records=[_visa()],
                ),
            ],
        )

        body = group_handler(event, None)["body"]

        per_traveler = {r["skymiles_number"]: r["alerts_sent"] for r in body["traveler_results"]}
        assert per_traveler == {"1000000001": 2, "1000000002": 0}
        assert body["total_alerts_sent"] == 2
        assert len(_stored_alerts()) == 2


class TestPersistedAlertFields:
    """Persisted alert records carry group metadata and a TTL."""

    def test_alert_record_references_group_confirmation_and_ttl(self, endpoint_arn):
        """Alerts reference the group confirmation number and expire after arrival."""
        event = _base_event(endpoint_arn, confirmation_number="DL-XYZ99")

        group_handler(event, None)

        alert = _stored_alerts("VISA")[0]
        assert alert["itinerary_ref"] == "DL-XYZ99"
        assert alert["skymiles_number"] == "1000000001"
        assert alert["status"] == "ACTIVE"
        assert alert["severity"] == "CRITICAL"
        assert int(alert["ttl"]) > 0

    def test_ttl_uses_latest_arrival_across_segments(self, endpoint_arn):
        """TTL is computed from the last arrival date of the itinerary."""
        early_event = _base_event(endpoint_arn)
        late_event = _base_event(
            endpoint_arn,
            segments=[
                _segment(),
                _segment(
                    destination="CN",
                    flight_number="DL300",
                    departure_date="2030-07-01",
                    arrival_date="2030-07-02",
                ),
            ],
        )

        group_handler(early_event, None)
        early_ttl = int(_stored_alerts("VISA")[0]["ttl"])
        alert_store_mod.table.delete_item(
            Key={
                "skymiles_number": _stored_alerts("VISA")[0]["skymiles_number"],
                "alert_id": _stored_alerts("VISA")[0]["alert_id"],
            }
        )
        group_handler(late_event, None)
        late_ttl = int(_stored_alerts("VISA")[0]["ttl"])

        assert late_ttl > early_ttl


class TestPrimaryTraveler:
    """The group summary notification targets the primary traveler."""

    def test_unknown_primary_traveler_still_returns_success(self, endpoint_arn):
        """An unmatched primary_traveler skips the group notification."""
        event = _base_event(endpoint_arn, primary_traveler="0000000000")

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["traveler_results"][0]["visa_status"] == "CRITICAL"

    def test_find_traveler_matches_by_skymiles_number(self, endpoint_arn):
        """_find_traveler returns the matching profile, or None when absent."""
        group = _parse_group_itinerary(
            _base_event(
                endpoint_arn,
                travelers=[
                    _traveler(endpoint_arn),
                    _traveler(endpoint_arn, skymiles_number="1000000002"),
                ],
            )
        )

        assert _find_traveler(group, "1000000002").skymiles_number == "1000000002"
        assert _find_traveler(group, "nope") is None


class TestParseGroupItinerary:
    """Event deserialization produces typed group itinerary objects."""

    def test_segments_and_travelers_are_deserialized(self, endpoint_arn):
        """Dates become date objects and travelers become profiles."""
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        segment = group.segments[0]
        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1000000001"
        assert segment.departure_date.isoformat() == "2030-06-01"
        assert segment.arrival_date.isoformat() == "2030-06-02"
        assert group.travelers[0].passport_expiry.isoformat() == "2035-01-01"

    def test_is_layover_defaults_to_false_when_absent(self, endpoint_arn):
        """A segment without is_layover is treated as a normal segment."""
        event = _base_event(endpoint_arn, segments=[_segment(is_layover=None)])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new destinations."""

    def test_rerouting_to_exempt_destination_clears_visa_alert(self, endpoint_arn):
        """Switching CN to a visa-exempt JP routing removes the visa alert."""
        traveler = _traveler(endpoint_arn, nationality="US")
        original = _base_event(endpoint_arn, travelers=[traveler])

        first = group_handler(original, None)["body"]
        changed = _base_event(
            endpoint_arn,
            travelers=[traveler],
            segments=[_segment(destination="JP", flight_number="DL400")],
        )
        second = group_handler(changed, None)["body"]

        assert first["traveler_results"][0]["visa_status"] == "CRITICAL"
        assert second["traveler_results"][0]["visa_status"] == "OK"
        assert second["total_alerts_sent"] == 0
        assert len(_stored_alerts("VISA")) == 1

    def test_added_segment_can_introduce_new_alert(self, endpoint_arn):
        """Adding a CN segment to a clean DE itinerary introduces a visa alert."""
        traveler = _traveler(endpoint_arn, nationality="US")
        clean = _base_event(
            endpoint_arn,
            travelers=[traveler],
            segments=[_segment(destination="DE")],
        )
        extended = _base_event(
            endpoint_arn,
            travelers=[traveler],
            segments=[
                _segment(destination="DE"),
                _segment(
                    destination="CN",
                    flight_number="DL500",
                    departure_date="2030-06-05",
                    arrival_date="2030-06-06",
                ),
            ],
        )

        assert group_handler(clean, None)["body"]["total_alerts_sent"] == 0
        assert group_handler(extended, None)["body"]["total_alerts_sent"] == 1
