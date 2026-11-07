"""Branch-coverage tests for the passport evaluator.

All "valid" dates are 2030+ so date-relative branches stay stable over time.
"""

from datetime import date

import pytest

from src.evaluators.passport_evaluator import _max_severity, evaluate_passport_expiry
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2031, 9, 1)
FAR_FUTURE_EXPIRY = date(2040, 1, 1)


def _reqs(country_code: str, months: int) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=False,
        transit_visa_required=False,
        passport_validity_months=months,
        visa_exempt_nationalities=[],
    )


@pytest.fixture
def requirements() -> dict[str, TravelDocRequirements]:
    """Fixture requirements independent of production COUNTRY_REQUIREMENTS."""
    return {
        "XA": _reqs("XA", 3),
        "XB": _reqs("XB", 6),
    }


class TestMissingPassportInfo:
    """Missing passport number or expiry short-circuits with a CRITICAL alert."""

    def test_missing_passport_number(self, requirements):
        profile = make_profile(passport_number=None)
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport information is incomplete"]
        assert len(result.validation_errors) == 1
        assert result.validation_errors[0].field == "passport_number"
        assert result.validation_errors[0].code == "MISSING_PASSPORT_INFO"

    def test_missing_passport_expiry(self, requirements):
        profile = make_profile(passport_expiry=None)
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.validation_errors[0].field == "passport_expiry"

    def test_missing_both_reports_passport_number_field(self, requirements):
        profile = make_profile(passport_number=None, passport_expiry=None)
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.validation_errors[0].field == "passport_number"

    def test_segments_still_reported_as_evaluated(self, requirements):
        segment = make_segment(destination="XA", departure=DEPARTURE)
        profile = make_profile(passport_number=None)
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.segments_evaluated == [segment]
        assert result.profile is profile


class TestAlreadyExpiredPassport:
    """A passport expired before today is CRITICAL regardless of segments."""

    def test_expired_passport_is_critical(self, requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport has expired"]
        assert result.validation_errors == []

    def test_expired_passport_short_circuits_segment_checks(self, requirements):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL1"),
            make_segment(destination="XB", departure=DEPARTURE, flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert len(result.reasons) == 1


class TestExpiresBeforeDeparture:
    """Passport valid today but expiring before a segment departs is CRITICAL."""

    def test_expires_before_departure(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 8, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL300")
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL300 to XA"]

    def test_expiry_on_departure_day_is_not_before_departure(self, requirements):
        profile = make_profile(passport_expiry=DEPARTURE)
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "expires before departure" not in result.reasons[0]
        assert "does not meet XA's 3-month" in result.reasons[0]

    def test_layover_segment_is_evaluated(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 8, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, is_layover=True, flight_number="DL5")
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL5 to XA"]


class TestCountryValidityWindow:
    """Validity below the destination's required months is CRITICAL."""

    def test_below_3_month_requirement(self, requirements):
        profile = make_profile(passport_expiry=date(2031, 11, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL10")
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Passport does not meet XA's 3-month validity requirement for DL10"
        ]

    def test_below_6_month_requirement(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 2, 1))
        itinerary = make_itinerary(make_segment(destination="XB", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet XB's 6-month" in result.reasons[0]

    def test_expiry_exactly_at_threshold_meets_requirement(self, requirements):
        # Departure + 6 months exactly: not below the threshold
        profile = make_profile(passport_expiry=date(2032, 3, 1))
        itinerary = make_itinerary(make_segment(destination="XB", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_requirements_override_changes_outcome(self, requirements):
        # Same passport/segment that passes XA's 3 months fails a 12-month override
        profile = make_profile(passport_expiry=date(2032, 6, 1))
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))
        override = {"XA": _reqs("XA", 12)}

        default_result = evaluate_passport_expiry(profile, itinerary, requirements)
        override_result = evaluate_passport_expiry(profile, itinerary, override)

        assert default_result.severity is None
        assert override_result.severity == AlertSeverity.CRITICAL
        assert "does not meet XA's 12-month" in override_result.reasons[0]


class TestSixMonthAdvisory:
    """Meeting a short country window but expiring within 6 months is INFO."""

    def test_info_when_within_six_months_of_travel(self, requirements):
        # 4 months after departure: meets XA's 3 months, under 6
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL20")
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL20 to XA"
        ]

    def test_no_info_when_beyond_six_months(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 3, 2))
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.reasons == []


class TestHappyPath:
    """A passport valid well beyond every threshold yields no alert."""

    def test_no_alert(self, requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL1"),
            make_segment(destination="XB", departure=date(2031, 9, 10), flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert len(result.segments_evaluated) == 2

    def test_empty_itinerary_has_no_alert(self, requirements):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)

        result = evaluate_passport_expiry(profile, make_itinerary(), requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestRequirementsFallback:
    """Destinations missing from the requirements dict use get_requirements."""

    def test_unknown_country_uses_6_month_default(self):
        # 4 months of validity fails the 6-month default
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(make_segment(destination="ZZ", departure=DEPARTURE))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "does not meet ZZ's 6-month" in result.reasons[0]

    def test_fallback_used_only_for_missing_destination(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL1"),
            make_segment(destination="ZZ", departure=DEPARTURE, flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons[0].startswith("Passport expiring within 6 months")
        assert "does not meet ZZ's 6-month" in result.reasons[1]


class TestMultiSegmentConsolidation:
    """Each segment adds its own reason; the highest severity wins."""

    def test_info_then_critical_escalates(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL1"),
            make_segment(destination="XB", departure=DEPARTURE, flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_stays_critical(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="XB", departure=DEPARTURE, flight_number="DL1"),
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_two_info_segments_stay_info(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=DEPARTURE, flight_number="DL1"),
            make_segment(destination="XA", departure=date(2031, 9, 15), flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.INFO
        assert len(result.reasons) == 2

    def test_return_leg_after_expiry_is_critical(self, requirements):
        # Outbound fine; return flight departs after the passport expires
        profile = make_profile(passport_expiry=date(2032, 6, 1))
        itinerary = make_itinerary(
            make_segment(destination="XA", departure=date(2031, 9, 1), flight_number="DL1"),
            make_segment(destination="XA", departure=date(2032, 7, 1), flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Passport expires before departure on DL2 to XA"]


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rebooking_later_flips_to_alert(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 6, 1))
        original = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="XA", departure=date(2032, 4, 1)))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_changing_destination_flips_to_alert(self, requirements):
        profile = make_profile(passport_expiry=date(2032, 1, 1))
        original = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="XB", departure=DEPARTURE))

        before = evaluate_passport_expiry(profile, original, requirements)
        after = evaluate_passport_expiry(profile, changed, requirements)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL


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
            (AlertSeverity.WARNING, AlertSeverity.INFO, AlertSeverity.WARNING),
            (AlertSeverity.INFO, AlertSeverity.INFO, AlertSeverity.INFO),
        ],
    )
    def test_higher_rank_wins(self, current, new, expected):
        assert _max_severity(current, new) == expected
