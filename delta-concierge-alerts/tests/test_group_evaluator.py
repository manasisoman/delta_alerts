"""Critical-path tests for the group itinerary evaluator."""

from datetime import date

import pytest

import src.data.country_requirements as country_requirements
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)

from tests.conftest import make_segment


DEPARTURE = date(2030, 9, 1)
ARRIVAL = date(2030, 9, 2)
VALID_PASSPORT_EXPIRY = date(2035, 1, 1)


@pytest.fixture
def group_requirements() -> dict[str, TravelDocRequirements]:
    """Made-up country codes so tests are isolated from real country data."""
    return {
        "XA": TravelDocRequirements(
            country_code="XA",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        ),
        "XB": TravelDocRequirements(
            country_code="XB",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "XV": TravelDocRequirements(
            country_code="XV",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US"],
        ),
        "XT": TravelDocRequirements(
            country_code="XT",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
    }


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Alex",
    last_name: str = "Traveler",
    nationality: str = "IN",
    passport_number: str | None = "P1000001",
    passport_expiry: date | None = VALID_PASSPORT_EXPIRY,
    visa_records: list[VisaRecord] | None = None,
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


def _segment(destination: str = "XA", **overrides) -> FlightSegment:
    params = {"departure": DEPARTURE, "arrival": ARRIVAL}
    params.update(overrides)
    return make_segment(destination=destination, **params)


def _visa(country_code: str, expiry_date: date) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2026, 1, 1),
        expiry_date=expiry_date,
        visa_number=f"V-{country_code}",
    )


def _group(
    segments: list[FlightSegment] | None = None,
    travelers: list[SkyMilesProfile] | None = None,
    confirmation_number: str = "DL-GROUP",
    primary_traveler: str = "1000001",
) -> GroupItinerary:
    return GroupItinerary(
        confirmation_number=confirmation_number,
        segments=segments if segments is not None else [_segment()],
        travelers=travelers if travelers is not None else [_traveler()],
        primary_traveler=primary_traveler,
    )


class TestCleanGroup:
    """All travelers compliant → no alerts and group severity is None."""

    def test_single_compliant_traveler(self, group_requirements):
        group = _group()

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_multiple_compliant_travelers(self, group_requirements):
        group = _group(travelers=[
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002"),
            _traveler(skymiles_number="1000003"),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 3

    def test_empty_traveler_list(self, group_requirements):
        group = _group(travelers=[])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None
        assert result.confirmation_number == "DL-GROUP"


class TestTravelerSummaries:
    """Each traveler gets a summary carrying identity and both evaluation results."""

    def test_summary_identity_fields(self, group_requirements):
        group = _group(travelers=[
            _traveler(skymiles_number="1000001", first_name="Ana", last_name="Lee"),
            _traveler(skymiles_number="1000002", first_name="Ben", last_name="Ray"),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        identities = [
            (s.skymiles_number, s.first_name, s.last_name)
            for s in result.traveler_summaries
        ]
        assert identities == [("1000001", "Ana", "Lee"), ("1000002", "Ben", "Ray")]

    def test_results_belong_to_each_traveler(self, group_requirements):
        ana = _traveler(skymiles_number="1000001")
        ben = _traveler(skymiles_number="1000002")
        group = _group(travelers=[ana, ben])

        result = evaluate_group_itinerary(group, group_requirements)

        for summary, traveler in zip(result.traveler_summaries, [ana, ben]):
            assert summary.passport_result.profile is traveler
            assert summary.visa_result.profile is traveler

    def test_confirmation_number_propagated(self, group_requirements):
        group = _group(confirmation_number="DL-ABC123")

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.confirmation_number == "DL-ABC123"

    def test_all_segments_evaluated_for_every_traveler(self, group_requirements):
        segments = [
            _segment("XA", flight_number="DL1"),
            _segment("XB", flight_number="DL2", departure=date(2030, 9, 10)),
        ]
        group = _group(segments=segments, travelers=[
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002"),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across all travelers."""

    def test_one_problem_traveler_flags_group(self, group_requirements):
        group = _group(travelers=[
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002", passport_expiry=date(2020, 1, 1)),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        clean, expired = result.traveler_summaries
        assert clean.passport_result.is_alert_required is False
        assert expired.passport_result.reasons == ["Passport has expired"]

    def test_info_only(self, group_requirements):
        # Meets XA's 3-month rule but expires within 6 months of departure.
        group = _group(travelers=[_traveler(passport_expiry=date(2031, 1, 1))])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_only(self, group_requirements):
        traveler = _traveler(visa_records=[_visa("XV", DEPARTURE)])
        group = _group(segments=[_segment("XV")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.WARNING
        assert result.traveler_summaries[0].visa_result.reasons == [
            "Visa for XV expires on date of travel"
        ]

    def test_warning_upgrades_info_across_travelers(self, group_requirements):
        info_traveler = _traveler(
            skymiles_number="1000001", passport_expiry=date(2031, 1, 1),
            visa_records=[_visa("XV", date(2033, 1, 1))],
        )
        warning_traveler = _traveler(
            skymiles_number="1000002", visa_records=[_visa("XV", DEPARTURE)],
        )
        group = _group(segments=[_segment("XV")], travelers=[info_traveler, warning_traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_not_downgraded_by_later_traveler(self, group_requirements):
        group = _group(travelers=[
            _traveler(skymiles_number="1000001", passport_number=None),
            _traveler(skymiles_number="1000002", passport_expiry=date(2031, 1, 1)),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_severity_considered_alongside_passport(self, group_requirements):
        # Passport INFO (expiring within 6 months), visa CRITICAL (missing).
        traveler = _traveler(passport_expiry=date(2031, 1, 1))
        segments = [_segment("XA"), _segment("XV", flight_number="DL200")]
        group = _group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_critical_with_lower_visa_severity(self, group_requirements):
        traveler = _traveler(
            passport_expiry=None, visa_records=[_visa("XV", DEPARTURE)],
        )
        group = _group(segments=[_segment("XV")], travelers=[traveler])

        result = evaluate_group_itinerary(group, group_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL
        assert summary.visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.CRITICAL


class TestPassportValidityWithinCountryRequirements:
    """Passport validity is judged per traveler against each destination's rule."""

    def test_meets_3_month_rule_but_not_6_month_rule(self, group_requirements):
        # Expires 4 months after departure: OK for XA (3-month), CRITICAL for XB (6-month).
        traveler = _traveler(passport_expiry=date(2031, 1, 1))
        group_xa = _group(segments=[_segment("XA")], travelers=[traveler])
        group_xb = _group(segments=[_segment("XB")], travelers=[traveler])

        result_xa = evaluate_group_itinerary(group_xa, group_requirements)
        result_xb = evaluate_group_itinerary(group_xb, group_requirements)

        assert result_xa.group_severity == AlertSeverity.INFO
        assert result_xb.group_severity == AlertSeverity.CRITICAL
        assert (
            "Passport does not meet XB's 6-month validity requirement for DL100"
            in result_xb.traveler_summaries[0].passport_result.reasons
        )

    def test_mixed_validity_within_group(self, group_requirements):
        group = _group(segments=[_segment("XB")], travelers=[
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002", passport_expiry=date(2030, 12, 1)),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        ok, short = result.traveler_summaries
        assert ok.passport_result.is_alert_required is False
        assert short.passport_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_expires_before_departure(self, group_requirements):
        group = _group(travelers=[_traveler(passport_expiry=date(2030, 8, 1))])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert (
            "Passport expires before departure on DL100 to XA"
            in result.traveler_summaries[0].passport_result.reasons
        )


class TestVisaRequirementsPerTraveler:
    """Visa rules apply per traveler based on nationality and visa records."""

    def test_exempt_nationality_not_flagged(self, group_requirements):
        group = _group(segments=[_segment("XV")], travelers=[
            _traveler(skymiles_number="1000001", nationality="US"),
            _traveler(skymiles_number="1000002", nationality="IN"),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        exempt, needs_visa = result.traveler_summaries
        assert exempt.visa_result.is_alert_required is False
        assert needs_visa.visa_result.reasons == ["No visa on file for XV"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_valid_visa_clears_group(self, group_requirements):
        group = _group(segments=[_segment("XV")], travelers=[
            _traveler(visa_records=[_visa("XV", date(2033, 1, 1))]),
        ])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None


class TestLayovers:
    """Layovers are evaluated for every traveler in the group."""

    def test_layover_without_transit_visa_requirement_skipped(self, group_requirements):
        segments = [
            _segment("XV", is_layover=True, flight_number="DL10"),
            _segment("XA", flight_number="DL11"),
        ]
        group = _group(segments=segments)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity is None

    def test_layover_requiring_transit_visa_flags_traveler(self, group_requirements):
        segments = [
            _segment("XT", is_layover=True, flight_number="DL10"),
            _segment("XA", flight_number="DL11"),
        ]
        group = _group(segments=segments)

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for XT"]

    def test_layover_passport_validity_checked(self, group_requirements):
        # Fine for XA's 3-month rule but not the 6-month rule at the XB layover.
        segments = [
            _segment("XB", is_layover=True, flight_number="DL10"),
            _segment("XA", flight_number="DL11"),
        ]
        group = _group(segments=segments, travelers=[_traveler(passport_expiry=date(2031, 1, 1))])

        result = evaluate_group_itinerary(group, group_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert (
            "Passport does not meet XB's 6-month validity requirement for DL10"
            in result.traveler_summaries[0].passport_result.reasons
        )


class TestItineraryChange:
    """Re-evaluating after an itinerary change reflects the new segments."""

    def test_change_to_stricter_destination_raises_severity(self, group_requirements):
        travelers = [_traveler(passport_expiry=date(2031, 1, 1))]
        original = _group(segments=[_segment("XA")], travelers=travelers)
        changed = _group(segments=[_segment("XB")], travelers=travelers)

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity == AlertSeverity.INFO
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_change_of_date_resolves_alert(self, group_requirements):
        traveler = _traveler(visa_records=[_visa("XV", DEPARTURE)])
        original = _group(segments=[_segment("XV")], travelers=[traveler])
        changed = _group(
            segments=[_segment("XV", departure=date(2030, 7, 1), arrival=date(2030, 7, 2))],
            travelers=[traveler],
        )

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity == AlertSeverity.WARNING
        assert after.group_severity is None

    def test_added_segment_adds_alert(self, group_requirements):
        travelers = [_traveler()]
        original = _group(segments=[_segment("XA")], travelers=travelers)
        changed = _group(
            segments=[_segment("XA"), _segment("XV", flight_number="DL200")],
            travelers=travelers,
        )

        before = evaluate_group_itinerary(original, group_requirements)
        after = evaluate_group_itinerary(changed, group_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL


class TestDefaultRequirementsLookup:
    """requirements=None builds the mapping from country data, once per destination."""

    @pytest.fixture
    def fake_country_data(self, group_requirements):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = dict(group_requirements)
        try:
            yield
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

    def test_uses_country_data_when_requirements_omitted(self, fake_country_data):
        group = _group(segments=[_segment("XB")], travelers=[
            _traveler(passport_expiry=date(2031, 1, 1)),
        ])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert (
            "Passport does not meet XB's 6-month validity requirement for DL100"
            in result.traveler_summaries[0].passport_result.reasons
        )

    def test_repeated_destination_and_clean_result(self, fake_country_data):
        segments = [
            _segment("XA", flight_number="DL1"),
            _segment("XA", flight_number="DL2", departure=date(2030, 9, 10)),
        ]
        group = _group(segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_unknown_destination_uses_default_requirements(self, fake_country_data):
        # Default: visa required, 6-month passport validity.
        group = _group(segments=[_segment("ZZ")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for ZZ"]
