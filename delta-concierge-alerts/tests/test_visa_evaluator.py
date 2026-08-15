"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


FAR_FUTURE_DEPARTURE = date(2030, 6, 1)


def make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    visa_type: str = "TOURIST",
    issue_date: date = date(2024, 1, 1),
    visa_number: str = "V001",
) -> VisaRecord:
    """Build a VisaRecord with sensible, far-future-valid defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


class TestHappyPath:
    """A valid, long-dated visa produces no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa()])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.segments_evaluated == itinerary.segments


class TestSkippedSegments:
    """Segments that do not need a visa check produce no reasons."""

    def test_country_does_not_require_visa(self, base_requirements):
        requirements = dict(base_requirements)
        requirements["FR"] = TravelDocRequirements(
            country_code="FR",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        )
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="FR", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(
                destination="DE", departure=FAR_FUTURE_DEPARTURE, is_layover=True
            )
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_visa_exempt_nationality(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_unknown_destination_falls_back_to_default_requirements(
        self, base_requirements
    ):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="ZZ", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(country_code="JP")]
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = make_visa(
            expiry_date=date(2022, 1, 1),
            issue_date=date(2020, 1, 1),
            visa_number="V001",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = make_visa(expiry_date=date(2030, 1, 1), visa_number="V002")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2031, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure_date(self, base_requirements):
        visa = make_visa(expiry_date=FAR_FUTURE_DEPARTURE, visa_number="V003")
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiryWarningWindow:
    """Visa expiring within VISA_EXPIRY_WARNING_DAYS of travel → INFO."""

    def test_visa_expires_inside_warning_window(self, base_requirements):
        expiry = FAR_FUTURE_DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1)
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=expiry)]
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expires_on_warning_boundary(self, base_requirements):
        expiry = FAR_FUTURE_DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=expiry)]
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_just_outside_warning_window(self, base_requirements):
        expiry = FAR_FUTURE_DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        profile = make_profile(
            nationality="IN", visa_records=[make_visa(expiry_date=expiry)]
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="CN", departure=FAR_FUTURE_DEPARTURE, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestVisaRecordSelection:
    """When several visas match a country, the latest expiry wins."""

    def test_latest_expiry_visa_is_selected(self, base_requirements):
        expired = make_visa(expiry_date=date(2021, 1, 1), visa_number="OLD")
        valid = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_latest_expiry_still_insufficient(self, base_requirements):
        older = make_visa(expiry_date=date(2020, 1, 1), visa_number="OLD")
        newer = make_visa(expiry_date=date(2021, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[older, newer])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN has expired"]


class TestConsolidatedSeverity:
    """Consolidated severity is the highest across all segments."""

    def test_critical_outranks_warning(self, base_requirements):
        jp_visa = make_visa(
            country_code="JP", expiry_date=FAR_FUTURE_DEPARTURE, visa_number="JP1"
        )
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=FAR_FUTURE_DEPARTURE),
            make_segment(
                destination="CN",
                departure=FAR_FUTURE_DEPARTURE,
                flight_number="DL200",
                origin="NRT",
            ),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Visa for JP expires on date of travel",
            "No visa on file for CN",
        ]

    def test_earlier_critical_not_downgraded_by_later_info(self, base_requirements):
        jp_visa = make_visa(
            country_code="JP",
            expiry_date=FAR_FUTURE_DEPARTURE + timedelta(days=5),
            visa_number="JP1",
        )
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE),
            make_segment(
                destination="JP",
                departure=FAR_FUTURE_DEPARTURE,
                flight_number="DL200",
                origin="PEK",
            ),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_outranks_info(self, base_requirements):
        cn_visa = make_visa(
            expiry_date=FAR_FUTURE_DEPARTURE + timedelta(days=10), visa_number="CN1"
        )
        jp_visa = make_visa(
            country_code="JP", expiry_date=FAR_FUTURE_DEPARTURE, visa_number="JP1"
        )
        profile = make_profile(nationality="IN", visa_records=[cn_visa, jp_visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=FAR_FUTURE_DEPARTURE),
            make_segment(
                destination="JP",
                departure=FAR_FUTURE_DEPARTURE,
                flight_number="DL200",
                origin="PEK",
            ),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING

    def test_empty_itinerary_produces_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.validation_errors == []


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_rebooked_departure_turns_valid_visa_into_critical(
        self, base_requirements
    ):
        visa = make_visa(expiry_date=date(2030, 12, 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        original = make_itinerary(
            make_segment(destination="CN", departure=date(2030, 1, 1))
        )
        rebooked = make_itinerary(
            make_segment(destination="CN", departure=date(2031, 1, 1))
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, rebooked, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in after.reasons


class TestRequirementsOverride:
    """Caller-supplied requirements take precedence over static data."""

    def test_override_makes_exempt_nationality_require_visa(self, base_requirements):
        requirements = dict(base_requirements)
        requirements["DE"] = TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        )
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(
            make_segment(
                destination="DE", departure=FAR_FUTURE_DEPARTURE, is_layover=True
            )
        )

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons
