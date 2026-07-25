"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_group, make_profile, make_segment


def _profile(skymiles: str, **overrides):
    """Build a profile with a unique skymiles number for group tests."""
    profile = make_profile(**overrides)
    profile.skymiles_number = skymiles
    profile.first_name = f"First{skymiles}"
    profile.last_name = f"Last{skymiles}"
    return profile


def _valid_de_visa(expiry=date(2030, 1, 1)) -> VisaRecord:
    return VisaRecord(
        country_code="DE",
        visa_type="TOURIST",
        issue_date=date(2024, 1, 1),
        expiry_date=expiry,
        visa_number="V-DE-1",
    )


class TestHappyPath:
    """A group of fully compliant travelers yields no group severity."""

    def test_all_clean_travelers(self, base_requirements):
        # US nationals are visa-exempt for DE and hold valid passports
        travelers = [
            _profile("111", nationality="US", passport_expiry=date(2030, 6, 1)),
            _profile("222", nationality="US", passport_expiry=date(2031, 6, 1)),
        ]
        group = make_group(*travelers)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2
        assert all(
            not s.passport_result.is_alert_required
            and not s.visa_result.is_alert_required
            for s in result.traveler_summaries
        )


class TestResultShape:
    """Per-traveler summaries preserve identity and both evaluations."""

    def test_summary_fields_match_travelers(self, base_requirements):
        traveler = _profile("333", nationality="US", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "333"
        assert summary.first_name == "First333"
        assert summary.last_name == "Last333"
        assert summary.passport_result is not None
        assert summary.visa_result is not None

    def test_empty_group_has_no_severity(self, base_requirements):
        group = make_group()

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers."""

    def test_single_critical_traveler(self, base_requirements):
        # IN national with no DE visa on file → CRITICAL
        traveler = _profile("444", nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_critical_wins_over_clean_traveler(self, base_requirements):
        # One clean US traveler and one CRITICAL IN traveler
        clean = _profile("555", nationality="US", passport_expiry=date(2030, 6, 1))
        critical = _profile("666", nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(clean, critical)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_info_severity_when_only_advisory(self, base_requirements):
        # US traveler (visa-exempt for DE) whose passport expires within 6
        # months of travel → passport INFO advisory, no higher severity.
        traveler = _profile("777", nationality="US", passport_expiry=date(2027, 1, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_highest_severity_not_downgraded_by_later_result(self, base_requirements):
        # Traveler hits CRITICAL (no visa) and separately an INFO passport
        # advisory; the aggregate must stay CRITICAL, not drop to INFO.
        traveler = _profile("888", nationality="IN", passport_expiry=date(2027, 1, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestRequirementsLookup:
    """When no requirements dict is passed, defaults are looked up."""

    def test_defaults_used_when_requirements_none(self):
        # DE is visa-exempt for US in the default dataset → clean traveler.
        traveler = _profile("999", nationality="US", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1

    def test_defaults_dedupe_repeated_destination(self):
        # Two segments to the same destination exercise the dedup branch in
        # the default-requirements builder.
        traveler = _profile("101", nationality="US", passport_expiry=date(2030, 6, 1))
        segments = [
            make_segment(destination="DE", flight_number="DL1"),
            make_segment(destination="DE", flight_number="DL2"),
        ]
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_provided_requirements_take_precedence(self, base_requirements):
        # Passing requirements skips the default-lookup path entirely.
        traveler = _profile("202", nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        # IN is not visa-exempt for DE and has no visa on file → CRITICAL.
        assert result.group_severity == AlertSeverity.CRITICAL
