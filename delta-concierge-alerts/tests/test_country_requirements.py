"""Tests for the country requirements lookup data module."""

from src.data.country_requirements import (
    _DEFAULT_REQUIREMENTS,
    COUNTRY_REQUIREMENTS,
    get_requirements,
)
from src.models.types import TravelDocRequirements


class TestKnownCountryLookup:
    """A listed country code returns its own TravelDocRequirements entry."""

    def test_known_code_returns_matching_entry(self):
        result = get_requirements("DE")

        assert isinstance(result, TravelDocRequirements)
        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_every_entry_is_keyed_by_its_own_country_code(self):
        mismatched = [
            code for code, req in COUNTRY_REQUIREMENTS.items() if req.country_code != code
        ]

        assert mismatched == []


class TestUnknownCountryFallback:
    """An unlisted country code falls back to the default requirements."""

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

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_code_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS


class TestRepresentativeEntries:
    """Representative countries carry sane validity and visa values."""

    def test_schengen_country_requires_three_months(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert "US" in result.visa_exempt_nationalities

    def test_us_requires_six_months_and_transit_visa(self):
        result = get_requirements("US")

        assert result.passport_validity_months == 6
        assert result.requires_visa is True
        assert result.transit_visa_required is True
        assert "GB" in result.visa_exempt_nationalities

    def test_china_requires_six_months_with_no_exemptions(self):
        result = get_requirements("CN")

        assert result.passport_validity_months == 6
        assert result.requires_visa is True
        assert result.transit_visa_required is True
        assert result.visa_exempt_nationalities == []


class TestRequirementValueRanges:
    """All entries stay within sane bounds for evaluator consumption."""

    def test_passport_validity_months_are_positive(self):
        assert all(
            req.passport_validity_months > 0 for req in COUNTRY_REQUIREMENTS.values()
        )

    def test_no_country_is_exempt_from_itself(self):
        self_exempt = [
            code
            for code, req in COUNTRY_REQUIREMENTS.items()
            if code in req.visa_exempt_nationalities
        ]

        assert self_exempt == []
