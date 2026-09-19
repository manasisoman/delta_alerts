"""Tests for the country requirements lookup."""

from datetime import date

import pytest

from src.data.country_requirements import (
    _DEFAULT_REQUIREMENTS,
    COUNTRY_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


class TestKnownCountryLookup:
    """Known country codes return their explicit requirements entry."""

    def test_returns_entry_for_known_country(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_schengen_country_requires_3_months(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3
        assert result.transit_visa_required is False

    def test_non_schengen_country_requires_6_months(self):
        result = get_requirements("US")

        assert result.passport_validity_months == 6
        assert result.transit_visa_required is True

    def test_visa_free_country(self):
        result = get_requirements("CL")

        assert result.requires_visa is False

    def test_optional_urls_populated_for_extended_entries(self):
        result = get_requirements("CA")

        assert result.embassy_url is not None
        assert result.evisa_portal_url is not None
        assert result.estimated_processing_days == 14

    def test_optional_urls_default_to_none(self):
        result = get_requirements("DE")

        assert result.embassy_url is None
        assert result.evisa_portal_url is None
        assert result.entry_form_url is None
        assert result.estimated_processing_days is None


class TestUnknownCountryFallback:
    """Unknown country codes fall back to the shared default requirements."""

    def test_unknown_code_returns_default(self):
        result = get_requirements("XX")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"

    def test_default_values(self):
        result = get_requirements("ZZ")

        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_string_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS


class TestDataIntegrity:
    """Every entry is internally consistent."""

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_entry_key_matches_country_code(self, code):
        entry = get_requirements(code)

        assert isinstance(entry, TravelDocRequirements)
        assert entry.country_code == code
        assert entry.passport_validity_months in (3, 6)
        assert entry.country_code not in entry.visa_exempt_nationalities

    def test_schengen_countries_all_use_3_month_window(self):
        schengen = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]

        windows = {get_requirements(code).passport_validity_months for code in schengen}

        assert windows == {3}


class TestEvaluatorFallbackIntegration:
    """Evaluators use get_requirements when the destination is not in the supplied dict."""

    def test_passport_uses_default_6_month_window_for_unknown_destination(self):
        # Passport expires 4 months after departure; default window is 6 months
        profile = make_profile(passport_expiry=date(2027, 1, 1))
        segment = make_segment(destination="XX", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("6-month" in r for r in result.reasons)

    def test_passport_uses_schengen_3_month_window_from_data(self):
        # Passport expires 4 months after departure; Germany only needs 3,
        # so this is only the generic 6-month INFO advisory, not CRITICAL
        profile = make_profile(passport_expiry=date(2027, 1, 1))
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO
        assert not any("3-month" in r for r in result.reasons)

    def test_visa_required_for_unknown_destination_without_visa(self):
        profile = make_profile(nationality="IN")
        segment = make_segment(destination="XX", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_visa_exempt_nationality_from_data_skips_alert(self):
        profile = make_profile(nationality="US")
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_skipped_when_transit_visa_not_required(self):
        # DE layover: transit_visa_required=False in data, so no visa check
        profile = make_profile(nationality="IN")
        segment = make_segment(destination="DE", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_checked_when_transit_visa_required(self):
        # CN layover: transit_visa_required=True in data, so missing visa is CRITICAL
        profile = make_profile(nationality="IN")
        segment = make_segment(destination="CN", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_visa_free_destination_skips_alert(self):
        profile = make_profile(nationality="IN")
        segment = make_segment(destination="CL", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False
