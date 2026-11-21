"""Tests for country-specific travel document requirements lookup and data."""

from dataclasses import replace
from datetime import date

import pytest

from src.data import country_requirements as cr_mod
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
DEPARTURE = date(2030, 6, 1)
ARRIVAL = date(2030, 6, 2)


def _segment(destination: str, **overrides):
    """Build a far-future segment so date-relative branches stay stable."""
    params = {"destination": destination, "departure": DEPARTURE, "arrival": ARRIVAL}
    params.update(overrides)
    return make_segment(**params)


class _SwappedRequirements:
    """Temporarily replace the module-level COUNTRY_REQUIREMENTS mapping."""

    def __init__(self, replacement: dict[str, TravelDocRequirements]):
        self.replacement = replacement
        self.original = None

    def __enter__(self):
        self.original = cr_mod.COUNTRY_REQUIREMENTS
        cr_mod.COUNTRY_REQUIREMENTS = self.replacement
        return self.replacement

    def __exit__(self, *exc):
        cr_mod.COUNTRY_REQUIREMENTS = self.original
        return False


class TestKnownCountryLookup:
    """A listed country code returns its exact configured requirements."""

    def test_returns_configured_entry(self):
        result = get_requirements("DE")

        assert result is COUNTRY_REQUIREMENTS["DE"]
        assert result.country_code == "DE"

    def test_returns_swapped_entry_for_made_up_code(self):
        custom = TravelDocRequirements(
            country_code="XA",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        )

        with _SwappedRequirements({"XA": custom}):
            result = get_requirements("XA")

        assert result is custom

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_every_listed_country_resolves_to_itself(self, code):
        result = get_requirements(code)

        assert result.country_code == code


class TestDefaultFallback:
    """Unknown codes fall back to the 6-month, visa-required default."""

    def test_unknown_code_returns_default(self):
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

    def test_empty_string_returns_default(self):
        result = get_requirements("")

        assert result is _DEFAULT_REQUIREMENTS

    def test_lookup_is_case_sensitive(self):
        result = get_requirements("de")

        assert result is _DEFAULT_REQUIREMENTS

    def test_default_used_when_mapping_is_empty(self):
        with _SwappedRequirements({}):
            result = get_requirements("DE")

        assert result is _DEFAULT_REQUIREMENTS

    def test_swap_is_restored(self):
        original = cr_mod.COUNTRY_REQUIREMENTS

        with _SwappedRequirements({}):
            pass

        assert cr_mod.COUNTRY_REQUIREMENTS is original


class TestDataIntegrity:
    """Every static entry is internally consistent."""

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_key_matches_country_code(self, code):
        entry = COUNTRY_REQUIREMENTS[code]

        assert entry.country_code == code
        assert len(code) == 2 and code.isupper()

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_validity_months_is_3_or_6(self, code):
        entry = COUNTRY_REQUIREMENTS[code]

        assert entry.passport_validity_months in (3, 6)

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_country_not_exempt_from_itself(self, code):
        entry = COUNTRY_REQUIREMENTS[code]

        assert code not in entry.visa_exempt_nationalities

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_exempt_list_has_no_duplicates(self, code):
        entry = COUNTRY_REQUIREMENTS[code]

        assert len(entry.visa_exempt_nationalities) == len(set(entry.visa_exempt_nationalities))

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_optional_urls_are_http(self, code):
        entry = COUNTRY_REQUIREMENTS[code]

        for url in (entry.embassy_url, entry.evisa_portal_url, entry.entry_form_url):
            assert url is None or url.startswith(("http://", "https://"))

    @pytest.mark.parametrize("code", sorted(COUNTRY_REQUIREMENTS))
    def test_processing_days_positive_when_set(self, code):
        entry = COUNTRY_REQUIREMENTS[code]

        assert entry.estimated_processing_days is None or entry.estimated_processing_days > 0


class TestSchengenRules:
    """Schengen countries share the 3-month validity rule and exempt list."""

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_three_month_validity(self, code):
        result = get_requirements(code)

        assert result.passport_validity_months == 3

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_no_transit_visa(self, code):
        result = get_requirements(code)

        assert result.transit_visa_required is False

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_us_exempt(self, code):
        result = get_requirements(code)

        assert "US" in result.visa_exempt_nationalities

    @pytest.mark.parametrize("code", SCHENGEN)
    def test_india_not_exempt(self, code):
        result = get_requirements(code)

        assert "IN" not in result.visa_exempt_nationalities


class TestTransitVisaCountries:
    """Countries requiring a transit visa for layovers are flagged as such."""

    @pytest.mark.parametrize("code", ["US", "AU", "CN", "IN", "CA", "NG", "SA", "GH"])
    def test_transit_visa_required(self, code):
        result = get_requirements(code)

        assert result.transit_visa_required is True

    @pytest.mark.parametrize("code", ["GB", "JP", "DE", "MX", "AE"])
    def test_transit_visa_not_required(self, code):
        result = get_requirements(code)

        assert result.transit_visa_required is False


class TestVisaFreeCountries:
    """Countries with requires_visa=False."""

    @pytest.mark.parametrize("code", ["CL", "JM", "CR"])
    def test_requires_visa_false(self, code):
        result = get_requirements(code)

        assert result.requires_visa is False

    def test_no_visa_free_country_in_schengen(self):
        result = [code for code in SCHENGEN if not get_requirements(code).requires_visa]

        assert result == []


class TestPassportEvaluatorFallback:
    """Passport evaluator uses get_requirements when a destination isn't passed in."""

    def test_unknown_destination_uses_default_six_months(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet ZZ's 6-month validity requirement for DL100" in result.reasons

    def test_schengen_destination_uses_three_month_rule(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("FR"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.INFO
        assert not any("validity requirement" in r for r in result.reasons)

    def test_schengen_below_three_months_is_critical(self):
        profile = make_profile(passport_expiry=date(2030, 8, 1))
        itinerary = make_itinerary(_segment("FR"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet FR's 3-month validity requirement for DL100" in result.reasons

    def test_well_valid_passport_no_alert(self):
        profile = make_profile(passport_expiry=date(2031, 6, 1))
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_layover_country_rule_applies(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(
            _segment("FR", is_layover=True, flight_number="DL1"),
            _segment("JP", flight_number="DL2"),
        )

        result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet JP's 6-month validity requirement for DL2" in result.reasons
        assert not any("FR's" in r for r in result.reasons)

    def test_swapped_mapping_rule_applies(self):
        custom = {
            "XB": TravelDocRequirements(
                country_code="XB",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            ),
            "XA": TravelDocRequirements(
                country_code="XA",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        itinerary = make_itinerary(_segment("XA", flight_number="DL1"), _segment("XB", flight_number="DL2"))

        with _SwappedRequirements(custom):
            result = evaluate_passport_expiry(profile, itinerary, {})

        assert result.reasons == [
            "Passport expiring within 6 months — consider renewing before DL1 to XA",
            "Passport does not meet XB's 6-month validity requirement for DL2",
        ]


class TestVisaEvaluatorFallback:
    """Visa evaluator uses get_requirements when a destination isn't passed in."""

    def test_unknown_destination_requires_visa(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons

    def test_unknown_layover_skipped_by_default_transit_rule(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("ZZ", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_transit_visa_country_layover_flagged(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CN", is_layover=True))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_exempt_nationality_no_alert(self):
        profile = make_profile(nationality="US")
        itinerary = make_itinerary(_segment("DE"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_visa_free_country_no_alert(self):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CL"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_valid_visa_for_listed_country_no_alert(self):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2029, 1, 1), expiry_date=date(2032, 1, 1),
            visa_number="V100",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(_segment("CN"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False

    def test_visa_expires_before_departure_with_default_rules(self):
        visa = VisaRecord(
            country_code="ZZ", visa_type="TOURIST",
            issue_date=date(2029, 1, 1), expiry_date=date(2030, 5, 1),
            visa_number="V200",
        )
        profile = make_profile(nationality="US", visa_records=[visa])
        itinerary = make_itinerary(_segment("ZZ"))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for ZZ expires before date of travel" in result.reasons


class TestItineraryChangeReevaluation:
    """A changed destination is re-evaluated against the new country's rules."""

    def test_rerouting_to_visa_country_triggers_alert(self):
        profile = make_profile(nationality="US")
        original = make_itinerary(_segment("DE"))
        changed = make_itinerary(_segment("CN"))

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in after.reasons

    def test_rerouting_to_six_month_country_triggers_passport_alert(self):
        profile = make_profile(passport_expiry=date(2030, 10, 1))
        original = make_itinerary(_segment("DE"))
        changed = make_itinerary(_segment("JP"))

        before = evaluate_passport_expiry(profile, original, {})
        after = evaluate_passport_expiry(profile, changed, {})

        assert before.severity == AlertSeverity.INFO
        assert after.severity == AlertSeverity.CRITICAL

    def test_adding_transit_layover_triggers_alert(self):
        profile = make_profile(nationality="IN")
        original = make_itinerary(_segment("CL"))
        changed = make_itinerary(_segment("US", is_layover=True, flight_number="DL1"), _segment("CL", flight_number="DL2"))

        before = evaluate_visa_requirements(profile, original, {})
        after = evaluate_visa_requirements(profile, changed, {})

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for US"]

    def test_override_takes_precedence_over_static_data(self):
        override = {"CN": replace(COUNTRY_REQUIREMENTS["CN"], visa_exempt_nationalities=["IN"])}
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary(_segment("CN"))

        result = evaluate_visa_requirements(profile, itinerary, override)

        assert result.is_alert_required is False
        assert COUNTRY_REQUIREMENTS["CN"].visa_exempt_nationalities == []
