"""Critical-path tests for country requirements lookup and data."""

from datetime import date

import pytest

from src.data import country_requirements as cr_mod
from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements

from tests.conftest import make_itinerary, make_profile, make_segment


SCHENGEN = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]
FUTURE_DEPARTURE = date(2031, 6, 1)
FUTURE_ARRIVAL = date(2031, 6, 2)


def _segment(destination: str, **overrides):
    """Build a far-future segment so date-relative branches stay stable."""
    kwargs = {"departure": FUTURE_DEPARTURE, "arrival": FUTURE_ARRIVAL}
    kwargs.update(overrides)
    return make_segment(destination=destination, **kwargs)


def _reqs(country_code: str = "ZZ", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements fixture with sensible defaults."""
    kwargs = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    kwargs.update(overrides)
    return TravelDocRequirements(**kwargs)


@pytest.fixture
def swapped_requirements():
    """Temporarily replace COUNTRY_REQUIREMENTS with fixture data."""
    original = cr_mod.COUNTRY_REQUIREMENTS
    fixture = {"ZZ": _reqs("ZZ", passport_validity_months=1)}
    cr_mod.COUNTRY_REQUIREMENTS = fixture
    try:
        yield fixture
    finally:
        cr_mod.COUNTRY_REQUIREMENTS = original


class TestKnownCountryLookup:
    """A listed country code returns its own requirements entry."""

    def test_returns_exact_entry_for_listed_country(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_every_listed_country_resolves_to_its_entry(self):
        for code, entry in COUNTRY_REQUIREMENTS.items():
            result = get_requirements(code)

            assert result is entry

    def test_lookup_reads_module_data_at_call_time(self, swapped_requirements):
        result = get_requirements("ZZ")

        assert result is swapped_requirements["ZZ"]
        assert result.passport_validity_months == 1


class TestDefaultFallback:
    """Unlisted codes fall back to the 6-month, visa-required default."""

    def test_unknown_country_returns_default(self):
        result = get_requirements("XX")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_values(self):
        result = get_requirements("XX")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_code_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_listed_country_missing_from_swapped_data_falls_back(self, swapped_requirements):
        result = get_requirements("DE")

        assert result is _DEFAULT_REQUIREMENTS


class TestDataIntegrity:
    """Every entry is internally consistent and well-formed."""

    def test_keys_match_country_code(self):
        for code, entry in COUNTRY_REQUIREMENTS.items():
            assert entry.country_code == code

    def test_keys_are_iso_alpha2(self):
        for code in COUNTRY_REQUIREMENTS:
            assert len(code) == 2 and code.isupper() and code.isalpha()

    def test_passport_validity_is_positive(self):
        for entry in COUNTRY_REQUIREMENTS.values():
            assert entry.passport_validity_months in (3, 6)

    def test_country_not_exempt_from_itself(self):
        for code, entry in COUNTRY_REQUIREMENTS.items():
            assert code not in entry.visa_exempt_nationalities

    def test_exempt_nationalities_are_iso_alpha2(self):
        for entry in COUNTRY_REQUIREMENTS.values():
            for nat in entry.visa_exempt_nationalities:
                assert len(nat) == 2 and nat.isupper()

    def test_optional_urls_are_absolute_http(self):
        for entry in COUNTRY_REQUIREMENTS.values():
            for url in (entry.embassy_url, entry.evisa_portal_url, entry.entry_form_url):
                assert url is None or url.startswith(("https://", "http://"))

    def test_processing_days_positive_when_set(self):
        for entry in COUNTRY_REQUIREMENTS.values():
            days = entry.estimated_processing_days
            assert days is None or days > 0


class TestPassportValidityLimits:
    """Schengen requires 3 months of validity; most others require 6."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_requires_three_months(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 3

    @pytest.mark.parametrize("code", ["US", "GB", "JP", "CN", "IN", "MX", "AE"])
    def test_non_schengen_requires_six_months(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 6

    def test_passport_meeting_schengen_minimum_has_no_critical(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 10, 1))
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO

    def test_same_passport_fails_six_month_country(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 10, 1))
        itinerary = make_itinerary(_segment("JP"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "6-month validity requirement" in result.reasons[0]

    def test_unlisted_country_applies_default_six_months(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 10, 1))
        itinerary = make_itinerary(_segment("XX"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "XX's 6-month validity requirement" in result.reasons[0]


class TestVisaExemptions:
    """Exempt nationalities skip visa checks; others need a visa on file."""

    def test_us_citizen_exempt_for_schengen(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("FR"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_non_exempt_nationality_needs_visa(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("FR"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for FR" in result.reasons

    def test_china_has_no_exemptions(self):
        result = get_requirements("CN")

        assert result.visa_exempt_nationalities == []

    @pytest.mark.parametrize("code", ["CL", "JM", "CR"])
    def test_visa_free_countries_never_alert(self, code):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(code))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert get_requirements(code).requires_visa is False
        assert result.is_alert_required is False

    def test_unlisted_country_requires_visa_for_everyone(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("XX"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert "No visa on file for XX" in result.reasons


class TestLayovers:
    """Transit visa flag decides whether a layover needs a visa."""

    @pytest.mark.parametrize("code", ["US", "AU", "CN", "IN", "CA"])
    def test_transit_visa_countries(self, code):
        result = get_requirements(code)

        assert result.transit_visa_required is True

    def test_layover_in_transit_visa_country_alerts(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("US", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert "No visa on file for US" in result.reasons

    def test_layover_in_non_transit_country_skipped(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("GB", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_in_unlisted_country_uses_default_no_transit(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("XX", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_still_checks_passport_validity(self):
        profile = make_profile(nationality="IN", passport_expiry=date(2031, 10, 1))
        itinerary = make_itinerary(
            _segment("GB", is_layover=True, flight_number="DL1"),
            _segment("DE", departure=date(2031, 6, 2), flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert any("GB's 6-month" in r for r in result.reasons)


class TestItineraryChange:
    """Re-evaluating a changed itinerary picks up the new country's rules."""

    def test_rerouting_to_visa_country_triggers_alert(self):
        profile = make_profile(nationality="US")
        original = make_itinerary(_segment("DE"))
        changed = make_itinerary(_segment("CN"))

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in after.reasons

    def test_adding_transit_layover_triggers_alert(self):
        profile = make_profile(nationality="BR")
        original = make_itinerary(_segment("DE"))
        changed = make_itinerary(
            _segment("US", is_layover=True, flight_number="DL1"),
            _segment("DE", departure=date(2031, 6, 2), flight_number="DL2"),
        )

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for US"]

    def test_rerouting_to_six_month_country_fails_passport(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 10, 1))
        original = make_itinerary(_segment("FR"))
        changed = make_itinerary(_segment("GB"))

        before = evaluate_passport_expiry(profile, original, {})
        after = evaluate_passport_expiry(profile, changed, {})

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL


class TestOverridePrecedence:
    """Explicit requirements passed to evaluators take precedence over data."""

    def test_override_replaces_country_data(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("DE"))
        overrides = {"DE": _reqs("DE", visa_exempt_nationalities=[])}

        result = evaluate_visa_requirements(profile, itinerary, overrides)

        assert "No visa on file for DE" in result.reasons

    def test_missing_override_falls_back_to_lookup(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("DE"))
        overrides = {"CN": _reqs("CN")}

        result = evaluate_visa_requirements(profile, itinerary, overrides)

        assert result.is_alert_required is False
