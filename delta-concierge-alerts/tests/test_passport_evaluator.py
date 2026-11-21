"""Tests for the passport evaluator."""

from datetime import date

from src.evaluators.passport_evaluator import _max_severity, evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2031, 9, 1)
ARRIVAL = date(2031, 9, 2)


def _segment(**overrides):
    defaults = {"destination": "DE", "departure": DEPARTURE, "arrival": ARRIVAL}
    defaults.update(overrides)
    return make_segment(**defaults)


class TestPassportBelowCountryMinimum:
    """Passport valid but under the destination's required validity window."""

    def test_below_3_month_schengen_requirement(self, base_requirements):
        # Passport expires 2 months after departure to Germany (needs 3)
        profile = make_profile(passport_expiry=date(2031, 10, 15))
        segment = _segment(destination="DE")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in result.reasons)

    def test_below_6_month_requirement(self, base_requirements):
        # Passport expires 4 months after departure to China (needs 6)
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        segment = _segment(destination="CN")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_expiry_exactly_on_threshold_meets_requirement(self, base_requirements):
        # Expiry exactly 6 months after departure is not "< threshold"
        profile = make_profile(passport_expiry=date(2032, 3, 1))
        segment = _segment(destination="CN")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestPassportValid:
    """A fully valid passport triggers no alert."""

    def test_no_alert_when_passport_is_valid(self, base_requirements):
        profile = make_profile(passport_expiry=date(2035, 6, 1))
        segment = _segment(destination="DE")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == [segment]
        assert result.profile is profile

    def test_no_alert_for_empty_itinerary(self, base_requirements):
        profile = make_profile(passport_expiry=date(2035, 6, 1))
        itinerary = make_itinerary()

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestMissingPassportInfo:
    """Incomplete passport data short-circuits to CRITICAL."""

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
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"

    def test_missing_info_reports_all_segments_evaluated(self, base_requirements):
        seg_a = _segment(flight_number="DL100")
        seg_b = _segment(destination="CN", flight_number="DL200")
        profile = make_profile(passport_number=None)
        itinerary = make_itinerary(seg_a, seg_b)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.segments_evaluated == [seg_a, seg_b]


class TestPassportAlreadyExpired:
    """A passport that has already expired is CRITICAL regardless of segments."""

    def test_expired_passport(self, base_requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []


class TestPassportExpiresBeforeDeparture:
    """Passport valid today but expired by the departure date."""

    def test_expires_before_departure(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 8, 1))
        itinerary = make_itinerary(_segment(destination="JP", flight_number="DL300"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL300 to JP"]

    def test_expires_before_departure_skips_validity_check(self, base_requirements):
        # Only the "expires before departure" reason is reported, not the validity one
        profile = make_profile(passport_expiry=date(2031, 8, 1))
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert len(result.reasons) == 1
        assert "expires before departure" in result.reasons[0]


class TestPassportExpiringSoonAdvisory:
    """Passport meets a short country rule but expires within 6 months of travel."""

    def test_info_when_meets_3_month_rule_but_under_6_months(self, base_requirements):
        # DE needs 3 months; passport has 4 months left at departure
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(_segment(destination="DE", flight_number="DL400"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL400 to DE"
        ]


class TestRequirementsLookup:
    """Requirements come from the passed dict, falling back to defaults."""

    def test_uses_passed_requirements_for_destination(self):
        requirements = {
            "XA": TravelDocRequirements(
                country_code="XA",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        # 4 months left: passes XA's 3-month rule, but within 6 months → INFO
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(_segment(destination="XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO

    def test_falls_back_to_default_requirements_for_unknown_country(self):
        # Unknown country uses the 6-month default → 4 months left is CRITICAL
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(_segment(destination="ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet ZZ's 6-month" in r for r in result.reasons)

    def test_custom_long_validity_requirement(self):
        requirements = {
            "XB": TravelDocRequirements(
                country_code="XB",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=12,
                visa_exempt_nationalities=[],
            ),
        }
        # 9 months left: fails XB's 12-month rule
        profile = make_profile(passport_expiry=date(2032, 6, 1))
        itinerary = make_itinerary(_segment(destination="XB"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet XB's 12-month" in r for r in result.reasons)


class TestLayoverSegments:
    """Layover segments are evaluated against their country's requirements too."""

    def test_layover_below_requirement_triggers_alert(self, base_requirements):
        # Final destination DE is fine (3 months), but CN layover needs 6
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        layover = _segment(destination="CN", is_layover=True, flight_number="DL500")
        final = _segment(destination="DE", flight_number="DL501")
        itinerary = make_itinerary(layover, final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("CN's 6-month" in r and "DL500" in r for r in result.reasons)
        assert any("DL501 to DE" in r for r in result.reasons)


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new departure date."""

    def test_later_departure_turns_valid_passport_into_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2032, 6, 1))
        original = make_itinerary(_segment(destination="CN", departure=date(2031, 9, 1)))
        changed = make_itinerary(_segment(destination="CN", departure=date(2032, 2, 1)))

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_earlier_departure_clears_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2032, 6, 1))
        original = make_itinerary(_segment(destination="CN", departure=date(2032, 2, 1)))
        changed = make_itinerary(_segment(destination="CN", departure=date(2031, 9, 1)))

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False


class TestSeverityConsolidation:
    """Highest severity across multiple segments wins."""

    def test_critical_overrides_warning(self, base_requirements):
        # Segment 1: DE → passport under 3-month min → CRITICAL
        # Segment 2: CN → passport expires before departure → CRITICAL
        profile = make_profile(passport_expiry=date(2031, 10, 15))
        seg_de = _segment(destination="DE", departure=date(2031, 9, 1), flight_number="DL100")
        seg_cn = _segment(destination="CN", departure=date(2031, 11, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_info_then_critical_escalates(self, base_requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        seg_de = _segment(destination="DE", flight_number="DL100")  # INFO
        seg_cn = _segment(destination="CN", flight_number="DL200")  # CRITICAL
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_stays_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        seg_cn = _segment(destination="CN", flight_number="DL200")  # CRITICAL
        seg_de = _segment(destination="DE", flight_number="DL100")  # INFO
        itinerary = make_itinerary(seg_cn, seg_de)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_multiple_info_segments_stay_info(self, base_requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        seg_a = _segment(destination="DE", flight_number="DL100")
        seg_b = _segment(destination="DE", flight_number="DL101")
        itinerary = make_itinerary(seg_a, seg_b)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 2


class TestMaxSeverity:
    """_max_severity returns the higher-ranked of two severities."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_wins(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.WARNING) == AlertSeverity.WARNING

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.WARNING) == AlertSeverity.CRITICAL

    def test_equal_keeps_current(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING
