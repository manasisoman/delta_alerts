"""Critical-path tests for the group itinerary evaluator."""

from datetime import date

import pytest

import src.data.country_requirements as country_requirements
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
SAFE_EXPIRY = date(2035, 1, 1)


def _segment(
    destination: str = "XA",
    departure: date = DEPARTURE,
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


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    nationality: str = "US",
    passport_number: str | None = "P100001",
    passport_expiry: date | None = SAFE_EXPIRY,
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


def _visa(country_code: str = "XC", expiry_date: date = SAFE_EXPIRY) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry_date,
        visa_number="V-" + country_code,
    )


def _group(
    segments: list[FlightSegment] | None = None,
    travelers: list[SkyMilesProfile] | None = None,
    confirmation_number: str = "DLGRP1",
) -> GroupItinerary:
    travelers = [_traveler()] if travelers is None else travelers
    return GroupItinerary(
        confirmation_number=confirmation_number,
        segments=[_segment()] if segments is None else segments,
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


@pytest.fixture
def requirements() -> dict[str, TravelDocRequirements]:
    """Made-up country codes so tests are isolated from production data."""
    return {
        # Schengen-like: 3-month validity, visa required unless exempt
        "XA": TravelDocRequirements(
            country_code="XA",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB"],
        ),
        # No visa required, 6-month validity
        "XB": TravelDocRequirements(
            country_code="XB",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        # Visa required for everyone, including transit
        "XC": TravelDocRequirements(
            country_code="XC",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
    }


@pytest.fixture
def fake_country_requirements(requirements):
    """Swap the default country data used when ``requirements`` is None."""
    original = country_requirements.COUNTRY_REQUIREMENTS
    country_requirements.COUNTRY_REQUIREMENTS = dict(requirements)
    try:
        yield country_requirements.COUNTRY_REQUIREMENTS
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestHappyPath:
    """Fully compliant travelers produce clean summaries and no group severity."""

    def test_single_compliant_traveler_has_no_alerts(self, requirements):
        group = _group()

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_confirmation_number_is_propagated(self, requirements):
        group = _group(confirmation_number="DLABC9")

        result = evaluate_group_itinerary(group, requirements)

        assert result.confirmation_number == "DLABC9"

    def test_multiple_compliant_travelers_all_clean(self, requirements):
        travelers = [
            _traveler(skymiles_number="1"),
            _traveler(skymiles_number="2", nationality="GB"),
        ]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity is None
        assert all(not s.passport_result.is_alert_required for s in result.traveler_summaries)
        assert all(not s.visa_result.is_alert_required for s in result.traveler_summaries)

    def test_empty_traveler_list_returns_empty_result(self, requirements):
        group = _group(travelers=[])

        result = evaluate_group_itinerary(group, requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestTravelerSummaries:
    """Each traveler gets a summary carrying their identity and own results."""

    def test_summary_carries_traveler_identity(self, requirements):
        traveler = _traveler(skymiles_number="7654321", first_name="Grace", last_name="Hopper")
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "7654321"
        assert summary.first_name == "Grace"
        assert summary.last_name == "Hopper"
        assert summary.passport_result.profile is traveler
        assert summary.visa_result.profile is traveler

    def test_summaries_preserve_traveler_order(self, requirements):
        travelers = [_traveler(skymiles_number=n) for n in ("3", "1", "2")]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == ["3", "1", "2"]

    def test_all_group_segments_are_evaluated_for_each_traveler(self, requirements):
        segments = [
            _segment("XB", flight_number="DL1", is_layover=True),
            _segment("XA", flight_number="DL2"),
        ]
        group = _group(segments=segments, travelers=[_traveler("1"), _traveler("2")])

        result = evaluate_group_itinerary(group, requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments

    def test_results_are_isolated_per_traveler(self, requirements):
        compliant = _traveler(skymiles_number="1", nationality="US")
        needs_visa = _traveler(skymiles_number="2", nationality="IN")
        group = _group(travelers=[compliant, needs_visa])

        result = evaluate_group_itinerary(group, requirements)

        first, second = result.traveler_summaries
        assert first.visa_result.is_alert_required is False
        assert second.visa_result.is_alert_required is True
        assert "No visa on file for XA" in second.visa_result.reasons


class TestPassportIssuesInGroup:
    """Passport problems on any traveler surface at the group level."""

    def test_missing_passport_is_critical(self, requirements):
        traveler = _traveler(passport_number=None)
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "Passport information is incomplete" in result.traveler_summaries[0].passport_result.reasons

    def test_expired_passport_is_critical(self, requirements):
        traveler = _traveler(passport_expiry=date(2020, 1, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "Passport has expired" in result.traveler_summaries[0].passport_result.reasons

    def test_passport_below_country_minimum_is_critical(self, requirements):
        traveler = _traveler(passport_expiry=date(2031, 7, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "3-month validity requirement" in r
            for r in result.traveler_summaries[0].passport_result.reasons
        )

    def test_passport_expiring_within_six_months_is_info(self, requirements):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO

    def test_one_traveler_with_bad_passport_flags_whole_group(self, requirements):
        travelers = [
            _traveler(skymiles_number="1"),
            _traveler(skymiles_number="2", passport_expiry=date(2020, 1, 1)),
        ]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[1].passport_result.is_alert_required is True


class TestVisaIssuesInGroup:
    """Visa problems on any traveler surface at the group level."""

    def test_missing_visa_is_critical(self, requirements):
        group = _group(segments=[_segment("XC")])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for XC" in result.traveler_summaries[0].visa_result.reasons

    def test_visa_expiring_on_departure_is_warning(self, requirements):
        traveler = _traveler(visa_records=[_visa("XC", expiry_date=DEPARTURE)])
        group = _group(segments=[_segment("XC")], travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.WARNING
        assert "Visa for XC expires on date of travel" in result.traveler_summaries[0].visa_result.reasons

    def test_visa_expiring_within_warning_window_is_info(self, requirements):
        traveler = _traveler(visa_records=[_visa("XC", expiry_date=date(2031, 6, 15))])
        group = _group(segments=[_segment("XC")], travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_valid_visa_produces_no_alert(self, requirements):
        traveler = _traveler(visa_records=[_visa("XC")])
        group = _group(segments=[_segment("XC")], travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity is None

    def test_visa_exempt_nationality_differs_from_non_exempt(self, requirements):
        travelers = [
            _traveler(skymiles_number="1", nationality="GB"),
            _traveler(skymiles_number="2", nationality="IN"),
        ]
        group = _group(segments=[_segment("XA")], travelers=travelers)

        result = evaluate_group_itinerary(group, requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL


class TestLayovers:
    """Layover segments are evaluated for every traveler in the group."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, requirements):
        traveler = _traveler(nationality="IN")
        segments = [_segment("XA", is_layover=True), _segment("XB", flight_number="DL2")]
        group = _group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity is None

    def test_layover_requiring_transit_visa_flags_group(self, requirements):
        traveler = _traveler()
        segments = [_segment("XC", is_layover=True), _segment("XB", flight_number="DL2")]
        group = _group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for XC" in result.traveler_summaries[0].visa_result.reasons

    def test_layover_passport_validity_is_checked(self, requirements):
        traveler = _traveler(passport_expiry=date(2031, 8, 1))
        segments = [_segment("XB", is_layover=True), _segment("XA", flight_number="DL2")]
        group = _group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        reasons = result.traveler_summaries[0].passport_result.reasons
        assert any("XB's 6-month validity requirement" in r for r in reasons)
        assert result.group_severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers and checks."""

    def test_critical_beats_warning_across_travelers(self, requirements):
        warning = _traveler(skymiles_number="1", visa_records=[_visa("XC", expiry_date=DEPARTURE)])
        critical = _traveler(skymiles_number="2")
        group = _group(segments=[_segment("XC")], travelers=[warning, critical])

        result = evaluate_group_itinerary(group, requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.WARNING
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_lower_severity_after_critical_does_not_downgrade(self, requirements):
        critical = _traveler(skymiles_number="1")
        info = _traveler(skymiles_number="2", visa_records=[_visa("XC", expiry_date=date(2031, 6, 15))])
        group = _group(segments=[_segment("XC")], travelers=[critical, info])

        result = evaluate_group_itinerary(group, requirements)

        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_beats_info_across_travelers(self, requirements):
        info = _traveler(skymiles_number="1", visa_records=[_visa("XC", expiry_date=date(2031, 6, 15))])
        warning = _traveler(skymiles_number="2", visa_records=[_visa("XC", expiry_date=DEPARTURE)])
        group = _group(segments=[_segment("XC")], travelers=[info, warning])

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_visa_severity_beats_passport_severity_for_same_traveler(self, requirements):
        traveler = _traveler(
            nationality="IN",
            passport_expiry=date(2031, 10, 1),
            visa_records=[_visa("XA", expiry_date=DEPARTURE)],
        )
        group = _group(segments=[_segment("XA")], travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_passport_severity_beats_visa_severity_for_same_traveler(self, requirements):
        traveler = _traveler(
            passport_expiry=date(2020, 1, 1),
            visa_records=[_visa("XC", expiry_date=date(2031, 6, 15))],
        )
        group = _group(segments=[_segment("XC")], travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL
        assert summary.visa_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_info_only_group_severity_is_info(self, requirements):
        travelers = [
            _traveler(skymiles_number="1", passport_expiry=date(2031, 10, 1)),
            _traveler(skymiles_number="2"),
        ]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, requirements)

        assert result.group_severity == AlertSeverity.INFO


class TestDefaultRequirementsLookup:
    """When requirements is None, defaults are looked up per destination."""

    def test_none_uses_country_requirements_data(self, fake_country_requirements):
        group = _group(segments=[_segment("XC")])

        result = evaluate_group_itinerary(group, None)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for XC" in result.traveler_summaries[0].visa_result.reasons

    def test_none_with_compliant_traveler_is_clean(self, fake_country_requirements):
        group = _group(segments=[_segment("XA"), _segment("XB", flight_number="DL2")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_none_with_repeated_destination(self, fake_country_requirements):
        segments = [
            _segment("XC", flight_number="DL1"),
            _segment("XB", flight_number="DL2", is_layover=True),
            _segment("XC", flight_number="DL3", departure=date(2031, 6, 10)),
        ]
        group = _group(segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for XC",
            "No visa on file for XC",
        ]

    def test_none_with_unknown_country_uses_default_requirements(self, fake_country_requirements):
        group = _group(segments=[_segment("ZZ")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.traveler_summaries[0].visa_result.reasons

    def test_none_with_unknown_country_applies_six_month_default(self, fake_country_requirements):
        traveler = _traveler(passport_expiry=date(2031, 10, 1), visa_records=[_visa("ZZ")])
        group = _group(segments=[_segment("ZZ")], travelers=[traveler])

        result = evaluate_group_itinerary(group)

        reasons = result.traveler_summaries[0].passport_result.reasons
        assert any("ZZ's 6-month validity requirement" in r for r in reasons)

    def test_explicit_requirements_override_default_data(self, fake_country_requirements):
        override = {
            "XC": TravelDocRequirements(
                country_code="XC",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        group = _group(segments=[_segment("XC")])

        result = evaluate_group_itinerary(group, override)

        assert result.group_severity is None


class TestItineraryChanges:
    """Re-evaluating a group after its itinerary changes reflects the new segments."""

    def test_rerouting_to_visa_country_raises_alert(self, requirements):
        group = _group(segments=[_segment("XB")])
        before = evaluate_group_itinerary(group, requirements)

        group.segments = [_segment("XC")]
        after = evaluate_group_itinerary(group, requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_moving_departure_later_triggers_passport_alert(self, requirements):
        traveler = _traveler(passport_expiry=date(2032, 1, 1))
        group = _group(travelers=[traveler])
        before = evaluate_group_itinerary(group, requirements)

        group.segments = [_segment("XA", departure=date(2031, 11, 1))]
        after = evaluate_group_itinerary(group, requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_adding_traveler_to_group_changes_severity(self, requirements):
        group = _group(segments=[_segment("XA")])
        before = evaluate_group_itinerary(group, requirements)

        group.travelers.append(_traveler(skymiles_number="2", nationality="IN"))
        after = evaluate_group_itinerary(group, requirements)

        assert before.group_severity is None
        assert len(after.traveler_summaries) == 2
        assert after.group_severity == AlertSeverity.CRITICAL
