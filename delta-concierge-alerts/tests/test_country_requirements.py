"""Tests for the static country requirements data and lookup helper."""

from src.data.country_requirements import COUNTRY_REQUIREMENTS, get_requirements
from src.models.types import TravelDocRequirements


class TestKnownCountryLookup:
    """A country present in the table returns its own requirements."""

    def test_returns_entry_for_known_country(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_schengen_country_requires_three_month_validity(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3
        assert "US" in result.visa_exempt_nationalities


class TestUnknownCountryFallback:
    """An unknown country code falls back to the conservative default."""

    def test_returns_default_requirements(self):
        result = get_requirements("ZZ")

        assert isinstance(result, TravelDocRequirements)
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_empty_country_code_falls_back(self):
        result = get_requirements("")

        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lowercase_country_code_is_not_matched(self):
        # Lookup is case-sensitive: keys are ISO alpha-2 uppercase.
        result = get_requirements("de")

        assert result.passport_validity_months == 6
        assert result.requires_visa is True

    def test_fallback_is_shared_instance_across_calls(self):
        first = get_requirements("ZZ")
        second = get_requirements("QQ")

        assert first is second


class TestRequirementsTableIntegrity:
    """Every entry in the table is internally consistent."""

    def test_keys_match_country_codes(self):
        mismatched = [
            code for code, req in COUNTRY_REQUIREMENTS.items() if req.country_code != code
        ]

        assert mismatched == []

    def test_validity_months_are_positive(self):
        assert all(req.passport_validity_months > 0 for req in COUNTRY_REQUIREMENTS.values())
