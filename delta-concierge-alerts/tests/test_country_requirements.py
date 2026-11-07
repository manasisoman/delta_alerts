"""Tests for country requirements lookup and static requirements data."""

from datetime import date

import pytest

from src.data import country_requirements
from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2031, 3, 1)
ARRIVAL = date(2031, 3, 2)


def _make_requirements(country_code: str = "XA", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults."""
    fields = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


@pytest.fixture
def swapped_requirements():
    """Swap COUNTRY_REQUIREMENTS for an isolated fixture dict; restore afterwards."""
    original = country_requirements.COUNTRY_REQUIREMENTS
    fixture_data = {
        "XA": _make_requirements("XA", passport_validity_months=3),
        "XB": _make_requirements("XB", requires_visa=False),
    }
    country_requirements.COUNTRY_REQUIREMENTS = fixture_data
    try:
        yield fixture_data
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestKnownCountryLookup:
    """get_requirements returns the configured entry for a listed country."""

    def test_returns_entry_from_mapping(self, swapped_requirements):
        result = get_requirements("XA")

        assert result is swapped_requirements["XA"]

    def test_returns_distinct_entries_per_country(self, swapped_requirements):
        result_a = get_requirements("XA")
        result_b = get_requirements("XB")

        assert result_a.country_code == "XA"
        assert result_b.country_code == "XB"
        assert result_b.requires_visa is False

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_every_real_country_resolves_to_its_own_entry(self, code):
        result = get_requirements(code)

        assert result is COUNTRY_REQUIREMENTS[code]
        assert result.country_code == code


class TestDefaultFallback:
    """Unknown country codes fall back to the 6-month, visa-required default."""

    def test_unknown_code_returns_default(self, swapped_requirements):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_values(self):
        result = get_requirements("ZZ")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []
        assert result.embassy_url is None
        assert result.evisa_portal_url is None
        assert result.estimated_processing_days is None
        assert result.entry_form_url is None

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_empty_code_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_lookup_reads_mapping_at_call_time(self, swapped_requirements):
        result = get_requirements("DE")

        assert result is _DEFAULT_REQUIREMENTS


class TestRequirementsDataIntegrity:
    """Every static entry is well-formed and keyed by its own country code."""

    @pytest.mark.parametrize("code,reqs", sorted(COUNTRY_REQUIREMENTS.items()))
    def test_entry_is_well_formed(self, code, reqs):
        assert isinstance(reqs, TravelDocRequirements)
        assert reqs.country_code == code
        assert len(code) == 2 and code.isupper()
        assert isinstance(reqs.requires_visa, bool)
        assert isinstance(reqs.transit_visa_required, bool)
        assert reqs.passport_validity_months in (0, 3, 6)
        assert code not in reqs.visa_exempt_nationalities
        assert all(len(n) == 2 and n.isupper() for n in reqs.visa_exempt_nationalities)

    @pytest.mark.parametrize("code,reqs", sorted(COUNTRY_REQUIREMENTS.items()))
    def test_optional_fields_are_valid(self, code, reqs):
        for url in (reqs.embassy_url, reqs.evisa_portal_url, reqs.entry_form_url):
            assert url is None or url.startswith(("https://", "http://"))
        assert reqs.estimated_processing_days is None or reqs.estimated_processing_days > 0

    def test_default_not_in_mapping(self):
        assert "DEFAULT" not in COUNTRY_REQUIREMENTS


class TestSchengenRequirements:
    """Schengen-area countries require 3 months of passport validity."""

    @pytest.mark.parametrize("code", ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "PT", "GR"])
    def test_schengen_three_month_validity(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 3
        assert result.transit_visa_required is False
        assert "US" in result.visa_exempt_nationalities

    def test_non_schengen_six_month_validity(self):
        result = get_requirements("CN")

        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []


class TestPassportLimitsFromCountryData:
    """Passport evaluator applies real country validity windows via the lookup."""

    def test_schengen_passport_meets_three_months(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(make_segment("FR", DEPARTURE, ARRIVAL))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert all("validity requirement" not in r for r in result.reasons)
        assert result.severity == AlertSeverity.INFO

    def test_six_month_country_rejects_four_months(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(make_segment("CN", DEPARTURE, ARRIVAL))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet CN's 6-month validity requirement for DL100" in result.reasons

    def test_unknown_country_uses_default_six_months(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(make_segment("ZZ", DEPARTURE, ARRIVAL))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet ZZ's 6-month validity requirement for DL100" in result.reasons

    def test_layover_country_validity_is_checked(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(
            make_segment("CN", DEPARTURE, ARRIVAL, is_layover=True, flight_number="DL1"),
            make_segment("FR", date(2031, 3, 3), date(2031, 3, 4), flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet CN's 6-month validity requirement for DL1" in result.reasons

    def test_long_validity_passport_has_no_alert(self):
        profile = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("CN", DEPARTURE, ARRIVAL))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is False


class TestVisaRulesFromCountryData:
    """Visa evaluator applies real exemptions and transit rules via the lookup."""

    def test_exempt_nationality_needs_no_visa(self):
        profile = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("DE", DEPARTURE, ARRIVAL))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_non_exempt_nationality_without_visa_is_critical(self):
        profile = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("DE", DEPARTURE, ARRIVAL))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons

    def test_layover_skipped_when_transit_visa_not_required(self):
        profile = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("GB", DEPARTURE, ARRIVAL, is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_flagged_when_transit_visa_required(self):
        profile = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("CN", DEPARTURE, ARRIVAL, is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_unknown_country_requires_visa_by_default(self):
        profile = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("ZZ", DEPARTURE, ARRIVAL))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert "No visa on file for ZZ" in result.reasons

    def test_unknown_country_layover_skipped_by_default(self):
        profile = make_profile(nationality="US", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("ZZ", DEPARTURE, ARRIVAL, is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_valid_visa_satisfies_requirement(self):
        visa = VisaRecord(
            country_code="CN",
            visa_type="tourist",
            issue_date=date(2030, 1, 1),
            expiry_date=date(2033, 1, 1),
            visa_number="V-CN-1",
        )
        profile = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1), visa_records=[visa])
        itinerary = make_itinerary(make_segment("CN", DEPARTURE, ARRIVAL))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_visa_not_required_country_in_swapped_data(self, swapped_requirements):
        profile = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(make_segment("XB", DEPARTURE, ARRIVAL))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestItineraryChangeUsesNewCountryRequirements:
    """Re-evaluating a changed itinerary applies the new destination's rules."""

    def test_rerouted_from_schengen_to_six_month_country(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        original = make_itinerary(make_segment("FR", DEPARTURE, ARRIVAL))
        changed = make_itinerary(make_segment("JP", DEPARTURE, ARRIVAL))

        before = evaluate_passport_expiry(profile, original, {})
        after = evaluate_passport_expiry(profile, changed, {})

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_rerouted_to_add_transit_visa_layover(self):
        profile = make_profile(nationality="IN", passport_expiry=date(2035, 1, 1))
        visa = VisaRecord("FR", "tourist", date(2030, 1, 1), date(2033, 1, 1), "V-FR-1")
        profile.visa_records = [visa]
        original = make_itinerary(make_segment("FR", DEPARTURE, ARRIVAL))
        changed = make_itinerary(
            make_segment("US", DEPARTURE, ARRIVAL, is_layover=True, flight_number="DL1"),
            make_segment("FR", date(2031, 3, 3), date(2031, 3, 4), flight_number="DL2"),
        )

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for US"]
