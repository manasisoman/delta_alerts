"""Tests for country requirement lookup in src.data.country_requirements."""

import src.data.country_requirements as cr_mod
from src.data.country_requirements import _DEFAULT_REQUIREMENTS, get_requirements
from src.models.types import TravelDocRequirements


def _requirements(country_code: str = "XA", **overrides) -> TravelDocRequirements:
    fields = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _with_requirements(table: dict[str, TravelDocRequirements], country_code: str):
    original = cr_mod.COUNTRY_REQUIREMENTS
    cr_mod.COUNTRY_REQUIREMENTS = table
    try:
        return get_requirements(country_code)
    finally:
        cr_mod.COUNTRY_REQUIREMENTS = original


class TestKnownCountryLookup:
    """A listed country code returns that country's own requirements."""

    def test_returns_listed_entry(self):
        xa = _requirements("XA")
        table = {"XA": xa, "XB": _requirements("XB", passport_validity_months=6)}

        result = _with_requirements(table, "XA")

        assert result is xa

    def test_selects_matching_entry_among_many(self):
        xb = _requirements("XB", passport_validity_months=6, transit_visa_required=True)
        table = {"XA": _requirements("XA"), "XB": xb}

        result = _with_requirements(table, "XB")

        assert result is xb
        assert result.passport_validity_months == 6
        assert result.transit_visa_required is True

    def test_lookup_is_case_sensitive(self):
        table = {"XA": _requirements("XA")}

        result = _with_requirements(table, "xa")

        assert result is _DEFAULT_REQUIREMENTS


class TestUnknownCountryFallback:
    """An unlisted country code falls back to the default requirements."""

    def test_unlisted_code_returns_default(self):
        table = {"XA": _requirements("XA")}

        result = _with_requirements(table, "ZZ")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_table_returns_default(self):
        result = _with_requirements({}, "XA")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_code_returns_default(self):
        table = {"XA": _requirements("XA")}

        result = _with_requirements(table, "")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_is_strict_six_month_visa_required(self):
        result = _with_requirements({}, "ZZ")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []


class TestProductionTableIntegrity:
    """Every entry in COUNTRY_REQUIREMENTS is internally consistent."""

    def test_keys_match_country_codes(self):
        for code, req in cr_mod.COUNTRY_REQUIREMENTS.items():
            assert req.country_code == code

    def test_passport_validity_is_three_or_six_months(self):
        for req in cr_mod.COUNTRY_REQUIREMENTS.values():
            assert req.passport_validity_months in (3, 6)

    def test_country_never_exempts_its_own_nationality(self):
        for code, req in cr_mod.COUNTRY_REQUIREMENTS.items():
            assert code not in req.visa_exempt_nationalities

    def test_schengen_countries_use_three_month_rule(self):
        for code in ("DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"):
            assert get_requirements(code).passport_validity_months == 3

    def test_transit_visa_country_is_flagged(self):
        result = get_requirements("CN")

        assert result.transit_visa_required is True
        assert result.visa_exempt_nationalities == []
