"""Tests for src.data.country_requirements — get_requirements lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)


class TestGetRequirementsKnownCountry:
    """get_requirements returns the matching entry for a known country code."""

    def test_returns_de_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 3
        assert "US" in result.visa_exempt_nationalities

    def test_returns_us_entry(self):
        result = get_requirements("US")

        assert result is COUNTRY_REQUIREMENTS["US"]
        assert result.country_code == "US"
        assert result.requires_visa is True
        assert result.transit_visa_required is True
        assert result.passport_validity_months == 6

    def test_returns_cn_entry(self):
        result = get_requirements("CN")

        assert result is COUNTRY_REQUIREMENTS["CN"]
        assert result.country_code == "CN"
        assert result.visa_exempt_nationalities == []


class TestGetRequirementsUnknownCountry:
    """get_requirements falls back to _DEFAULT_REQUIREMENTS for unknown codes."""

    def test_unknown_code_returns_default(self):
        result = get_requirements("ZZ")

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
        """Codes are case-sensitive; lowercase 'de' is not found."""
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS
