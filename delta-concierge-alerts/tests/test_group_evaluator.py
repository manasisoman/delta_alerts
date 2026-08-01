"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_group, make_profile, make_segment


def _visa(country_code="DE", expiry=date(2030, 1, 1)):
    """Build a VisaRecord with sensible defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2020, 1, 1),
        expiry_date=expiry,
        visa_number="V001",
    )


class TestHappyPath:
    """A group of fully valid travelers triggers no group alert."""

    def test_single_valid_traveler_has_no_severity(self, base_requirements):
        # Visa-exempt US national with far-future passport → clean
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[0].visa_result.is_alert_required is False


class TestResultMetadata:
    """The result mirrors the group's identity and traveler roster."""

    def test_confirmation_number_and_summary_fields(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = make_group(
            traveler,
            segments=[make_segment(destination="DE")],
            confirmation="DL-XYZ",
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-XYZ"
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == traveler.skymiles_number
        assert summary.first_name == traveler.first_name
        assert summary.last_name == traveler.last_name

    def test_empty_group_has_no_summaries_or_severity(self, base_requirements):
        group = make_group(segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestDefaultRequirementsLookup:
    """When requirements is None, they are looked up from the default data."""

    def test_none_requirements_uses_default_data(self):
        # US is visa-exempt for DE in the default dataset → clean
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None

    def test_duplicate_destinations_looked_up_once(self):
        # Two segments to the same country exercise the dedup branch
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        seg_a = make_segment(destination="DE", flight_number="DL100")
        seg_b = make_segment(destination="DE", flight_number="DL200")
        group = make_group(traveler, segments=[seg_a, seg_b])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None


class TestSeverityAggregation:
    """group_severity is the highest severity found across all travelers."""

    def test_critical_from_missing_passport(self, base_requirements):
        traveler = make_profile(passport_number=None, passport_expiry=None)
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_from_visa_expiring_on_departure(self, base_requirements):
        # Passport valid; visa for DE expires exactly on departure → WARNING
        visa = _visa(country_code="DE", expiry=date(2026, 9, 1))
        traveler = make_profile(
            nationality="IN", passport_expiry=date(2030, 1, 1), visa_records=[visa]
        )
        group = make_group(
            traveler, segments=[make_segment(destination="DE", departure=date(2026, 9, 1))]
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_info_from_passport_expiring_within_six_months(self, base_requirements):
        # Meets DE's 3-month min but expires within 6 months of travel → INFO
        traveler = make_profile(nationality="US", passport_expiry=date(2026, 12, 15))
        group = make_group(
            traveler, segments=[make_segment(destination="DE", departure=date(2026, 9, 1))]
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_highest_severity_wins_when_lower_precedes_higher(self, base_requirements):
        # Traveler 1 → INFO, traveler 2 → CRITICAL: CRITICAL must win
        info_traveler = make_profile(nationality="US", passport_expiry=date(2026, 12, 15))
        critical_traveler = make_profile(passport_number=None, passport_expiry=None)
        group = make_group(
            info_traveler,
            critical_traveler,
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert len(result.traveler_summaries) == 2

    def test_higher_severity_not_overridden_by_later_lower(self, base_requirements):
        # Traveler 1 → CRITICAL, traveler 2 → INFO: CRITICAL must persist
        critical_traveler = make_profile(passport_number=None, passport_expiry=None)
        info_traveler = make_profile(nationality="US", passport_expiry=date(2026, 12, 15))
        group = make_group(
            critical_traveler,
            info_traveler,
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_clean_traveler_does_not_raise_severity(self, base_requirements):
        # One clean traveler alongside an INFO traveler → group stays INFO
        clean = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        info_traveler = make_profile(nationality="US", passport_expiry=date(2026, 12, 15))
        group = make_group(
            clean,
            info_traveler,
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO
