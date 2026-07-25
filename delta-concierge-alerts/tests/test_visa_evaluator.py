"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
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


def _visa(
    country_code: str = "CN",
    expiry: date = date(2030, 1, 1),
    visa_type: str = "TOURIST",
    issue: date = date(2020, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    """Helper to build a VisaRecord with sensible defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue,
        expiry_date=expiry,
        visa_number=visa_number,
    )


DEPARTURE = date(2030, 9, 1)


class TestValidVisa:
    """A visa valid well beyond the travel date triggers no alert."""

    def test_visa_valid_far_beyond_travel(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=date(2035, 1, 1))])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == itinerary.segments


class TestSkippedSegments:
    """Segments that do not need a visa check are skipped entirely."""

    def test_country_not_requiring_visa(self, base_requirements):
        requirements = dict(base_requirements)
        requirements["MX"] = TravelDocRequirements(
            country_code="MX",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="MX", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_visa_exempt_nationality(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExpiresRelativeToDeparture:
    """Severity depends on how close the visa expiry is to the departure date."""

    def test_expires_after_today_but_before_departure(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(expiry=date(2030, 8, 1))]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons

    def test_expires_on_departure_date(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=DEPARTURE)])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons

    def test_expires_within_warning_window(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(expiry=date(2030, 9, 15))]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert "Visa for CN expires within 30 days of travel" in result.reasons

    def test_expires_exactly_on_warning_threshold(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))],
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO

    def test_expires_one_day_after_warning_threshold(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1))
            ],
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaSelection:
    """When several visas match a country, the latest expiry wins."""

    def test_latest_expiry_visa_is_selected(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(expiry=date(2022, 1, 1), visa_number="OLD"),
                _visa(expiry=date(2035, 1, 1), visa_number="NEW"),
            ],
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_visa_for_other_country_is_ignored(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(country_code="JP", expiry=date(2035, 1, 1))]
        )
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestUnknownDestinationFallback:
    """Destinations missing from the requirements map fall back to defaults."""

    def test_falls_back_to_country_requirements_lookup(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="FR", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for FR" in result.reasons


class TestMultiSegmentItinerary:
    """Consolidated severity is the highest across all evaluated segments."""

    def test_lower_severity_segment_does_not_downgrade(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(country_code="CN", expiry=date(2030, 8, 1)),
                _visa(country_code="JP", expiry=date(2030, 9, 15), visa_number="V200"),
            ],
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_higher_severity_segment_upgrades(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(country_code="CN", expiry=date(2030, 9, 15)),
                _visa(country_code="JP", expiry=DEPARTURE, visa_number="V200"),
            ],
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires within 30 days of travel" in result.reasons
        assert "Visa for JP expires on date of travel" in result.reasons

    def test_layover_and_destination_segments_evaluated_together(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True),
            make_segment(destination="DE", departure=DEPARTURE, is_layover=True),
            make_segment(destination="JP", departure=DEPARTURE),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN", "No visa on file for JP"]

    def test_empty_itinerary_produces_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []
