"""Tests for the static country requirements data and its lookup helper."""

import re
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


DEPARTURE = date(2030, 6, 1)
ARRIVAL = date(2030, 6, 2)
FAR_FUTURE = date(2035, 1, 1)

SCHENGEN = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]


def _segment(destination: str, **overrides):
    kwargs = {"destination": destination, "departure": DEPARTURE, "arrival": ARRIVAL}
    kwargs.update(overrides)
    return make_segment(**kwargs)


def _visa(country_code: str, expiry: date = FAR_FUTURE) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="tourist",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry,
        visa_number=f"V-{country_code}",
    )


@pytest.fixture
def swapped_requirements():
    """Temporarily replace COUNTRY_REQUIREMENTS with a made-up dataset."""
    original = country_requirements.COUNTRY_REQUIREMENTS
    fake = {
        "XA": TravelDocRequirements(
            country_code="XA",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=1,
            visa_exempt_nationalities=[],
        ),
    }
    country_requirements.COUNTRY_REQUIREMENTS = fake
    try:
        yield fake
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestKnownCountryLookup:
    """Listed country codes return their exact configured entry."""

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_returns_configured_entry(self, code):
        result = get_requirements(code)

        assert result is COUNTRY_REQUIREMENTS[code]
        assert result.country_code == code

    def test_happy_path_germany(self):
        result = get_requirements("DE")

        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 3
        assert "US" in result.visa_exempt_nationalities

    def test_lookup_reads_module_global_at_call_time(self, swapped_requirements):
        result = get_requirements("XA")

        assert result is swapped_requirements["XA"]

    def test_swapped_dataset_hides_production_entries(self, swapped_requirements):
        result = get_requirements("DE")

        assert result is _DEFAULT_REQUIREMENTS


class TestDefaultFallback:
    """Unknown or malformed codes fall back to the conservative default."""

    @pytest.mark.parametrize("code", ["ZZ", "XX", "", "de", "DEU", " DE"])
    def test_unknown_code_returns_default(self, code):
        result = get_requirements(code)

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

    def test_default_not_in_dataset(self):
        assert "DEFAULT" not in COUNTRY_REQUIREMENTS
        assert _DEFAULT_REQUIREMENTS not in COUNTRY_REQUIREMENTS.values()


class TestDataIntegrity:
    """Every entry in the static dataset is well-formed."""

    @pytest.mark.parametrize("code,reqs", sorted(COUNTRY_REQUIREMENTS.items()))
    def test_key_matches_country_code(self, code, reqs):
        assert re.fullmatch(r"[A-Z]{2}", code)
        assert reqs.country_code == code

    @pytest.mark.parametrize("code,reqs", sorted(COUNTRY_REQUIREMENTS.items()))
    def test_field_types_and_ranges(self, code, reqs):
        assert isinstance(reqs, TravelDocRequirements)
        assert isinstance(reqs.requires_visa, bool)
        assert isinstance(reqs.transit_visa_required, bool)
        assert reqs.passport_validity_months in (3, 6)
        if reqs.estimated_processing_days is not None:
            assert reqs.estimated_processing_days > 0

    @pytest.mark.parametrize("code,reqs", sorted(COUNTRY_REQUIREMENTS.items()))
    def test_exempt_nationalities_well_formed(self, code, reqs):
        exempt = reqs.visa_exempt_nationalities

        assert all(re.fullmatch(r"[A-Z]{2}", n) for n in exempt)
        assert len(exempt) == len(set(exempt))
        assert code not in exempt

    @pytest.mark.parametrize("code,reqs", sorted(COUNTRY_REQUIREMENTS.items()))
    def test_urls_are_http(self, code, reqs):
        for url in (reqs.embassy_url, reqs.evisa_portal_url, reqs.entry_form_url):
            if url is not None:
                assert url.startswith(("https://", "http://"))

    def test_visa_free_countries_have_no_transit_requirement(self):
        for reqs in COUNTRY_REQUIREMENTS.values():
            if not reqs.requires_visa:
                assert reqs.transit_visa_required is False


class TestPassportValidityLimits:
    """Country passport-validity windows match published rules."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_three_months(self, code):
        assert get_requirements(code).passport_validity_months == 3

    @pytest.mark.parametrize("code", ["CN", "IN", "AE", "TH", "SG", "BR"])
    def test_six_month_countries(self, code):
        assert get_requirements(code).passport_validity_months == 6

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_schengen_shares_exempt_list(self, code):
        assert get_requirements(code).visa_exempt_nationalities == (
            get_requirements("DE").visa_exempt_nationalities
        )


class TestVisaAndTransitFlags:
    """Visa-required, visa-free and transit-visa flags are as configured."""

    @pytest.mark.parametrize("code", ["CL", "JM", "CR"])
    def test_visa_free_countries(self, code):
        assert get_requirements(code).requires_visa is False

    @pytest.mark.parametrize("code", ["US", "AU", "CN", "IN", "CA", "NG", "SA", "GH"])
    def test_transit_visa_countries(self, code):
        assert get_requirements(code).transit_visa_required is True

    def test_china_exempts_nobody(self):
        assert get_requirements("CN").visa_exempt_nationalities == []


class TestPassportEvaluatorUsesRealData:
    """Passing {} requirements makes the passport evaluator use this dataset."""

    def test_schengen_passport_meets_window_but_under_six_months(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("FR"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO

    def test_six_month_country_rejects_four_month_passport(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("CN"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "6-month" in result.reasons[0]

    def test_unknown_country_uses_default_six_months(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "ZZ's 6-month" in result.reasons[0]

    def test_passport_exactly_six_months_past_departure_no_alert(self):
        profile = make_profile(passport_expiry=date(2030, 12, 1))
        itinerary = make_itinerary(_segment("CN"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_in_six_month_country_still_checked(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(
            _segment("DE", is_layover=True, flight_number="DL1"),
            _segment("IN", flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2


class TestVisaEvaluatorUsesRealData:
    """Passing {} requirements makes the visa evaluator use this dataset."""

    def test_exempt_nationality_no_alert(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_non_exempt_nationality_missing_visa(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for DE"]

    def test_visa_free_country_no_alert(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CL"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_unknown_country_requires_visa(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.reasons == ["No visa on file for ZZ"]

    def test_layover_without_transit_requirement_skipped(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("DE", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_with_transit_requirement_flagged(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CN", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.reasons == ["No visa on file for CN"]

    def test_default_layover_skipped(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("ZZ", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_valid_visa_clears_alert(self):
        profile = make_profile(nationality="IN", visa_records=[_visa("CN")])
        itinerary = make_itinerary(_segment("CN"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestItineraryChange:
    """Re-evaluating a changed itinerary picks up the new destination's rules."""

    def test_rebooking_from_exempt_to_visa_country_raises_alert(self):
        profile = make_profile(nationality="GB")
        original = make_itinerary(_segment("FR"))
        changed = make_itinerary(_segment("CN"))

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for CN"]

    def test_rebooking_to_three_month_country_clears_passport_alert(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        original = make_itinerary(_segment("JP"))
        changed = make_itinerary(_segment("NZ"))

        before = evaluate_passport_expiry(profile, original, {})
        after = evaluate_passport_expiry(profile, changed, {})

        assert before.severity == AlertSeverity.CRITICAL
        assert after.severity == AlertSeverity.INFO

    def test_explicit_requirements_take_precedence(self):
        override = {
            "CN": TravelDocRequirements(
                country_code="CN",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            )
        }
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CN"))

        result = evaluate_visa_requirements(profile, itinerary, override)

        assert result.is_alert_required is False
