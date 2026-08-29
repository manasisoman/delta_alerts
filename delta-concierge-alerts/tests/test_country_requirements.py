"""Tests for country requirements lookup."""

from src.data.country_requirements import COUNTRY_REQUIREMENTS, get_requirements


class TestKnownCountry:
    """Known country codes return their own requirements."""

    def test_known_country_returns_its_requirements(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"
        assert result.passport_validity_months == 3


class TestUnknownCountry:
    """Unknown country codes fall back to the visa-required 6-month default."""

    def test_unknown_country_returns_default(self):
        result = get_requirements("ZZ")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []
