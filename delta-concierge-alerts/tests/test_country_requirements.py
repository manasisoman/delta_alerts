"""Tests for the static country travel document requirements data."""

from datetime import date

import pytest

from src.data.country_requirements import (
    COUNTRY_REQUIREMENTS,
    _DEFAULT_REQUIREMENTS,
    get_requirements,
)
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


SCHENGEN_COUNTRIES = ["DE", "FR", "IT", "ES", "NL", "AT", "BE", "CH", "PT", "GR"]
SIX_MONTH_COUNTRIES = ["US", "GB", "JP", "AU", "CN", "IN", "BR"]


class TestGetRequirementsLookup:
    """get_requirements returns the entry for known codes, default otherwise."""

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_known_country_returns_its_own_entry(self, country_code):
        result = get_requirements(country_code)

        assert result is COUNTRY_REQUIREMENTS[country_code]
        assert result.country_code == country_code

    def test_unknown_country_falls_back_to_default(self):
        result = get_requirements("ZZ")

        assert result is _DEFAULT_REQUIREMENTS
        assert result.country_code == "DEFAULT"
        assert result.requires_visa is True
        assert result.transit_visa_required is False
        assert result.passport_validity_months == 6
        assert result.visa_exempt_nationalities == []

    def test_lookup_is_case_sensitive(self):
        # Lowercase codes are not in the table, so the default applies.
        assert get_requirements("de") is _DEFAULT_REQUIREMENTS

    def test_empty_country_code_falls_back_to_default(self):
        assert get_requirements("") is _DEFAULT_REQUIREMENTS


class TestPassportValidityWindows:
    """Per-country passport validity windows match the documented rules."""

    @pytest.mark.parametrize("country_code", SCHENGEN_COUNTRIES)
    def test_schengen_requires_three_months(self, country_code):
        assert get_requirements(country_code).passport_validity_months == 3

    @pytest.mark.parametrize("country_code", SIX_MONTH_COUNTRIES)
    def test_major_destinations_require_six_months(self, country_code):
        assert get_requirements(country_code).passport_validity_months == 6

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_every_entry_has_a_positive_validity_window(self, country_code):
        assert get_requirements(country_code).passport_validity_months > 0


class TestDataIntegrity:
    """Every entry in the table is internally consistent."""

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_key_matches_country_code_field(self, country_code):
        assert COUNTRY_REQUIREMENTS[country_code].country_code == country_code

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_entry_shape(self, country_code):
        reqs = get_requirements(country_code)

        assert isinstance(reqs, TravelDocRequirements)
        assert len(country_code) == 2 and country_code.isupper()
        assert isinstance(reqs.requires_visa, bool)
        assert isinstance(reqs.transit_visa_required, bool)
        assert all(
            len(n) == 2 and n.isupper() for n in reqs.visa_exempt_nationalities
        )

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_exempt_nationalities_have_no_duplicates(self, country_code):
        exempt = get_requirements(country_code).visa_exempt_nationalities

        assert len(exempt) == len(set(exempt))

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_visa_free_countries_do_not_require_transit_visas(self, country_code):
        reqs = get_requirements(country_code)

        if not reqs.requires_visa:
            assert reqs.transit_visa_required is False

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_processing_days_are_positive_when_present(self, country_code):
        days = get_requirements(country_code).estimated_processing_days

        assert days is None or days > 0

    @pytest.mark.parametrize("country_code", sorted(COUNTRY_REQUIREMENTS))
    def test_urls_are_absolute_when_present(self, country_code):
        reqs = get_requirements(country_code)

        for url in (reqs.embassy_url, reqs.evisa_portal_url, reqs.entry_form_url):
            assert url is None or url.startswith(("https://", "http://"))


class TestTransitVisaFlags:
    """Countries known to require transit visas are flagged as such."""

    @pytest.mark.parametrize("country_code", ["US", "CN", "IN", "CA", "AU"])
    def test_transit_visa_required(self, country_code):
        assert get_requirements(country_code).transit_visa_required is True

    @pytest.mark.parametrize("country_code", SCHENGEN_COUNTRIES)
    def test_schengen_does_not_require_transit_visa(self, country_code):
        assert get_requirements(country_code).transit_visa_required is False


class TestVisaExemptions:
    """Exempt nationality lists drive the visa evaluator's skip logic."""

    @pytest.mark.parametrize("nationality", ["US", "GB", "CA", "AU", "JP", "BR"])
    def test_schengen_exempts_common_nationalities(self, nationality):
        assert nationality in get_requirements("DE").visa_exempt_nationalities

    def test_china_exempts_nobody(self):
        assert get_requirements("CN").visa_exempt_nationalities == []

    def test_countries_without_visa_requirement_exist(self):
        visa_free = [
            code
            for code, reqs in COUNTRY_REQUIREMENTS.items()
            if not reqs.requires_visa
        ]

        assert "CL" in visa_free
        assert "JM" in visa_free
        assert "CR" in visa_free


class TestPassportEvaluationAgainstRealData:
    """The real table produces the documented passport severities."""

    def test_schengen_three_month_window_is_satisfied(self):
        # Passport valid 4 months past departure — above DE's 3-month minimum,
        # so only the advisory "expiring soon" notice applies.
        profile = make_profile(passport_expiry=date(2030, 5, 1))
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=date(2030, 1, 1))
        )

        result = evaluate_passport_expiry(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.severity == AlertSeverity.INFO
        assert not any("3-month" in r for r in result.reasons)

    def test_long_dated_passport_clears_schengen(self):
        profile = make_profile(passport_expiry=date(2035, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=date(2030, 1, 1))
        )

        result = evaluate_passport_expiry(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_same_passport_fails_six_month_destination(self):
        # Identical passport against CN (6-month minimum) is now non-compliant.
        profile = make_profile(passport_expiry=date(2030, 5, 1))
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=date(2030, 1, 1))
        )

        result = evaluate_passport_expiry(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("CN's 6-month" in r for r in result.reasons)

    def test_unknown_destination_uses_default_six_month_window(self):
        profile = make_profile(passport_expiry=date(2030, 5, 1))
        itinerary = make_itinerary(
            make_segment(destination="ZZ", departure=date(2030, 1, 1))
        )

        result = evaluate_passport_expiry(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_expired_passport_is_critical_for_visa_free_destination(self):
        profile = make_profile(passport_expiry=date(2020, 1, 1))
        itinerary = make_itinerary(
            make_segment(destination="CL", departure=date(2030, 1, 1))
        )

        result = evaluate_passport_expiry(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestVisaEvaluationAgainstRealData:
    """The real table drives the visa evaluator's skip and alert paths."""

    def test_visa_free_destination_is_skipped(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="CL", departure=date(2030, 1, 1))
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_exempt_nationality_is_skipped(self):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="FR", departure=date(2030, 1, 1))
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is False

    def test_non_exempt_nationality_without_visa_is_critical(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="FR", departure=date(2030, 1, 1))
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_layover_without_transit_visa_requirement_is_skipped(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(
                destination="JP", departure=date(2030, 1, 1), is_layover=True
            )
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is False

    def test_layover_in_transit_visa_country_still_alerts(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(
                destination="US", departure=date(2030, 1, 1), is_layover=True
            )
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_valid_visa_for_transit_country_clears_layover(self):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                VisaRecord(
                    country_code="US",
                    visa_type="B1/B2",
                    issue_date=date(2024, 1, 1),
                    expiry_date=date(2035, 1, 1),
                    visa_number="V-US-1",
                )
            ],
        )
        itinerary = make_itinerary(
            make_segment(
                destination="US", departure=date(2030, 1, 1), is_layover=True
            )
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is False

    def test_unknown_destination_requires_a_visa_by_default(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="ZZ", departure=date(2030, 1, 1))
        )

        result = evaluate_visa_requirements(profile, itinerary, COUNTRY_REQUIREMENTS)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestRequirementsOverride:
    """Callers may override the shared table for a single evaluation."""

    def test_override_relaxes_visa_requirement(self):
        override = {
            **COUNTRY_REQUIREMENTS,
            "FR": TravelDocRequirements(
                country_code="FR",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="FR", departure=date(2030, 1, 1))
        )

        result = evaluate_visa_requirements(profile, itinerary, override)

        assert result.is_alert_required is False
        assert COUNTRY_REQUIREMENTS["FR"].requires_visa is True

    def test_override_tightens_passport_window(self):
        override = {
            **COUNTRY_REQUIREMENTS,
            "DE": TravelDocRequirements(
                country_code="DE",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=12,
                visa_exempt_nationalities=["IN"],
            ),
        }
        profile = make_profile(passport_expiry=date(2030, 5, 1))
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=date(2030, 1, 1))
        )

        result = evaluate_passport_expiry(profile, itinerary, override)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert COUNTRY_REQUIREMENTS["DE"].passport_validity_months == 3


class TestItineraryChangeReEvaluation:
    """Re-evaluating a changed itinerary picks up the new destinations."""

    def test_rerouting_to_stricter_country_introduces_alert(self):
        profile = make_profile(passport_expiry=date(2030, 5, 1))
        original = make_itinerary(
            make_segment(destination="DE", departure=date(2030, 1, 1))
        )
        changed = make_itinerary(
            make_segment(destination="DE", departure=date(2030, 1, 1)),
            make_segment(
                destination="IN", departure=date(2030, 1, 5), flight_number="DL200"
            ),
        )

        before = evaluate_passport_expiry(profile, original, COUNTRY_REQUIREMENTS)
        after = evaluate_passport_expiry(profile, changed, COUNTRY_REQUIREMENTS)

        # DE's 3-month window is met (advisory only); IN's 6-month window is not.
        assert before.severity == AlertSeverity.INFO
        assert after.is_alert_required is True
        assert after.severity == AlertSeverity.CRITICAL
        assert len(after.segments_evaluated) == 2

    def test_changed_layover_to_transit_visa_country_introduces_alert(self):
        profile = make_profile(nationality="IN", visa_records=[])
        original = make_itinerary(
            make_segment(
                destination="JP", departure=date(2030, 1, 1), is_layover=True
            )
        )
        changed = make_itinerary(
            make_segment(
                destination="CN", departure=date(2030, 1, 1), is_layover=True
            )
        )

        before = evaluate_visa_requirements(profile, original, COUNTRY_REQUIREMENTS)
        after = evaluate_visa_requirements(profile, changed, COUNTRY_REQUIREMENTS)

        assert before.is_alert_required is False
        assert after.is_alert_required is True
        assert after.severity == AlertSeverity.CRITICAL
