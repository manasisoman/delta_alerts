"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment

TODAY = date.today()
DEPARTURE = TODAY + timedelta(days=120)


def make_visa(
    country_code: str = "CN",
    expiry_date: date = DEPARTURE + timedelta(days=365),
    issue_date: date = TODAY - timedelta(days=365),
    visa_type: str = "TOURIST",
    visa_number: str = "V000",
) -> VisaRecord:
    """Build a VisaRecord with defaults valid well past the travel date."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
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
            issue_date=TODAY - timedelta(days=365),
            expiry_date=DEPARTURE - timedelta(days=10),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE)
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
    """Valid visa well beyond travel date → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa()])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == itinerary.segments


class TestSkippedSegments:
    """Segments that need no visa check are skipped entirely."""

    def test_country_not_requiring_visa(self):
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

    def test_layover_without_transit_visa_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_visa_exempt_nationality(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_unknown_destination_falls_back_to_default_requirements(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="ZZ", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=DEPARTURE)]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expiring inside the warning window after travel → INFO."""

    def test_visa_expires_inside_window(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1)
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=expiry)]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expires_exactly_on_window_boundary(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=expiry)]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_just_outside_window(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=expiry)]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaSelection:
    """When multiple visas match a country, the latest expiry wins."""

    def test_latest_expiry_visa_is_selected(self, base_requirements):
        expired = make_visa(expiry_date=TODAY - timedelta(days=30), visa_number="V-OLD")
        valid = make_visa(visa_number="V-NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_visa_for_other_country_is_not_matched(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa(country_code="JP")])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestMultiSegmentItineraries:
    """Consolidated severity is the highest across all segments."""

    def test_critical_outranks_info(self, base_requirements):
        info_visa = make_visa(
            country_code="JP",
            expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1),
        )
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE + timedelta(days=3)),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_outranks_info(self, base_requirements):
        second_departure = DEPARTURE + timedelta(days=3)
        profile = make_profile(
            nationality="IN",
            visa_records=[
                make_visa(
                    country_code="JP",
                    expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1),
                ),
                make_visa(country_code="CN", expiry_date=second_departure),
            ],
        )
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=second_departure),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING

    def test_later_lower_severity_does_not_downgrade(self, base_requirements):
        second_departure = DEPARTURE + timedelta(days=3)
        profile = make_profile(
            nationality="IN",
            visa_records=[
                make_visa(
                    country_code="CN",
                    expiry_date=second_departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1),
                )
            ],
        )
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=second_departure),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL

    def test_empty_itinerary_produces_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])

        result = evaluate_visa_requirements(profile, make_itinerary(), base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []
