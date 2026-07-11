"""Tests for country travel-document requirements lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)


class TestKnownCountry:
    """Looking up a listed country returns its own requirements entry."""

    def test_returns_matching_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_schengen_country_three_month_validity(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3

    def test_non_schengen_country_six_month_validity(self):
        result = get_requirements("JP")

        assert result.passport_validity_months == 6


class TestUnknownCountry:
    """Looking up an unlisted country falls back to the default requirements."""

    def test_returns_default_object(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_field_values(self):
        result = get_requirements("ZZ")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_empty_string_falls_back_to_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS
