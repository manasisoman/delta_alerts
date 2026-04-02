"""Tests for the passport evaluator."""

from datetime import date

from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.models.types import AlertSeverity

from tests.conftest import make_itinerary, make_profile, make_segment


class TestPassportBelowCountryMinimum:
    """Passport valid but under the destination's required validity window -> CRITICAL."""

    def test_below_3_month_schengen_requirement(self, base_requirements):
        # Passport expires 2 months after departure to Germany (needs 3)
        profile = make_profile(passport_expiry=date(2026, 10, 15))
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in result.reasons)

    def test_below_6_month_requirement(self, base_requirements):
        # Passport expires 4 months after departure to China (needs 6)
        profile = make_profile(passport_expiry=date(2027, 1, 1))
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)


class TestPassportValid:
    """A fully valid passport triggers no alert."""

    def test_no_alert_when_passport_is_valid(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 6, 1))
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestSeverityConsolidation:
    """Highest severity across multiple segments wins."""

    def test_critical_overrides_warning(self, base_requirements):
        # Segment 1: DE -> passport under 3-month min -> CRITICAL
        # Segment 2: CN -> passport expires before departure -> CRITICAL
        profile = make_profile(passport_expiry=date(2026, 10, 15))
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=date(2026, 11, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
