"""Critical-path tests for the group itinerary Lambda handler end-to-end flow."""

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
    first_name: str = "Alpha",
    last_name: str = "Traveler",
    nationality: str = "IN",
    passport_number: str | None = "P100001",
    passport_expiry: str | None = "2036-01-01",
    visa_records: list[dict] | None = None,
) -> dict:
    """Build a traveler payload dict with sensible, valid defaults."""
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
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
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
    expiry_date: str = "2035-01-01",
    issue_date: str = "2025-01-01",
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
    """Build a valid group Lambda event with optional top-level overrides.

    Defaults: two travelers flying to CN. The first has no CN visa (CRITICAL),
    the second holds a far-future CN visa (clean).
    """
    event = {
        "confirmation_number": "DL-GROUP",
        "primary_traveler": "1000000001",
        "segments": [_segment()],
        "travelers": [
            _traveler(endpoint_arn),
            _traveler(
                endpoint_arn,
                skymiles_number="1000000002",
                first_name="Beta",
                passport_number="P100002",
                visa_records=[_visa()],
            ),
        ],
        "requirements_override": None,
    }
    event.update(overrides)
    return event


def _alerts_by_type(alert_type: str) -> list[dict]:
    """Return persisted DynamoDB alert records of the given alert type."""
    items = alert_store_mod.table.scan()["Items"]
    return [i for i in items if i["alert_type"] == alert_type]


def _result_for(response: dict, skymiles_number: str) -> dict:
    """Pull one traveler's result dict out of a handler response."""
    return next(
        r
        for r in response["body"]["traveler_results"]
        if r["skymiles_number"] == skymiles_number
    )


class TestGroupHappyPath:
    """Full round-trip: group event → evaluations → DynamoDB + SNS → response."""

    def test_multiple_travelers_response_shape(self, endpoint_arn):
        """Per-traveler results, counts and group severity are reported."""
        event = _base_event(endpoint_arn)

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["confirmation_number"] == "DL-GROUP"
        assert body["group_severity"] == "CRITICAL"
        assert body["total_alerts_sent"] == 1
        assert len(body["traveler_results"]) == 2

        first = _result_for(response, "1000000001")
        assert first["first_name"] == "Alpha"
        assert first["last_name"] == "Traveler"
        assert first["passport_status"] == "OK"
        assert first["visa_status"] == "CRITICAL"
        assert first["alerts_sent"] == 1

        second = _result_for(response, "1000000002")
        assert second["passport_status"] == "OK"
        assert second["visa_status"] == "OK"
        assert second["alerts_sent"] == 0

    def test_visa_alert_persisted_to_dynamodb(self, endpoint_arn):
        """The missing-visa alert is stored with reasons, status and TTL."""
        event = _base_event(endpoint_arn)

        group_handler(event, None)

        visa_alerts = _alerts_by_type("VISA")
        assert len(visa_alerts) == 1
        record = visa_alerts[0]
        assert record["skymiles_number"] == "1000000001"
        assert record["severity"] == "CRITICAL"
        assert record["status"] == "ACTIVE"
        assert record["itinerary_ref"] == "DL-GROUP"
        assert "No visa on file for CN" in record["reasons"]
        assert int(record["ttl"]) > 0

    def test_clean_group_sends_no_alerts(self, endpoint_arn):
        """Visa-exempt travelers with far-future passports produce no alerts."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE")],
            travelers=[
                _traveler(endpoint_arn, nationality="US"),
                _traveler(
                    endpoint_arn,
                    skymiles_number="1000000002",
                    nationality="US",
                    passport_number="P100002",
                ),
            ],
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"
        assert alert_store_mod.table.scan()["Items"] == []

    def test_single_traveler_group(self, endpoint_arn):
        """A group of one is evaluated and summarized like any other group."""
        event = _base_event(endpoint_arn, travelers=[_traveler(endpoint_arn)])

        response = group_handler(event, None)

        assert len(response["body"]["traveler_results"]) == 1
        assert response["body"]["total_alerts_sent"] == 1


class TestPassportBranches:
    """Passport evaluation outcomes surfaced by the group handler."""

    def test_expired_passport_is_critical(self, endpoint_arn):
        """A past passport expiry yields a CRITICAL passport alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    passport_expiry="2020-01-01",
                    visa_records=[_visa()],
                )
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["passport_status"] == "CRITICAL"
        passport_alerts = _alerts_by_type("PASSPORT")
        assert len(passport_alerts) == 1
        assert "Passport has expired" in passport_alerts[0]["reasons"]

    def test_missing_passport_fields_are_critical(self, endpoint_arn):
        """Null passport number and expiry yield a CRITICAL passport alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    passport_number=None,
                    passport_expiry=None,
                    visa_records=[_visa()],
                )
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["passport_status"] == "CRITICAL"
        assert "Passport information is incomplete" in _alerts_by_type("PASSPORT")[0]["reasons"]

    def test_passport_below_destination_validity_window(self, endpoint_arn):
        """Passport with under CN's 6-month validity margin is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(
                    endpoint_arn,
                    passport_expiry="2030-08-01",
                    visa_records=[_visa()],
                )
            ],
        )

        response = group_handler(event, None)

        result = _result_for(response, "1000000001")
        assert result["passport_status"] == "CRITICAL"
        assert result["alerts_sent"] == 1
        assert "6-month" in _alerts_by_type("PASSPORT")[0]["reasons"][0]

    def test_passport_expiring_within_six_months_is_info(self, endpoint_arn):
        """Passport meeting DE's 3-month rule but under 6 months is advisory only."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE")],
            travelers=[
                _traveler(
                    endpoint_arn,
                    nationality="US",
                    passport_expiry="2030-10-01",
                )
            ],
        )

        response = group_handler(event, None)

        result = _result_for(response, "1000000001")
        assert result["passport_status"] == "INFO"
        assert result["visa_status"] == "OK"
        assert response["body"]["group_severity"] == "INFO"

    def test_passport_and_visa_alerts_both_persisted(self, endpoint_arn):
        """A traveler failing both checks gets two alerts and two notifications."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, passport_expiry="2020-01-01")],
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 2
        assert _result_for(response, "1000000001")["alerts_sent"] == 2
        assert len(_alerts_by_type("PASSPORT")) == 1
        assert len(_alerts_by_type("VISA")) == 1


class TestVisaBranches:
    """Visa evaluation outcomes surfaced by the group handler."""

    def test_expired_visa_is_critical(self, endpoint_arn):
        """A visa whose expiry is already in the past is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2020-01-01")])
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"
        assert "Visa for CN has expired" in _alerts_by_type("VISA")[0]["reasons"]

    def test_visa_expiring_before_departure_is_critical(self, endpoint_arn):
        """A still-valid visa that lapses before departure is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2029-01-01")])
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"
        assert (
            "Visa for CN expires before date of travel"
            in _alerts_by_type("VISA")[0]["reasons"]
        )

    def test_visa_expiring_on_departure_is_warning(self, endpoint_arn):
        """A visa expiring exactly on the departure date is a WARNING."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2030-06-01")])
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "WARNING"
        assert response["body"]["group_severity"] == "WARNING"

    def test_visa_expiring_within_warning_window_is_info(self, endpoint_arn):
        """A visa expiring shortly after departure is advisory only."""
        event = _base_event(
            endpoint_arn,
            travelers=[
                _traveler(endpoint_arn, visa_records=[_visa(expiry_date="2030-06-20")])
            ],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "INFO"

    def test_latest_expiring_visa_wins(self, endpoint_arn):
        """With several CN visas on file the latest expiry is used."""
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

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "OK"
        assert response["body"]["total_alerts_sent"] == 0


class TestLayoverHandling:
    """Layover segments only require documents when transit rules say so."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, endpoint_arn):
        """A DE layover needs no transit visa, so no alert is raised."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="DE", is_layover=True)],
            travelers=[_traveler(endpoint_arn)],
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert _result_for(response, "1000000001")["visa_status"] == "OK"

    def test_layover_requiring_transit_visa_alerts(self, endpoint_arn):
        """A CN layover requires a transit visa, so a missing visa is CRITICAL."""
        event = _base_event(
            endpoint_arn,
            segments=[_segment(destination="CN", is_layover=True)],
            travelers=[_traveler(endpoint_arn)],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"

    def test_multi_segment_itinerary_consolidates_severity(self, endpoint_arn):
        """Layover plus final destination produce a single consolidated alert."""
        event = _base_event(
            endpoint_arn,
            segments=[
                _segment(destination="DE", is_layover=True, flight_number="DL10"),
                _segment(
                    destination="CN",
                    departure_date="2030-06-03",
                    arrival_date="2030-06-04",
                    flight_number="DL11",
                ),
            ],
            travelers=[_traveler(endpoint_arn)],
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "CRITICAL"
        assert len(_alerts_by_type("VISA")) == 1


class TestRequirementsOverride:
    """The event may override the default country requirements."""

    def test_override_removing_visa_requirement_clears_alert(self, endpoint_arn):
        """requires_visa=False for CN suppresses the missing-visa alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn)],
            requirements_override={
                "CN": {
                    "requires_visa": False,
                    "transit_visa_required": False,
                    "passport_validity_months": 6,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert response["body"]["group_severity"] == "OK"

    def test_override_exempt_nationality_clears_alert(self, endpoint_arn):
        """Adding the traveler's nationality to the exempt list clears the alert."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn)],
            requirements_override={"CN": {"visa_exempt_nationalities": ["IN"]}},
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["visa_status"] == "OK"

    def test_override_tightens_passport_validity_window(self, endpoint_arn):
        """A longer required validity window makes a valid passport CRITICAL."""
        event = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, visa_records=[_visa()])],
            requirements_override={
                "CN": {
                    "country_code": "CN",
                    "requires_visa": True,
                    "passport_validity_months": 120,
                    "visa_exempt_nationalities": [],
                }
            },
        )

        response = group_handler(event, None)

        assert _result_for(response, "1000000001")["passport_status"] == "CRITICAL"
        assert "120-month" in _alerts_by_type("PASSPORT")[0]["reasons"][0]


class TestItineraryChange:
    """Re-evaluating a changed itinerary refreshes alerts and TTLs."""

    def test_changed_dates_extend_alert_ttl(self, endpoint_arn):
        """Later arrival dates yield a larger TTL on the re-evaluated alert."""
        event = _base_event(endpoint_arn, travelers=[_traveler(endpoint_arn)])
        group_handler(event, None)
        original_ttl = int(_alerts_by_type("VISA")[0]["ttl"])

        changed = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn)],
            segments=[_segment(departure_date="2031-06-01", arrival_date="2031-06-02")],
        )
        group_handler(changed, None)

        ttls = sorted(int(a["ttl"]) for a in _alerts_by_type("VISA"))
        assert len(ttls) == 2
        assert ttls[0] == original_ttl
        assert ttls[1] > original_ttl

    def test_changed_destination_resolves_alert_condition(self, endpoint_arn):
        """Rerouting to a visa-exempt destination produces no new alerts."""
        event = _base_event(endpoint_arn, travelers=[_traveler(endpoint_arn)])
        group_handler(event, None)

        rerouted = _base_event(
            endpoint_arn,
            travelers=[_traveler(endpoint_arn, nationality="US")],
            segments=[_segment(destination="DE")],
        )
        response = group_handler(rerouted, None)

        assert response["body"]["total_alerts_sent"] == 0
        assert len(_alerts_by_type("VISA")) == 1


class TestPrimaryTravelerSummary:
    """The group summary notification targets the primary traveler."""

    def test_unknown_primary_traveler_skips_group_notification(self, endpoint_arn):
        """An unmatched primary_traveler still returns a normal response."""
        event = _base_event(
            endpoint_arn,
            primary_traveler="9999999999",
            travelers=[_traveler(endpoint_arn)],
        )

        response = group_handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"]["total_alerts_sent"] == 1

    def test_find_traveler_lookup(self, endpoint_arn):
        """_find_traveler matches on SkyMiles number and returns None otherwise."""
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert _find_traveler(group, "1000000002").first_name == "Beta"
        assert _find_traveler(group, "0000000000") is None


class TestInvalidInput:
    """Malformed events fail loudly rather than silently mis-evaluating."""

    def test_missing_travelers_key_raises(self, endpoint_arn):
        """An event without travelers cannot be parsed."""
        event = _base_event(endpoint_arn)
        del event["travelers"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_segments_key_raises(self, endpoint_arn):
        """An event without segments cannot be parsed."""
        event = _base_event(endpoint_arn)
        del event["segments"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_missing_primary_traveler_key_raises(self, endpoint_arn):
        """An event without primary_traveler cannot be parsed."""
        event = _base_event(endpoint_arn)
        del event["primary_traveler"]

        with pytest.raises(KeyError):
            group_handler(event, None)

    def test_invalid_departure_date_raises(self, endpoint_arn):
        """A non-ISO departure date is rejected during parsing."""
        event = _base_event(endpoint_arn, segments=[_segment(departure_date="06/01/2030")])

        with pytest.raises(ValueError):
            group_handler(event, None)

    def test_missing_traveler_field_raises(self, endpoint_arn):
        """A traveler payload missing a required field is rejected."""
        traveler = _traveler(endpoint_arn)
        del traveler["nationality"]
        event = _base_event(endpoint_arn, travelers=[traveler])

        with pytest.raises(KeyError):
            group_handler(event, None)


class TestParseGroupItinerary:
    """Deserialization of the group itinerary payload."""

    def test_segments_and_travelers_deserialized(self, endpoint_arn):
        """Dates become date objects and defaults fill optional fields."""
        group = _parse_group_itinerary(_base_event(endpoint_arn))

        assert group.confirmation_number == "DL-GROUP"
        assert group.primary_traveler == "1000000001"
        assert len(group.travelers) == 2
        assert group.segments[0].departure_date.isoformat() == "2030-06-01"
        assert group.segments[0].is_layover is False

    def test_layover_flag_defaults_to_false(self, endpoint_arn):
        """An omitted is_layover flag defaults to False."""
        segment = _segment()
        del segment["is_layover"]
        event = _base_event(endpoint_arn, segments=[segment])

        group = _parse_group_itinerary(event)

        assert group.segments[0].is_layover is False
