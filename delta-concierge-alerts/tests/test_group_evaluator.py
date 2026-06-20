"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, GroupItinerary, VisaRecord

from tests.conftest import make_profile, make_segment


def _group(travelers=None, segments=None, confirmation="GRP-TEST"):
    """Build a GroupItinerary with sensible defaults."""
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments or [make_segment(destination="DE")],
        travelers=travelers or [make_profile(nationality="US")],
        primary_traveler="9999999",
    )


class TestHappyPath:
    """All travelers have valid documents — no alerts, group_severity is None."""

    def test_single_traveler_all_valid(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _group(travelers=[traveler], segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None
        assert result.confirmation_number == "GRP-TEST"
        assert len(result.traveler_summaries) == 1
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[0].visa_result.is_alert_required is False

    def test_multiple_travelers_all_valid(self, base_requirements):
        t1 = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        t2 = make_profile(nationality="GB", passport_expiry=date(2030, 6, 1))
        group = _group(
            travelers=[t1, t2],
            segments=[make_segment(destination="DE")],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2


class TestPassportIssue:
    """One traveler has a passport problem — group_severity reflects it."""

    def test_expired_passport_yields_critical(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2020, 1, 1))
        group = _group(travelers=[traveler], segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.is_alert_required is True
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL

    def test_passport_below_country_minimum(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2026, 10, 15))
        group = _group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_info_advisory(self, base_requirements):
        """Passport valid but expiring within 6 months of travel → INFO."""
        traveler = make_profile(nationality="US", passport_expiry=date(2027, 2, 1))
        group = _group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.INFO


class TestVisaIssue:
    """One traveler has a visa problem — group_severity reflects it."""

    def test_missing_visa_yields_critical(self, base_requirements):
        traveler = make_profile(nationality="IN", visa_records=[])
        group = _group(travelers=[traveler], segments=[make_segment(destination="CN")])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.is_alert_required is True
        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL

    def test_expired_visa_yields_critical(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V001",
        )
        traveler = make_profile(nationality="IN", visa_records=[visa])
        group = _group(travelers=[traveler], segments=[make_segment(destination="CN")])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expires_on_departure_yields_warning(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V002",
        )
        traveler = make_profile(nationality="IN", visa_records=[visa])
        group = _group(
            travelers=[traveler],
            segments=[make_segment(destination="CN", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING


class TestSeverityRanking:
    """Multiple travelers with different severity levels — group gets highest."""

    def test_critical_outranks_warning(self, base_requirements):
        visa_warning = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V010",
        )
        t1 = make_profile(nationality="IN", visa_records=[visa_warning])

        t2 = make_profile(nationality="IN", visa_records=[])

        group = _group(
            travelers=[t1, t2],
            segments=[make_segment(destination="CN", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert len(result.traveler_summaries) == 2

    def test_warning_outranks_info(self, base_requirements):
        t1 = make_profile(nationality="US", passport_expiry=date(2027, 2, 1))

        visa_on_departure = VisaRecord(
            country_code="DE", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V011",
        )
        t2 = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[visa_on_departure],
        )

        group = _group(
            travelers=[t1, t2],
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING


class TestRequirementsAutoLoad:
    """requirements=None triggers auto-load from get_requirements."""

    def test_requirements_none_auto_loads(self):
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _group(
            travelers=[traveler],
            segments=[make_segment(destination="DE")],
        )

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1

    def test_requirements_none_with_missing_visa(self):
        traveler = make_profile(nationality="IN", visa_records=[])
        group = _group(
            travelers=[traveler],
            segments=[make_segment(destination="CN")],
        )

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_requirements_none_deduplicates_destinations(self):
        """Two segments to the same destination only look up requirements once."""
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _group(
            travelers=[traveler],
            segments=[
                make_segment(destination="DE", flight_number="DL100"),
                make_segment(destination="DE", flight_number="DL101"),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None


class TestRequirementsProvided:
    """Explicitly provided requirements skip auto-load."""

    def test_uses_provided_requirements(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _group(
            travelers=[traveler],
            segments=[make_segment(destination="DE")],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None


class TestBothIssues:
    """Same traveler has both passport and visa issues."""

    def test_passport_and_visa_issues(self, base_requirements):
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2020, 1, 1),
            visa_records=[],
        )
        group = _group(travelers=[traveler], segments=[make_segment(destination="CN")])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is True
        assert summary.visa_result.is_alert_required is True


class TestMultipleSegments:
    """Multiple segments with different destinations."""

    def test_two_destinations_both_clean(self, base_requirements):
        t = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _group(
            travelers=[t],
            segments=[
                make_segment(destination="DE", flight_number="DL100"),
                make_segment(destination="JP", flight_number="DL200"),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None

    def test_second_destination_triggers_alert(self, base_requirements):
        traveler = make_profile(nationality="IN", visa_records=[])
        group = _group(
            travelers=[traveler],
            segments=[
                make_segment(destination="DE", flight_number="DL100"),
                make_segment(destination="CN", flight_number="DL200"),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_with_transit_visa_required(self, base_requirements):
        traveler = make_profile(nationality="IN", visa_records=[])
        group = _group(
            travelers=[traveler],
            segments=[
                make_segment(destination="CN", is_layover=True, flight_number="DL300"),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestTravelerSummaryFields:
    """Per-traveler summary contains correct profile fields."""

    def test_summary_captures_traveler_info(self, base_requirements):
        traveler = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "9999999"
        assert summary.first_name == "Test"
        assert summary.last_name == "User"
