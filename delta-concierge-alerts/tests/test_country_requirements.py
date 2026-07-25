"""Tests for country travel document requirements lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.models.types import TravelDocRequirements


class TestKnownCountryLookup:
    """A listed country code returns that country's requirements."""

    def test_returns_entry_for_listed_country(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_schengen_country_requires_three_month_validity(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3

    def test_non_schengen_country_requires_six_month_validity(self):
        result = get_requirements("JP")

        assert result.passport_validity_months == 6

    def test_country_without_visa_requirement(self):
        result = get_requirements("CL")

        assert result.requires_visa is False

    def test_country_requiring_transit_visa(self):
        result = get_requirements("CN")

        assert result.transit_visa_required is True
        assert result.visa_exempt_nationalities == []

    def test_country_with_exempt_nationalities(self):
        result = get_requirements("GB")

        assert "US" in result.visa_exempt_nationalities
        assert "CN" not in result.visa_exempt_nationalities

    def test_country_with_optional_guidance_fields(self):
        result = get_requirements("CA")

        assert result.embassy_url is not None
        assert result.evisa_portal_url is not None
        assert result.estimated_processing_days == 14

    def test_country_without_optional_guidance_fields(self):
        result = get_requirements("PE")

        assert result.embassy_url is None
        assert result.evisa_portal_url is None
        assert result.entry_form_url is None
        assert result.estimated_processing_days is None


class TestUnknownCountryFallback:
    """Unlisted country codes fall back to the conservative default."""

    def test_unknown_code_returns_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"

    def test_default_is_conservative(self):
        result = get_requirements("XX")

        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lowercase_code_is_not_matched(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_code_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS


class TestRequirementsDataIntegrity:
    """Every entry is well formed and self-consistent."""

    def test_all_entries_are_requirements_objects(self):
        assert all(
            isinstance(req, TravelDocRequirements)
            for req in COUNTRY_REQUIREMENTS.values()
        )

    def test_keys_match_country_codes(self):
        mismatched = [
            code for code, req in COUNTRY_REQUIREMENTS.items()
            if code != req.country_code
        ]

        assert mismatched == []

    def test_country_codes_are_two_letter_uppercase(self):
        invalid = [
            code for code in COUNTRY_REQUIREMENTS
            if len(code) != 2 or not code.isupper()
        ]

        assert invalid == []

    def test_passport_validity_months_are_positive(self):
        invalid = [
            code for code, req in COUNTRY_REQUIREMENTS.items()
            if req.passport_validity_months <= 0
        ]

        assert invalid == []

    def test_no_country_exempts_itself(self):
        self_exempt = [
            code for code, req in COUNTRY_REQUIREMENTS.items()
            if code in req.visa_exempt_nationalities
        ]

        assert self_exempt == []
