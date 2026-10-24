"""Tests for the passport evaluator."""

from datetime import date, timedelta

from src.evaluators.passport_evaluator import _max_severity, evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)
FAR_FUTURE_EXPIRY = date(2040, 1, 1)


def _requirements(country_code: str, months: int) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=months,
        visa_exempt_nationalities=[],
    )


class TestPassportBelowCountryMinimum:
    """Passport valid but under the destination's required validity window."""

    def test_below_3_month_schengen_requirement(self, base_requirements):
        # Passport expires ~6 weeks after departure to Germany (needs 3 months)
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in result.reasons)

    def test_below_6_month_requirement(self, base_requirements):
        # Passport expires 4 months after departure to China (needs 6)
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_reason_names_flight_number(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        segment = make_segment(destination="JP", departure=DEPARTURE, flight_number="DL275")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.reasons == [
            "Passport does not meet JP's 6-month validity requirement for DL275"
        ]

    def test_expiry_on_departure_day_fails_validity_window(self, base_requirements):
        # Not "before departure", but zero months of validity remain
        profile = make_profile(passport_expiry=DEPARTURE)
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 1
        assert "does not meet DE's 3-month" in result.reasons[0]

    def test_one_day_short_of_schengen_window(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 11, 30))
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet DE's 3-month" in result.reasons[0]


class TestPassportExpiresBeforeDeparture:
    """Passport still valid today but lapses before a segment departs."""

    def test_expires_before_departure_is_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 8, 1))
        segment = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL400")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL400 to DE"]
        assert result.validation_errors == []

    def test_expires_one_day_before_departure(self, base_requirements):
        profile = make_profile(passport_expiry=DEPARTURE - timedelta(days=1))
        segment = make_segment(destination="CN", departure=DEPARTURE, flight_number="DL401")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.reasons == ["Passport expires before departure on DL401 to CN"]

    def test_before_departure_skips_validity_window_reason(self, base_requirements):
        # Only the "expires before departure" reason should be emitted
        profile = make_profile(passport_expiry=date(2030, 8, 1))
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert len(result.reasons) == 1
        assert "expires before departure" in result.reasons[0]


class TestPassportExpiringSoonAdvisory:
    """Passport meets the country minimum but expires within 6 months of travel."""

    def test_schengen_ok_but_within_six_months_is_info(self, base_requirements):
        # 4 months of validity: passes DE's 3-month rule, under 6 months
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        segment = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL500")
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL500 to DE"
        ]

    def test_exactly_at_schengen_threshold_is_info(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert "expiring within 6 months" in result.reasons[0]

    def test_exactly_six_months_is_no_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestPassportValid:
    """A fully valid passport triggers no alert."""

    def test_no_alert_when_passport_is_valid(self, base_requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_result_carries_profile_and_segments(self, base_requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        segment = make_segment(destination="JP", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.profile is profile
        assert result.segments_evaluated == [segment]

    def test_empty_itinerary_has_no_alert(self, base_requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        itinerary = make_itinerary()

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestMissingPassportInfo:
    """Incomplete passport data short-circuits to a CRITICAL validation error."""

    def test_missing_passport_number(self, base_requirements):
        profile = make_profile(passport_number=None)
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport information is incomplete"]
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "passport_number"
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"
        assert result.segments_evaluated == [segment]

    def test_missing_passport_expiry(self, base_requirements):
        profile = make_profile(passport_expiry=None)
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.validation_errors[0].field == "passport_expiry"
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"

    def test_missing_both_reports_passport_number(self, base_requirements):
        profile = make_profile(passport_number=None, passport_expiry=None)
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.validation_errors[0].field == "passport_number"


class TestPassportAlreadyExpired:
    """A passport expired before today is CRITICAL regardless of itinerary."""

    def test_expired_passport_is_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []

    def test_expired_passport_short_circuits_segment_checks(self, base_requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        seg_de = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.reasons == ["Passport has expired"]
        assert result.segments_evaluated == [seg_de, seg_cn]


class TestRequirementsLookup:
    """Per-request requirements take precedence; unknown countries use the default."""

    def test_unknown_country_falls_back_to_six_month_default(self, base_requirements):
        # "ZZ" is in neither the passed dict nor COUNTRY_REQUIREMENTS
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        segment = make_segment(destination="ZZ", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet ZZ's 6-month" in result.reasons[0]

    def test_passed_requirements_override_country_defaults(self):
        # A 1-month override for CN lets a 4-month passport pass the minimum
        requirements = {"CN": _requirements("CN", 1)}
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO
        assert "expiring within 6 months" in result.reasons[0]

    def test_stricter_override_raises_to_critical(self):
        # A 12-month override makes an 8-month passport insufficient
        requirements = {"DE": _requirements("DE", 12)}
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet DE's 12-month" in result.reasons[0]


class TestLayovers:
    """Layover segments are evaluated against their own country's requirements."""

    def test_layover_country_requirement_triggers_alert(self, base_requirements):
        # 4 months validity: OK for DE (3), not for CN layover (6)
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        layover = make_segment(
            destination="CN", departure=DEPARTURE, is_layover=True, flight_number="DL10"
        )
        final = make_segment(
            destination="DE", departure=DEPARTURE, origin="PEK", flight_number="DL11"
        )
        itinerary = make_itinerary(layover, final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons[0] == (
            "Passport does not meet CN's 6-month validity requirement for DL10"
        )
        assert "expiring within 6 months" in result.reasons[1]

    def test_valid_passport_through_layover_has_no_alert(self, base_requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        layover = make_segment(destination="JP", departure=DEPARTURE, is_layover=True)
        final = make_segment(destination="CN", departure=DEPARTURE, origin="NRT")
        itinerary = make_itinerary(layover, final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new dates and destinations."""

    def test_rebooked_later_departure_introduces_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 6, 1))
        original = make_itinerary(make_segment(destination="CN", departure=date(2030, 9, 1)))
        changed = make_itinerary(make_segment(destination="CN", departure=date(2031, 3, 1)))

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_rebooked_destination_changes_requirement(self, base_requirements):
        # 4 months validity: fine for Schengen, insufficient for Japan
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        original = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="JP", departure=DEPARTURE))

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_rebooked_earlier_departure_clears_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 8, 1))
        original = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="DE", departure=date(2030, 1, 1)))

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False


class TestSeverityConsolidation:
    """Highest severity across multiple segments wins."""

    def test_critical_overrides_warning(self, base_requirements):
        # Segment 1: DE → passport under 3-month min → CRITICAL
        # Segment 2: CN → passport expires before departure → CRITICAL
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        seg_de = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=date(2030, 11, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_info_then_critical_escalates(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        seg_de = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_stays_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200")
        seg_de = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL100")
        itinerary = make_itinerary(seg_cn, seg_de)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "DL200" in result.reasons[0]
        assert "DL100" in result.reasons[1]

    def test_multiple_info_segments_stay_info(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        seg_1 = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL100")
        seg_2 = make_segment(destination="DE", departure=DEPARTURE, flight_number="DL101")
        itinerary = make_itinerary(seg_1, seg_2)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 2

    def test_only_failing_segment_contributes_reason(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 6, 1))
        seg_ok = make_segment(destination="CN", departure=DEPARTURE, flight_number="DL100")
        seg_late = make_segment(destination="CN", departure=date(2031, 7, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_ok, seg_late)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.reasons == ["Passport expires before departure on DL200 to CN"]


class TestMaxSeverity:
    """_max_severity returns the higher-ranked of the two severities."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_wins(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.INFO) == AlertSeverity.CRITICAL

    def test_equal_keeps_current(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING
