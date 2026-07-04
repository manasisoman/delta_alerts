"""Critical-path tests for the visa evaluator."""

from datetime import date

from src.evaluators.visa_evaluator import (
    evaluate_visa_requirements,
    _max_severity,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


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
            issue_date=date(2025, 1, 1), expiry_date=date(2028, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2028, 9, 1))
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


class TestSkipNoVisaRequired:
    """Country does not require a visa → no alert."""

    def test_requires_visa_false_skips(self, base_requirements):
        base_requirements["CL"] = TravelDocRequirements(
            country_code="CL", requires_visa=False,
            transit_visa_required=False, passport_validity_months=6,
            visa_exempt_nationalities=[],
        )
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CL", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestSkipLayoverNoTransitVisaRequired:
    """Layover at a country that does not require transit visa → skip."""

    def test_layover_transit_visa_not_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 6, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExemptNationality:
    """Traveler's nationality is visa-exempt for the destination → skip."""

    def test_exempt_nationality_skips(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExpiresOnTravelDate:
    """Visa expires on the departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 6, 1),
            visa_number="V003",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within 30 days of travel → INFO."""

    def test_visa_expires_within_30_days(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 6, 15),
            visa_number="V004",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert "Visa for CN expires within 30 days of travel" in result.reasons

    def test_visa_expires_exactly_at_30_day_boundary(self, base_requirements):
        """Visa expiry == departure + 30 days → still INFO (<=)."""
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 7, 1),
            visa_number="V005",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert "Visa for CN expires within 30 days of travel" in result.reasons

    def test_visa_expires_beyond_warning_window(self, base_requirements):
        """Visa expiry > departure + 30 days → no alert."""
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2031, 1, 1),
            visa_number="V006",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestMaxSeverityHelper:
    """_max_severity returns the higher of two severity values."""

    def test_none_returns_new(self):
        result = _max_severity(None, AlertSeverity.WARNING)

        assert result == AlertSeverity.WARNING

    def test_higher_replaces_lower(self):
        result = _max_severity(AlertSeverity.INFO, AlertSeverity.CRITICAL)

        assert result == AlertSeverity.CRITICAL

    def test_lower_does_not_replace_higher(self):
        result = _max_severity(AlertSeverity.CRITICAL, AlertSeverity.INFO)

        assert result == AlertSeverity.CRITICAL

    def test_equal_severity_unchanged(self):
        result = _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING)

        assert result == AlertSeverity.WARNING


class TestFallbackToGetRequirements:
    """Destination not in provided requirements dict → falls back to get_requirements."""

    def test_unknown_destination_uses_default_requirements(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="XX", departure=date(2030, 6, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for XX" in result.reasons
