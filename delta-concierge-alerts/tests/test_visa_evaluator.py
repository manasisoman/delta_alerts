"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.evaluators.visa_evaluator import evaluate_visa_requirements
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
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
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


class TestCountryDoesNotRequireVisa:
    """Destination does not require a visa → no alert (skip path, line 56)."""

    def test_no_visa_required_skips_evaluation(self, base_requirements):
        base_requirements["CL"] = TravelDocRequirements(
            country_code="CL",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CL", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverWithoutTransitVisaRequired:
    """Layover to country where transit_visa_required=False → no alert (skip path, line 60)."""

    def test_layover_no_transit_visa_skips(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaExemptNationality:
    """Traveler nationality is visa-exempt for destination → no alert."""

    def test_exempt_nationality_skips(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaExpiresOnDepartureDate:
    """Visa expires exactly on departure date → WARNING (lines 87-90)."""

    def test_visa_expires_on_departure_date(self, base_requirements):
        departure = date(2026, 9, 1)
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=departure,
            visa_number="V003",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within 30 days of travel → INFO (lines 92-98)."""

    def test_visa_expires_within_30_days_of_travel(self, base_requirements):
        departure = date(2026, 9, 1)
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=departure + timedelta(days=15),
            visa_number="V004",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert "Visa for CN expires within 30 days of travel" in result.reasons


class TestSeverityConsolidation:
    """Multi-segment trip: highest severity wins (lines 132-134)."""

    def test_critical_plus_info_returns_critical(self, base_requirements):
        departure = date(2026, 9, 1)
        # CN segment: no visa → CRITICAL
        seg_critical = make_segment(destination="CN", departure=departure)
        # JP segment: visa expires within 30 days → INFO
        jp_visa = VisaRecord(
            country_code="JP", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=departure + timedelta(days=10),
            visa_number="V005",
        )
        seg_info = make_segment(destination="JP", departure=departure, flight_number="DL200")
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        itinerary = make_itinerary(seg_critical, seg_info)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_info_then_critical_escalates(self, base_requirements):
        """INFO first, then CRITICAL → severity escalates to CRITICAL (line 133)."""
        departure = date(2026, 9, 1)
        # JP segment first: visa expires within 30 days → INFO
        jp_visa = VisaRecord(
            country_code="JP", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=departure + timedelta(days=10),
            visa_number="V005",
        )
        seg_info = make_segment(destination="JP", departure=departure, flight_number="DL200")
        # CN segment second: no visa → CRITICAL
        seg_critical = make_segment(destination="CN", departure=departure)
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        itinerary = make_itinerary(seg_info, seg_critical)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
