"""Tests for the country_requirements data module."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)


class TestGetRequirementsKnownCountry:
    """get_requirements returns the correct entry for a listed country."""

    def test_known_country_returns_matching_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"
        assert result.passport_validity_months == 3

    def test_known_country_with_six_month_requirement(self):
        result = get_requirements("CN")

        assert result is COUNTRY_REQUIREMENTS["CN"]
        assert result.country_code == "CN"
        assert result.passport_validity_months == 6
        assert result.transit_visa_required is True
        assert result.visa_exempt_nationalities == []


class TestGetRequirementsUnknownCountry:
    """get_requirements falls back to _DEFAULT_REQUIREMENTS for unlisted codes."""

    def test_unknown_country_returns_default(self):
        result = get_requirements("XX")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_empty_string_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_lowercase_code_returns_default(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS


class TestCountryRequirementsRegistry:
    """COUNTRY_REQUIREMENTS contains expected countries with correct data."""

    def test_contains_schengen_countries(self):
        schengen = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]
        for code in schengen:
            assert code in COUNTRY_REQUIREMENTS
            assert COUNTRY_REQUIREMENTS[code].passport_validity_months == 3

    def test_contains_non_schengen_countries(self):
        non_schengen = ["US", "GB", "JP", "AU", "CN", "IN", "BR"]
        for code in non_schengen:
            assert code in COUNTRY_REQUIREMENTS
            assert COUNTRY_REQUIREMENTS[code].passport_validity_months == 6

    def test_country_code_matches_key(self):
        for code, req in COUNTRY_REQUIREMENTS.items():
            assert req.country_code == code

    def test_us_requires_transit_visa(self):
        us = COUNTRY_REQUIREMENTS["US"]

        assert us.requires_visa is True
        assert us.transit_visa_required is True

    def test_chile_does_not_require_visa(self):
        cl = COUNTRY_REQUIREMENTS["CL"]

        assert cl.requires_visa is False
