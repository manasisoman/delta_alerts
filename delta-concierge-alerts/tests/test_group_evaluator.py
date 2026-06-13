"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, GroupItinerary, VisaRecord

from tests.conftest import make_profile, make_segment


def _make_group(
    travelers=None,
    segments=None,
    confirmation="GRP-TEST",
    primary="9999999",
):
    """Build a GroupItinerary with sensible defaults."""
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments or [make_segment(destination="DE", departure=date(2030, 6, 1))],
        travelers=travelers or [make_profile(nationality="US", passport_expiry=date(2035, 1, 1))],
        primary_traveler=primary,
    )


class TestCleanGroup:
    """All travelers have valid documents — no alerts expected."""

    def test_single_clean_traveler(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        group = _make_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "GRP-TEST"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == traveler.skymiles_number
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_multiple_clean_travelers(self, base_requirements):
        t1 = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        t2 = make_profile(nationality="GB", passport_expiry=date(2035, 1, 1))
        group = _make_group(travelers=[t1, t2])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2
        assert all(s.passport_result.is_alert_required is False for s in result.traveler_summaries)
        assert all(s.visa_result.is_alert_required is False for s in result.traveler_summaries)


class TestSingleTravelerPassportIssue:
    """One traveler has a passport problem — group severity reflects it."""

    def test_expired_passport_triggers_critical(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2020, 1, 1))
        group = _make_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.is_alert_required is True
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL

    def test_missing_passport_triggers_critical(self, base_requirements):
        traveler = make_profile(nationality="US", passport_number=None, passport_expiry=None)
        group = _make_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL

    def test_passport_below_country_minimum(self, base_requirements):
        # DE requires 3 months; passport expires ~2 months after departure
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 7, 15))
        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL


class TestSingleTravelerVisaIssue:
    """One traveler has a visa problem — group severity reflects it."""

    def test_no_visa_on_file(self, base_requirements):
        # IN nationality not exempt for CN, no visa records
        traveler = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.is_alert_required is True
        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL

    def test_expired_visa(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V001",
        )
        traveler = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestMixedGroup:
    """Group with a mix of clean and problematic travelers."""

    def test_one_clean_one_passport_issue(self, base_requirements):
        clean = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        expired = make_profile(nationality="US", passport_expiry=date(2020, 1, 1))
        group = _make_group(travelers=[clean, expired])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert len(result.traveler_summaries) == 2
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[1].passport_result.is_alert_required is True

    def test_one_clean_one_visa_issue(self, base_requirements):
        # US is exempt for DE; IN is not and has no visa
        clean = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        needs_visa = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[])
        group = _make_group(travelers=[clean, needs_visa])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.traveler_summaries[1].visa_result.is_alert_required is True

    def test_traveler_summary_fields_populated(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        group = _make_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.first_name == "Test"
        assert summary.last_name == "User"
        assert summary.skymiles_number == "9999999"


class TestRequirementsAutoLoad:
    """When requirements=None, the function auto-loads from get_requirements()."""

    def test_auto_loads_requirements_for_known_destination(self):
        # DE is a known country in country_requirements; US traveler is visa-exempt
        traveler = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None
        assert result.traveler_summaries[0].passport_result.is_alert_required is False

    def test_auto_loads_deduplicates_destinations(self):
        # Two segments to DE — get_requirements should only be called once per dest
        traveler = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        seg1 = make_segment(destination="DE", departure=date(2030, 6, 1), flight_number="DL100")
        seg2 = make_segment(destination="DE", departure=date(2030, 7, 1), flight_number="DL200")
        group = _make_group(travelers=[traveler], segments=[seg1, seg2])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None

    def test_auto_loads_multiple_destinations(self):
        # Segments to DE and JP; US traveler is exempt for both
        traveler = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        seg_de = make_segment(destination="DE", departure=date(2030, 6, 1), flight_number="DL100")
        seg_jp = make_segment(destination="JP", departure=date(2030, 7, 1), flight_number="DL200")
        group = _make_group(travelers=[traveler], segments=[seg_de, seg_jp])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None

    def test_auto_load_with_visa_issue(self):
        # IN traveler going to CN with no visa, requirements=None
        traveler = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestSeverityConsolidation:
    """Group severity is the highest across all travelers and evaluations."""

    def test_critical_overrides_warning(self, base_requirements):
        # Traveler 1: visa expires on travel date → WARNING
        visa_warning = VisaRecord(
            country_code="DE", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 6, 1),
            visa_number="V001",
        )
        t1 = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa_warning])

        # Traveler 2: expired passport → CRITICAL
        t2 = make_profile(nationality="IN", passport_expiry=date(2020, 1, 1))

        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        group = _make_group(travelers=[t1, t2], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_overrides_info(self, base_requirements):
        # Traveler 1: passport expiring within 6 months of travel → INFO
        t1 = make_profile(nationality="US", passport_expiry=date(2030, 10, 1))

        # Traveler 2: visa expires on travel date → WARNING
        visa_warning = VisaRecord(
            country_code="DE", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 6, 1),
            visa_number="V002",
        )
        t2 = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa_warning])

        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        group = _make_group(travelers=[t1, t2], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_info_severity_from_passport_advisory(self, base_requirements):
        # Passport valid but expiring within 6 months of travel → INFO
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 10, 1))
        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO

    def test_severity_none_when_all_clean(self, base_requirements):
        t1 = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        t2 = make_profile(nationality="GB", passport_expiry=date(2035, 1, 1))
        group = _make_group(travelers=[t1, t2])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None

    def test_multiple_severity_levels_across_three_travelers(self, base_requirements):
        # Traveler 1: clean (US, valid passport, DE visa-exempt)
        t1 = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))

        # Traveler 2: passport expiring within 6 months → INFO
        t2 = make_profile(nationality="US", passport_expiry=date(2030, 10, 1))

        # Traveler 3: expired passport → CRITICAL
        t3 = make_profile(nationality="US", passport_expiry=date(2020, 1, 1))

        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        group = _make_group(travelers=[t1, t2, t3], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert len(result.traveler_summaries) == 3


class TestLayoverAndTransitVisa:
    """Transit visa requirements during layovers are evaluated per-traveler."""

    def test_layover_with_transit_visa_required(self, base_requirements):
        # CN requires transit visa; IN traveler with no visa
        traveler = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1), is_layover=True)
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_without_transit_visa_skipped(self, base_requirements):
        # DE does not require transit visa; layover should not trigger alert
        traveler = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 6, 1), is_layover=True)
        group = _make_group(travelers=[traveler], segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None


class TestConfirmationNumberPassthrough:
    """The confirmation number from the group is propagated to the result."""

    def test_confirmation_number_matches_input(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        group = _make_group(travelers=[traveler], confirmation="DL-12345")

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-12345"
