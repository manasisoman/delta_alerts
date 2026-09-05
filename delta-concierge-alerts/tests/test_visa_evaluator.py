"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS

from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)


def _make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    issue_date: date = date(2025, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    """Build a VisaRecord with far-future validity by default."""
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


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
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestHappyPath:
    """Valid visa well beyond the warning window → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_make_visa()])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == [segment]
        assert result.profile is profile

    def test_empty_itinerary_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestSkippedSegments:
    """Segments that do not need a visa check produce no reasons."""

    def test_country_not_requiring_visa_is_skipped(self):
        requirements = {
            "CL": TravelDocRequirements(
                country_code="CL",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        }
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CL", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="JP", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_non_layover_without_transit_visa_requirement_still_checked(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="JP", departure=DEPARTURE, is_layover=False)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for JP" in result.reasons

    def test_visa_exempt_nationality_is_skipped(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []


class TestRequirementsFallback:
    """Destinations missing from the requirements dict fall back to defaults."""

    def test_known_country_falls_back_to_country_requirements(self):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_unknown_country_falls_back_to_default_requirements(self):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="XX", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for XX" in result.reasons

    def test_unknown_country_layover_uses_default_no_transit_visa(self):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="XX", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestVisaExpiresOnTravelDate:
    """Visa expiry equals departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = _make_visa(expiry_date=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within 30 days after departure → INFO; beyond → no alert."""

    def test_visa_expires_inside_window(self, base_requirements):
        visa = _make_visa(expiry_date=DEPARTURE + timedelta(days=10))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel" in result.reasons

    def test_visa_expires_exactly_at_window_boundary(self, base_requirements):
        visa = _make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_one_day_past_window(self, base_requirements):
        visa = _make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestSeverityConsolidation:
    """Multiple segments → highest severity wins and all reasons are collected."""

    def test_critical_overrides_info_across_segments(self, base_requirements):
        cn_visa = _make_visa(country_code="CN", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[cn_visa])
        info_segment = make_segment(destination="CN", departure=DEPARTURE)
        critical_segment = make_segment(destination="JP", departure=DEPARTURE)
        itinerary = make_itinerary(info_segment, critical_segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
        assert "No visa on file for JP" in result.reasons

    def test_warning_not_downgraded_by_later_info(self, base_requirements):
        cn_visa = _make_visa(country_code="CN", expiry_date=DEPARTURE)
        jp_visa = _make_visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[cn_visa, jp_visa])
        warning_segment = make_segment(destination="CN", departure=DEPARTURE)
        info_segment = make_segment(destination="JP", departure=DEPARTURE)
        itinerary = make_itinerary(warning_segment, info_segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert len(result.reasons) == 2

    def test_itinerary_change_to_later_departure_clears_alert(self, base_requirements):
        visa = _make_visa(expiry_date=DEPARTURE + timedelta(days=10))
        profile = make_profile(nationality="IN", visa_records=[visa])
        original = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))
        changed = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE - timedelta(days=60))
        )

        original_result = evaluate_visa_requirements(profile, original, base_requirements)
        changed_result = evaluate_visa_requirements(profile, changed, base_requirements)

        assert original_result.severity == AlertSeverity.INFO
        assert changed_result.is_alert_required is False


class TestVisaSelection:
    """When multiple visas match a country, the latest-expiring one is used."""

    def test_latest_expiry_visa_selected_over_expired(self, base_requirements):
        expired = _make_visa(expiry_date=date(2020, 1, 1), visa_number="OLD")
        valid = _make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_find_matching_visa_returns_latest_expiry(self):
        older = _make_visa(expiry_date=date(2031, 1, 1), visa_number="A")
        newer = _make_visa(expiry_date=date(2033, 1, 1), visa_number="B")
        other_country = _make_visa(country_code="JP", expiry_date=date(2040, 1, 1), visa_number="C")

        match = _find_matching_visa([older, other_country, newer], "CN")

        assert match is newer

    def test_find_matching_visa_returns_none_without_match(self):
        visa = _make_visa(country_code="JP")

        match = _find_matching_visa([visa], "CN")

        assert match is None

    def test_find_matching_visa_empty_records(self):
        assert _find_matching_visa([], "CN") is None


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
