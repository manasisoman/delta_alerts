"""Branch-coverage tests for the visa evaluator.

All "valid" dates are 2030+ and all "expired" dates are pre-2025 so the
date-relative branches stay stable over time. Requirements use made-up
country codes (XA/XB/XC/XD) to isolate tests from production data, except
where the ``get_requirements`` fallback is under test.
"""

from datetime import date, timedelta

import pytest

import src.data.country_requirements as country_requirements
from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2031, 6, 1)


def _requirements(
    code: str,
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    visa_exempt_nationalities: list[str] | None = None,
) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=6,
        visa_exempt_nationalities=visa_exempt_nationalities or [],
    )


@pytest.fixture
def requirements() -> dict[str, TravelDocRequirements]:
    """Made-up countries covering each requirement combination."""
    return {
        # Visa required, transit visa required, nobody exempt
        "XA": _requirements("XA", transit_visa_required=True),
        # Visa required, no transit visa, US/GB exempt
        "XB": _requirements("XB", visa_exempt_nationalities=["US", "GB"]),
        # No visa required at all
        "XC": _requirements("XC", requires_visa=False),
        # Visa required, transit visa not required, nobody exempt
        "XD": _requirements("XD"),
    }


def _visa(
    country_code: str = "XA",
    expiry_date: date = date(2035, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2024, 1, 1),
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


def _segment(
    destination: str = "XA",
    departure: date = DEPARTURE,
    is_layover: bool = False,
    flight_number: str = "DL100",
):
    return make_segment(
        destination=destination,
        departure=departure,
        arrival=departure + timedelta(days=1),
        is_layover=is_layover,
        flight_number=flight_number,
    )


def _evaluate(requirements, *segments, nationality="IN", visa_records=None):
    profile = make_profile(nationality=nationality, visa_records=visa_records or [])
    itinerary = make_itinerary(*(segments or (_segment(),)))
    return evaluate_visa_requirements(profile, itinerary, requirements)


class TestHappyPath:
    """A valid visa far beyond the warning window produces no alert."""

    def test_valid_visa_no_alert(self, requirements):
        result = _evaluate(requirements, visa_records=[_visa()])

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_result_echoes_profile_and_segments(self, requirements):
        profile = make_profile(visa_records=[_visa()])
        segment = _segment()
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.profile is profile
        assert result.segments_evaluated == [segment]

    def test_empty_itinerary_no_alert(self, requirements):
        profile = make_profile()
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestVisaNotRequired:
    """Destinations with requires_visa=False are skipped entirely."""

    def test_no_visa_required_country_skipped(self, requirements):
        result = _evaluate(requirements, _segment(destination="XC"))

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_no_visa_required_layover_skipped(self, requirements):
        result = _evaluate(requirements, _segment(destination="XC", is_layover=True))

        assert result.is_alert_required is False


class TestLayovers:
    """Layovers only need a visa where a transit visa is required."""

    def test_layover_without_transit_requirement_skipped(self, requirements):
        result = _evaluate(requirements, _segment(destination="XD", is_layover=True))

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_layover_with_transit_requirement_missing_visa_critical(self, requirements):
        result = _evaluate(requirements, _segment(destination="XA", is_layover=True))

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XA"]

    def test_layover_with_transit_requirement_valid_visa_no_alert(self, requirements):
        result = _evaluate(
            requirements,
            _segment(destination="XA", is_layover=True),
            visa_records=[_visa("XA")],
        )

        assert result.is_alert_required is False

    def test_non_layover_same_country_still_requires_visa(self, requirements):
        result = _evaluate(requirements, _segment(destination="XD", is_layover=False))

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XD"]


class TestVisaExemptNationality:
    """Exempt nationalities need no visa; others still do."""

    @pytest.mark.parametrize("nationality", ["US", "GB"])
    def test_exempt_nationality_skipped(self, requirements, nationality):
        result = _evaluate(requirements, _segment(destination="XB"), nationality=nationality)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_non_exempt_nationality_alerts(self, requirements):
        result = _evaluate(requirements, _segment(destination="XB"), nationality="IN")

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XB"]


class TestMissingVisa:
    """No matching visa record for a visa-required destination → CRITICAL."""

    def test_no_visa_records(self, requirements):
        result = _evaluate(requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XA"]

    def test_visa_for_other_country_does_not_match(self, requirements):
        result = _evaluate(requirements, visa_records=[_visa("XD")])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XA"]


class TestExpiredVisa:
    """Visa already expired relative to today → CRITICAL."""

    def test_visa_expired(self, requirements):
        result = _evaluate(requirements, visa_records=[_visa(expiry_date=date(2020, 1, 1))])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for XA has expired"]

    def test_visa_expiring_today_is_not_expired(self, requirements):
        today = date.today()
        result = _evaluate(
            requirements,
            _segment(departure=today),
            visa_records=[_visa(expiry_date=today)],
        )

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for XA expires on date of travel"]


class TestVisaExpiresBeforeTravel:
    """Visa valid today but expiring before departure → CRITICAL."""

    def test_expires_before_departure(self, requirements):
        result = _evaluate(requirements, visa_records=[_visa(expiry_date=date(2031, 1, 1))])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for XA expires before date of travel"]

    def test_expires_one_day_before_departure(self, requirements):
        result = _evaluate(
            requirements, visa_records=[_visa(expiry_date=DEPARTURE - timedelta(days=1))]
        )

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for XA expires before date of travel"]


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_expires_on_departure(self, requirements):
        result = _evaluate(requirements, visa_records=[_visa(expiry_date=DEPARTURE)])

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for XA expires on date of travel"]


class TestVisaExpiryWarningWindow:
    """Visa expiring within VISA_EXPIRY_WARNING_DAYS of departure → INFO."""

    def test_expires_day_after_departure(self, requirements):
        result = _evaluate(
            requirements, visa_records=[_visa(expiry_date=DEPARTURE + timedelta(days=1))]
        )

        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for XA expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_expires_exactly_at_threshold(self, requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)

        result = _evaluate(requirements, visa_records=[_visa(expiry_date=expiry)])

        assert result.severity == AlertSeverity.INFO
        assert result.is_alert_required is True

    def test_expires_day_after_threshold_no_alert(self, requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)

        result = _evaluate(requirements, visa_records=[_visa(expiry_date=expiry)])

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaSelection:
    """When several visas match, the latest-expiring record is used."""

    def test_valid_visa_preferred_over_expired(self, requirements):
        visas = [
            _visa(expiry_date=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry_date=date(2035, 1, 1), visa_number="NEW"),
        ]

        result = _evaluate(requirements, visa_records=visas)

        assert result.is_alert_required is False

    def test_order_of_records_does_not_matter(self, requirements):
        visas = [
            _visa(expiry_date=date(2035, 1, 1), visa_number="NEW"),
            _visa(expiry_date=date(2020, 1, 1), visa_number="OLD"),
        ]

        result = _evaluate(requirements, visa_records=visas)

        assert result.is_alert_required is False

    def test_find_matching_visa_returns_latest_expiry(self):
        records = [
            _visa("XA", date(2031, 1, 1), "A1"),
            _visa("XA", date(2034, 1, 1), "A2"),
            _visa("XD", date(2040, 1, 1), "D1"),
            _visa("XA", date(2032, 1, 1), "A3"),
        ]

        match = _find_matching_visa(records, "XA")

        assert match.visa_number == "A2"

    def test_find_matching_visa_none_when_no_match(self):
        records = [_visa("XD")]

        assert _find_matching_visa(records, "XA") is None

    def test_find_matching_visa_empty_records(self):
        assert _find_matching_visa([], "XA") is None


class TestMultiSegmentConsolidation:
    """Reasons accumulate per segment; severity is the highest seen."""

    def test_critical_overrides_warning_and_info(self, requirements):
        visas = [
            _visa("XA", DEPARTURE),  # WARNING
            _visa("XD", DEPARTURE + timedelta(days=5)),  # INFO
        ]
        segments = (
            _segment("XA", flight_number="DL1"),
            _segment("XD", flight_number="DL2"),
            _segment("XB", flight_number="DL3"),  # CRITICAL: no visa
        )

        result = _evaluate(requirements, *segments, visa_records=visas)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Visa for XA expires on date of travel",
            f"Visa for XD expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "No visa on file for XB",
        ]

    def test_later_lower_severity_does_not_downgrade(self, requirements):
        visas = [_visa("XD", DEPARTURE + timedelta(days=5))]  # INFO
        segments = (
            _segment("XA", flight_number="DL1"),  # CRITICAL: no visa
            _segment("XD", flight_number="DL2"),
        )

        result = _evaluate(requirements, *segments, visa_records=visas)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_overrides_info(self, requirements):
        visas = [
            _visa("XD", DEPARTURE + timedelta(days=5)),  # INFO
            _visa("XA", DEPARTURE),  # WARNING
        ]
        segments = (_segment("XD", flight_number="DL1"), _segment("XA", flight_number="DL2"))

        result = _evaluate(requirements, *segments, visa_records=visas)

        assert result.severity == AlertSeverity.WARNING

    def test_skipped_segments_do_not_affect_result(self, requirements):
        segments = (
            _segment("XC", flight_number="DL1"),
            _segment("XD", is_layover=True, flight_number="DL2"),
            _segment("XA", flight_number="DL3"),
        )

        result = _evaluate(requirements, *segments, visa_records=[_visa("XA")])

        assert result.is_alert_required is False
        assert len(result.segments_evaluated) == 3

    def test_same_country_on_two_dates_checked_per_segment(self, requirements):
        visa = _visa("XA", DEPARTURE + timedelta(days=10))
        segments = (
            _segment("XA", departure=DEPARTURE, flight_number="DL1"),  # INFO
            _segment("XA", departure=DEPARTURE + timedelta(days=20), flight_number="DL2"),
        )

        result = _evaluate(requirements, *segments, visa_records=[visa])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            f"Visa for XA expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "Visa for XA expires before date of travel",
        ]


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rebooked_to_later_date_flags_visa(self, requirements):
        visa = _visa("XA", DEPARTURE + timedelta(days=60))
        original = _evaluate(requirements, _segment("XA"), visa_records=[visa])

        changed = _evaluate(
            requirements,
            _segment("XA", departure=DEPARTURE + timedelta(days=90)),
            visa_records=[visa],
        )

        assert original.is_alert_required is False
        assert changed.severity == AlertSeverity.CRITICAL
        assert changed.reasons == ["Visa for XA expires before date of travel"]

    def test_rerouted_to_visa_free_country_clears_alert(self, requirements):
        original = _evaluate(requirements, _segment("XA"))

        changed = _evaluate(requirements, _segment("XC"))

        assert original.severity == AlertSeverity.CRITICAL
        assert changed.is_alert_required is False


class TestRequirementsFallback:
    """Destinations missing from `requirements` fall back to get_requirements."""

    @pytest.fixture
    def swapped_country_requirements(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {
            "YA": _requirements("YA", requires_visa=False),
            "YB": _requirements("YB", visa_exempt_nationalities=["IN"]),
        }
        try:
            yield
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

    def test_fallback_country_not_requiring_visa(self, swapped_country_requirements):
        result = _evaluate({}, _segment("YA"))

        assert result.is_alert_required is False

    def test_fallback_country_exempt_nationality(self, swapped_country_requirements):
        result = _evaluate({}, _segment("YB"), nationality="IN")

        assert result.is_alert_required is False

    def test_unknown_country_uses_default_visa_required(self, swapped_country_requirements):
        result = _evaluate({}, _segment("ZZ"))

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for ZZ"]

    def test_unknown_country_layover_uses_default_no_transit(
        self, swapped_country_requirements
    ):
        result = _evaluate({}, _segment("ZZ", is_layover=True))

        assert result.is_alert_required is False

    def test_explicit_requirements_take_precedence(self, swapped_country_requirements):
        reqs = {"YA": _requirements("YA", requires_visa=True)}

        result = _evaluate(reqs, _segment("YA"))

        assert result.severity == AlertSeverity.CRITICAL


class TestMaxSeverity:
    """_max_severity returns the higher-ranked severity."""

    @pytest.mark.parametrize("new", list(AlertSeverity))
    def test_none_current_returns_new(self, new):
        assert _max_severity(None, new) == new

    @pytest.mark.parametrize(
        "current, new, expected",
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
    def test_higher_severity_wins(self, current, new, expected):
        assert _max_severity(current, new) == expected
