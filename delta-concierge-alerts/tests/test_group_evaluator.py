"""Critical-path tests for the group itinerary evaluator."""

from datetime import date

import pytest

from src.data import country_requirements
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupEvaluationResult,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)

from tests.conftest import make_segment


DEPARTURE = date(2031, 3, 1)
ARRIVAL = date(2031, 3, 2)
VALID_PASSPORT_EXPIRY = date(2035, 1, 1)
VALID_VISA_EXPIRY = date(2032, 1, 1)


def _segment(destination: str = "DE", **overrides):
    """Build a far-future FlightSegment so date-relative branches stay stable."""
    params = {"departure": DEPARTURE, "arrival": ARRIVAL}
    params.update(overrides)
    return make_segment(destination=destination, **params)


def _visa(country_code: str, expiry_date: date = VALID_VISA_EXPIRY) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry_date,
        visa_number=f"V-{country_code}",
    )


def _traveler(
    skymiles_number: str = "1000001",
    nationality: str = "US",
    passport_number: str | None = "P1000001",
    passport_expiry: date | None = VALID_PASSPORT_EXPIRY,
    visa_records: list[VisaRecord] | None = None,
    first_name: str = "Ada",
    last_name: str = "Traveler",
) -> SkyMilesProfile:
    return SkyMilesProfile(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        nationality=nationality,
        passport_number=passport_number,
        passport_expiry=passport_expiry,
        visa_records=visa_records or [],
        endpoint_arn=f"arn:aws:sns:us-east-1:000:endpoint/{skymiles_number}",
    )


def _base_group(**overrides) -> GroupItinerary:
    """A clean two-traveler group flying to DE (US nationals are visa-exempt)."""
    params = {
        "confirmation_number": "DL-GROUP1",
        "segments": [_segment("DE")],
        "travelers": [
            _traveler(skymiles_number="1000001", first_name="Ada"),
            _traveler(skymiles_number="1000002", first_name="Grace"),
        ],
        "primary_traveler": "1000001",
    }
    params.update(overrides)
    return GroupItinerary(**params)


@pytest.fixture
def group_requirements() -> dict[str, TravelDocRequirements]:
    """Explicit requirements: Schengen 3-month rule vs. 6-month rule countries."""
    return {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB", "CA"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "JP": TravelDocRequirements(
            country_code="JP",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US", "GB"],
        ),
        "GB": TravelDocRequirements(
            country_code="GB",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
    }


@pytest.fixture
def stub_country_requirements():
    """Swap the module-level COUNTRY_REQUIREMENTS so default lookups use test data."""
    stub = {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
    }
    original = country_requirements.COUNTRY_REQUIREMENTS
    country_requirements.COUNTRY_REQUIREMENTS = stub
    try:
        yield stub
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestCleanGroup:
    """Happy path: every traveler compliant → no group severity, one summary each."""

    def test_clean_group_has_no_severity(self, group_requirements):
        group = _base_group()

        result = evaluate_group_itinerary(group, group_requirements)

        assert isinstance(result, GroupEvaluationResult)
        assert result.confirmation_number == "DL-GROUP1"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2
        for summary in result.traveler_summaries:
            assert summary.passport_result.is_alert_required is False
            assert summary.visa_result.is_alert_required is False

    def test_summaries_preserve_traveler_identity_and_order(self, group_requirements):
        group = _base_group()

        result = evaluate_group_itinerary(group, group_requirements)

        identities = [
            (s.skymiles_number, s.first_name, s.last_name) for s in result.traveler_summaries
        ]
        assert identities == [
            ("1000001", "Ada", "Traveler"),
            ("1000002", "Grace", "Traveler"),
        ]

    def test_each_result_is_bound_to_its_own_traveler(self, group_requirements):
        group = _base_group()

        result = evaluate_group_itinerary(group, group_requirements)

        for traveler, summary in zip(group.travelers, result.traveler_summaries):
            assert summary.passport_result.profile is traveler
            assert summary.visa_result.profile is traveler

    def test_shared_segments_evaluated_for_every_traveler(self, group_requirements):
        segments = [_segment("DE"), _segment("GB", flight_number="DL200")]
        group = _base_group(segments=segments)

        result = evaluate_group_itinerary(group, group_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self, group_requirements):
        group = _base_group(travelers=[])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None
        assert result.confirmation_number == "DL-GROUP1"


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers and both checks."""

    def test_single_critical_traveler_escalates_group(self, group_requirements):
        travelers = [
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002", passport_expiry=date(2020, 1, 1)),
        ]
        group = _base_group(travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity is None
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL
        assert "Passport has expired" in result.traveler_summaries[1].passport_result.reasons

    def test_later_higher_severity_replaces_earlier_lower(self, group_requirements):
        travelers = [
            _traveler(
                skymiles_number="1000001", nationality="IN",
                visa_records=[_visa("CN", expiry_date=date(2031, 3, 15))],
            ),
            _traveler(
                skymiles_number="1000002", nationality="IN",
                visa_records=[_visa("CN", expiry_date=DEPARTURE)],
            ),
        ]
        group = _base_group(segments=[_segment("CN")], travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_later_lower_severity_does_not_downgrade(self, group_requirements):
        travelers = [
            _traveler(skymiles_number="1000001", nationality="IN", visa_records=[]),
            _traveler(
                skymiles_number="1000002", nationality="IN",
                visa_records=[_visa("CN", expiry_date=date(2031, 3, 15))],
            ),
        ]
        group = _base_group(segments=[_segment("CN")], travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_equal_severities_keep_that_severity(self, group_requirements):
        travelers = [
            _traveler(
                skymiles_number=str(1000001 + i), nationality="IN",
                visa_records=[_visa("CN", expiry_date=DEPARTURE)],
            )
            for i in range(2)
        ]
        group = _base_group(segments=[_segment("CN")], travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_visa_severity_outranks_passport_for_same_traveler(self, group_requirements):
        traveler = _traveler(
            nationality="IN", passport_expiry=date(2031, 7, 1), visa_records=[],
        )
        group = _base_group(segments=[_segment("DE")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_severity_kept_when_visa_clean(self, group_requirements):
        traveler = _traveler(passport_number=None)
        group = _base_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL
        assert "Passport information is incomplete" in summary.passport_result.reasons
        assert summary.visa_result.severity is None
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_info_only_group(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 7, 1))
        group = _base_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.INFO


class TestPassportValidityPerCountry:
    """Passport validity window is checked against each destination's own rule."""

    def test_meets_schengen_three_month_rule(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 7, 1))
        group = _base_group(segments=[_segment("DE")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.INFO
        assert not any("validity requirement" in r for r in passport.reasons)

    def test_fails_six_month_rule_for_same_passport(self, group_requirements):
        traveler = _traveler(
            nationality="IN", passport_expiry=date(2031, 7, 1),
            visa_records=[_visa("CN")],
        )
        group = _base_group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet CN's 6-month validity requirement for DL100" in passport.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_fails_schengen_three_month_rule(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 5, 1))
        group = _base_group(segments=[_segment("DE")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet DE's 3-month validity requirement for DL100" in passport.reasons

    def test_passport_expires_before_departure(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 2, 1))
        group = _base_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert "Passport expires before departure on DL100 to DE" in passport.reasons


class TestVisaPerTraveler:
    """Visa exemption and visa records are evaluated per traveler's nationality."""

    def test_mixed_nationalities_only_non_exempt_alerted(self, group_requirements):
        travelers = [
            _traveler(skymiles_number="1000001", nationality="US"),
            _traveler(skymiles_number="1000002", nationality="IN", visa_records=[]),
        ]
        group = _base_group(segments=[_segment("DE")], travelers=travelers)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.traveler_summaries[1].visa_result.reasons == ["No visa on file for DE"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_non_exempt_traveler_with_valid_visa_is_clean(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        group = _base_group(travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None

    def test_destination_without_visa_requirement(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[])
        group = _base_group(segments=[_segment("GB")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None


class TestLayovers:
    """Layover segments only require a visa where a transit visa is mandated."""

    def test_layover_without_transit_visa_requirement_skipped(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        segments = [
            _segment("JP", is_layover=True, flight_number="DL100"),
            _segment("DE", flight_number="DL200"),
        ]
        group = _base_group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_with_transit_visa_requirement_alerts(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        segments = [
            _segment("CN", is_layover=True, flight_number="DL100"),
            _segment("DE", flight_number="DL200"),
        ]
        group = _base_group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_still_checks_passport_validity(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 7, 1), visa_records=[])
        segments = [
            _segment("JP", is_layover=True, flight_number="DL100"),
            _segment("DE", flight_number="DL200"),
        ]
        group = _base_group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert "Passport does not meet JP's 6-month validity requirement for DL100" in passport.reasons
        assert result.group_severity == AlertSeverity.CRITICAL


class TestItineraryChange:
    """Re-evaluating a changed group itinerary reflects the new segments."""

    def test_rerouting_to_stricter_country_escalates(self, group_requirements):
        traveler = _traveler(passport_expiry=date(2031, 7, 1))
        original = _base_group(segments=[_segment("DE")], travelers=[traveler])
        changed = _base_group(segments=[_segment("JP")], travelers=[traveler])

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity == AlertSeverity.INFO
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_moving_departure_past_visa_expiry_escalates(self, group_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN", expiry_date=date(2031, 6, 1))])
        original = _base_group(segments=[_segment("CN")], travelers=[traveler])
        changed = _base_group(
            segments=[_segment("CN", departure=date(2031, 8, 1), arrival=date(2031, 8, 2))],
            travelers=[traveler],
        )

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in after.traveler_summaries[0].visa_result.reasons

    def test_adding_traveler_to_group(self, group_requirements):
        original = _base_group()
        changed = _base_group(
            travelers=original.travelers + [
                _traveler(skymiles_number="1000003", nationality="IN", visa_records=[])
            ]
        )

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity is None
        assert len(after.traveler_summaries) == 3
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_confirmation_number_carried_through(self, group_requirements):
        group = _base_group(confirmation_number="DL-CHANGED")

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.confirmation_number == "DL-CHANGED"


class TestDefaultRequirementsLookup:
    """When requirements is None, each destination is looked up from default data."""

    def test_uses_default_data_when_requirements_none(self, stub_country_requirements):
        traveler = _traveler(nationality="IN", visa_records=[])
        group = _base_group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_default_data_applies_exemptions(self, stub_country_requirements):
        group = _base_group(segments=[_segment("DE")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_repeated_destination_looked_up_once(self, stub_country_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE")])
        segments = [
            _segment("DE", flight_number="DL100"),
            _segment("DE", flight_number="DL200", departure=date(2031, 3, 10), arrival=date(2031, 3, 11)),
        ]
        group = _base_group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.segments_evaluated == segments

    def test_unknown_destination_falls_back_to_default(self, stub_country_requirements):
        traveler = _traveler(nationality="US", visa_records=[])
        group = _base_group(segments=[_segment("ZZ")], travelers=[traveler])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for ZZ"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_explicit_empty_requirements_not_replaced(self, stub_country_requirements):
        traveler = _traveler(nationality="US", visa_records=[])
        group = _base_group(segments=[_segment("DE")], travelers=[traveler])
        requirements: dict[str, TravelDocRequirements] = {}

        result = evaluate_group_itinerary(group, requirements)

        assert requirements == {}
        assert result.group_severity is None

    def test_explicit_requirements_override_defaults(self, stub_country_requirements):
        override = {
            "DE": TravelDocRequirements(
                country_code="DE",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            )
        }
        group = _base_group(segments=[_segment("DE")])

        result = evaluate_group_itinerary(group, override)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert all(
            s.visa_result.reasons == ["No visa on file for DE"] for s in result.traveler_summaries
        )
