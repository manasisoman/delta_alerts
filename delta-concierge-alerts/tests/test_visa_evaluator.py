"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment, make_visa


DEPARTURE = date(2030, 6, 1)


class TestVisaNotRequired:
    """Destinations that do not require a visa are skipped entirely."""

    def test_country_without_visa_requirement(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CL", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverWithoutTransitVisa:
    """Layovers in countries without a transit visa requirement are skipped."""

    def test_layover_without_transit_visa_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExemptNationality:
    """Travelers whose nationality is visa-exempt for the destination are skipped."""

    def test_exempt_nationality_needs_no_visa(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(country_code="JP")]
        )
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_unlisted_destination_falls_back_to_default_requirements(self):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="ZZ", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=date(2022, 1, 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=DEPARTURE - timedelta(days=90))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expires exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires shortly after departure → INFO, unless beyond the window."""

    def test_visa_expires_inside_warning_window(self, base_requirements):
        visa = make_visa(
            country_code="CN",
            expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expires_outside_warning_window(self, base_requirements):
        visa = make_visa(
            country_code="CN",
            expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1),
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaSelection:
    """When several visas match a country, the latest expiry date wins."""

    def test_latest_expiry_visa_is_selected(self, base_requirements):
        expired = make_visa(country_code="CN", expiry=date(2022, 1, 1), visa_number="V1")
        valid = make_visa(country_code="CN", expiry=date(2035, 1, 1), visa_number="V2")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_latest_expiry_visa_is_selected_regardless_of_order(self, base_requirements):
        valid = make_visa(country_code="CN", expiry=date(2035, 1, 1), visa_number="V2")
        expired = make_visa(country_code="CN", expiry=date(2022, 1, 1), visa_number="V1")
        profile = make_profile(nationality="IN", visa_records=[valid, expired])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestConsolidatedSeverity:
    """Severity across segments is the highest one, regardless of segment order."""

    def test_info_then_critical_escalates(self, base_requirements):
        expiring = make_visa(
            country_code="JP",
            expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
            visa_number="V-JP",
        )
        profile = make_profile(nationality="IN", visa_records=[expiring])
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_is_not_downgraded_by_later_info(self, base_requirements):
        expiring = make_visa(
            country_code="JP",
            expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
            visa_number="V-JP",
        )
        profile = make_profile(nationality="IN", visa_records=[expiring])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons[0] == "No visa on file for CN"


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new departure dates."""

    def test_moving_departure_later_expires_the_visa(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=date(2030, 8, 1))
        profile = make_profile(nationality="IN", visa_records=[visa])

        original = evaluate_visa_requirements(
            profile,
            make_itinerary(make_segment(destination="CN", departure=date(2030, 6, 1))),
            base_requirements,
        )
        rebooked = evaluate_visa_requirements(
            profile,
            make_itinerary(make_segment(destination="CN", departure=date(2030, 9, 1))),
            base_requirements,
        )

        assert original.is_alert_required is False
        assert rebooked.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in rebooked.reasons


class TestEvaluationPayload:
    """The evaluation echoes back the inputs it was given."""

    def test_profile_and_segments_are_returned(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[VisaRecord("CN", "TOURIST", date(2020, 1, 1), date(2035, 1, 1), "V9")],
        )
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.profile is profile
        assert result.segments_evaluated == [segment]
        assert result.validation_errors == []
