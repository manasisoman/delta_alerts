"""Tests for country-specific travel document requirements data and lookup."""

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.models.types import TravelDocRequirements


class TestGetRequirementsKnownCountry:
    """Looking up a known country code returns the correct entry."""

    def test_returns_requirements_for_known_country(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_schengen_country_has_3_month_validity(self):
        result = get_requirements("FR")

        assert result.passport_validity_months == 3
        assert result.requires_visa is True
        assert result.transit_visa_required is False

    def test_non_schengen_country_has_6_month_validity(self):
        result = get_requirements("US")

        assert result.passport_validity_months == 6
        assert result.requires_visa is True
        assert result.transit_visa_required is True


class TestGetRequirementsDefaultFallback:
    """Unknown country codes fall back to the default requirements."""

    def test_unknown_country_returns_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"

    def test_default_has_6_month_validity(self):
        result = get_requirements("XX")

        assert result.passport_validity_months == 6

    def test_default_requires_visa(self):
        result = get_requirements("XX")

        assert result.requires_visa is True

    def test_default_no_transit_visa(self):
        result = get_requirements("XX")

        assert result.transit_visa_required is False

    def test_default_empty_exempt_list(self):
        result = get_requirements("XX")

        assert result.visa_exempt_nationalities == []


class TestCountryRequirementsData:
    """Static COUNTRY_REQUIREMENTS dictionary contains expected entries."""

    def test_all_entries_are_travel_doc_requirements(self):
        for code, req in COUNTRY_REQUIREMENTS.items():
            assert isinstance(req, TravelDocRequirements), f"{code} is not TravelDocRequirements"

    def test_country_code_matches_key(self):
        for code, req in COUNTRY_REQUIREMENTS.items():
            assert req.country_code == code, f"Key {code} != country_code {req.country_code}"

    def test_schengen_countries_have_3_month_validity(self):
        schengen_codes = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]
        for code in schengen_codes:
            req = COUNTRY_REQUIREMENTS[code]

            assert req.passport_validity_months == 3, f"{code} should require 3-month validity"

    def test_six_month_countries(self):
        six_month_codes = ["US", "GB", "JP", "AU", "CN", "IN", "BR"]
        for code in six_month_codes:
            req = COUNTRY_REQUIREMENTS[code]

            assert req.passport_validity_months == 6, f"{code} should require 6-month validity"

    def test_china_has_no_visa_exemptions(self):
        req = COUNTRY_REQUIREMENTS["CN"]

        assert req.visa_exempt_nationalities == []
        assert req.transit_visa_required is True

    def test_chile_does_not_require_visa(self):
        req = COUNTRY_REQUIREMENTS["CL"]

        assert req.requires_visa is False

    def test_country_with_optional_fields(self):
        req = COUNTRY_REQUIREMENTS["CA"]

        assert req.embassy_url is not None
        assert req.evisa_portal_url is not None
        assert req.estimated_processing_days == 14

    def test_country_without_optional_fields(self):
        req = COUNTRY_REQUIREMENTS["DE"]

        assert req.embassy_url is None
        assert req.evisa_portal_url is None
        assert req.estimated_processing_days is None

    def test_entry_form_url_present_for_israel(self):
        req = COUNTRY_REQUIREMENTS["IL"]

        assert req.entry_form_url is not None

    def test_total_country_count(self):
        assert len(COUNTRY_REQUIREMENTS) >= 35


class TestVisaExemptionData:
    """Visa exemption lists reflect expected diplomatic relationships."""

    def test_us_exempt_from_schengen(self):
        for code in ["DE", "FR", "IT", "ES", "NL"]:
            req = COUNTRY_REQUIREMENTS[code]

            assert "US" in req.visa_exempt_nationalities, f"US should be exempt for {code}"

    def test_india_exempts_nepal_and_bhutan(self):
        req = COUNTRY_REQUIREMENTS["IN"]

        assert "NP" in req.visa_exempt_nationalities
        assert "BT" in req.visa_exempt_nationalities

    def test_australia_only_exempts_new_zealand(self):
        req = COUNTRY_REQUIREMENTS["AU"]

        assert req.visa_exempt_nationalities == ["NZ"]


class TestTransitVisaRequirements:
    """Transit visa requirements vary by country."""

    def test_us_requires_transit_visa(self):
        assert COUNTRY_REQUIREMENTS["US"].transit_visa_required is True

    def test_schengen_no_transit_visa(self):
        for code in ["DE", "FR", "IT"]:
            assert COUNTRY_REQUIREMENTS[code].transit_visa_required is False

    def test_australia_requires_transit_visa(self):
        assert COUNTRY_REQUIREMENTS["AU"].transit_visa_required is True

    def test_ghana_requires_transit_visa(self):
        assert COUNTRY_REQUIREMENTS["GH"].transit_visa_required is True
