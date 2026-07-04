"""Tests for country requirements lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.models.types import TravelDocRequirements


class TestGetRequirementsKnownCountry:
    """get_requirements returns the correct entry for listed country codes."""

    def test_returns_germany_requirements(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert isinstance(result, TravelDocRequirements)
        assert result.country_code == "DE"

    def test_returns_china_requirements(self):
        result = get_requirements("CN")

        assert result is COUNTRY_REQUIREMENTS["CN"]
        assert result.country_code == "CN"

    def test_returns_japan_requirements(self):
        result = get_requirements("JP")

        assert result is COUNTRY_REQUIREMENTS["JP"]
        assert result.country_code == "JP"


class TestGetRequirementsUnknownCountry:
    """get_requirements falls back to _DEFAULT_REQUIREMENTS for unlisted codes."""

    def test_unknown_code_returns_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_string_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_lowercase_code_returns_default(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS


class TestKnownCountryFields:
    """Verify key fields of specific country requirements."""

    def test_germany_schengen_fields(self):
        reqs = get_requirements("DE")

        assert reqs.requires_visa is True
        assert reqs.transit_visa_required is False
        assert reqs.passport_validity_months == 3
        assert "US" in reqs.visa_exempt_nationalities

    def test_us_fields(self):
        reqs = get_requirements("US")

        assert reqs.requires_visa is True
        assert reqs.transit_visa_required is True
        assert reqs.passport_validity_months == 6
        assert "GB" in reqs.visa_exempt_nationalities

    def test_chile_no_visa_required(self):
        reqs = get_requirements("CL")

        assert reqs.requires_visa is False
        assert reqs.passport_validity_months == 6

    def test_canada_optional_fields(self):
        reqs = get_requirements("CA")

        assert reqs.embassy_url is not None
        assert reqs.evisa_portal_url is not None
        assert reqs.estimated_processing_days == 14

    def test_china_no_exempt_nationalities(self):
        reqs = get_requirements("CN")

        assert reqs.visa_exempt_nationalities == []
        assert reqs.transit_visa_required is True


class TestDefaultRequirements:
    """_DEFAULT_REQUIREMENTS has the expected fallback values."""

    def test_default_fields(self):
        assert _DEFAULT_REQUIREMENTS.country_code == "DEFAULT"
        assert _DEFAULT_REQUIREMENTS.requires_visa is True
        assert _DEFAULT_REQUIREMENTS.transit_visa_required is False
        assert _DEFAULT_REQUIREMENTS.passport_validity_months == 6
        assert _DEFAULT_REQUIREMENTS.visa_exempt_nationalities == []
