"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment

DEPARTURE = date(2030, 6, 1)


def make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    issue_date: date = date(2025, 1, 1),
    visa_number: str = "V001",
) -> VisaRecord:
    """Helper to build a VisaRecord with a far-future expiry by default."""
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


class TestHappyPath:
    """Valid visa well beyond the warning window → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa()])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == itinerary.segments
        assert result.profile is profile


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa(country_code="JP")])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V001",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE - timedelta(days=1), visa_number="V002")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expires exactly on departure → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within VISA_EXPIRY_WARNING_DAYS after departure → INFO."""

    def test_visa_expires_inside_window(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expires_on_window_boundary(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_just_outside_window(self, base_requirements):
        visa = make_visa(
            expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestSkippedSegments:
    """Segments that do not need a visa produce no reasons."""

    def test_country_not_requiring_visa_is_skipped(self):
        requirements = {
            "MX": TravelDocRequirements(
                country_code="MX",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        }
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="MX", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_visa_exempt_nationality_is_skipped(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestRequirementsFallback:
    """Destinations absent from the requirements dict fall back to defaults."""

    def test_unknown_country_falls_back_to_default_requirements(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="ZZ", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons

    def test_known_country_uses_static_requirements_when_not_overridden(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL


class TestGetRequirements:
    """get_requirements returns static entries or the shared default."""

    def test_known_country_returns_static_entry(self):
        code = next(iter(COUNTRY_REQUIREMENTS))

        result = get_requirements(code)

        assert result is COUNTRY_REQUIREMENTS[code]
        assert result.country_code == code

    def test_unknown_country_returns_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []


class TestMultiSegmentSeverity:
    """Consolidated severity is the highest across all segments."""

    def test_info_and_critical_yields_critical(self, base_requirements):
        info_visa = make_visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE + timedelta(days=3)),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_keeps_critical(self, base_requirements):
        info_visa = make_visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL

    def test_two_critical_segments_collect_both_reasons(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN", "No visa on file for JP"]


class TestMaxSeverity:
    """_max_severity returns the higher-ranked severity."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_replaces_current(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.WARNING) == AlertSeverity.CRITICAL

    def test_equal_severity_keeps_current(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING


class TestFindMatchingVisa:
    """Visa selection picks the record with the latest expiry for the country."""

    def test_no_records_returns_none(self):
        assert _find_matching_visa([], "CN") is None

    def test_no_matching_country_returns_none(self):
        assert _find_matching_visa([make_visa(country_code="JP")], "CN") is None

    def test_latest_expiry_wins_among_multiple_matches(self):
        old = make_visa(expiry_date=date(2022, 1, 1), visa_number="OLD")
        new = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        other = make_visa(country_code="JP", expiry_date=date(2040, 1, 1), visa_number="JP")

        result = _find_matching_visa([old, other, new], "CN")

        assert result is new

    def test_renewed_visa_suppresses_expired_alert(self, base_requirements):
        old = make_visa(expiry_date=date(2022, 1, 1), visa_number="OLD")
        new = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[old, new])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
