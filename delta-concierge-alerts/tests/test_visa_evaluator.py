"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

import src.data.country_requirements as country_requirements_mod
from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)


def _visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    visa_number: str = "V001",
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code, visa_type="TOURIST",
        issue_date=date(2020, 1, 1), expiry_date=expiry_date,
        visa_number=visa_number,
    )


def _requirements(
    country_code: str = "XA",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    visa_exempt_nationalities: list[str] | None = None,
) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=6,
        visa_exempt_nationalities=visa_exempt_nationalities or [],
    )


class TestValidVisa:
    """Visa valid well beyond the warning window → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa()])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.profile is profile
        assert result.segments_evaluated == itinerary.segments

    def test_visa_one_day_past_warning_window_no_alert(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry_date=expiry)])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

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
        profile = make_profile(nationality="IN", visa_records=[_visa(country_code="JP")])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(expiry_date=date(2022, 1, 1))]
        )
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(expiry_date=date(2030, 7, 1))]
        )
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons

    def test_visa_expires_day_before_departure(self, base_requirements):
        expiry = DEPARTURE - timedelta(days=1)
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry_date=expiry)])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry_date=DEPARTURE)])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestVisaExpiresWithinWarningWindow:
    """Visa expiring within the warning window after departure → INFO."""

    def test_visa_expires_day_after_departure(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=1)
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry_date=expiry)])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_visa_expires_on_warning_threshold(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        profile = make_profile(nationality="IN", visa_records=[_visa(expiry_date=expiry)])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO


class TestVisaNotRequired:
    """Countries that do not require a visa are skipped."""

    def test_country_without_visa_requirement_skipped(self):
        requirements = {"XA": _requirements(requires_visa=False)}
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaExemptNationality:
    """Travelers whose nationality is exempt are skipped."""

    def test_exempt_nationality_no_alert(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_non_exempt_nationality_alerts(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for DE"]


class TestLayoverWithTransitVisaRequired:
    """Layovers only require a visa when the country requires a transit visa."""

    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_layover_without_transit_visa_requirement_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_layover_then_final_destination_only_alerts_final(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        layover = make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        final = make_segment(
            destination="JP", departure=DEPARTURE, origin="DE", flight_number="DL200"
        )
        itinerary = make_itinerary(layover, final)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.reasons == ["No visa on file for JP"]
        assert result.severity == AlertSeverity.CRITICAL


class TestRequirementsFallback:
    """Destinations missing from the requirements dict use get_requirements()."""

    def test_falls_back_to_country_requirements_data(self):
        original = country_requirements_mod.COUNTRY_REQUIREMENTS
        country_requirements_mod.COUNTRY_REQUIREMENTS = {
            "XB": _requirements(country_code="XB", requires_visa=False),
        }
        try:
            profile = make_profile(nationality="IN", visa_records=[])
            itinerary = make_itinerary(make_segment(destination="XB", departure=DEPARTURE))

            result = evaluate_visa_requirements(profile, itinerary, {})
        finally:
            country_requirements_mod.COUNTRY_REQUIREMENTS = original

        assert result.is_alert_required is False

    def test_unknown_country_uses_default_requirements(self):
        original = country_requirements_mod.COUNTRY_REQUIREMENTS
        country_requirements_mod.COUNTRY_REQUIREMENTS = {}
        try:
            profile = make_profile(nationality="IN", visa_records=[])
            itinerary = make_itinerary(make_segment(destination="XZ", departure=DEPARTURE))

            result = evaluate_visa_requirements(profile, itinerary, {})
        finally:
            country_requirements_mod.COUNTRY_REQUIREMENTS = original

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XZ"]

    def test_provided_requirements_take_precedence(self):
        original = country_requirements_mod.COUNTRY_REQUIREMENTS
        country_requirements_mod.COUNTRY_REQUIREMENTS = {
            "XA": _requirements(requires_visa=True),
        }
        try:
            profile = make_profile(nationality="IN", visa_records=[])
            itinerary = make_itinerary(make_segment(destination="XA", departure=DEPARTURE))

            result = evaluate_visa_requirements(
                profile, itinerary, {"XA": _requirements(requires_visa=False)}
            )
        finally:
            country_requirements_mod.COUNTRY_REQUIREMENTS = original

        assert result.is_alert_required is False


class TestBestVisaSelection:
    """When multiple visas match, the one with the latest expiry is used."""

    def test_valid_visa_chosen_over_expired(self, base_requirements):
        visas = [
            _visa(expiry_date=date(2021, 1, 1), visa_number="OLD"),
            _visa(expiry_date=date(2035, 1, 1), visa_number="NEW"),
        ]
        profile = make_profile(nationality="IN", visa_records=visas)
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_find_matching_visa_returns_latest_expiry(self):
        visas = [
            _visa(expiry_date=date(2031, 1, 1), visa_number="A"),
            _visa(expiry_date=date(2034, 1, 1), visa_number="B"),
            _visa(country_code="JP", expiry_date=date(2040, 1, 1), visa_number="C"),
            _visa(expiry_date=date(2032, 1, 1), visa_number="D"),
        ]

        match = _find_matching_visa(visas, "CN")

        assert match.visa_number == "B"

    def test_find_matching_visa_none_when_no_match(self):
        assert _find_matching_visa([_visa(country_code="JP")], "CN") is None


class TestSeverityConsolidation:
    """Across segments, the highest severity wins and all reasons are kept."""

    def test_critical_overrides_info(self, base_requirements):
        info_visa = _visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            f"Visa for JP expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "No visa on file for CN",
        ]

    def test_lower_severity_does_not_downgrade(self, base_requirements):
        info_visa = _visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[info_visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_overrides_info(self, base_requirements):
        visas = [
            _visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5)),
            _visa(country_code="CN", expiry_date=DEPARTURE),
        ]
        profile = make_profile(nationality="IN", visa_records=visas)
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING

    def test_max_severity_from_none(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_max_severity_upgrades(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL

    def test_max_severity_keeps_higher(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.WARNING) == AlertSeverity.CRITICAL

    def test_max_severity_equal(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING


class TestItineraryChange:
    """Re-evaluating after an itinerary change reflects the new dates/destinations."""

    def test_departure_moved_past_visa_expiry_escalates(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa(expiry_date=date(2030, 12, 1))]
        )
        original = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="CN", departure=date(2031, 1, 15)))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["Visa for CN expires before date of travel"]

    def test_destination_changed_to_exempt_country_clears_alert(self, base_requirements):
        profile = make_profile(nationality="GB", visa_records=[])
        original = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="JP", departure=DEPARTURE))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False
        assert after.severity is None


class TestEmptyItinerary:
    """An itinerary with no segments produces no alert."""

    def test_no_segments(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])

        result = evaluate_visa_requirements(profile, make_itinerary(), base_requirements)

        assert result.is_alert_required is False
        assert result.segments_evaluated == []
