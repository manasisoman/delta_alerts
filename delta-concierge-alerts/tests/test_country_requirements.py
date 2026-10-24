"""Tests for country-specific travel document requirements lookup."""

from datetime import date

import pytest
from dateutil.relativedelta import relativedelta

import src.data.country_requirements as cr_mod
from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord
from tests.conftest import make_itinerary, make_profile, make_segment


FUTURE_DEPARTURE = date(2030, 6, 1)
FUTURE_ARRIVAL = date(2030, 6, 2)
SCHENGEN = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]


def _reqs(country_code: str = "XX", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults."""
    fields = dict(
        country_code=country_code,
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=[],
    )
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _future_segment(destination: str, **overrides):
    """Build a far-future segment so date-relative branches stay stable."""
    kwargs = dict(departure=FUTURE_DEPARTURE, arrival=FUTURE_ARRIVAL)
    kwargs.update(overrides)
    return make_segment(destination=destination, **kwargs)


@pytest.fixture
def swap_country_requirements():
    """Swap the module-level COUNTRY_REQUIREMENTS for a test-provided dict."""
    original = cr_mod.COUNTRY_REQUIREMENTS

    def _swap(replacement: dict[str, TravelDocRequirements]) -> None:
        cr_mod.COUNTRY_REQUIREMENTS = replacement

    try:
        yield _swap
    finally:
        cr_mod.COUNTRY_REQUIREMENTS = original


class TestKnownCountryLookup:
    """get_requirements returns the configured entry for listed countries."""

    def test_returns_configured_entry(self, swap_country_requirements):
        entry = _reqs("ZZ", passport_validity_months=3)
        swap_country_requirements({"ZZ": entry})

        result = get_requirements("ZZ")

        assert result is entry

    def test_returns_production_entry_for_germany(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_every_listed_country_resolves_to_itself(self, code):
        result = get_requirements(code)

        assert result.country_code == code
        assert result is not _DEFAULT_REQUIREMENTS


class TestDefaultFallback:
    """Unlisted countries fall back to the conservative default requirements."""

    def test_unknown_country_returns_default(self, swap_country_requirements):
        swap_country_requirements({"ZZ": _reqs("ZZ")})

        result = get_requirements("QQ")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_table_returns_default(self, swap_country_requirements):
        swap_country_requirements({})

        result = get_requirements("DE")

        assert result is _DEFAULT_REQUIREMENTS

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_code_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_values_are_conservative(self):
        result = get_requirements("QQ")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []
        assert result.evisa_portal_url is None
        assert result.estimated_processing_days is None


class TestPassportValidityLimits:
    """Configured passport validity windows match each country's rules."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_requires_three_months(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 3

    @pytest.mark.parametrize("code", ["US", "GB", "JP", "CN", "IN", "AU", "CA", "MX"])
    def test_non_schengen_requires_six_months(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 6

    @pytest.mark.parametrize("code", ["CO", "NZ", "PA"])
    def test_non_schengen_three_month_exceptions(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 3

    def test_validity_months_are_three_or_six(self):
        values = {r.passport_validity_months for r in COUNTRY_REQUIREMENTS.values()}

        assert values <= {3, 6}


class TestVisaRules:
    """Visa and exemption data is internally consistent."""

    @pytest.mark.parametrize("code", ["CL", "JM", "CR"])
    def test_visa_free_countries(self, code):
        result = get_requirements(code)

        assert result.requires_visa is False

    @pytest.mark.parametrize("code", ["CN", "NG", "SA", "GH"])
    def test_no_exempt_nationalities(self, code):
        result = get_requirements(code)

        assert result.visa_exempt_nationalities == []

    def test_us_travelers_exempt_for_schengen(self):
        for code in SCHENGEN:
            assert "US" in get_requirements(code).visa_exempt_nationalities

    def test_country_never_exempts_its_own_code(self):
        for code, reqs in COUNTRY_REQUIREMENTS.items():
            assert code not in reqs.visa_exempt_nationalities

    def test_keys_match_country_code(self):
        for code, reqs in COUNTRY_REQUIREMENTS.items():
            assert reqs.country_code == code

    def test_processing_days_positive_when_set(self):
        for reqs in COUNTRY_REQUIREMENTS.values():
            if reqs.estimated_processing_days is not None:
                assert reqs.estimated_processing_days > 0

    def test_evisa_urls_are_https(self):
        for reqs in COUNTRY_REQUIREMENTS.values():
            if reqs.evisa_portal_url is not None:
                assert reqs.evisa_portal_url.startswith("https://")


class TestTransitVisaLayovers:
    """Transit visa flags drive whether layovers need a visa."""

    @pytest.mark.parametrize("code", ["US", "AU", "CN", "IN", "CA", "NG", "SA", "GH"])
    def test_transit_visa_required(self, code):
        result = get_requirements(code)

        assert result.transit_visa_required is True

    @pytest.mark.parametrize("code", ["DE", "GB", "JP", "AE", "QA", "TR"])
    def test_transit_visa_not_required(self, code):
        result = get_requirements(code)

        assert result.transit_visa_required is False

    def test_layover_in_transit_visa_country_alerts(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_future_segment("CN", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]

    def test_layover_in_non_transit_country_skipped(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_future_segment("AE", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_in_unlisted_country_uses_default_and_skips(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_future_segment("QQ", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestEvaluatorFallbackIntegration:
    """Evaluators use get_requirements when a destination is not overridden."""

    def test_unlisted_destination_requires_visa_via_default(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_future_segment("QQ"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.reasons == ["No visa on file for QQ"]

    def test_exempt_nationality_from_production_data(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_future_segment("DE"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_visa_free_country_from_production_data(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_future_segment("CL"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_override_takes_precedence_over_production(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_future_segment("DE"))
        override = {"DE": _reqs("DE", visa_exempt_nationalities=[])}

        result = evaluate_visa_requirements(profile, itinerary, override)

        assert result.reasons == ["No visa on file for DE"]

    def test_swapped_table_drives_evaluator(self, swap_country_requirements):
        swap_country_requirements({"DE": _reqs("DE", requires_visa=False)})
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_future_segment("DE"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestPassportLimitIntegration:
    """Passport evaluation honours each country's configured validity window."""

    def test_four_months_passes_schengen(self):
        profile = make_profile(passport_expiry=FUTURE_DEPARTURE + relativedelta(months=4))
        itinerary = make_itinerary(_future_segment("FR"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO

    def test_four_months_fails_six_month_country(self):
        profile = make_profile(passport_expiry=FUTURE_DEPARTURE + relativedelta(months=4))
        itinerary = make_itinerary(_future_segment("JP"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "JP's 6-month validity requirement" in result.reasons[0]

    def test_four_months_fails_unlisted_country_default(self):
        profile = make_profile(passport_expiry=FUTURE_DEPARTURE + relativedelta(months=4))
        itinerary = make_itinerary(_future_segment("QQ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "QQ's 6-month validity requirement" in result.reasons[0]

    def test_two_months_fails_schengen(self):
        profile = make_profile(passport_expiry=FUTURE_DEPARTURE + relativedelta(months=2))
        itinerary = make_itinerary(_future_segment("DE"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert "DE's 3-month validity requirement" in result.reasons[0]

    def test_layover_country_limit_applies(self):
        profile = make_profile(passport_expiry=FUTURE_DEPARTURE + relativedelta(months=4))
        itinerary = make_itinerary(
            _future_segment("FR"),
            _future_segment("CN", is_layover=True, flight_number="DL200"),
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert any("CN's 6-month" in r for r in result.reasons)


class TestItineraryChange:
    """Changing destinations re-resolves requirements for the new country."""

    def test_rebooking_to_stricter_country_raises_alert(self):
        profile = make_profile(passport_expiry=FUTURE_DEPARTURE + relativedelta(months=4))
        original = make_itinerary(_future_segment("DE"))
        changed = make_itinerary(_future_segment("GB"))

        before = evaluate_passport_expiry(profile, original, {})
        after = evaluate_passport_expiry(profile, changed, {})

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_rebooking_to_visa_free_country_clears_alert(self):
        profile = make_profile(nationality="IN")
        original = make_itinerary(_future_segment("CN"))
        changed = make_itinerary(_future_segment("CR"))

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is True
        assert after.is_alert_required is False

    def test_adding_transit_layover_adds_visa_alert(self):
        visa = VisaRecord("GB", "TOURIST", date(2029, 1, 1), date(2032, 1, 1), "V1")
        profile = make_profile(nationality="IN", visa_records=[visa])
        original = make_itinerary(_future_segment("GB"))
        changed = make_itinerary(
            _future_segment("US", is_layover=True, flight_number="DL1"),
            _future_segment("GB", flight_number="DL2"),
        )

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for US"]
