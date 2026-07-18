"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, GroupItinerary, VisaRecord

from tests.conftest import make_profile, make_segment


def make_group(
    *travelers,
    segments=None,
    confirmation: str = "DL-GROUP",
) -> GroupItinerary:
    """Build a GroupItinerary from travelers with sensible defaults.

    Defaults to a single Germany segment departing in 2026. Callers pass the
    travelers positionally and override segments only when needed.
    """
    segs = segments if segments is not None else [make_segment(destination="DE")]
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segs,
        travelers=list(travelers),
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


class TestCleanGroup:
    """A group where every traveler is document-ready produces no alerts."""

    def test_no_alerts_when_all_travelers_valid(self, base_requirements):
        # US nationals are visa-exempt for DE and hold far-future passports.
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False


class TestSingleCriticalTraveler:
    """A single traveler with a CRITICAL issue drives the group severity."""

    def test_missing_passport_yields_critical_group(self, base_requirements):
        traveler = make_profile(nationality="US", passport_number=None)
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL


class TestMixedSeverities:
    """The group severity is the highest severity across all travelers."""

    def test_critical_outranks_info(self, base_requirements):
        # Traveler A: passport valid but expiring within 6 months → INFO.
        info_traveler = make_profile(
            nationality="US", passport_expiry=date(2027, 1, 15)
        )
        # Traveler B: missing passport → CRITICAL.
        critical_traveler = make_profile(nationality="US", passport_number=None)
        group = make_group(info_traveler, critical_traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL

    def test_warning_outranks_info(self, base_requirements):
        # Traveler A: visa expires on departure date → WARNING.
        warning_visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V001",
        )
        warning_traveler = make_profile(nationality="IN", visa_records=[warning_visa])
        # Traveler B: visa expires within the warning window after travel → INFO.
        info_visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 15),
            visa_number="V002",
        )
        info_traveler = make_profile(nationality="IN", visa_records=[info_visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = make_group(warning_traveler, info_traveler, segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING


class TestAggregatedSummaries:
    """Per-traveler summaries capture each traveler's identity and results."""

    def test_summary_fields_match_travelers(self, base_requirements):
        first = make_profile(nationality="US")
        first.skymiles_number = "1111111"
        first.first_name = "Ada"
        first.last_name = "Lovelace"
        second = make_profile(nationality="US")
        second.skymiles_number = "2222222"
        second.first_name = "Alan"
        second.last_name = "Turing"
        group = make_group(first, second)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert [s.skymiles_number for s in result.traveler_summaries] == ["1111111", "2222222"]
        assert result.traveler_summaries[0].first_name == "Ada"
        assert result.traveler_summaries[1].last_name == "Turing"

    def test_empty_group_has_no_summaries(self, base_requirements):
        group = make_group(segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestDefaultRequirementsLookup:
    """When requirements is None the evaluator loads them from default data."""

    def test_requirements_built_from_default_data(self):
        # No requirements passed → evaluator looks up DE from country data.
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1

    def test_repeated_destination_looked_up_once(self):
        # Two segments to the same destination exercise the dedupe branch.
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        seg_one = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_two = make_segment(destination="DE", departure=date(2026, 10, 1), flight_number="DL200")
        group = make_group(traveler, segments=[seg_one, seg_two])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
