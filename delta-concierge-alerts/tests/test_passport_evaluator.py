"""Tests for the passport evaluator."""

from datetime import date

import pytest

import src.data.country_requirements as country_requirements_mod
from src.evaluators.passport_evaluator import _max_severity, evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


FAR_FUTURE_DEPARTURE = date(2030, 9, 1)
FAR_FUTURE_ARRIVAL = date(2030, 9, 2)


def _segment(**overrides):
    """Build a far-future segment so date-relative branches stay stable."""
    defaults = {"departure": FAR_FUTURE_DEPARTURE, "arrival": FAR_FUTURE_ARRIVAL}
    defaults.update(overrides)
    return make_segment(**defaults)


@pytest.fixture
def swapped_country_requirements():
    """Replace production COUNTRY_REQUIREMENTS with fixture data for fallback tests."""
    fixture_data = {
        "FR": TravelDocRequirements(
            country_code="FR",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        ),
    }
    original = country_requirements_mod.COUNTRY_REQUIREMENTS
    country_requirements_mod.COUNTRY_REQUIREMENTS = fixture_data
    try:
        yield fixture_data
    finally:
        country_requirements_mod.COUNTRY_REQUIREMENTS = original


class TestMissingPassportInfo:
    """Incomplete passport data is always CRITICAL and short-circuits segment checks."""

    def test_missing_passport_number(self, base_requirements):
        profile = make_profile(passport_number=None)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport information is incomplete"]
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "passport_number"
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"

    def test_missing_passport_expiry(self, base_requirements):
        profile = make_profile(passport_expiry=None)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.validation_errors[0].field == "passport_expiry"
        assert result.segments_evaluated == itinerary.segments


class TestPassportAlreadyExpired:
    """A passport that expired before today is CRITICAL regardless of itinerary."""

    def test_expired_passport(self, base_requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []


class TestPassportExpiresBeforeDeparture:
    """Passport valid today but expiring before a segment's departure is CRITICAL."""

    def test_expires_before_departure(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 6, 1))
        itinerary = make_itinerary(_segment(destination="DE", flight_number="DL300"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL300 to DE"]

    def test_expires_before_departure_skips_further_checks_for_segment(self, base_requirements):
        # Only the "expires before departure" reason is added, not the validity-window one
        profile = make_profile(passport_expiry=date(2030, 8, 31))
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert len(result.reasons) == 1
        assert "expires before departure" in result.reasons[0]


class TestPassportBelowCountryMinimum:
    """Passport valid but under the destination's required validity window."""

    def test_below_3_month_schengen_requirement(self, base_requirements):
        # Passport expires ~1.5 months after departure to Germany (needs 3)
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        segment = make_segment(destination="DE", departure=date(2030, 9, 1), arrival=date(2030, 9, 2))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in result.reasons)

    def test_below_6_month_requirement(self, base_requirements):
        # Passport expires 4 months after departure to China (needs 6)
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        segment = _segment(destination="CN")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_expiry_exactly_at_country_minimum_passes(self, base_requirements):
        # Exactly 6 months after departure meets CN's 6-month rule (strict < comparison)
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_requirement_message_includes_flight_number(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="JP", flight_number="DL275"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.reasons == [
            "Passport does not meet JP's 6-month validity requirement for DL275"
        ]


class TestSixMonthAdvisory:
    """Passport meets a shorter country minimum but expires within 6 months → INFO."""

    def test_info_when_within_six_months_of_schengen_travel(self, base_requirements):
        # 4 months of validity: meets DE's 3 months, under the 6-month advisory
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="DE", flight_number="DL400"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 1
        assert "consider renewing before DL400 to DE" in result.reasons[0]

    def test_expiry_exactly_at_six_months_no_advisory(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []


class TestPassportValid:
    """A fully valid passport triggers no alert."""

    def test_no_alert_when_passport_is_valid(self, base_requirements):
        # Passport valid for years after departure — well above any threshold
        profile = make_profile(passport_expiry=date(2035, 6, 1))
        segment = _segment(destination="DE")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []

    def test_no_segments_no_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2035, 6, 1))
        itinerary = make_itinerary()

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestLayoverSegments:
    """Layover segments are evaluated against the layover country's requirements."""

    def test_layover_country_requirement_triggers_alert(self, base_requirements):
        # 4 months validity: fine for DE final destination (INFO only), fails CN layover (6)
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        layover = _segment(destination="CN", is_layover=True, flight_number="DL10")
        final = _segment(destination="DE", origin="PEK", flight_number="DL11")
        itinerary = make_itinerary(layover, final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r and "DL10" in r for r in result.reasons)
        assert any("consider renewing before DL11 to DE" in r for r in result.reasons)

    def test_valid_layover_no_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2035, 1, 1))
        layover = _segment(destination="JP", is_layover=True)
        final = _segment(destination="DE", origin="NRT", flight_number="DL11")
        itinerary = make_itinerary(layover, final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.segments_evaluated == [layover, final]


class TestRequirementsFallback:
    """Destinations missing from the passed requirements fall back to get_requirements."""

    def test_falls_back_to_country_requirements_lookup(
        self, base_requirements, swapped_country_requirements
    ):
        # FR (3 months, from swapped lookup data) not in base_requirements
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        itinerary = make_itinerary(_segment(destination="FR"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet FR's 3-month" in r for r in result.reasons)

    def test_unknown_country_uses_default_six_months(
        self, base_requirements, swapped_country_requirements
    ):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet ZZ's 6-month" in r for r in result.reasons)

    def test_passed_requirements_take_precedence(self, swapped_country_requirements):
        # Override FR to 6 months; lookup data says 3
        override = {
            "FR": TravelDocRequirements(
                country_code="FR",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        }
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="FR"))

        result = evaluate_passport_expiry(profile, itinerary, override)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet FR's 6-month" in r for r in result.reasons)


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments' dates and destinations."""

    def test_later_departure_turns_valid_trip_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 6, 1))
        original = make_itinerary(_segment(destination="CN"))
        changed = make_itinerary(
            _segment(destination="CN", departure=date(2031, 3, 1), arrival=date(2031, 3, 2))
        )

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_destination_change_to_schengen_downgrades_to_info(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        original = make_itinerary(_segment(destination="CN"))
        changed = make_itinerary(_segment(destination="DE"))

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.severity == AlertSeverity.INFO

    def test_added_segment_after_expiry_is_flagged(self, base_requirements):
        profile = make_profile(passport_expiry=date(2035, 1, 1))
        outbound = _segment(destination="DE", flight_number="DL1")
        new_return = _segment(
            destination="JP", departure=date(2035, 2, 1), arrival=date(2035, 2, 2),
            flight_number="DL2",
        )

        result = evaluate_passport_expiry(
            profile, make_itinerary(outbound, new_return), base_requirements
        )

        assert result.reasons == ["Passport expires before departure on DL2 to JP"]
        assert result.segments_evaluated == [outbound, new_return]


class TestSeverityConsolidation:
    """Highest severity across multiple segments wins."""

    def test_critical_overrides_warning(self, base_requirements):
        # Segment 1: DE → passport under 3-month min → CRITICAL
        # Segment 2: CN → passport expires before departure → CRITICAL
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        seg_de = make_segment(
            destination="DE", departure=date(2030, 9, 1), arrival=date(2030, 9, 2),
            flight_number="DL100",
        )
        seg_cn = make_segment(
            destination="CN", departure=date(2030, 11, 1), arrival=date(2030, 11, 2),
            flight_number="DL200",
        )
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_info_then_critical_escalates(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        seg_de = _segment(destination="DE", flight_number="DL1")
        seg_cn = _segment(destination="CN", flight_number="DL2")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_stays_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        seg_cn = _segment(destination="CN", flight_number="DL1")
        seg_de = _segment(destination="DE", flight_number="DL2")
        itinerary = make_itinerary(seg_cn, seg_de)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2


class TestMaxSeverity:
    """_max_severity returns the higher-ranked severity."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_wins(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.WARNING) == AlertSeverity.WARNING

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.INFO) == AlertSeverity.CRITICAL
