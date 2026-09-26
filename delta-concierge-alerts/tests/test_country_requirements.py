"""Tests for the static country requirements lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)


class TestGetRequirements:
    """Known codes return their entry; unknown codes fall back to defaults."""

    def test_known_country_returns_its_entry(self):
        result = get_requirements("US")

        assert result is COUNTRY_REQUIREMENTS["US"]
        assert result.country_code == "US"

    def test_unknown_country_falls_back_to_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("us")

        assert result is _DEFAULT_REQUIREMENTS
