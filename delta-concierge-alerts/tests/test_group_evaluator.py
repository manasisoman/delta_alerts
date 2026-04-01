"""Critical-path tests for the group evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment


def _base_group(
    travelers=None,
    segments=None,
    confirmation="GRP-001",
    primary="9999999",
):
    """Build a complete valid GroupItinerary with sensible defaults."""
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments or [make_segment(destination="DE", departure=date(2026, 9, 1))],
        travelers=travelers or [make_profile()],
        primary_traveler=primary,
    )


class TestHappyPath:
    """All travelers have valid documents — no alerts, group severity is None."""

    def test_single_traveler_no_alerts(self, base_requirements):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        group = _base_group(travelers=[profile])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.confirmation_number == "GRP-001"
        assert len(result.traveler_summaries) == 1
        assert result.group_severity is None

    def test_multiple_travelers_all_clean(self, base_requirements):
        traveler_a = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        traveler_b = make_profile(
            nationality="GB",
            passport_expiry=date(2030, 6, 1),
        )
        group = _base_group(travelers=[traveler_a, traveler_b])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert len(result.traveler_summaries) == 2
        assert result.group_severity is None


class TestSeverityEscalation:
    """Group severity reflects the highest severity across all travelers."""

    def test_single_traveler_critical_passport(self, base_requirements):
        profile = make_profile(passport_number=None)
        group = _base_group(travelers=[profile])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_from_passport_validity(self, base_requirements):
        # US national (visa-exempt for DE) with passport expiring 2 months
        # after departure to DE (needs 3) → WARNING only
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2026, 10, 15),
        )
        group = _base_group(travelers=[profile])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_overrides_warning_across_travelers(self, base_requirements):
        # Traveler A: passport below 3-month min for DE → WARNING
        traveler_warning = make_profile(
            nationality="US",
            passport_expiry=date(2026, 10, 15),
        )
        # Traveler B: missing passport number → CRITICAL
        traveler_critical = make_profile(
            nationality="IN",
            passport_number=None,
        )
        group = _base_group(travelers=[traveler_warning, traveler_critical])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_first_traveler_sets_severity_then_second_escalates(self, base_requirements):
        """First traveler produces WARNING, second escalates to CRITICAL."""
        traveler_a = make_profile(
            nationality="IN",
            passport_expiry=date(2026, 10, 15),
        )
        traveler_b = make_profile(
            nationality="IN",
            passport_expiry=date(2020, 1, 1),
        )
        group = _base_group(travelers=[traveler_a, traveler_b])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_severity_does_not_downgrade(self, base_requirements):
        """First traveler is CRITICAL, second is clean — stays CRITICAL."""
        traveler_critical = make_profile(
            nationality="IN",
            passport_number=None,
        )
        traveler_clean = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        group = _base_group(travelers=[traveler_critical, traveler_clean])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestTravelerSummaryMapping:
    """Each traveler summary maps to the correct profile and evaluation results."""

    def test_summary_carries_traveler_identity(self, base_requirements):
        profile = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _base_group(travelers=[profile])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "9999999"
        assert summary.first_name == "Test"
        assert summary.last_name == "User"

    def test_summary_contains_both_evaluations(self, base_requirements):
        visa = VisaRecord(
            country_code="DE", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2028, 1, 1),
            visa_number="V100",
        )
        profile = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[visa],
        )
        group = _base_group(travelers=[profile])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result is not None
        assert summary.visa_result is not None


class TestDefaultRequirementsLookup:
    """When requirements=None, the function builds them from default data."""

    def test_uses_default_requirements_when_none_provided(self):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        group = _base_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.confirmation_number == "GRP-001"
        assert result.group_severity is None

    def test_default_lookup_deduplicates_destinations(self):
        """Two segments to same destination should only look up requirements once."""
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        seg1 = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg2 = make_segment(destination="DE", departure=date(2026, 9, 15), flight_number="DL200")
        group = _base_group(travelers=[profile], segments=[seg1, seg2])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None

    def test_default_lookup_multiple_destinations(self):
        """Segments to different destinations each get looked up."""
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_jp = make_segment(destination="JP", departure=date(2026, 9, 10), flight_number="DL200")
        group = _base_group(travelers=[profile], segments=[seg_de, seg_jp])

        result = evaluate_group_itinerary(group, requirements=None)

        assert len(result.traveler_summaries) == 1
        assert result.group_severity is None


class TestVisaSeverityInGroup:
    """Visa evaluation results also contribute to group severity."""

    def test_missing_visa_raises_group_to_critical(self, base_requirements):
        # IN national going to CN with no visa → CRITICAL from visa evaluator
        profile = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[],
        )
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _base_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_warning_sets_group_severity(self, base_requirements):
        # Visa expires on departure date → WARNING from visa evaluator
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V200",
        )
        profile = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[visa],
        )
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _base_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING


class TestSharedItineraryConstruction:
    """The function builds a shared Itinerary from the group's confirmation and segments."""

    def test_confirmation_number_propagates(self, base_requirements):
        profile = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _base_group(
            travelers=[profile],
            confirmation="GRP-CUSTOM-99",
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.confirmation_number == "GRP-CUSTOM-99"
