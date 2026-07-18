"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


def make_visa(
    country_code: str = "CN",
    visa_type: str = "TOURIST",
    issue_date: date = date(2025, 1, 1),
    expiry_date: date = date(2030, 1, 1),
    visa_number: str = "V001",
) -> VisaRecord:
    """Helper to build a VisaRecord with sensible defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


class TestVisaNotRequired:
    """Destination does not require a visa → no alert, segment skipped."""

    def test_requires_visa_false(self, base_requirements):
        base_requirements["DE"].requires_visa = False
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverTransitVisaNotRequired:
    """Layover where transit visa is not required → segment skipped."""

    def test_layover_no_transit_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="JP", departure=date(2030, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaExemptNationality:
    """Traveler nationality is visa-exempt for the destination → skipped."""

    def test_exempt_nationality(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


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
        visa = make_visa(expiry_date=date(2022, 1, 1), visa_number="V001")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = make_visa(expiry_date=date(2030, 7, 1), visa_number="V002")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravel:
    """Visa expires exactly on departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        departure = date(2030, 9, 1)
        visa = make_visa(expiry_date=departure, visa_number="V003")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within VISA_EXPIRY_WARNING_DAYS after travel → INFO."""

    def test_visa_expires_within_window(self, base_requirements):
        departure = date(2030, 9, 1)
        expiry = departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        visa = make_visa(expiry_date=expiry, visa_number="V004")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )


class TestValidVisa:
    """Visa valid well beyond the warning window → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        departure = date(2030, 9, 1)
        expiry = departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        visa = make_visa(expiry_date=expiry, visa_number="V005")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaSelection:
    """When multiple visas match a country, the latest expiry wins."""

    def test_latest_expiry_visa_selected(self, base_requirements):
        departure = date(2030, 9, 1)
        expired = make_visa(expiry_date=date(2022, 1, 1), visa_number="OLD")
        valid = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
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


class TestConsolidatedSeverity:
    """Consolidated severity is the highest across all segments."""

    def test_highest_severity_across_segments(self, base_requirements):
        departure = date(2030, 9, 1)
        info_visa = make_visa(
            country_code="JP",
            expiry_date=departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
            visa_number="JP-INFO",
        )
        # JP is visa-exempt for US/GB but not IN; requires a visa here.
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        info_segment = make_segment(destination="JP", departure=departure)
        critical_segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(info_segment, critical_segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_earlier_higher_severity_is_retained(self, base_requirements):
        departure = date(2030, 9, 1)
        info_visa = make_visa(
            country_code="JP",
            expiry_date=departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
            visa_number="JP-INFO",
        )
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        critical_segment = make_segment(destination="CN", departure=departure)
        info_segment = make_segment(destination="JP", departure=departure)
        itinerary = make_itinerary(critical_segment, info_segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
