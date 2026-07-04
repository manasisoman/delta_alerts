"""Tests for the group itinerary evaluator."""

from datetime import date
from unittest.mock import patch

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment


def _make_group(
    travelers=None,
    segments=None,
    confirmation="GRP-001",
    primary_traveler="9999999",
):
    """Build a GroupItinerary with sensible defaults."""
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments or [make_segment(destination="DE", departure=date(2026, 9, 1))],
        travelers=travelers or [make_profile()],
        primary_traveler=primary_traveler,
    )


class TestHappyPath:
    """Group where all travelers have valid documents → no severity."""

    def test_single_traveler_all_valid(self, base_requirements):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        group = _make_group(travelers=[profile])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None
        assert result.confirmation_number == "GRP-001"
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_multiple_travelers_all_valid(self, base_requirements):
        profiles = [
            make_profile(nationality="US", passport_expiry=date(2030, 1, 1)),
            make_profile(nationality="GB", passport_expiry=date(2031, 6, 1)),
        ]
        group = _make_group(travelers=profiles)

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2


class TestPassportIssuesInGroup:
    """One traveler has passport issues → group severity reflects that."""

    def test_expired_passport_raises_critical(self, base_requirements):
        good = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        bad = make_profile(passport_expiry=date(2020, 1, 1))
        group = _make_group(travelers=[good, bad])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        bad_summary = result.traveler_summaries[1]
        assert bad_summary.passport_result.is_alert_required is True
        assert bad_summary.passport_result.severity == AlertSeverity.CRITICAL

    def test_missing_passport_raises_critical(self, base_requirements):
        good = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        bad = make_profile(passport_number=None, passport_expiry=None)
        group = _make_group(travelers=[good, bad])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_below_country_minimum(self, base_requirements):
        profile = make_profile(passport_expiry=date(2026, 10, 15))
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestVisaIssuesInGroup:
    """One traveler has visa issues → group severity reflects that."""

    def test_missing_visa_raises_critical(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        summary = result.traveler_summaries[0]
        assert summary.visa_result.is_alert_required is True
        assert summary.visa_result.severity == AlertSeverity.CRITICAL

    def test_expired_visa_raises_critical(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V001",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expires_on_departure_raises_warning(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING


class TestSeverityAggregation:
    """Highest severity across all travelers wins (CRITICAL > WARNING > INFO)."""

    def test_critical_beats_warning(self, base_requirements):
        visa_warning = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V-WARN",
        )
        traveler_warning = make_profile(nationality="IN", visa_records=[visa_warning])

        traveler_critical = make_profile(
            nationality="IN", passport_expiry=date(2020, 1, 1),
        )
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(
            travelers=[traveler_warning, traveler_critical],
            segments=[segment],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_beats_info(self, base_requirements):
        visa_info = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 20),
            visa_number="V-INFO",
        )
        visa_warning = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V-WARN",
        )
        traveler_info = make_profile(nationality="IN", visa_records=[visa_info])
        traveler_warning = make_profile(nationality="IN", visa_records=[visa_warning])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(
            travelers=[traveler_info, traveler_warning],
            segments=[segment],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_info_from_passport_only(self, base_requirements):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2027, 1, 1),
        )
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.INFO


class TestRequirementsAutoLookup:
    """When requirements=None, auto-lookup via get_requirements()."""

    def test_auto_lookup_produces_valid_result(self):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1

    def test_auto_lookup_deduplicates_destinations(self):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        seg1 = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg2 = make_segment(destination="DE", departure=date(2026, 9, 10), flight_number="DL200")
        group = _make_group(travelers=[profile], segments=[seg1, seg2])

        with patch(
            "src.evaluators.group_evaluator.get_requirements"
        ) as mock_get:
            mock_get.return_value = base_reqs_de()
            evaluate_group_itinerary(group, requirements=None)
            mock_get.assert_called_once_with("DE")

    def test_auto_lookup_calls_for_each_unique_destination(self):
        profile = make_profile(
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=date(2026, 9, 10), flight_number="DL200")
        group = _make_group(travelers=[profile], segments=[seg_de, seg_cn])

        with patch(
            "src.evaluators.group_evaluator.get_requirements"
        ) as mock_get:
            mock_get.side_effect = lambda dest: base_reqs_de() if dest == "DE" else base_reqs_cn()
            evaluate_group_itinerary(group, requirements=None)
            assert mock_get.call_count == 2


class TestRequirementsProvided:
    """When requirements dict is provided, uses it directly."""

    def test_provided_requirements_used(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        with patch(
            "src.evaluators.group_evaluator.get_requirements"
        ) as mock_get:
            result = evaluate_group_itinerary(group, requirements=base_requirements)
            mock_get.assert_not_called()

        assert result.group_severity == AlertSeverity.CRITICAL


class TestTravelerSummaryContent:
    """Per-traveler summaries contain correct passport and visa results."""

    def test_summary_fields_match_traveler(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[],
        )
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "9999999"
        assert summary.first_name == "Test"
        assert summary.last_name == "User"
        assert summary.passport_result.severity is None
        assert summary.visa_result.severity == AlertSeverity.CRITICAL

    def test_multiple_travelers_each_have_summary(self, base_requirements):
        t1 = make_profile(nationality="US", passport_expiry=date(2030, 1, 1))
        t2 = make_profile(nationality="IN", passport_expiry=date(2020, 1, 1))
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        group = _make_group(travelers=[t1, t2], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert len(result.traveler_summaries) == 2
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[1].passport_result.is_alert_required is True
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL


class TestLayoverHandling:
    """Layovers with transit visa requirements are evaluated correctly."""

    def test_layover_transit_visa_required_no_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="CN", departure=date(2026, 9, 1), is_layover=True,
        )
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_transit_visa_not_required_skipped(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[],
        )
        segment = make_segment(
            destination="DE", departure=date(2026, 9, 1), is_layover=True,
        )
        group = _make_group(travelers=[profile], segments=[segment])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False


class TestMultiSegmentItinerary:
    """Group evaluation handles multi-segment itineraries."""

    def test_two_segments_different_countries(self, base_requirements):
        visa_cn = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 1, 1),
            visa_number="V-CN",
        )
        profile = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[visa_cn],
        )
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=date(2026, 9, 5), flight_number="DL200")
        group = _make_group(travelers=[profile], segments=[seg_de, seg_cn])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        summary = result.traveler_summaries[0]
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in summary.visa_result.reasons


# --- Helper functions for mock returns ---

def base_reqs_de():
    from src.models.types import TravelDocRequirements
    return TravelDocRequirements(
        country_code="DE",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA"],
    )


def base_reqs_cn():
    from src.models.types import TravelDocRequirements
    return TravelDocRequirements(
        country_code="CN",
        requires_visa=True,
        transit_visa_required=True,
        passport_validity_months=6,
        visa_exempt_nationalities=[],
    )
