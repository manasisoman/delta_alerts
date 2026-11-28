"""Branch-coverage tests for the visa evaluator.

All dates are 2030+ so date-relative branches stay stable over time, and
requirements are passed explicitly (made-up country codes where possible)
to isolate tests from production COUNTRY_REQUIREMENTS data.
"""

from datetime import date, timedelta

import src.data.country_requirements as country_requirements
from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment

DEPARTURE = date(2030, 9, 1)


def _visa(country_code="XA", expiry=date(2032, 1, 1), visa_number="V100"):
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2029, 1, 1),
        expiry_date=expiry,
        visa_number=visa_number,
    )


def _segment(destination="XA", departure=DEPARTURE, **kwargs):
    return make_segment(
        destination=destination,
        departure=departure,
        arrival=departure + timedelta(days=1),
        **kwargs,
    )


def _requirements(**overrides):
    defaults = {
        "XA": TravelDocRequirements(
            country_code="XA",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        ),
        "XB": TravelDocRequirements(
            country_code="XB",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "XC": TravelDocRequirements(
            country_code="XC",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
    }
    defaults.update(overrides)
    return defaults


class TestHappyPath:
    """A visa valid well beyond the warning window produces no alert."""

    def test_valid_visa_no_alert(self):
        profile = make_profile(nationality="IN", visa_records=[_visa()])
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == itinerary.segments
        assert result.profile is profile


class TestSkippedSegments:
    """Segments that don't need a visa are skipped even with no visa on file."""

    def test_country_not_requiring_visa_is_skipped(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(_segment(destination="XC"))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement_is_skipped(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(_segment(destination="XA", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_with_transit_visa_requirement_alerts(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(_segment(destination="XB", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XB"]

    def test_visa_exempt_nationality_is_skipped(self):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(_segment(destination="XA"))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExpiryRelativeToTravel:
    """Visa expiry relative to departure drives CRITICAL / WARNING / INFO."""

    def test_visa_for_other_country_does_not_match(self):
        profile = make_profile(nationality="IN", visa_records=[_visa(country_code="XB")])
        itinerary = make_itinerary(_segment(destination="XA"))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XA"]

    def test_visa_expired_before_today(self):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=date(2020, 1, 1))])
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for XA has expired"]

    def test_visa_expires_before_departure(self):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=date(2030, 6, 1))])
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for XA expires before date of travel"]

    def test_visa_expires_on_departure(self):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=DEPARTURE)])
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for XA expires on date of travel"]

    def test_visa_expires_day_after_departure_is_info(self):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(expiry=DEPARTURE + timedelta(days=1))]
        )
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for XA expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_visa_expires_at_warning_threshold_is_info(self):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=expiry)])
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_just_past_warning_threshold_no_alert(self):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=expiry)])
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaRecordSelection:
    """When several visas match a country, the latest expiry wins."""

    def test_valid_visa_preferred_over_expired(self):
        visas = [
            _visa(expiry=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry=date(2032, 1, 1), visa_number="NEW"),
        ]
        profile = make_profile(nationality="IN", visa_records=visas)
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False

    def test_latest_expiry_selected_regardless_of_order(self):
        visas = [
            _visa(expiry=date(2032, 1, 1), visa_number="NEW"),
            _visa(expiry=DEPARTURE, visa_number="ON_DEPARTURE"),
        ]
        profile = make_profile(nationality="IN", visa_records=visas)
        itinerary = make_itinerary(_segment())

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.is_alert_required is False


class TestSeverityConsolidation:
    """Overall severity is the highest across segments; all reasons are kept."""

    def test_critical_after_warning_escalates(self):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=DEPARTURE)])
        itinerary = make_itinerary(_segment(destination="XA"), _segment(destination="XB"))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Visa for XA expires on date of travel",
            "No visa on file for XB",
        ]

    def test_info_after_critical_does_not_downgrade(self):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(expiry=DEPARTURE + timedelta(days=10))],
        )
        itinerary = make_itinerary(_segment(destination="XB"), _segment(destination="XA"))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_after_info_escalates(self):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(country_code="XA", expiry=DEPARTURE + timedelta(days=10)),
                _visa(country_code="XB", expiry=DEPARTURE),
            ],
        )
        itinerary = make_itinerary(_segment(destination="XA"), _segment(destination="XB"))

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.WARNING
        assert len(result.reasons) == 2

    def test_layover_skipped_but_destination_evaluated(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            _segment(destination="XA", is_layover=True),
            _segment(destination="XB"),
        )

        result = evaluate_visa_requirements(profile, itinerary, _requirements())

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XB"]


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new departure date."""

    def test_later_departure_flips_visa_to_critical(self):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry=date(2031, 1, 1))])
        original = make_itinerary(_segment(departure=DEPARTURE))
        changed = make_itinerary(_segment(departure=date(2031, 3, 1)))

        original_result = evaluate_visa_requirements(profile, original, _requirements())
        changed_result = evaluate_visa_requirements(profile, changed, _requirements())

        assert original_result.is_alert_required is False
        assert changed_result.severity == AlertSeverity.CRITICAL
        assert changed_result.reasons == ["Visa for XA expires before date of travel"]

    def test_changed_destination_to_visa_free_country_clears_alert(self):
        profile = make_profile(nationality="IN", visa_records=[])
        original = make_itinerary(_segment(destination="XB"))
        changed = make_itinerary(_segment(destination="XC"))

        original_result = evaluate_visa_requirements(profile, original, _requirements())
        changed_result = evaluate_visa_requirements(profile, changed, _requirements())

        assert original_result.severity == AlertSeverity.CRITICAL
        assert changed_result.is_alert_required is False


class TestRequirementsFallback:
    """Destinations missing from the requirements dict use get_requirements()."""

    def test_unknown_destination_uses_default_requirements(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {}
        try:
            profile = make_profile(nationality="IN", visa_records=[])
            itinerary = make_itinerary(_segment(destination="XZ"))

            result = evaluate_visa_requirements(profile, itinerary, _requirements())
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XZ"]

    def test_destination_falls_back_to_country_requirements_entry(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {
            "XD": TravelDocRequirements(
                country_code="XD",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        }
        try:
            profile = make_profile(nationality="IN", visa_records=[])
            itinerary = make_itinerary(_segment(destination="XD"))

            result = evaluate_visa_requirements(profile, itinerary, {})
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

        assert result.is_alert_required is False
