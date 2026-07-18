"""Tests for the country_requirements reference-data accessor."""

from src.data.country_requirements import (
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.models.types import TravelDocRequirements


class TestKnownCountry:
    """get_requirements returns the country's own TravelDocRequirements."""

    def test_returns_requirements_for_known_code(self):
        result = get_requirements("DE")

        assert isinstance(result, TravelDocRequirements)
        assert result.country_code == "DE"

    def test_returns_distinct_entries_for_distinct_codes(self):
        de = get_requirements("DE")
        us = get_requirements("US")

        assert de.country_code == "DE"
        assert us.country_code == "US"
        assert de is not us


class TestUnknownCountry:
    """get_requirements falls back to the default for unlisted codes."""

    def test_returns_default_for_unknown_code(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"

    def test_default_has_expected_fallback_values(self):
        result = get_requirements("XX")

        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []
