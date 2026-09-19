"""Tests for the group itinerary evaluator."""

from dataclasses import replace
from datetime import date

from src.data.country_requirements import get_requirements
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment

DEPARTURE = date(2027, 3, 1)
ARRIVAL = date(2027, 3, 2)


def make_traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Test",
    last_name: str = "User",
    **profile_overrides,
) -> SkyMilesProfile:
    """Build a traveler with a unique SkyMiles number and valid documents by default."""
    profile = make_profile(**profile_overrides)
    return replace(
        profile,
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
    )


def make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2030, 1, 1),
    issue_date: date = date(2025, 1, 1),
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=f"V-{country_code}",
    )


def make_group(
    *travelers: SkyMilesProfile,
    segments: list[FlightSegment] | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupItinerary:
    """Build a GroupItinerary; defaults to a single non-layover DE segment."""
    if segments is None:
        segments = [make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)]
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments,
        travelers=list(travelers),
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


class TestHappyPath:
    """All travelers hold valid documents → no alerts, group_severity is None."""

    def test_single_clean_traveler(self, base_requirements):
        traveler = make_traveler(nationality="US")
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_summary_carries_traveler_identity(self, base_requirements):
        traveler = make_traveler(
            skymiles_number="4242424", first_name="Ada", last_name="Lovelace", nationality="US"
        )
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "4242424"
        assert summary.first_name == "Ada"
        assert summary.last_name == "Lovelace"
        assert summary.passport_result.profile is traveler
        assert summary.visa_result.profile is traveler

    def test_shared_segments_evaluated_for_every_traveler(self, base_requirements):
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL100"),
            make_segment(destination="JP", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL200"),
        ]
        group = make_group(
            make_traveler(skymiles_number="1", nationality="US"),
            make_traveler(skymiles_number="2", nationality="GB"),
            segments=segments,
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self, base_requirements):
        group = make_group()

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestPerTravelerResults:
    """Each traveler is evaluated independently and summaries preserve order."""

    def test_one_traveler_missing_passport_does_not_affect_others(self, base_requirements):
        clean = make_traveler(skymiles_number="1", nationality="US")
        broken = make_traveler(skymiles_number="2", nationality="US", passport_number=None)
        group = make_group(clean, broken)

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == ["1", "2"]
        clean_summary, broken_summary = result.traveler_summaries
        assert clean_summary.passport_result.is_alert_required is False
        assert broken_summary.passport_result.severity == AlertSeverity.CRITICAL
        assert "Passport information is incomplete" in broken_summary.passport_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expired_passport_traveler(self, base_requirements):
        traveler = make_traveler(nationality="US", passport_expiry=date(2020, 1, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_below_country_minimum(self, base_requirements):
        # CN requires 6 months; passport expires 4 months after departure
        traveler = make_traveler(
            nationality="IN",
            passport_expiry=DEPARTURE.replace(month=7),
            visa_records=[make_visa("CN")],
        )
        group = make_group(
            traveler, segments=[make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)]
        )

        result = evaluate_group_itinerary(group, base_requirements)

        passport_result = result.traveler_summaries[0].passport_result
        assert passport_result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in passport_result.reasons)
        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_missing_visa_traveler(self, base_requirements):
        traveler = make_traveler(nationality="IN", visa_records=[])
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        visa_result = result.traveler_summaries[0].visa_result
        assert visa_result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """group_severity is the highest severity found across all travelers and both evaluators."""

    def test_info_only_when_all_alerts_are_info(self, base_requirements):
        # Passport valid past DE's 3-month minimum but within 6 months → INFO
        traveler = make_traveler(nationality="US", passport_expiry=DEPARTURE.replace(month=8))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.INFO

    def test_warning_from_visa_beats_info_from_passport(self, base_requirements):
        # Visa expires on departure date → WARNING; passport in 6-month advisory window → INFO
        traveler = make_traveler(
            nationality="IN",
            passport_expiry=DEPARTURE.replace(month=8),
            visa_records=[make_visa("DE", expiry_date=DEPARTURE)],
        )
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_traveler_dominates_info_traveler(self, base_requirements):
        info_traveler = make_traveler(
            skymiles_number="1", nationality="US", passport_expiry=DEPARTURE.replace(month=8)
        )
        critical_traveler = make_traveler(skymiles_number="2", nationality="IN", visa_records=[])
        group = make_group(info_traveler, critical_traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_severity_does_not_downgrade_when_later_traveler_is_lower(self, base_requirements):
        critical_traveler = make_traveler(skymiles_number="1", nationality="IN", visa_records=[])
        info_traveler = make_traveler(
            skymiles_number="2", nationality="US", passport_expiry=DEPARTURE.replace(month=8)
        )
        group = make_group(critical_traveler, info_traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_equal_severity_across_travelers_is_retained(self, base_requirements):
        group = make_group(
            make_traveler(skymiles_number="1", nationality="IN", visa_records=[]),
            make_traveler(skymiles_number="2", nationality="IN", visa_records=[]),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert all(
            s.visa_result.severity == AlertSeverity.CRITICAL for s in result.traveler_summaries
        )


class TestLayovers:
    """Layover segments follow the destination's transit-visa rule per traveler."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        # DE: transit_visa_required=False → layover needs no visa even for non-exempt nationality
        traveler = make_traveler(nationality="IN", visa_records=[])
        group = make_group(
            traveler,
            segments=[
                make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True)
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_with_transit_visa_requirement_alerts(self, base_requirements):
        # CN: transit_visa_required=True → layover without visa is CRITICAL
        traveler = make_traveler(nationality="IN", visa_records=[])
        group = make_group(
            traveler,
            segments=[
                make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True)
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert "No visa on file for CN" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_passport_validity_still_enforced(self, base_requirements):
        # Passport checks apply to layovers: CN needs 6 months, passport has ~4
        traveler = make_traveler(
            nationality="IN",
            passport_expiry=DEPARTURE.replace(month=7),
            visa_records=[make_visa("CN")],
        )
        group = make_group(
            traveler,
            segments=[
                make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True)
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        passport_result = result.traveler_summaries[0].passport_result
        assert passport_result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in passport_result.reasons)

    def test_mixed_nationalities_on_same_layover(self, base_requirements):
        # JP layover: transit visa not required → skipped for everyone regardless of exemption
        segments = [
            make_segment(destination="JP", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True),
            make_segment(
                destination="CN", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL200"
            ),
        ]
        exempt = make_traveler(skymiles_number="1", nationality="US", visa_records=[make_visa("CN")])
        non_exempt = make_traveler(skymiles_number="2", nationality="IN", visa_records=[])
        group = make_group(exempt, non_exempt, segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.traveler_summaries[1].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL


class TestDefaultRequirementsLookup:
    """When requirements is None, defaults are resolved per unique destination."""

    def test_defaults_used_when_requirements_omitted(self):
        # DE defaults exempt US nationals from visa; passport far in future → clean
        traveler = make_traveler(nationality="US")
        group = make_group(traveler)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.is_alert_required is False

    def test_defaults_apply_country_specific_visa_rule(self):
        # Default CN requirements have no exemptions → US national without visa is CRITICAL
        traveler = make_traveler(nationality="US", visa_records=[])
        group = make_group(
            traveler, segments=[make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)]
        )

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_defaults_apply_country_specific_passport_window(self):
        # Default DE requires 3 months; passport expires 2 months after departure
        traveler = make_traveler(nationality="US", passport_expiry=DEPARTURE.replace(month=5))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group)

        passport_result = result.traveler_summaries[0].passport_result
        assert passport_result.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in passport_result.reasons)

    def test_defaults_fall_back_for_unknown_country(self):
        # Unknown country → _DEFAULT_REQUIREMENTS: visa required, no exemptions
        assert get_requirements("ZZ").country_code == "DEFAULT"
        traveler = make_traveler(nationality="US", visa_records=[])
        group = make_group(
            traveler, segments=[make_segment(destination="ZZ", departure=DEPARTURE, arrival=ARRIVAL)]
        )

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for ZZ"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_repeated_destination_resolved_once(self):
        # Two segments to the same destination: both are still evaluated
        segments = [
            make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL100"),
            make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL300"),
        ]
        traveler = make_traveler(nationality="US", visa_records=[])
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for CN",
            "No visa on file for CN",
        ]

    def test_explicit_requirements_override_defaults(self):
        # Explicit dict makes CN visa-free, so the default "visa required" rule does not apply
        traveler = make_traveler(nationality="US", visa_records=[])
        group = make_group(
            traveler, segments=[make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)]
        )
        overrides = {"CN": replace(get_requirements("CN"), requires_visa=False)}

        result = evaluate_group_itinerary(group, overrides)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None


class TestItineraryChange:
    """Re-evaluating after an itinerary change reflects the new segments."""

    def test_adding_segment_introduces_alert(self, base_requirements):
        traveler = make_traveler(nationality="US", visa_records=[])
        original = make_group(traveler)
        changed = make_group(
            traveler,
            segments=original.segments
            + [make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL200")],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in after.traveler_summaries[0].visa_result.reasons

    def test_moving_departure_past_visa_expiry_escalates(self, base_requirements):
        # Within the 30-day advisory window of the original departure → INFO
        visa_expiry = date(2027, 3, 20)
        traveler = make_traveler(nationality="IN", visa_records=[make_visa("DE", expiry_date=visa_expiry)])
        original = make_group(traveler)
        changed = make_group(
            traveler,
            segments=[make_segment(destination="DE", departure=date(2027, 5, 1), arrival=date(2027, 5, 2))],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity == AlertSeverity.INFO
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "Visa for DE expires before date of travel" in after.traveler_summaries[0].visa_result.reasons

    def test_removing_problem_segment_clears_group_severity(self, base_requirements):
        traveler = make_traveler(nationality="US", visa_records=[])
        original = make_group(
            traveler,
            segments=[
                make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL),
                make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL200"),
            ],
        )
        changed = make_group(traveler, segments=original.segments[:1])

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity == AlertSeverity.CRITICAL
        assert after.group_severity is None
