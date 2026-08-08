"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment, make_visa


TRAVEL_DATE = date(2031, 3, 1)


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
        visa = make_visa(country_code="CN", expiry=date(2030, 7, 1), visa_number="V002")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2031, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=TRAVEL_DATE, visa_number="V003")
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expiring within VISA_EXPIRY_WARNING_DAYS of travel → INFO."""

    def test_visa_expires_inside_window(self, base_requirements):
        visa = make_visa(
            country_code="CN",
            expiry=TRAVEL_DATE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 5),
            visa_number="V004",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expires_exactly_on_window_boundary(self, base_requirements):
        visa = make_visa(
            country_code="CN",
            expiry=TRAVEL_DATE + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
            visa_number="V005",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_valid_beyond_window_is_clean(self, base_requirements):
        visa = make_visa(
            country_code="CN",
            expiry=TRAVEL_DATE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1),
            visa_number="V006",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []


class TestSkippedSegments:
    """Segments that need no visa check are skipped entirely."""

    def test_country_not_requiring_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CL", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=TRAVEL_DATE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_visa_exempt_nationality(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_non_layover_to_same_country_still_checked(self, base_requirements):
        """A non-layover DE segment for a non-exempt nationality is not skipped."""
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons


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
    """Destinations absent from the supplied mapping fall back to static data."""

    def test_falls_back_to_country_requirements(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is True
        assert "No visa on file for CN" in result.reasons


class TestVisaSelection:
    """When several visas match a country, the latest expiry wins."""

    def test_valid_visa_preferred_over_expired(self, base_requirements):
        expired = make_visa(
            country_code="CN", issue=date(2020, 1, 1), expiry=date(2022, 1, 1),
            visa_number="OLD",
        )
        valid = make_visa(country_code="CN", expiry=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_visas_for_other_countries_ignored(self, base_requirements):
        other = make_visa(country_code="JP", expiry=date(2035, 1, 1), visa_number="JP1")
        profile = make_profile(nationality="IN", visa_records=[other])
        itinerary = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestMultiSegmentSeverity:
    """Consolidated severity is the highest across all segments."""

    def test_info_then_critical_escalates(self, base_requirements):
        expiring = make_visa(
            country_code="CN",
            expiry=TRAVEL_DATE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1),
            visa_number="V007",
        )
        profile = make_profile(nationality="IN", visa_records=[expiring])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=TRAVEL_DATE),
            make_segment(destination="JP", departure=TRAVEL_DATE + timedelta(days=5)),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_keeps_critical(self, base_requirements):
        expiring = make_visa(
            country_code="JP",
            expiry=TRAVEL_DATE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 4),
            visa_number="V008",
        )
        profile = make_profile(nationality="IN", visa_records=[expiring])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=TRAVEL_DATE),
            make_segment(destination="JP", departure=TRAVEL_DATE + timedelta(days=5)),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons
        assert (
            f"Visa for JP expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_warning_not_downgraded_by_later_info(self, base_requirements):
        cn_visa = make_visa(country_code="CN", expiry=TRAVEL_DATE, visa_number="V009")
        jp_visa = make_visa(
            country_code="JP",
            expiry=TRAVEL_DATE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 4),
            visa_number="V010",
        )
        profile = make_profile(nationality="IN", visa_records=[cn_visa, jp_visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=TRAVEL_DATE),
            make_segment(destination="JP", departure=TRAVEL_DATE + timedelta(days=5)),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING

    def test_empty_itinerary_is_clean(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rebooking_later_makes_visa_insufficient(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=date(2031, 6, 1), visa_number="V011")
        profile = make_profile(nationality="IN", visa_records=[visa])

        original = make_itinerary(make_segment(destination="CN", departure=TRAVEL_DATE))
        before = evaluate_visa_requirements(profile, original, base_requirements)

        changed = make_itinerary(
            make_segment(destination="CN", departure=date(2031, 8, 1))
        )
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.is_alert_required is True
        assert after.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in after.reasons

    def test_rebooking_adds_layover_requiring_transit_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])

        original = make_itinerary(make_segment(destination="CL", departure=TRAVEL_DATE))
        before = evaluate_visa_requirements(profile, original, base_requirements)

        changed = make_itinerary(
            make_segment(destination="CN", departure=TRAVEL_DATE, is_layover=True),
            make_segment(destination="CL", departure=TRAVEL_DATE + timedelta(days=1)),
        )
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["No visa on file for CN"]
        assert len(after.segments_evaluated) == 2
