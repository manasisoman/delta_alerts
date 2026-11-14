"""Additional branch-coverage tests for the passport evaluator.

Uses made-up country codes (XA = 3-month rule, XB = 6-month rule) and 2030+
dates so results don't depend on production data or today's date.
"""

from datetime import date, timedelta

import pytest

from src.evaluators.passport_evaluator import _max_severity, evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)


def _reqs(code: str, months: int) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=code,
        requires_visa=False,
        transit_visa_required=False,
        passport_validity_months=months,
        visa_exempt_nationalities=[],
    )


@pytest.fixture
def requirements() -> dict[str, TravelDocRequirements]:
    return {"XA": _reqs("XA", 3), "XB": _reqs("XB", 6)}


def _segment(destination="XA", departure=DEPARTURE, flight_number="DL100", is_layover=False):
    return make_segment(
        destination=destination,
        departure=departure,
        arrival=departure + timedelta(days=1),
        flight_number=flight_number,
        is_layover=is_layover,
    )


class TestMissingPassportInfo:
    """Missing passport number or expiry short-circuits to CRITICAL."""

    def test_missing_passport_number(self, requirements):
        profile = make_profile(passport_number=None)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport information is incomplete"]
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "passport_number"
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"

    def test_missing_passport_expiry(self, requirements):
        profile = make_profile(passport_expiry=None)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.validation_errors[0].field == "passport_expiry"

    def test_missing_both_reports_passport_number(self, requirements):
        profile = make_profile(passport_number=None, passport_expiry=None)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.validation_errors[0].field == "passport_number"

    def test_segments_evaluated_returned(self, requirements):
        segments = [_segment(flight_number="DL1"), _segment(flight_number="DL2")]
        profile = make_profile(passport_number=None)

        result = evaluate_passport_expiry(profile, make_itinerary(*segments), requirements)

        assert result.segments_evaluated == segments
        assert result.profile is profile


class TestPassportAlreadyExpired:
    """A passport expired before today is CRITICAL regardless of segments."""

    def test_expired_passport(self, requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []

    def test_expired_passport_with_no_segments(self, requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))

        result = evaluate_passport_expiry(profile, make_itinerary(), requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.segments_evaluated == []


class TestPassportExpiresBeforeDeparture:
    """Passport valid today but expiring before the flight departs is CRITICAL."""

    def test_expires_day_before_departure(self, requirements):
        profile = make_profile(passport_expiry=DEPARTURE - timedelta(days=1))
        itinerary = make_itinerary(_segment(flight_number="DL555"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL555 to XA"]

    def test_expires_on_departure_fails_validity_window(self, requirements):
        profile = make_profile(passport_expiry=DEPARTURE)
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet XA's 3-month" in result.reasons[0]

    def test_expires_before_departure_on_layover(self, requirements):
        profile = make_profile(passport_expiry=DEPARTURE - timedelta(days=1))
        itinerary = make_itinerary(_segment(is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "expires before departure" in result.reasons[0]


class TestCountryValidityRequirement:
    """Remaining validity below the destination's minimum is CRITICAL."""

    def test_one_day_short_of_3_months(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 11, 30))
        itinerary = make_itinerary(_segment("XA", flight_number="DL300"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Passport does not meet XA's 3-month validity requirement for DL300"
        ]

    def test_one_day_short_of_6_months(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 2, 28))
        itinerary = make_itinerary(_segment("XB"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet XB's 6-month" in result.reasons[0]

    def test_layover_country_requirement_enforced(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("XA", is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet XA's 3-month" in result.reasons[0]


class TestSixMonthAdvisory:
    """Meeting a shorter requirement but expiring within 6 months yields INFO."""

    def test_exactly_3_months_meets_requirement_but_info(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        itinerary = make_itinerary(_segment("XA", flight_number="DL400"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL400 to XA"
        ]

    def test_one_day_before_6_months_is_info(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 2, 28))
        itinerary = make_itinerary(_segment("XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO


class TestNoAlert:
    """Expiry at or beyond departure + 6 months produces no alert."""

    def test_exactly_6_months_after_departure(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        itinerary = make_itinerary(_segment("XB"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_3_month_country_with_6_months_validity(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        itinerary = make_itinerary(_segment("XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False

    def test_empty_itinerary(self, requirements):
        profile = make_profile(passport_expiry=date(2035, 1, 1))

        result = evaluate_passport_expiry(profile, make_itinerary(), requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_with_valid_passport(self, requirements):
        profile = make_profile(passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(_segment("XA", is_layover=True), _segment("XB", flight_number="DL200"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False


class TestRequirementsFallback:
    """Destinations missing from the requirements dict fall back to get_requirements."""

    def test_unknown_country_uses_6_month_default(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet ZZ's 6-month" in result.reasons[0]

    def test_empty_requirements_uses_country_data(self):
        # DE (Schengen) requires 3 months in COUNTRY_REQUIREMENTS
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO

    def test_override_takes_precedence_over_country_data(self):
        # DE normally needs 3 months; override to 6 makes this CRITICAL
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_passport_expiry(profile, itinerary, {"DE": _reqs("DE", 6)})

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet DE's 6-month" in result.reasons[0]


class TestMultiSegmentConsolidation:
    """Each segment is evaluated and the highest severity wins."""

    def test_info_then_critical_yields_critical(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        seg_info = _segment("XA", flight_number="DL1")
        seg_crit = _segment("XB", flight_number="DL2")

        result = evaluate_passport_expiry(profile, make_itinerary(seg_info, seg_crit), requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
        assert "DL1 to XA" in result.reasons[0]
        assert "XB's 6-month" in result.reasons[1]

    def test_critical_then_info_stays_critical(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        seg_crit = _segment("XB", flight_number="DL1")
        seg_info = _segment("XA", flight_number="DL2")

        result = evaluate_passport_expiry(profile, make_itinerary(seg_crit, seg_info), requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_ok_segment_then_info_segment(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 1, 1))
        seg_ok = _segment("XA", departure=date(2030, 6, 1), flight_number="DL1")
        seg_info = _segment("XA", departure=date(2030, 9, 1), flight_number="DL2")

        result = evaluate_passport_expiry(profile, make_itinerary(seg_ok, seg_info), requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 1
        assert "DL2" in result.reasons[0]

    def test_multiple_info_segments(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 15))
        segs = [_segment("XA", flight_number="DL1"), _segment("XA", flight_number="DL2")]

        result = evaluate_passport_expiry(profile, make_itinerary(*segs), requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 2

    def test_later_return_leg_departs_after_expiry(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        outbound = _segment("XA", departure=date(2030, 9, 1), flight_number="DL1")
        inbound = _segment("XA", departure=date(2031, 4, 1), flight_number="DL2")

        result = evaluate_passport_expiry(profile, make_itinerary(outbound, inbound), requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL2 to XA"]


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new dates/destination."""

    def test_moving_departure_later_triggers_alert(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        original = make_itinerary(_segment("XB", departure=date(2030, 9, 1)))
        changed = make_itinerary(_segment("XB", departure=date(2030, 12, 1)))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_moving_departure_earlier_clears_alert(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 1))
        original = make_itinerary(_segment("XB", departure=date(2030, 12, 1)))
        changed = make_itinerary(_segment("XB", departure=date(2030, 8, 1)))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False

    def test_changing_destination_to_stricter_country(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        original = make_itinerary(_segment("XA"))
        changed = make_itinerary(_segment("XB"))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_adding_layover_segment(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        original = make_itinerary(_segment("XA"))
        changed = make_itinerary(_segment("XB", is_layover=True, flight_number="DL9"), _segment("XA"))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL
        assert "XB's 6-month" in after.reasons[0]


class TestMaxSeverity:
    """_max_severity returns the higher-ranked severity."""

    @pytest.mark.parametrize("new", list(AlertSeverity))
    def test_none_current_returns_new(self, new):
        assert _max_severity(None, new) == new

    @pytest.mark.parametrize(
        "current,new,expected",
        [
            (AlertSeverity.INFO, AlertSeverity.WARNING, AlertSeverity.WARNING),
            (AlertSeverity.INFO, AlertSeverity.CRITICAL, AlertSeverity.CRITICAL),
            (AlertSeverity.WARNING, AlertSeverity.CRITICAL, AlertSeverity.CRITICAL),
            (AlertSeverity.CRITICAL, AlertSeverity.INFO, AlertSeverity.CRITICAL),
            (AlertSeverity.CRITICAL, AlertSeverity.WARNING, AlertSeverity.CRITICAL),
            (AlertSeverity.WARNING, AlertSeverity.INFO, AlertSeverity.WARNING),
            (AlertSeverity.WARNING, AlertSeverity.WARNING, AlertSeverity.WARNING),
        ],
    )
    def test_higher_rank_wins(self, current, new, expected):
        assert _max_severity(current, new) == expected
