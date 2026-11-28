"""Tests for group itinerary evaluation logic."""

from datetime import date

import src.data.country_requirements as country_requirements_mod
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)
from tests.conftest import make_segment


DEPARTURE = date(2031, 6, 1)
ARRIVAL = date(2031, 6, 2)
FAR_FUTURE_EXPIRY = date(2040, 1, 1)


def _requirements() -> dict[str, TravelDocRequirements]:
    """Made-up countries so tests stay isolated from real COUNTRY_REQUIREMENTS."""
    return {
        "XA": TravelDocRequirements(
            country_code="XA",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        ),
        "XB": TravelDocRequirements(
            country_code="XB",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "XV": TravelDocRequirements(
            country_code="XV",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        ),
        "XT": TravelDocRequirements(
            country_code="XT",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        ),
    }


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    nationality: str = "IN",
    passport_number: str | None = "P1000001",
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


def _segment(destination: str = "XA", **overrides):
    overrides.setdefault("departure", DEPARTURE)
    overrides.setdefault("arrival", ARRIVAL)
    return make_segment(destination=destination, **overrides)


def _visa(country_code: str, expiry_date: date) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="tourist",
        issue_date=date(2029, 1, 1),
        expiry_date=expiry_date,
        visa_number=f"V-{country_code}",
    )


def _group(
    travelers: list[SkyMilesProfile] | None = None,
    segments=None,
    confirmation_number: str = "DL-GROUP",
) -> GroupItinerary:
    travelers = [_traveler()] if travelers is None else travelers
    return GroupItinerary(
        confirmation_number=confirmation_number,
        segments=[_segment()] if segments is None else segments,
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


class TestCleanGroup:
    """A group with valid documents produces per-traveler summaries and no group severity."""

    def test_single_clean_traveler_has_no_group_severity(self):
        group = _group()

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_summary_copies_traveler_identity_and_confirmation(self):
        traveler = _traveler(skymiles_number="2000002", first_name="Grace", last_name="Hopper")
        group = _group(travelers=[traveler], confirmation_number="DL-ABC123")

        result = evaluate_group_itinerary(group, _requirements())

        assert result.confirmation_number == "DL-ABC123"
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "2000002"
        assert summary.first_name == "Grace"
        assert summary.last_name == "Hopper"
        assert summary.passport_result.profile is traveler
        assert summary.visa_result.profile is traveler

    def test_summaries_preserve_traveler_order(self):
        travelers = [_traveler(skymiles_number=str(n)) for n in ("111", "222", "333")]
        group = _group(travelers=travelers)

        result = evaluate_group_itinerary(group, _requirements())

        assert [s.skymiles_number for s in result.traveler_summaries] == ["111", "222", "333"]

    def test_all_segments_are_evaluated_for_each_traveler(self):
        segments = [_segment("XA", flight_number="DL1"), _segment("XB", flight_number="DL2")]
        group = _group(travelers=[_traveler("1"), _traveler("2")], segments=segments)

        result = evaluate_group_itinerary(group, _requirements())

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments

    def test_empty_group_has_no_summaries(self):
        group = _group(travelers=[])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across every traveler's passport and visa results."""

    def test_single_critical_traveler_makes_group_critical(self):
        clean = _traveler("1")
        missing_passport = _traveler("2", passport_number=None)
        group = _group(travelers=[clean, missing_passport])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity is None
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL

    def test_info_only_group_is_info(self):
        expiring_soon = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[expiring_soon], segments=[_segment("XA")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_overrides_earlier_info(self):
        info_traveler = _traveler("1", nationality="US", passport_expiry=date(2031, 10, 1))
        warning_traveler = _traveler("2", visa_records=[_visa("XV", DEPARTURE)])
        group = _group(travelers=[info_traveler, warning_traveler], segments=[_segment("XV")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_later_lower_severity_does_not_downgrade_group(self):
        critical_traveler = _traveler("1", passport_expiry=date(2020, 1, 1))
        info_traveler = _traveler("2", passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[critical_traveler, info_traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_severity_counts_toward_group(self):
        no_visa = _traveler(visa_records=[])
        group = _group(travelers=[no_visa], segments=[_segment("XV")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].passport_result.severity is None
        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_info_traveler_after_clean_traveler_sets_group_info(self):
        group = _group(travelers=[_traveler("1"), _traveler("2", passport_expiry=date(2031, 10, 1))])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.INFO


class TestPassportValidityAgainstCountryRules:
    """Each traveler's passport is checked against the destination country's validity window."""

    def test_passport_meeting_3_month_rule_but_under_6_months_is_info(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[traveler], segments=[_segment("XA")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO

    def test_same_passport_fails_6_month_rule_as_critical(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[traveler], segments=[_segment("XB")])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("6-month validity requirement" in r for r in passport.reasons)

    def test_passport_below_3_month_rule_is_critical(self):
        traveler = _traveler(passport_expiry=date(2031, 7, 15))
        group = _group(travelers=[traveler], segments=[_segment("XA")])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("3-month validity requirement" in r for r in passport.reasons)

    def test_passport_expiring_before_departure_is_critical(self):
        traveler = _traveler(passport_expiry=date(2031, 5, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("expires before departure" in r for r in passport.reasons)


class TestVisaRulesPerTraveler:
    """Visa evaluation is applied per traveler, honoring exemptions, validity and layover rules."""

    def test_exempt_nationality_needs_no_visa(self):
        exempt = _traveler("1", nationality="US")
        non_exempt = _traveler("2", nationality="IN")
        group = _group(travelers=[exempt, non_exempt], segments=[_segment("XV")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL

    def test_valid_visa_produces_no_alert(self):
        traveler = _traveler(visa_records=[_visa("XV", date(2035, 1, 1))])
        group = _group(travelers=[traveler], segments=[_segment("XV")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_expired_visa_is_critical(self):
        traveler = _traveler(visa_records=[_visa("XV", date(2020, 1, 1))])
        group = _group(travelers=[traveler], segments=[_segment("XV")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL


class TestLayovers:
    """Layover segments only trigger visa alerts when the country requires a transit visa."""

    def test_layover_without_transit_requirement_is_skipped(self):
        group = _group(segments=[_segment("XV", is_layover=True)])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_requiring_transit_visa_is_critical(self):
        group = _group(segments=[_segment("XT", is_layover=True)])

        result = evaluate_group_itinerary(group, _requirements())

        visa = result.traveler_summaries[0].visa_result
        assert visa.severity == AlertSeverity.CRITICAL
        assert "No visa on file for XT" in visa.reasons

    def test_layover_passport_validity_still_checked(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(
            travelers=[traveler],
            segments=[_segment("XB", is_layover=True, flight_number="DL1"), _segment("XA", flight_number="DL2")],
        )

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL


class TestItineraryChange:
    """Re-evaluating a group after its itinerary changes reflects the new segments."""

    def test_rerouting_to_stricter_country_escalates_severity(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        original = _group(travelers=[traveler], segments=[_segment("XA")])
        changed = _group(travelers=[traveler], segments=[_segment("XB")])

        before = evaluate_group_itinerary(original, _requirements())
        after = evaluate_group_itinerary(changed, _requirements())

        assert before.group_severity == AlertSeverity.INFO
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_moving_departure_later_clears_alert(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        changed = _group(
            travelers=[traveler],
            segments=[_segment("XA", departure=date(2030, 1, 1), arrival=date(2030, 1, 2))],
        )

        result = evaluate_group_itinerary(changed, _requirements())

        assert result.group_severity is None

    def test_adding_visa_country_segment_adds_visa_alert(self):
        traveler = _traveler()
        changed = _group(
            travelers=[traveler],
            segments=[_segment("XA", flight_number="DL1"), _segment("XV", flight_number="DL2")],
        )

        result = evaluate_group_itinerary(changed, _requirements())

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for XV"]


class TestDefaultRequirementsLookup:
    """With requirements=None, requirements are built from default country data per unique destination."""

    def _with_country_data(self, data, fn):
        original = country_requirements_mod.COUNTRY_REQUIREMENTS
        country_requirements_mod.COUNTRY_REQUIREMENTS = data
        try:
            return fn()
        finally:
            country_requirements_mod.COUNTRY_REQUIREMENTS = original

    def test_default_data_used_when_requirements_none(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(travelers=[traveler], segments=[_segment("XB")])

        result = self._with_country_data(_requirements(), lambda: evaluate_group_itinerary(group))

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_repeated_destination_is_handled(self):
        traveler = _traveler(passport_expiry=date(2031, 10, 1))
        group = _group(
            travelers=[traveler],
            segments=[_segment("XA", flight_number="DL1"), _segment("XA", flight_number="DL2")],
        )

        result = self._with_country_data(_requirements(), lambda: evaluate_group_itinerary(group))

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.INFO
        assert len(passport.reasons) == 2

    def test_unknown_destination_falls_back_to_default_requirements(self):
        traveler = _traveler(visa_records=[])
        group = _group(travelers=[traveler], segments=[_segment("ZZ")])

        result = self._with_country_data({}, lambda: evaluate_group_itinerary(group))

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for ZZ"]
        assert result.group_severity == AlertSeverity.CRITICAL
