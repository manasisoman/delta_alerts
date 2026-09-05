"""Tests for the static country requirements lookup."""

from src.data.country_requirements import (
    _DEFAULT_REQUIREMENTS,
    COUNTRY_REQUIREMENTS,
    get_requirements,
)


class TestGetRequirements:
    """get_requirements returns the listed entry or the default fallback."""

    def test_known_country_returns_its_entry(self):
        result = get_requirements("CN")

        assert result is COUNTRY_REQUIREMENTS["CN"]
        assert result.country_code == "CN"
        assert result.requires_visa is True
        assert result.transit_visa_required is True
        assert result.passport_validity_months == 6

    def test_schengen_country_has_three_month_validity(self):
        result = get_requirements("DE")

        assert result.passport_validity_months == 3
        assert "US" in result.visa_exempt_nationalities

    def test_unknown_country_returns_default(self):
        result = get_requirements("XX")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("cn")

        assert result is _DEFAULT_REQUIREMENTS


class TestCountryRequirementsData:
    """Static data is internally consistent."""

    def test_every_entry_key_matches_country_code(self):
        for code, reqs in COUNTRY_REQUIREMENTS.items():
            assert reqs.country_code == code

    def test_no_country_is_visa_exempt_for_itself(self):
        for code, reqs in COUNTRY_REQUIREMENTS.items():
            assert code not in reqs.visa_exempt_nationalities
