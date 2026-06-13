"""Tests for country requirements data lookups."""

from src.data.country_requirements import COUNTRY_REQUIREMENTS, get_requirements


class TestKnownCountryLookup:
    """Looking up a known country code returns that country's requirements."""

    def test_known_country_returns_matching_requirements(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"
        assert result.passport_validity_months == 3

    def test_another_known_country(self):
        result = get_requirements("JP")

        assert result is COUNTRY_REQUIREMENTS["JP"]
        assert result.country_code == "JP"
        assert result.passport_validity_months == 6


class TestUnknownCountryFallback:
    """Looking up an unknown country code returns the default requirements."""

    def test_unknown_country_returns_default(self):
        result = get_requirements("XX")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_another_unknown_country(self):
        result = get_requirements("ZZ")

        assert result.country_code == "DEFAULT"
