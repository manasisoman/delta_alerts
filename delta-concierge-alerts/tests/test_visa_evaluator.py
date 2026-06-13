"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.config import VISA_EXPIRY_WARNING_DAYS
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


class TestNoVisaRequired:
    """Destination does not require a visa → no alert."""

    def test_requires_visa_false_skips_check(self):
        reqs = {
            "MX": TravelDocRequirements(
                country_code="MX",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            ),
        }
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="MX", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, reqs)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverWithoutTransitVisa:
    """Layover at a country that does not require a transit visa → skipped."""

    def test_layover_transit_visa_not_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExpiresOnDepartureDate:
    """Visa expiry equals departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
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
        assert "expires on date of travel" in result.reasons[0]


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within VISA_EXPIRY_WARNING_DAYS after departure → INFO."""

    def test_visa_expires_within_warning_days(self, base_requirements):
        departure = date(2026, 9, 1)
        expiry = departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=expiry,
            visa_number="V004",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert f"within {VISA_EXPIRY_WARNING_DAYS} days of travel" in result.reasons[0]

    def test_visa_outside_warning_window_no_alert(self, base_requirements):
        departure = date(2026, 9, 1)
        expiry = departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=expiry,
            visa_number="V005",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestMaxSeverityConsolidation:
    """Multi-segment itinerary keeps the highest severity across segments."""

    def test_critical_not_downgraded_by_info(self, base_requirements):
        departure = date(2026, 9, 1)

        # Segment 1: missing visa for CN → CRITICAL
        seg_critical = make_segment(destination="CN", departure=departure, flight_number="DL200")

        # Segment 2: JP visa within warning window → INFO
        jp_expiry = departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        jp_visa = VisaRecord(
            country_code="JP", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=jp_expiry,
            visa_number="V006",
        )
        seg_info = make_segment(destination="JP", departure=departure, flight_number="DL300")

        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        itinerary = make_itinerary(seg_critical, seg_info)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
