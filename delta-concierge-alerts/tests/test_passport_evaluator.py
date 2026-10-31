"""Tests for the passport evaluator."""

from datetime import date

from src.evaluators.passport_evaluator import _max_severity, evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)
ARRIVAL = date(2030, 9, 2)


def _segment(**overrides):
    """Build a far-future FlightSegment, overriding only the fields under test."""
    fields = {"departure": DEPARTURE, "arrival": ARRIVAL}
    fields.update(overrides)
    return make_segment(**fields)


def _requirements(country_code: str, months: int) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=False,
        transit_visa_required=False,
        passport_validity_months=months,
        visa_exempt_nationalities=[],
    )


class TestMissingPassportInfo:
    """Incomplete passport data is always CRITICAL and short-circuits evaluation."""

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

        assert result.severity == AlertSeverity.CRITICAL
        assert result.validation_errors[0].field == "passport_expiry"
        assert result.validation_errors[0].message == "Passport information is incomplete"

    def test_missing_both_reports_passport_number(self, base_requirements):
        profile = make_profile(passport_number=None, passport_expiry=None)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.validation_errors[0].field == "passport_number"

    def test_segments_evaluated_preserved(self, base_requirements):
        segment = _segment()
        profile = make_profile(passport_number=None)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.segments_evaluated == [segment]
        assert result.profile is profile


class TestPassportAlreadyExpired:
    """A passport expired before today is CRITICAL regardless of itinerary."""

    def test_expired_passport(self, base_requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []

    def test_expired_passport_with_empty_itinerary(self, base_requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary()

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.segments_evaluated == []


class TestPassportExpiresBeforeDeparture:
    """Passport valid today but expiring before the flight departs is CRITICAL."""

    def test_expires_before_departure(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 8, 1))
        itinerary = make_itinerary(_segment(destination="DE", flight_number="DL300"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL300 to DE"]

    def test_expires_on_departure_day_is_validity_failure(self, base_requirements):
        # Not "before" departure, so it falls through to the validity-window check
        profile = make_profile(passport_expiry=DEPARTURE)
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 1
        assert "does not meet DE's 3-month" in result.reasons[0]

    def test_layover_segment_is_evaluated(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 8, 1))
        itinerary = make_itinerary(_segment(destination="JP", is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "before departure" in result.reasons[0]


class TestPassportBelowCountryMinimum:
    """Passport valid but under the destination's required validity window."""

    def test_below_3_month_schengen_requirement(self, base_requirements):
        # Passport expires ~6 weeks after departure to Germany (needs 3 months)
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
        segment = make_segment(destination="CN", departure=date(2030, 9, 1), arrival=date(2030, 9, 2))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_reason_includes_flight_number(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        itinerary = make_itinerary(_segment(destination="DE", flight_number="DL777"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.reasons == [
            "Passport does not meet DE's 3-month validity requirement for DL777"
        ]

    def test_layover_below_minimum(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="CN", is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet CN's 6-month" in result.reasons[0]

    def test_custom_requirement_dict_is_honored(self):
        # 8 months of validity fails a 12-month custom rule
        requirements = {"XA": _requirements("XA", 12)}
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(_segment(destination="XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet XA's 12-month" in result.reasons[0]


class TestValidityThresholdBoundary:
    """Expiry exactly at departure + required months satisfies the requirement."""

    def test_exactly_at_3_month_threshold_is_not_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_one_day_short_of_3_month_threshold_is_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2030, 11, 30))
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL

    def test_exactly_at_6_month_threshold_is_valid(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestPassportExpiringSoonAdvisory:
    """Meets the country minimum but expires within 6 months of travel: INFO only."""

    def test_info_when_within_6_months_for_3_month_country(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="DE", flight_number="DL400"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL400 to DE"
        ]

    def test_info_on_layover(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="DE", is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_zero_month_requirement_still_gets_advisory(self):
        requirements = {"XB": _requirements("XB", 0)}
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment(destination="XB"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO


class TestPassportValid:
    """A fully valid passport triggers no alert."""

    def test_no_alert_when_passport_is_valid(self, base_requirements):
        # Passport valid for years beyond departure — well above any threshold
        profile = make_profile(passport_expiry=date(2035, 6, 1))
        segment = make_segment(destination="DE", departure=date(2030, 9, 1), arrival=date(2030, 9, 2))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_empty_itinerary_has_no_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2035, 6, 1))
        itinerary = make_itinerary()

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestRequirementsFallback:
    """Destinations missing from the requirements dict fall back to get_requirements."""

    def test_unknown_country_uses_6_month_default(self):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment(destination="ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet ZZ's 6-month" in result.reasons[0]

    def test_unknown_country_passes_with_long_validity(self):
        profile = make_profile(passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(_segment(destination="ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_missing_destination_falls_back_while_others_use_dict(self, base_requirements):
        # DE (3-month, in dict) passes with an INFO; ZZ (6-month default) fails
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(
            _segment(destination="DE", flight_number="DL100"),
            _segment(destination="ZZ", flight_number="DL200"),
        )

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "consider renewing before DL100 to DE" in result.reasons[0]
        assert "does not meet ZZ's 6-month" in result.reasons[1]


class TestSeverityConsolidation:
    """Highest severity across multiple segments wins."""

    def test_critical_overrides_warning(self, base_requirements):
        # Segment 1: DE → passport under 3-month min → CRITICAL
        # Segment 2: CN → passport expires before departure → CRITICAL
        profile = make_profile(passport_expiry=date(2030, 10, 15))
        seg_de = make_segment(destination="DE", departure=date(2030, 9, 1), flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=date(2030, 11, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_info_then_critical_escalates(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(
            _segment(destination="DE", flight_number="DL100"),
            _segment(destination="CN", flight_number="DL200"),
        )

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_stays_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(
            _segment(destination="CN", flight_number="DL200"),
            _segment(destination="DE", flight_number="DL100"),
        )

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_multiple_info_stays_info(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(
            _segment(destination="DE", flight_number="DL100"),
            _segment(destination="DE", flight_number="DL101", is_layover=True),
        )

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 2

    def test_valid_segment_adds_no_reason(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 6, 1))
        itinerary = make_itinerary(
            _segment(destination="CN", flight_number="DL200"),
            _segment(destination="DE", departure=date(2031, 2, 1), arrival=date(2031, 2, 2)),
        )

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 1


class TestItineraryChange:
    """Re-evaluating after an itinerary change reflects the new segments."""

    def test_moving_departure_later_escalates_to_critical(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        original = make_itinerary(_segment(destination="DE"))
        changed = make_itinerary(
            _segment(destination="DE", departure=date(2030, 12, 1), arrival=date(2030, 12, 2))
        )

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_changing_destination_resolves_alert(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        original = make_itinerary(_segment(destination="CN"))
        changed = make_itinerary(
            _segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False

    def test_adding_layover_adds_reason(self, base_requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        original = make_itinerary(_segment(destination="DE"))
        changed = make_itinerary(
            _segment(destination="CN", is_layover=True, flight_number="DL050"),
            _segment(destination="DE"),
        )

        before = evaluate_passport_expiry(profile, original, base_requirements)
        after = evaluate_passport_expiry(profile, changed, base_requirements)

        assert len(before.reasons) == 1
        assert len(after.reasons) == 2
        assert after.severity == AlertSeverity.CRITICAL
        assert after.segments_evaluated == changed.segments


class TestMaxSeverity:
    """_max_severity returns the higher-ranked severity."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_replaces_current(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL

    def test_warning_beats_info(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.WARNING) == AlertSeverity.WARNING

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.WARNING) == AlertSeverity.CRITICAL

    def test_equal_keeps_current(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING
