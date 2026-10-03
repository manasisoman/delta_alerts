"""Tests for country-specific travel document requirements data and lookup."""

from datetime import date

import pytest

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


SCHENGEN_COUNTRIES = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]
VISA_FREE_COUNTRIES = ["CL", "JM", "CR"]
UNLISTED_COUNTRY = "ZZ"

DEPARTURE = date(2030, 6, 1)
ARRIVAL = date(2030, 6, 2)
FAR_FUTURE_EXPIRY = date(2040, 1, 1)
# 4 months after DEPARTURE: satisfies a 3-month rule, fails a 6-month rule.
FOUR_MONTHS_AFTER_DEPARTURE = date(2030, 10, 1)

# Empty override dict forces the evaluators to fall back to get_requirements().
NO_OVERRIDES: dict = {}


def _segment(destination: str = "DE", **overrides):
    """Build a far-future flight segment; override any field by keyword."""
    kwargs = {"destination": destination, "departure": DEPARTURE, "arrival": ARRIVAL}
    kwargs.update(overrides)
    return make_segment(**kwargs)


def _visa(country_code: str = "CN", expiry_date: date = FAR_FUTURE_EXPIRY) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry_date,
        visa_number="V-TEST",
    )


class TestKnownCountryLookup:
    """get_requirements returns the configured entry for listed countries."""

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_returns_configured_entry(self, country_code):
        result = get_requirements(country_code)

        assert result is COUNTRY_REQUIREMENTS[country_code]
        assert result.country_code == country_code

    def test_does_not_return_default_for_listed_country(self):
        result = get_requirements("CN")

        assert result is not _DEFAULT_REQUIREMENTS
        assert result.transit_visa_required is True


class TestUnknownCountryFallback:
    """Unlisted countries fall back to the documented default requirements."""

    def test_unlisted_country_returns_default(self):
        result = get_requirements(UNLISTED_COUNTRY)

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_values_match_documented_contract(self):
        result = get_requirements(UNLISTED_COUNTRY)

        assert result.country_code == "DEFAULT"
        assert result.passport_validity_months == 6
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.visa_exempt_nationalities == []

    def test_empty_country_code_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS


class TestRequirementsDataIntegrity:
    """Static requirements data is internally consistent."""

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_key_matches_country_code(self, country_code):
        reqs = COUNTRY_REQUIREMENTS[country_code]

        assert reqs.country_code == country_code

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_validity_months_is_three_or_six(self, country_code):
        reqs = COUNTRY_REQUIREMENTS[country_code]

        assert reqs.passport_validity_months in (3, 6)

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_country_does_not_exempt_its_own_nationals(self, country_code):
        reqs = COUNTRY_REQUIREMENTS[country_code]

        assert country_code not in reqs.visa_exempt_nationalities

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_exempt_nationalities_are_unique_alpha2_codes(self, country_code):
        exempt = COUNTRY_REQUIREMENTS[country_code].visa_exempt_nationalities

        assert len(exempt) == len(set(exempt))
        assert all(len(code) == 2 and code.isupper() for code in exempt)

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_optional_urls_are_http(self, country_code):
        reqs = COUNTRY_REQUIREMENTS[country_code]
        urls = [reqs.embassy_url, reqs.evisa_portal_url, reqs.entry_form_url]

        assert all(url.startswith(("http://", "https://")) for url in urls if url)

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_processing_days_positive_when_present(self, country_code):
        days = COUNTRY_REQUIREMENTS[country_code].estimated_processing_days

        assert days is None or days > 0


class TestSchengenRequirements:
    """Schengen countries require 3 months validity and no transit visa."""

    @pytest.mark.parametrize("country_code", SCHENGEN_COUNTRIES)
    def test_three_month_validity(self, country_code):
        reqs = get_requirements(country_code)

        assert reqs.passport_validity_months == 3

    @pytest.mark.parametrize("country_code", SCHENGEN_COUNTRIES)
    def test_no_transit_visa(self, country_code):
        reqs = get_requirements(country_code)

        assert reqs.transit_visa_required is False

    @pytest.mark.parametrize("country_code", SCHENGEN_COUNTRIES)
    def test_shared_exempt_list(self, country_code):
        reqs = get_requirements(country_code)

        assert reqs.visa_exempt_nationalities == get_requirements("DE").visa_exempt_nationalities


class TestPassportValidityPerCountry:
    """Passport evaluator applies each destination's validity window via lookup."""

    def test_three_month_country_accepts_four_months_validity(self):
        profile = make_profile(passport_expiry=FOUR_MONTHS_AFTER_DEPARTURE)
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_passport_expiry(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.INFO
        assert not any("does not meet" in r for r in result.reasons)

    def test_six_month_country_rejects_four_months_validity(self):
        profile = make_profile(passport_expiry=FOUR_MONTHS_AFTER_DEPARTURE)
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_unlisted_country_uses_default_six_months(self):
        profile = make_profile(passport_expiry=FOUR_MONTHS_AFTER_DEPARTURE)
        itinerary = make_itinerary(_segment(destination=UNLISTED_COUNTRY))

        result = evaluate_passport_expiry(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.CRITICAL
        assert any(f"does not meet {UNLISTED_COUNTRY}'s 6-month" in r for r in result.reasons)

    def test_far_future_passport_passes_listed_country(self):
        profile = make_profile(passport_expiry=FAR_FUTURE_EXPIRY)
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_segment_still_checks_validity(self):
        profile = make_profile(passport_expiry=FOUR_MONTHS_AFTER_DEPARTURE)
        itinerary = make_itinerary(_segment(destination="CN", is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_override_takes_precedence_over_lookup(self, base_requirements):
        base_requirements["CN"].passport_validity_months = 3
        profile = make_profile(passport_expiry=FOUR_MONTHS_AFTER_DEPARTURE)
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert not any("does not meet" in r for r in result.reasons)


class TestVisaRequirementPerCountry:
    """Visa evaluator applies requires_visa and exemptions from lookup data."""

    def test_visa_required_country_without_visa_is_critical(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    @pytest.mark.parametrize("country_code", VISA_FREE_COUNTRIES)
    def test_visa_free_country_skips_check(self, country_code):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(destination=country_code))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False

    def test_exempt_nationality_skips_check(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False

    def test_non_exempt_nationality_requires_visa(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(destination="DE"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert "No visa on file for DE" in result.reasons

    def test_empty_exempt_list_requires_visa_for_everyone(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert "No visa on file for CN" in result.reasons

    def test_unlisted_country_requires_visa_by_default(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment(destination=UNLISTED_COUNTRY))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert f"No visa on file for {UNLISTED_COUNTRY}" in result.reasons

    def test_valid_visa_for_required_country_passes(self):
        profile = make_profile(nationality="IN", visa_records=[_visa("CN")])
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False

    def test_visa_expiring_within_warning_window_is_info(self):
        visa = _visa("CN", expiry_date=date(2030, 6, 15))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.INFO
        assert "Visa for CN expires within 30 days of travel" in result.reasons

    def test_visa_expiring_on_departure_is_warning(self):
        visa = _visa("CN", expiry_date=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(_segment(destination="CN"))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestLayoverTransitRules:
    """Layovers only need a visa where transit_visa_required is set."""

    def test_layover_in_transit_free_country_skips_visa(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(destination="DE", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False

    def test_layover_in_transit_visa_country_requires_visa(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(destination="CN", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_layover_in_unlisted_country_uses_default_no_transit_visa(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment(destination=UNLISTED_COUNTRY, is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False

    def test_layover_exempt_nationality_skips_transit_visa(self):
        profile = make_profile(nationality="GB")
        itinerary = make_itinerary(_segment(destination="US", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.is_alert_required is False

    def test_layover_then_final_destination_evaluates_both(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(
            _segment(destination="CN", is_layover=True, flight_number="DL1"),
            _segment(destination="DE", flight_number="DL2"),
        )

        result = evaluate_visa_requirements(profile, itinerary, NO_OVERRIDES)

        assert result.reasons == ["No visa on file for CN", "No visa on file for DE"]


class TestItineraryChange:
    """Re-evaluating a changed itinerary picks up the new destination's rules."""

    def test_change_to_visa_required_destination_raises_alert(self):
        profile = make_profile(nationality="US")
        original = make_itinerary(_segment(destination="DE"))
        changed = make_itinerary(_segment(destination="CN"))

        before = evaluate_visa_requirements(profile, original, NO_OVERRIDES)
        after = evaluate_visa_requirements(profile, changed, NO_OVERRIDES)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_change_to_visa_free_destination_clears_alert(self):
        profile = make_profile(nationality="IN")
        original = make_itinerary(_segment(destination="CN"))
        changed = make_itinerary(_segment(destination="CL"))

        before = evaluate_visa_requirements(profile, original, NO_OVERRIDES)
        after = evaluate_visa_requirements(profile, changed, NO_OVERRIDES)

        assert before.is_alert_required is True
        assert after.is_alert_required is False

    def test_change_to_stricter_validity_destination_raises_passport_alert(self):
        profile = make_profile(passport_expiry=FOUR_MONTHS_AFTER_DEPARTURE)
        original = make_itinerary(_segment(destination="FR"))
        changed = make_itinerary(_segment(destination="JP"))

        before = evaluate_passport_expiry(profile, original, NO_OVERRIDES)
        after = evaluate_passport_expiry(profile, changed, NO_OVERRIDES)

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_added_transit_layover_raises_alert(self):
        profile = make_profile(nationality="IN", visa_records=[_visa("DE")])
        original = make_itinerary(_segment(destination="DE"))
        changed = make_itinerary(
            _segment(destination="CN", is_layover=True, flight_number="DL1"),
            _segment(destination="DE", flight_number="DL2"),
        )

        before = evaluate_visa_requirements(profile, original, NO_OVERRIDES)
        after = evaluate_visa_requirements(profile, changed, NO_OVERRIDES)

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for CN"]
