"""Tests for group itinerary evaluation."""

from datetime import date

import pytest

import src.data.country_requirements as country_requirements_mod
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)


DEPARTURE = date(2031, 6, 1)
ARRIVAL = date(2031, 6, 2)


def _segment(
    destination: str = "DE",
    departure: date = DEPARTURE,
    arrival: date = ARRIVAL,
    is_layover: bool = False,
    flight_number: str = "DL100",
) -> FlightSegment:
    return FlightSegment(
        flight_number=flight_number,
        origin="ATL",
        destination=destination,
        departure_date=departure,
        arrival_date=arrival,
        is_layover=is_layover,
    )


def _visa(
    country_code: str = "DE",
    expiry: date = date(2035, 1, 1),
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2024, 1, 1),
        expiry_date=expiry,
        visa_number=f"V-{country_code}-{expiry.isoformat()}",
    )


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Ana",
    last_name: str = "Lopez",
    nationality: str = "US",
    passport_number: str | None = "P1000001",
    passport_expiry: date | None = date(2040, 1, 1),
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
        endpoint_arn=f"arn:aws:sns:us-east-1:000:endpoint/{skymiles_number}",
    )


def _group(
    segments: list[FlightSegment] | None = None,
    travelers: list[SkyMilesProfile] | None = None,
    confirmation_number: str = "DL-GRP1",
) -> GroupItinerary:
    travelers = travelers if travelers is not None else [_traveler()]
    return GroupItinerary(
        confirmation_number=confirmation_number,
        segments=segments if segments is not None else [_segment()],
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


@pytest.fixture
def group_requirements() -> dict[str, TravelDocRequirements]:
    """Fixture requirements isolated from production country data."""
    return {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "MX": TravelDocRequirements(
            country_code="MX",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=0,
            visa_exempt_nationalities=[],
        ),
    }


@pytest.fixture
def fake_country_data():
    """Swap the default country data so the requirements=None path is deterministic."""
    fake = {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
    }
    original = country_requirements_mod.COUNTRY_REQUIREMENTS
    country_requirements_mod.COUNTRY_REQUIREMENTS = fake
    try:
        yield fake
    finally:
        country_requirements_mod.COUNTRY_REQUIREMENTS = original


class TestCleanGroup:
    """A group whose travelers all hold valid documents has no group severity."""

    def test_single_compliant_traveler(self, group_requirements):
        group = _group()

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.confirmation_number == "DL-GRP1"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "1000001"
        assert summary.first_name == "Ana"
        assert summary.last_name == "Lopez"
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_multiple_compliant_travelers(self, group_requirements):
        travelers = [
            _traveler(skymiles_number="1000001", nationality="US"),
            _traveler(skymiles_number="1000002", nationality="GB"),
            _traveler(skymiles_number="1000003", nationality="IN", visa_records=[_visa("DE")]),
        ]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None
        assert [s.skymiles_number for s in result.traveler_summaries] == [
            "1000001", "1000002", "1000003",
        ]

    def test_empty_traveler_list(self, group_requirements):
        group = _group(travelers=[])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None

    def test_destination_without_visa_requirement(self, group_requirements):
        group = _group(
            segments=[_segment(destination="MX")],
            travelers=[_traveler(nationality="IN")],
        )

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None


class TestPerTravelerResults:
    """Each traveler is evaluated independently against the shared segments."""

    def test_only_non_compliant_traveler_flagged(self, group_requirements):
        ok = _traveler(skymiles_number="1000001", nationality="US")
        needs_visa = _traveler(skymiles_number="1000002", nationality="IN")
        group = _group(travelers=[ok, needs_visa])

        result = evaluate_group_itinerary(group, group_requirements)

        ok_summary, bad_summary = result.traveler_summaries
        assert ok_summary.visa_result.is_alert_required is False
        assert bad_summary.visa_result.is_alert_required is True
        assert "No visa on file for DE" in bad_summary.visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_results_reference_correct_profile(self, group_requirements):
        travelers = [
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002", passport_expiry=date(2020, 1, 1)),
        ]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        for traveler, summary in zip(travelers, result.traveler_summaries):
            assert summary.passport_result.profile is traveler
            assert summary.visa_result.profile is traveler

    def test_shared_segments_evaluated_for_every_traveler(self, group_requirements):
        segments = [_segment("DE", flight_number="DL100"), _segment("MX", flight_number="DL200")]
        group = _group(
            segments=segments,
            travelers=[_traveler(skymiles_number="1000001"), _traveler(skymiles_number="1000002")],
        )

        result = evaluate_group_itinerary(group, group_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers and evaluations."""

    def test_info_only(self, group_requirements):
        # 4 months of validity: meets DE's 3-month rule but within 6-month nudge
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_from_visa_expiring_on_travel_date(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", expiry=DEPARTURE)])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_warning_outranks_info_across_travelers(self, group_requirements):
        info = _traveler(skymiles_number="1000001", passport_expiry=date(2031, 10, 1))
        warning = _traveler(
            skymiles_number="1000002", nationality="IN",
            visa_records=[_visa("DE", expiry=DEPARTURE)],
        )
        group = _group(travelers=[info, warning])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_outranks_earlier_warning(self, group_requirements):
        warning = _traveler(
            skymiles_number="1000001", nationality="IN",
            visa_records=[_visa("DE", expiry=DEPARTURE)],
        )
        critical = _traveler(skymiles_number="1000002", passport_number=None)
        group = _group(travelers=[warning, critical])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_lower_severity_after_critical_does_not_downgrade(self, group_requirements):
        critical = _traveler(skymiles_number="1000001", passport_expiry=date(2020, 1, 1))
        info = _traveler(skymiles_number="1000002", passport_expiry=date(2031, 10, 1))
        clean = _traveler(skymiles_number="1000003")
        group = _group(travelers=[critical, info, clean])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_severity_higher_than_passport_for_same_traveler(self, group_requirements):
        traveler = _traveler(nationality="IN", passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_clean_traveler_after_info_keeps_info(self, group_requirements):
        info = _traveler(skymiles_number="1000001", passport_expiry=date(2031, 10, 1))
        clean = _traveler(skymiles_number="1000002")
        group = _group(travelers=[info, clean])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.INFO


class TestPassportLimitsWithinCountryRequirements:
    """Passport validity is checked against each destination's month requirement."""

    def test_below_three_month_schengen_requirement(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 7, 15))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("3-month validity requirement" in r for r in passport.reasons)

    def test_meets_three_month_but_fails_six_month_destination(self, group_requirements):
        traveler = _traveler(
            passport_expiry=date(2031, 10, 1),
            visa_records=[_visa("CN")],
        )
        group = _group(
            segments=[_segment("DE", flight_number="DL100"), _segment("CN", flight_number="DL200")],
            travelers=[traveler],
        )

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert any("CN's 6-month validity requirement" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_expires_before_departure(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 5, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert any("expires before departure" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expired_passport(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2020, 1, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_missing_passport_expiry(self, group_requirements):
        traveler = _traveler(passport_expiry=None)
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.reasons == ["Passport information is incomplete"]
        assert result.group_severity == AlertSeverity.CRITICAL


class TestVisaLimitsWithinCountryRequirements:
    """Visa checks honor exemptions, expiry and the warning window per traveler."""

    def test_visa_expired(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", expiry=date(2020, 1, 1))])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert "Visa for DE has expired" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expires_before_travel(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", expiry=date(2031, 5, 1))])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert (
            "Visa for DE expires before date of travel"
            in result.traveler_summaries[0].visa_result.reasons
        )
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_within_warning_window(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", expiry=date(2031, 6, 15))])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.INFO

    def test_latest_visa_selected_when_multiple_on_file(self, group_requirements):
        traveler = _traveler(
            nationality="IN",
            visa_records=[_visa("DE", expiry=date(2020, 1, 1)), _visa("DE", expiry=date(2035, 1, 1))],
        )
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None

    def test_mixed_nationalities_only_non_exempt_flagged(self, group_requirements):
        travelers = [
            _traveler(skymiles_number="1000001", nationality="US"),
            _traveler(skymiles_number="1000002", nationality="GB"),
            _traveler(skymiles_number="1000003", nationality="CN"),
        ]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        flagged = [s.skymiles_number for s in result.traveler_summaries if s.visa_result.is_alert_required]
        assert flagged == ["1000003"]


class TestLayovers:
    """Layover segments only trigger visa alerts when a transit visa is required."""

    def test_layover_without_transit_requirement_skipped(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN")])
        group = _group(
            segments=[_segment("DE", is_layover=True, flight_number="DL100"),
                      _segment("CN", flight_number="DL200")],
            travelers=[traveler],
        )

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_with_transit_requirement_flags_missing_visa(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        group = _group(
            segments=[_segment("CN", is_layover=True, flight_number="DL100"),
                      _segment("DE", flight_number="DL200")],
            travelers=[traveler],
        )

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_still_checks_passport_validity(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 10, 1), visa_records=[_visa("CN")])
        group = _group(
            segments=[_segment("CN", is_layover=True, flight_number="DL100"),
                      _segment("DE", flight_number="DL200")],
            travelers=[traveler],
        )

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert any("CN's 6-month validity requirement for DL100" in r for r in passport.reasons)


class TestItineraryChanges:
    """Re-evaluating a changed group itinerary reflects the new segments."""

    def test_rerouting_to_visa_country_introduces_alert(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        original = _group(segments=[_segment("DE")], travelers=[traveler])
        changed = _group(segments=[_segment("DE"), _segment("CN", flight_number="DL300")],
                         travelers=[traveler])

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in after.traveler_summaries[0].visa_result.reasons

    def test_moving_departure_later_breaks_passport_validity(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 12, 15))
        original = _group(segments=[_segment("DE", departure=date(2031, 3, 1),
                                             arrival=date(2031, 3, 2))],
                          travelers=[traveler])
        changed = _group(segments=[_segment("DE", departure=date(2031, 11, 1),
                                            arrival=date(2031, 11, 2))],
                         travelers=[traveler])

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_new_departure_on_visa_expiry_downgrades_to_warning(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", expiry=date(2031, 8, 1))])
        original = _group(segments=[_segment("DE", departure=date(2031, 9, 1),
                                             arrival=date(2031, 9, 2))],
                          travelers=[traveler])
        changed = _group(segments=[_segment("DE", departure=date(2031, 8, 1),
                                            arrival=date(2031, 8, 2))],
                         travelers=[traveler])

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity == AlertSeverity.CRITICAL
        assert after.group_severity == AlertSeverity.WARNING


class TestDefaultRequirementsLookup:
    """When requirements is None, defaults are looked up per segment destination."""

    def test_uses_country_data_for_destination(self, fake_country_data):
        traveler = _traveler(nationality="IN")
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for DE"]

    def test_exemption_from_country_data_applies(self, fake_country_data):
        group = _group(travelers=[_traveler(nationality="US")])

        result = evaluate_group_itinerary(group, None)

        assert result.group_severity is None

    def test_repeated_destination_and_layover(self, fake_country_data):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        group = _group(
            segments=[
                _segment("CN", is_layover=True, flight_number="DL100"),
                _segment("DE", flight_number="DL200"),
                _segment("CN", is_layover=True, flight_number="DL300"),
            ],
            travelers=[traveler],
        )

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for CN",
            "No visa on file for CN",
        ]

    def test_unknown_destination_falls_back_to_default(self, fake_country_data):
        traveler = _traveler(nationality="US", passport_expiry=date(2031, 10, 1))
        group = _group(segments=[_segment("ZZ")], travelers=[traveler])

        result = evaluate_group_itinerary(group)

        summary = result.traveler_summaries[0]
        assert summary.visa_result.reasons == ["No visa on file for ZZ"]
        assert any("ZZ's 6-month validity requirement" in r for r in summary.passport_result.reasons)

    def test_empty_requirements_dict_is_not_replaced(self, fake_country_data):
        fake_country_data["DE"] = TravelDocRequirements(
            country_code="DE",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        )
        group = _group(travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group, {})

        assert result.group_severity is None
