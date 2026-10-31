"""Tests for country-specific travel document requirements data and lookup."""

from datetime import date

import pytest

from src.data import country_requirements as cr_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS, get_requirements
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment

SCHENGEN = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]


def _fake_requirements(**overrides) -> TravelDocRequirements:
    fields = dict(
        country_code="XA",
        requires_visa=False,
        transit_visa_required=False,
        passport_validity_months=1,
        visa_exempt_nationalities=["ZZ"],
    )
    fields.update(overrides)
    return TravelDocRequirements(**fields)


@pytest.fixture
def swapped_requirements():
    """Swap the module-level COUNTRY_REQUIREMENTS with an isolated dict."""
    original = cr_mod.COUNTRY_REQUIREMENTS
    fake = {"XA": _fake_requirements()}
    cr_mod.COUNTRY_REQUIREMENTS = fake
    try:
        yield fake
    finally:
        cr_mod.COUNTRY_REQUIREMENTS = original


class TestKnownCountryLookup:
    """get_requirements returns the exact entry for a listed country code."""

    def test_returns_listed_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_lookup_reads_module_level_dict(self, swapped_requirements):
        result = get_requirements("XA")

        assert result is swapped_requirements["XA"]
        assert result.passport_validity_months == 1

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result.country_code == "DEFAULT"


class TestDefaultFallback:
    """Unknown country codes fall back to the conservative default."""

    @pytest.mark.parametrize("code", ["ZZ", "", "USA"])
    def test_unknown_code_returns_default(self, code):
        result = get_requirements(code)

        assert result is cr_mod._DEFAULT_REQUIREMENTS

    def test_default_values(self):
        result = get_requirements("ZZ")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_listed_code_missing_after_swap_uses_default(self, swapped_requirements):
        result = get_requirements("DE")

        assert result is cr_mod._DEFAULT_REQUIREMENTS


class TestDataIntegrity:
    """Every entry in COUNTRY_REQUIREMENTS is internally consistent."""

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_entry_is_well_formed(self, code):
        reqs = COUNTRY_REQUIREMENTS[code]

        assert isinstance(reqs, TravelDocRequirements)
        assert reqs.country_code == code
        assert len(code) == 2 and code.isupper()
        assert reqs.passport_validity_months in (3, 6)
        assert code not in reqs.visa_exempt_nationalities
        assert all(len(n) == 2 and n.isupper() for n in reqs.visa_exempt_nationalities)

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_optional_fields_have_valid_types(self, code):
        reqs = COUNTRY_REQUIREMENTS[code]

        for url in (reqs.embassy_url, reqs.evisa_portal_url, reqs.entry_form_url):
            assert url is None or url.startswith(("https://", "http://"))
        assert reqs.estimated_processing_days is None or reqs.estimated_processing_days > 0


class TestPassportValidityWindows:
    """Schengen countries need 3 months; non-Schengen need 6."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_three_months(self, code):
        assert get_requirements(code).passport_validity_months == 3

    @pytest.mark.parametrize("code", ["US", "GB", "JP", "CN", "IN", "AU"])
    def test_non_schengen_six_months(self, code):
        assert get_requirements(code).passport_validity_months == 6


class TestVisaExemptions:
    """Visa-exempt nationality lists per destination."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_us_exempt_from_schengen(self, code):
        assert "US" in get_requirements(code).visa_exempt_nationalities

    def test_china_has_no_exemptions(self):
        assert get_requirements("CN").visa_exempt_nationalities == []

    def test_australia_only_exempts_new_zealand(self):
        assert get_requirements("AU").visa_exempt_nationalities == ["NZ"]

    def test_us_not_exempt_from_turkey(self):
        assert "US" not in get_requirements("TR").visa_exempt_nationalities


class TestTransitVisaRules:
    """Layover countries that require a transit visa."""

    @pytest.mark.parametrize("code", ["US", "AU", "CN", "IN", "CA"])
    def test_transit_visa_required(self, code):
        assert get_requirements(code).transit_visa_required is True

    @pytest.mark.parametrize("code", ["DE", "GB", "JP"])
    def test_transit_visa_not_required(self, code):
        assert get_requirements(code).transit_visa_required is False


class TestEvaluatorIntegration:
    """Evaluators fall back to get_requirements when no override is supplied."""

    def test_passport_valid_for_schengen_destination(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="FR", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_passport_within_window_for_unknown_destination_uses_default(self):
        profile = make_profile(nationality="US", passport_expiry=date(2030, 9, 1))
        itinerary = make_itinerary(
            make_segment(destination="ZZ", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is True

    def test_expired_passport_flagged(self):
        profile = make_profile(nationality="US", passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_visa_exempt_nationality_needs_no_visa(self):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_non_exempt_nationality_without_visa_is_critical(self):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_valid_visa_on_file_clears_alert(self):
        visa = VisaRecord(
            country_code="CN",
            visa_type="tourist",
            issue_date=date(2029, 1, 1),
            expiry_date=date(2032, 1, 1),
            visa_number="V123",
        )
        profile = make_profile(nationality="US", visa_records=[visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=date(2030, 6, 1), arrival=date(2030, 6, 2))
        )

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False
