"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.data import country_requirements
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)
ARRIVAL = date(2030, 9, 2)


def _visa(country_code: str = "CN", expiry: date = date(2032, 1, 1), **overrides) -> VisaRecord:
    """Build a VisaRecord with far-future defaults."""
    fields = {
        "country_code": country_code,
        "visa_type": "TOURIST",
        "issue_date": date(2025, 1, 1),
        "expiry_date": expiry,
        "visa_number": "V100",
    }
    fields.update(overrides)
    return VisaRecord(**fields)


def _segment(destination: str = "CN", departure: date = DEPARTURE, **overrides):
    """Build a FlightSegment with far-future dates."""
    overrides.setdefault("arrival", departure + timedelta(days=1))
    return make_segment(destination=destination, departure=departure, **overrides)


def _evaluate(requirements, visa_records=None, segments=None, nationality: str = "IN"):
    profile = make_profile(nationality=nationality, visa_records=visa_records or [])
    itinerary = make_itinerary(*(segments or [_segment()]))
    return evaluate_visa_requirements(profile, itinerary, requirements)


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        visas = [_visa(country_code="JP")]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2018, 1, 1), expiry_date=date(2020, 1, 1),
            visa_number="V001",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons

    def test_visa_expires_one_day_before_departure(self, base_requirements):
        visas = [_visa(expiry=DEPARTURE - timedelta(days=1))]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visas = [_visa(expiry=DEPARTURE)]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestVisaExpiryWarningWindow:
    """Visa expiring within VISA_EXPIRY_WARNING_DAYS after departure → INFO."""

    def test_visa_expires_day_after_departure(self, base_requirements):
        visas = [_visa(expiry=DEPARTURE + timedelta(days=1))]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_visa_expires_exactly_at_warning_threshold(self, base_requirements):
        visas = [_visa(expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_after_warning_threshold(self, base_requirements):
        visas = [_visa(expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1))]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestValidVisaHappyPath:
    """A valid, far-future visa produces no alert and echoes inputs."""

    def test_valid_visa_no_alert(self, base_requirements):
        visas = [_visa(expiry=date(2035, 1, 1))]
        segment = _segment()
        profile = make_profile(nationality="IN", visa_records=visas)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.profile is profile
        assert result.segments_evaluated == [segment]

    def test_empty_itinerary_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaNotRequired:
    """Destinations that do not require a visa are skipped entirely."""

    def test_country_not_requiring_visa_skipped(self):
        requirements = {
            "XA": TravelDocRequirements(
                country_code="XA",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        }

        result = _evaluate(requirements, segments=[_segment(destination="XA")])

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExemptNationality:
    """Travelers whose nationality is exempt need no visa."""

    def test_exempt_nationality_skipped(self, base_requirements):
        result = _evaluate(
            base_requirements, nationality="US", segments=[_segment(destination="DE")]
        )

        assert result.is_alert_required is False

    def test_non_exempt_nationality_alerts(self, base_requirements):
        result = _evaluate(
            base_requirements, nationality="IN", segments=[_segment(destination="DE")]
        )

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for DE"]


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="CN", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestLayoverWithoutTransitVisa:
    """Layovers in countries without a transit-visa rule are skipped."""

    def test_layover_skipped_when_transit_visa_not_required(self, base_requirements):
        segment = _segment(destination="JP", is_layover=True)

        result = _evaluate(base_requirements, segments=[segment])

        assert result.is_alert_required is False
        assert result.severity is None

    def test_non_layover_same_country_alerts(self, base_requirements):
        segment = _segment(destination="JP", is_layover=False)

        result = _evaluate(base_requirements, segments=[segment])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for JP"]


class TestBestVisaSelection:
    """With multiple visas for a country, the latest-expiring one is used."""

    def test_valid_visa_wins_over_expired(self, base_requirements):
        visas = [
            _visa(expiry=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry=date(2035, 1, 1), visa_number="NEW"),
        ]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.is_alert_required is False

    def test_find_matching_visa_returns_latest_expiry(self):
        old = _visa(expiry=date(2031, 1, 1), visa_number="OLD")
        new = _visa(expiry=date(2033, 1, 1), visa_number="NEW")
        other = _visa(country_code="JP", expiry=date(2040, 1, 1), visa_number="JP")

        match = _find_matching_visa([new, other, old], "CN")

        assert match is new

    def test_find_matching_visa_none_when_no_match(self):
        result = _find_matching_visa([_visa(country_code="JP")], "CN")

        assert result is None


class TestMultiSegmentConsolidation:
    """Severity is the highest across segments; reasons accumulate in order."""

    def test_critical_overrides_info(self, base_requirements):
        visas = [_visa(country_code="CN", expiry=DEPARTURE + timedelta(days=5))]
        segments = [_segment(destination="CN"), _segment(destination="JP")]

        result = _evaluate(base_requirements, visa_records=visas, segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "No visa on file for JP",
        ]

    def test_critical_not_downgraded_by_later_warning(self, base_requirements):
        visas = [_visa(country_code="JP", expiry=DEPARTURE)]
        segments = [_segment(destination="CN"), _segment(destination="JP")]

        result = _evaluate(base_requirements, visa_records=visas, segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "No visa on file for CN",
            "Visa for JP expires on date of travel",
        ]

    def test_itinerary_change_reevaluates_new_destination(self, base_requirements):
        visas = [_visa(country_code="JP", expiry=date(2035, 1, 1))]

        original = _evaluate(
            base_requirements, visa_records=visas, segments=[_segment(destination="JP")]
        )
        changed = _evaluate(
            base_requirements, visa_records=visas, segments=[_segment(destination="CN")]
        )

        assert original.is_alert_required is False
        assert changed.severity == AlertSeverity.CRITICAL
        assert changed.reasons == ["No visa on file for CN"]


class TestMaxSeverity:
    """_max_severity keeps the higher-ranked severity."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_replaces_current(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.WARNING) == AlertSeverity.CRITICAL

    def test_equal_severity_keeps_current(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING


class TestRequirementsFallback:
    """Destinations missing from the requirements dict fall back to get_requirements."""

    def test_fallback_uses_country_requirements_table(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {
            "XB": TravelDocRequirements(
                country_code="XB",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        }
        try:
            result = _evaluate({}, segments=[_segment(destination="XB")])
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

        assert result.is_alert_required is False

    def test_fallback_default_requires_visa_for_unknown_country(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {}
        try:
            result = _evaluate({}, segments=[_segment(destination="ZZ")])
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for ZZ"]

    def test_fallback_default_skips_layover(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {}
        try:
            result = _evaluate({}, segments=[_segment(destination="ZZ", is_layover=True)])
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

        assert result.is_alert_required is False
