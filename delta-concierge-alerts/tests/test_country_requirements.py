"""Tests for country-specific travel document requirements data and lookup."""

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


SCHENGEN = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]
SIX_MONTH_COUNTRIES = ["US", "GB", "JP", "AU", "CN", "IN", "BR"]
TRANSIT_VISA_COUNTRIES = ["US", "AU", "CN", "IN", "CA", "NG", "SA", "GH"]
VISA_FREE_COUNTRIES = ["CL", "JM", "CR"]

DEPARTURE = date(2031, 3, 1)
ARRIVAL = date(2031, 3, 2)


def _reqs(country_code: str = "XX", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults."""
    fields = dict(
        country_code=country_code,
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=[],
    )
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _segment(destination: str = "DE", **overrides):
    """Far-future segment so date-relative evaluator branches stay stable."""
    fields = dict(destination=destination, departure=DEPARTURE, arrival=ARRIVAL)
    fields.update(overrides)
    return make_segment(**fields)


@pytest.fixture
def swapped_requirements():
    """Swap the module-level COUNTRY_REQUIREMENTS for fixture data, then restore."""
    original = country_requirements.COUNTRY_REQUIREMENTS
    fixture_data = {
        "ZZ": _reqs("ZZ", passport_validity_months=1, requires_visa=False),
    }
    country_requirements.COUNTRY_REQUIREMENTS = fixture_data
    try:
        yield fixture_data
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestGetRequirementsKnownCountry:
    """A listed country code returns its own requirements entry."""

    def test_returns_entry_for_listed_country(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_reads_module_level_mapping_at_call_time(self, swapped_requirements):
        result = get_requirements("ZZ")

        assert result is swapped_requirements["ZZ"]
        assert result.passport_validity_months == 1

    def test_previously_listed_country_falls_back_when_mapping_swapped(
        self, swapped_requirements
    ):
        result = get_requirements("DE")

        assert result is _DEFAULT_REQUIREMENTS


class TestGetRequirementsFallback:
    """Unlisted country codes fall back to the strict default requirements."""

    @pytest.mark.parametrize("code", ["XX", "", "de", "DEU"])
    def test_unlisted_code_returns_default(self, code):
        result = get_requirements(code)

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_is_strict(self):
        result = get_requirements("XX")

        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []
        assert result.evisa_portal_url is None
        assert result.estimated_processing_days is None


class TestCountryRequirementsDataIntegrity:
    """Every entry in the static data is internally consistent."""

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_entry_country_code_matches_key(self, code):
        assert COUNTRY_REQUIREMENTS[code].country_code == code

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_validity_months_is_3_or_6(self, code):
        assert COUNTRY_REQUIREMENTS[code].passport_validity_months in (3, 6)

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_country_not_exempt_from_itself(self, code):
        assert code not in COUNTRY_REQUIREMENTS[code].visa_exempt_nationalities

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_processing_days_positive_when_set(self, code):
        days = COUNTRY_REQUIREMENTS[code].estimated_processing_days

        assert days is None or days > 0

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_evisa_url_is_https_when_set(self, code):
        url = COUNTRY_REQUIREMENTS[code].evisa_portal_url

        assert url is None or url.startswith("https://")


class TestPassportValidityRules:
    """Per-country passport validity windows match documented rules."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_requires_3_months(self, code):
        assert get_requirements(code).passport_validity_months == 3

    @pytest.mark.parametrize("code", SIX_MONTH_COUNTRIES)
    def test_six_month_countries(self, code):
        assert get_requirements(code).passport_validity_months == 6

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_shares_exempt_list(self, code):
        assert get_requirements(code).visa_exempt_nationalities == (
            COUNTRY_REQUIREMENTS["DE"].visa_exempt_nationalities
        )


class TestVisaAndTransitRules:
    """Visa-required, visa-free and transit-visa flags match documented rules."""

    @pytest.mark.parametrize("code", TRANSIT_VISA_COUNTRIES)
    def test_transit_visa_required(self, code):
        assert get_requirements(code).transit_visa_required is True

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_no_transit_visa(self, code):
        assert get_requirements(code).transit_visa_required is False

    @pytest.mark.parametrize("code", VISA_FREE_COUNTRIES)
    def test_visa_free_countries(self, code):
        assert get_requirements(code).requires_visa is False

    def test_transit_visa_countries_also_require_visa(self):
        for code, reqs in COUNTRY_REQUIREMENTS.items():
            if reqs.transit_visa_required:
                assert reqs.requires_visa is True, code


class TestEvaluatorsUseRealData:
    """Evaluators fall back to get_requirements when the override dict lacks a destination."""

    def test_passport_meets_schengen_3_months_but_flagged_info(self):
        # 4 months of validity at travel: passes DE's 3-month rule, under 6-month advisory
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO

    def test_same_passport_fails_6_month_country(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(_segment("JP"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet JP's 6-month" in r for r in result.reasons)

    def test_unlisted_destination_uses_default_6_months(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(_segment("XX"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet XX's 6-month" in r for r in result.reasons)

    def test_unlisted_destination_requires_visa_by_default(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("XX"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XX"]

    def test_visa_exempt_nationality_no_alert(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_visa_free_country_no_alert_for_any_nationality(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CL"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_override_dict_takes_precedence_over_real_data(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("DE"))
        overrides = {"DE": _reqs("DE", visa_exempt_nationalities=[])}

        result = evaluate_visa_requirements(profile, itinerary, overrides)

        assert result.reasons == ["No visa on file for DE"]


class TestLayovers:
    """Transit-visa flag controls whether a layover triggers a visa alert."""

    def test_layover_in_transit_visa_country_alerts(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CN", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]

    def test_layover_in_schengen_skipped(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("DE", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_in_unlisted_country_skipped_by_default(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("XX", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_still_checks_passport_validity(self):
        profile = make_profile(nationality="IN", passport_expiry=date(2031, 7, 1))
        itinerary = make_itinerary(_segment("CN", is_layover=True))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_valid_transit_visa_clears_layover_alert(self):
        visa = VisaRecord(
            country_code="CN",
            visa_type="TRANSIT",
            issue_date=date(2030, 1, 1),
            expiry_date=date(2033, 1, 1),
            visa_number="V1",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(_segment("CN", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestItineraryChanges:
    """Changing an itinerary's destination re-resolves requirements for the new country."""

    def test_rebooking_from_schengen_to_6_month_country_escalates(self):
        profile = make_profile(nationality="US", passport_expiry=date(2031, 7, 1))
        original = make_itinerary(_segment("FR"))
        changed = make_itinerary(_segment("GB"))

        before = evaluate_passport_expiry(profile, original, {})
        after = evaluate_passport_expiry(profile, changed, {})

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_rebooking_to_visa_free_country_clears_visa_alert(self):
        profile = make_profile(nationality="IN")
        original = make_itinerary(_segment("TH"))
        changed = make_itinerary(_segment("CR"))

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.reasons == ["No visa on file for TH"]
        assert after.is_alert_required is False

    def test_adding_transit_layover_adds_visa_alert(self):
        profile = make_profile(nationality="IN")
        original = make_itinerary(_segment("CL"))
        changed = make_itinerary(
            _segment("US", is_layover=True, flight_number="DL1"),
            _segment("CL", flight_number="DL2"),
        )

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for US"]
