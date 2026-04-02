"""Tests for src.data.country_requirements — get_requirements lookup logic."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)


class TestGetRequirementsKnownCountry:
    """get_requirements returns the correct entry for a known country code."""

    def test_known_country_returns_matching_entry(self):
        result = get_requirements("DE")

        assert result.country_code == "DE"
        assert result.passport_validity_months == 3
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert "US" in result.visa_exempt_nationalities

    def test_known_country_matches_dict_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]


class TestGetRequirementsFallback:
    """get_requirements falls back to _DEFAULT_REQUIREMENTS for unknown codes."""

    def test_unknown_country_returns_default(self):
        result = get_requirements("XX")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_has_expected_country_code(self):
        result = get_requirements("ZZ")

        assert result.country_code == "DEFAULT"

    def test_default_requires_visa(self):
        result = get_requirements("XX")

        assert result.requires_visa is True

    def test_default_no_transit_visa(self):
        result = get_requirements("XX")

        assert result.transit_visa_required is False

    def test_default_passport_validity_six_months(self):
        result = get_requirements("XX")

        assert result.passport_validity_months == 6

    def test_default_no_visa_exemptions(self):
        result = get_requirements("XX")

        assert result.visa_exempt_nationalities == []
