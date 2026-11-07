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


DEPARTURE = date(2030, 6, 1)
ARRIVAL = date(2030, 6, 2)
FAR_FUTURE_EXPIRY = date(2035, 1, 1)


def _requirements(
    country_code: str = "XA",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    passport_validity_months: int = 6,
    visa_exempt_nationalities: list[str] | None = None,
) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=["US"] if visa_exempt_nationalities is None else visa_exempt_nationalities,
    )


def _requirements_map(*reqs: TravelDocRequirements) -> dict[str, TravelDocRequirements]:
    return {r.country_code: r for r in reqs}


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
        arrival_date=ARRIVAL,
        is_layover=is_layover,
    )


def _visa(
    country_code: str = "XA",
    expiry: date = FAR_FUTURE_EXPIRY,
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry,
        visa_number="V001",
    )


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    nationality: str = "US",
    passport_number: str | None = "P123456",
    passport_expiry: date | None = FAR_FUTURE_EXPIRY,
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
    travelers: list[SkyMilesProfile] | None = None,
    segments: list[FlightSegment] | None = None,
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
def swapped_country_requirements():
    """Replace production COUNTRY_REQUIREMENTS with test data, restoring afterwards."""
    original = country_requirements.COUNTRY_REQUIREMENTS
    test_data = _requirements_map(
        _requirements("XA", visa_exempt_nationalities=["US"]),
        _requirements("XB", requires_visa=False, passport_validity_months=3),
    )
    country_requirements.COUNTRY_REQUIREMENTS = test_data
    try:
        yield test_data
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestHappyPath:
    """A fully compliant group yields one clean summary per traveler and no group severity."""

    def test_single_compliant_traveler(self):
        group = _group()

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.confirmation_number == "DLGRP1"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_summary_carries_traveler_identity(self):
        traveler = _traveler(skymiles_number="2000002", first_name="Grace", last_name="Hopper")
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "2000002"
        assert summary.first_name == "Grace"
        assert summary.last_name == "Hopper"
        assert summary.passport_result.profile is traveler
        assert summary.visa_result.profile is traveler

    def test_multiple_compliant_travelers_preserve_order(self):
        travelers = [_traveler(skymiles_number=str(n)) for n in ("1", "2", "3")]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert [s.skymiles_number for s in result.traveler_summaries] == ["1", "2", "3"]
        assert result.group_severity is None

    def test_every_traveler_evaluated_against_shared_segments(self):
        segments = [_segment("XA", flight_number="DL1"), _segment("XB", flight_number="DL2")]
        travelers = [_traveler(skymiles_number="1"), _traveler(skymiles_number="2")]
        group = _group(travelers=travelers, segments=segments)
        reqs = _requirements_map(_requirements("XA"), _requirements("XB"))

        result = evaluate_group_itinerary(group, reqs)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestEmptyGroup:
    """A group with no travelers produces no summaries and no severity."""

    def test_no_travelers(self):
        group = _group(travelers=[])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.traveler_summaries == []
        assert result.group_severity is None
        assert result.confirmation_number == "DLGRP1"


class TestSingleTravelerSeverity:
    """Each individual alert severity propagates to the group severity."""

    def test_missing_visa_is_critical(self):
        traveler = _traveler(nationality="IN")
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for XA" in result.traveler_summaries[0].visa_result.reasons

    def test_visa_expiring_on_departure_is_warning(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa(expiry=DEPARTURE)])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.WARNING

    def test_visa_expiring_soon_after_departure_is_info(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa(expiry=date(2030, 6, 15))])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.INFO

    def test_passport_expiring_within_six_months_is_info(self):
        reqs = _requirements_map(_requirements(passport_validity_months=3))
        traveler = _traveler(passport_expiry=date(2030, 10, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, reqs)

        assert result.group_severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO

    def test_passport_below_country_validity_is_critical(self):
        traveler = _traveler(passport_expiry=date(2030, 9, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_missing_passport_is_critical(self):
        traveler = _traveler(passport_number=None)
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expired_passport_is_critical(self):
        traveler = _traveler(passport_expiry=date(2020, 1, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_and_visa_severities_combined_takes_highest(self):
        reqs = _requirements_map(_requirements(passport_validity_months=3))
        traveler = _traveler(nationality="IN", passport_expiry=date(2030, 10, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, reqs)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers, regardless of order."""

    @staticmethod
    def _info_traveler() -> SkyMilesProfile:
        return _traveler(skymiles_number="I", nationality="IN", visa_records=[_visa(expiry=date(2030, 6, 15))])

    @staticmethod
    def _warning_traveler() -> SkyMilesProfile:
        return _traveler(skymiles_number="W", nationality="IN", visa_records=[_visa(expiry=DEPARTURE)])

    @staticmethod
    def _critical_traveler() -> SkyMilesProfile:
        return _traveler(skymiles_number="C", nationality="IN")

    @pytest.mark.parametrize(
        "order",
        [
            ("info", "warning", "critical"),
            ("critical", "warning", "info"),
            ("warning", "critical", "info"),
        ],
    )
    def test_highest_severity_wins_in_any_order(self, order):
        builders = {
            "info": self._info_traveler,
            "warning": self._warning_traveler,
            "critical": self._critical_traveler,
        }
        group = _group(travelers=[builders[name]() for name in order])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_outranks_info(self):
        group = _group(travelers=[self._warning_traveler(), self._info_traveler()])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.WARNING

    def test_equal_severities_keep_that_severity(self):
        group = _group(travelers=[self._info_traveler(), self._info_traveler()])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity == AlertSeverity.INFO

    def test_one_problem_traveler_does_not_affect_others_summaries(self):
        clean = _traveler(skymiles_number="OK")
        group = _group(travelers=[clean, self._critical_traveler()])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        clean_summary, problem_summary = result.traveler_summaries
        assert clean_summary.visa_result.is_alert_required is False
        assert clean_summary.passport_result.is_alert_required is False
        assert problem_summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL


class TestNationalityAndVisaExemption:
    """Visa exemptions apply per traveler, so mixed-nationality groups get distinct results."""

    def test_mixed_nationalities_only_non_exempt_alerted(self):
        exempt = _traveler(skymiles_number="US1", nationality="US")
        non_exempt = _traveler(skymiles_number="IN1", nationality="IN")
        group = _group(travelers=[exempt, non_exempt])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        exempt_summary, non_exempt_summary = result.traveler_summaries
        assert exempt_summary.visa_result.is_alert_required is False
        assert non_exempt_summary.visa_result.is_alert_required is True
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_non_exempt_traveler_with_valid_visa_is_clean(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa()])
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements_map(_requirements()))

        assert result.group_severity is None


class TestLayovers:
    """Layover segments only require visas where a transit visa is mandated."""

    def test_layover_without_transit_visa_requirement_skipped(self):
        reqs = _requirements_map(
            _requirements("XA", transit_visa_required=False, visa_exempt_nationalities=[]),
            _requirements("XB", requires_visa=False),
        )
        segments = [_segment("XA", is_layover=True, flight_number="DL1"), _segment("XB", flight_number="DL2")]
        group = _group(travelers=[_traveler(nationality="IN")], segments=segments)

        result = evaluate_group_itinerary(group, reqs)

        assert result.group_severity is None

    def test_layover_with_transit_visa_requirement_alerts(self):
        reqs = _requirements_map(
            _requirements("XA", transit_visa_required=True, visa_exempt_nationalities=[]),
            _requirements("XB", requires_visa=False),
        )
        segments = [_segment("XA", is_layover=True, flight_number="DL1"), _segment("XB", flight_number="DL2")]
        group = _group(travelers=[_traveler(nationality="IN")], segments=segments)

        result = evaluate_group_itinerary(group, reqs)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for XA" in result.traveler_summaries[0].visa_result.reasons

    def test_layover_passport_validity_still_checked(self):
        reqs = _requirements_map(
            _requirements("XA", passport_validity_months=6, requires_visa=False),
            _requirements("XB", passport_validity_months=3, requires_visa=False),
        )
        segments = [_segment("XA", is_layover=True, flight_number="DL1"), _segment("XB", flight_number="DL2")]
        group = _group(travelers=[_traveler(passport_expiry=date(2030, 10, 1))], segments=segments)

        result = evaluate_group_itinerary(group, reqs)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("XA" in r and "DL1" in r for r in passport.reasons)


class TestItineraryChange:
    """Re-evaluating a group after its itinerary changes reflects the new segments."""

    def test_change_to_visa_required_destination_raises_alert(self):
        reqs = _requirements_map(
            _requirements("XA", requires_visa=False),
            _requirements("XB", requires_visa=True, visa_exempt_nationalities=[]),
        )
        traveler = _traveler(nationality="IN")
        original = _group(travelers=[traveler], segments=[_segment("XA")])
        changed = _group(travelers=[traveler], segments=[_segment("XB")])

        before = evaluate_group_itinerary(original, reqs)
        after = evaluate_group_itinerary(changed, reqs)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_later_departure_past_visa_expiry_raises_alert(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa(expiry=date(2030, 12, 31))])
        reqs = _requirements_map(_requirements())
        original = _group(travelers=[traveler], segments=[_segment(departure=date(2030, 6, 1))])
        changed = _group(travelers=[traveler], segments=[_segment(departure=date(2031, 2, 1))])

        before = evaluate_group_itinerary(original, reqs)
        after = evaluate_group_itinerary(changed, reqs)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "Visa for XA expires before date of travel" in after.traveler_summaries[0].visa_result.reasons


class TestDefaultRequirementsLookup:
    """When requirements is None, per-destination requirements come from country data."""

    def test_none_requirements_uses_country_data(self, swapped_country_requirements):
        group = _group(travelers=[_traveler(nationality="IN")], segments=[_segment("XA")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for XA" in result.traveler_summaries[0].visa_result.reasons

    def test_none_requirements_respects_country_exemptions(self, swapped_country_requirements):
        group = _group(travelers=[_traveler(nationality="US")], segments=[_segment("XA")])

        result = evaluate_group_itinerary(group, None)

        assert result.group_severity is None

    def test_none_requirements_with_repeated_destination(self, swapped_country_requirements):
        segments = [
            _segment("XB", flight_number="DL1"),
            _segment("XA", flight_number="DL2"),
            _segment("XB", departure=date(2030, 6, 10), flight_number="DL3"),
        ]
        group = _group(travelers=[_traveler(nationality="US")], segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.segments_evaluated == segments

    def test_none_requirements_unknown_country_falls_back_to_default(self, swapped_country_requirements):
        group = _group(travelers=[_traveler(nationality="US")], segments=[_segment("ZZ")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.traveler_summaries[0].visa_result.reasons

    def test_none_requirements_uses_country_passport_validity(self, swapped_country_requirements):
        # XB requires 3 months; 4 months remaining is INFO, not CRITICAL
        group = _group(
            travelers=[_traveler(passport_expiry=date(2030, 10, 1))],
            segments=[_segment("XB")],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.INFO

    def test_explicit_requirements_override_country_data(self, swapped_country_requirements):
        reqs = _requirements_map(_requirements("XA", requires_visa=False))
        group = _group(travelers=[_traveler(nationality="IN")], segments=[_segment("XA")])

        result = evaluate_group_itinerary(group, reqs)

        assert result.group_severity is None

    def test_explicit_requirements_not_mutated(self):
        reqs = _requirements_map(_requirements("XA"))
        group = _group(segments=[_segment("XA"), _segment("XB")])

        evaluate_group_itinerary(group, reqs)

        assert set(reqs) == {"XA"}
