"""Branch-coverage tests for the passport evaluator.

Uses made-up country codes (XA = 3-month rule, XB = 6-month rule) and 2030+
dates so results do not depend on real country data or the current date.
"""

from datetime import date

import pytest

import src.data.country_requirements as country_requirements
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2031, 1, 1)
ARRIVAL = date(2031, 1, 2)
FAR_FUTURE_EXPIRY = date(2040, 1, 1)


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


@pytest.fixture
def swap_country_requirements():
    """Temporarily replace the module-level COUNTRY_REQUIREMENTS table."""
    original = country_requirements.COUNTRY_REQUIREMENTS

    def _swap(table):
        country_requirements.COUNTRY_REQUIREMENTS = table

    try:
        yield _swap
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


def _segment(destination="XA", departure=DEPARTURE, **kwargs):
    return make_segment(destination=destination, departure=departure, arrival=ARRIVAL, **kwargs)


class TestMissingPassportInfo:
    """Missing passport number or expiry short-circuits to CRITICAL."""

    def test_missing_passport_number(self, requirements):
        profile = make_profile(passport_number=None, passport_expiry=FAR_FUTURE_EXPIRY)
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
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"

    def test_missing_info_reports_all_segments_evaluated(self, requirements):
        profile = make_profile(passport_number=None)
        segments = [_segment(flight_number="DL1"), _segment("XB", flight_number="DL2")]
        itinerary = make_itinerary(*segments)

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.segments_evaluated == segments


class TestPassportAlreadyExpired:
    """A passport expired before today is CRITICAL regardless of itinerary."""

    def test_expired_passport(self, requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(_segment())

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []


class TestPassportExpiresBeforeDeparture:
    """Passport still valid today but expiring before the flight departs."""

    def test_expires_before_departure(self, requirements):
        profile = make_profile(passport_expiry=date(2030, 12, 31))
        itinerary = make_itinerary(_segment(flight_number="DL300"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL300 to XA"]

    def test_expires_on_departure_day_fails_validity_rule_not_departure_check(self, requirements):
        profile = make_profile(passport_expiry=DEPARTURE)
        itinerary = make_itinerary(_segment(flight_number="DL301"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport does not meet XA's 3-month validity requirement for DL301"]


class TestCountryValidityRequirement:
    """Passport validity at departure must meet the destination's minimum."""

    def test_below_3_month_rule(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 3, 15))
        itinerary = make_itinerary(_segment("XA", flight_number="DL400"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport does not meet XA's 3-month validity requirement for DL400"]

    def test_below_6_month_rule(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(_segment("XB", flight_number="DL401"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport does not meet XB's 6-month validity requirement for DL401"]

    def test_exactly_at_3_month_threshold_meets_rule(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 4, 1))
        itinerary = make_itinerary(_segment("XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO
        assert not any("does not meet" in r for r in result.reasons)

    def test_layover_segment_is_evaluated(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(_segment("XB", is_layover=True, flight_number="DL402"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport does not meet XB's 6-month validity requirement for DL402"]


class TestSixMonthAdvisory:
    """Passport meets the country rule but expires within 6 months: INFO."""

    def test_meets_3_month_rule_but_within_6_months(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(_segment("XA", flight_number="DL500"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL500 to XA"
        ]

    def test_exactly_6_months_triggers_no_alert(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(_segment("XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestNoAlert:
    """Valid passports and empty itineraries produce no alert."""

    def test_far_future_passport(self, requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        itinerary = make_itinerary(_segment("XA"), _segment("XB"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_empty_itinerary(self, requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        itinerary = make_itinerary()

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestSeverityConsolidationAcrossSegments:
    """Highest severity across segments wins, in either segment order."""

    def test_info_then_critical_escalates(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(
            _segment("XA", flight_number="DL600"),
            _segment("XB", flight_number="DL601"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
        assert "DL600" in result.reasons[0] and "6 months" in result.reasons[0]
        assert "DL601" in result.reasons[1] and "does not meet" in result.reasons[1]

    def test_critical_then_info_stays_critical(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(
            _segment("XB", flight_number="DL610"),
            _segment("XA", flight_number="DL611"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_multiple_info_stays_info(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(
            _segment("XA", flight_number="DL620"),
            _segment("XA", flight_number="DL621"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 2

    def test_before_departure_and_validity_failures_both_reported(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 2, 1))
        itinerary = make_itinerary(
            _segment("XA", flight_number="DL630"),
            _segment("XA", departure=date(2031, 3, 1), flight_number="DL631"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet" in result.reasons[0]
        assert result.reasons[1] == "Passport expires before departure on DL631 to XA"


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new dates/destinations."""

    def test_later_departure_turns_valid_trip_critical(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 8, 1))
        original = make_itinerary(_segment("XB", departure=DEPARTURE))
        changed = make_itinerary(_segment("XB", departure=date(2031, 6, 1)))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_destination_change_to_stricter_country(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        original = make_itinerary(_segment("XA"))
        changed = make_itinerary(_segment("XB"))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL


class TestRequirementsFallback:
    """Destinations missing from the requirements dict fall back to get_requirements."""

    def test_falls_back_to_country_table(self, swap_country_requirements):
        swap_country_requirements({"XC": _reqs("XC", 3)})
        profile = make_profile(passport_expiry=date(2031, 2, 15))
        itinerary = make_itinerary(_segment("XC", flight_number="DL700"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.reasons == ["Passport does not meet XC's 3-month validity requirement for DL700"]

    def test_unknown_country_uses_default_6_month_rule(self, swap_country_requirements):
        swap_country_requirements({})
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(_segment("XZ", flight_number="DL701"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport does not meet XZ's 6-month validity requirement for DL701"]

    def test_provided_requirements_take_precedence(self, requirements, swap_country_requirements):
        swap_country_requirements({"XA": _reqs("XA", 6)})
        profile = make_profile(passport_expiry=date(2031, 5, 1))
        itinerary = make_itinerary(_segment("XA"))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO
