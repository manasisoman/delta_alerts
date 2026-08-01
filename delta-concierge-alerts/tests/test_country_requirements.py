"""Tests for country travel document requirements lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.models.types import TravelDocRequirements


class TestKnownCountryLookup:
    """get_requirements returns the exact entry registered for a known country."""

    def test_returns_registered_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_schengen_country_requires_three_month_validity(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3
        assert result.transit_visa_required is False

    def test_non_schengen_country_requires_six_month_validity(self):
        result = get_requirements("JP")

        assert result.passport_validity_months == 6

    def test_layover_country_requiring_transit_visa(self):
        result = get_requirements("CN")

        assert result.transit_visa_required is True
        assert result.visa_exempt_nationalities == []

    def test_country_not_requiring_visa(self):
        result = get_requirements("CL")

        assert result.requires_visa is False

    def test_exempt_nationality_membership(self):
        result = get_requirements("BR")

        assert "US" in result.visa_exempt_nationalities
        assert "IN" not in result.visa_exempt_nationalities

    def test_every_registered_country_resolves_to_itself(self):
        for code, requirements in COUNTRY_REQUIREMENTS.items():
            assert get_requirements(code) is requirements


class TestUnknownCountryFallback:
    """Unknown or malformed country codes fall back to the default requirements."""

    def test_unknown_code_returns_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"

    def test_default_is_conservative(self):
        result = get_requirements("ZZ")

        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_empty_code_returns_default(self):
        assert get_requirements("") is _DEFAULT_REQUIREMENTS

    def test_lookup_is_case_sensitive(self):
        assert get_requirements("de") is _DEFAULT_REQUIREMENTS
        assert get_requirements("de") is not COUNTRY_REQUIREMENTS["DE"]

    def test_full_country_name_returns_default(self):
        assert get_requirements("GERMANY") is _DEFAULT_REQUIREMENTS


class TestRequirementsDataIntegrity:
    """Every registered entry is well-formed and internally consistent."""

    def test_entries_are_typed_records_keyed_by_their_own_code(self):
        for code, requirements in COUNTRY_REQUIREMENTS.items():
            assert isinstance(requirements, TravelDocRequirements)
            assert requirements.country_code == code
            assert len(code) == 2 and code.isupper()

    def test_passport_validity_months_are_supported_values(self):
        for requirements in COUNTRY_REQUIREMENTS.values():
            assert requirements.passport_validity_months in (3, 6)

    def test_exempt_nationalities_are_unique_two_letter_codes(self):
        for requirements in COUNTRY_REQUIREMENTS.values():
            exempt = requirements.visa_exempt_nationalities
            assert len(exempt) == len(set(exempt))
            assert all(len(code) == 2 and code.isupper() for code in exempt)

    def test_no_country_exempts_its_own_nationals(self):
        for code, requirements in COUNTRY_REQUIREMENTS.items():
            assert code not in requirements.visa_exempt_nationalities

    def test_processing_days_are_positive_when_present(self):
        for requirements in COUNTRY_REQUIREMENTS.values():
            if requirements.estimated_processing_days is not None:
                assert requirements.estimated_processing_days > 0

    def test_urls_are_absolute_when_present(self):
        for requirements in COUNTRY_REQUIREMENTS.values():
            urls = (
                requirements.embassy_url,
                requirements.evisa_portal_url,
                requirements.entry_form_url,
            )
            for url in urls:
                if url is not None:
                    assert url.startswith("http")

    def test_optional_fields_default_to_none(self):
        result = get_requirements("PE")

        assert result.embassy_url is None
        assert result.evisa_portal_url is None
        assert result.entry_form_url is None
        assert result.estimated_processing_days is None
