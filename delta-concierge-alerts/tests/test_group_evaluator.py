"""Critical-path tests for the group itinerary evaluator."""

from dataclasses import replace
from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment


DEPARTURE = date(2030, 6, 1)
ARRIVAL = date(2030, 6, 2)
VALID_PASSPORT = date(2035, 1, 1)
# 4 months after DEPARTURE: meets a 3-month rule, fails a 6-month rule.
FOUR_MONTH_PASSPORT = date(2030, 10, 1)


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Test",
    last_name: str = "User",
    **profile_kwargs,
) -> SkyMilesProfile:
    """Build a uniquely identified traveler on top of conftest's make_profile."""
    profile_kwargs.setdefault("passport_expiry", VALID_PASSPORT)
    return replace(
        make_profile(**profile_kwargs),
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
    )


def _visa(
    country_code: str = "CN",
    expiry_date: date = date(2033, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


def _segment(destination: str = "DE", **overrides) -> FlightSegment:
    overrides.setdefault("departure", DEPARTURE)
    overrides.setdefault("arrival", ARRIVAL)
    return make_segment(destination=destination, **overrides)


def _group(
    *travelers: SkyMilesProfile,
    segments: list[FlightSegment] | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupItinerary:
    travelers_list = list(travelers) or [_traveler(nationality="US")]
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments if segments is not None else [_segment()],
        travelers=travelers_list,
        primary_traveler=travelers_list[0].skymiles_number if travelers_list else "",
    )


class TestCleanGroup:
    """A group with valid documents for every traveler produces no alerts."""

    def test_all_travelers_clean_has_no_group_severity(self, base_requirements):
        group = _group(
            _traveler("1000001", nationality="US"),
            _traveler("1000002", nationality="GB"),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert result.confirmation_number == "DL-GROUP"
        assert len(result.traveler_summaries) == 2
        for summary in result.traveler_summaries:
            assert summary.passport_result.is_alert_required is False
            assert summary.visa_result.is_alert_required is False

    def test_summary_carries_traveler_identity(self, base_requirements):
        group = _group(_traveler("1000042", "Ada", "Lovelace", nationality="US"))

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "1000042"
        assert summary.first_name == "Ada"
        assert summary.last_name == "Lovelace"

    def test_shared_segments_are_evaluated_for_each_traveler(self, base_requirements):
        segments = [_segment("DE", flight_number="DL1"), _segment("JP", flight_number="DL2")]
        group = _group(
            _traveler("1000001", nationality="US"),
            _traveler("1000002", nationality="GB"),
            segments=segments,
        )

        result = evaluate_group_itinerary(group, base_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments

    def test_traveler_order_is_preserved(self, base_requirements):
        group = _group(
            _traveler("1000003", nationality="US"),
            _traveler("1000001", nationality="US"),
            _traveler("1000002", nationality="US"),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == [
            "1000003", "1000001", "1000002",
        ]


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self, base_requirements):
        group = GroupItinerary(
            confirmation_number="DL-EMPTY",
            segments=[_segment()],
            travelers=[],
            primary_traveler="",
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None
        assert result.confirmation_number == "DL-EMPTY"


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across all travelers."""

    def test_single_critical_traveler_escalates_group(self, base_requirements):
        group = _group(
            _traveler("1000001", nationality="US"),
            _traveler("1000002", nationality="US", passport_number=None),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity is None
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL

    def test_info_only_group(self, base_requirements):
        group = _group(_traveler(nationality="US", passport_expiry=FOUR_MONTH_PASSPORT))

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_only_group(self, base_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN", expiry_date=DEPARTURE)])
        group = _group(traveler, segments=[_segment("CN")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in (
            result.traveler_summaries[0].visa_result.reasons
        )

    def test_later_lower_severity_does_not_downgrade(self, base_requirements):
        group = _group(
            _traveler("1000001", nationality="US", passport_number=None),
            _traveler("1000002", nationality="US", passport_expiry=FOUR_MONTH_PASSPORT),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_later_higher_severity_upgrades(self, base_requirements):
        group = _group(
            _traveler("1000001", nationality="US", passport_expiry=FOUR_MONTH_PASSPORT),
            _traveler("1000002", nationality="US", passport_number=None),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_severity_outranks_passport_severity_within_traveler(self, base_requirements):
        traveler = _traveler(
            nationality="IN",
            passport_expiry=date(2030, 11, 1),
            visa_records=[_visa("CN", expiry_date=DEPARTURE)],
        )
        reqs = {**base_requirements, "CN": replace(base_requirements["CN"], passport_validity_months=3)}
        group = _group(traveler, segments=[_segment("CN")])

        result = evaluate_group_itinerary(group, reqs)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_issues_are_isolated_per_traveler(self, base_requirements):
        group = _group(
            _traveler("1000001", nationality="IN", visa_records=[]),
            _traveler("1000002", nationality="IN", visa_records=[_visa("CN")]),
            segments=[_segment("CN")],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        missing, covered = result.traveler_summaries
        assert missing.visa_result.reasons == ["No visa on file for CN"]
        assert covered.visa_result.reasons == []
        assert result.group_severity == AlertSeverity.CRITICAL


class TestPassportValidityPerCountry:
    """Passport checks honour each destination's validity-month requirement."""

    def test_four_months_meets_schengen_three_month_rule(self, base_requirements):
        group = _group(
            _traveler(nationality="US", passport_expiry=FOUR_MONTH_PASSPORT),
            segments=[_segment("DE")],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.INFO
        assert not any("validity requirement" in r for r in passport.reasons)

    def test_four_months_fails_japan_six_month_rule(self, base_requirements):
        group = _group(
            _traveler(nationality="US", passport_expiry=FOUR_MONTH_PASSPORT),
            segments=[_segment("JP")],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert "Passport does not meet JP's 6-month validity requirement for DL100" in passport.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_expiring_before_departure(self, base_requirements):
        group = _group(_traveler(nationality="US", passport_expiry=date(2030, 5, 1)))

        result = evaluate_group_itinerary(group, base_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert "Passport expires before departure on DL100 to DE" in passport.reasons

    def test_passport_already_expired(self, base_requirements):
        group = _group(_traveler(nationality="US", passport_expiry=date(2020, 1, 1)))

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_missing_passport_expiry(self, base_requirements):
        group = _group(_traveler(nationality="US", passport_expiry=None))

        result = evaluate_group_itinerary(group, base_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.reasons == ["Passport information is incomplete"]
        assert passport.validation_errors[0].field == "passport_expiry"
        assert result.group_severity == AlertSeverity.CRITICAL


class TestVisaLimits:
    """Visa checks honour exemptions, expiry windows and best-match selection."""

    def test_visa_exempt_nationality_has_no_visa_alert(self, base_requirements):
        group = _group(_traveler(nationality="GB", visa_records=[]), segments=[_segment("JP")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_non_exempt_nationality_without_visa_is_critical(self, base_requirements):
        group = _group(_traveler(nationality="IN", visa_records=[]), segments=[_segment("JP")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert "No visa on file for JP" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expiring_within_warning_window_is_info(self, base_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN", expiry_date=date(2030, 6, 20))])
        group = _group(traveler, segments=[_segment("CN")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "Visa for CN expires within 30 days of travel"
        ]
        assert result.group_severity == AlertSeverity.INFO

    def test_latest_matching_visa_is_selected(self, base_requirements):
        traveler = _traveler(
            nationality="IN",
            visa_records=[
                _visa("CN", expiry_date=date(2022, 1, 1), visa_number="OLD"),
                _visa("CN", expiry_date=date(2033, 1, 1), visa_number="NEW"),
            ],
        )
        group = _group(traveler, segments=[_segment("CN")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None


class TestLayovers:
    """Layover segments skip visa checks unless the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        group = _group(
            _traveler(nationality="IN", visa_records=[]),
            segments=[_segment("DE", is_layover=True)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_with_transit_visa_requirement_alerts(self, base_requirements):
        group = _group(
            _traveler(nationality="IN", visa_records=[]),
            segments=[_segment("CN", is_layover=True)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert "No visa on file for CN" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_still_enforces_passport_validity(self, base_requirements):
        group = _group(
            _traveler(nationality="US", passport_expiry=FOUR_MONTH_PASSPORT),
            segments=[_segment("JP", is_layover=True)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_connecting_itinerary_flags_only_final_destination(self, base_requirements):
        segments = [
            _segment("DE", flight_number="DL1", is_layover=True),
            _segment("JP", flight_number="DL2", departure=date(2030, 6, 2), arrival=date(2030, 6, 3)),
        ]
        group = _group(_traveler(nationality="IN", visa_records=[]), segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for JP"]


class TestRequirementsSource:
    """Explicit requirements override defaults; None falls back to country data."""

    def test_explicit_requirements_override_defaults(self):
        override = {
            "CN": TravelDocRequirements(
                country_code="CN",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        group = _group(_traveler(nationality="IN", visa_records=[]), segments=[_segment("CN")])

        result = evaluate_group_itinerary(group, override)

        assert result.group_severity is None

    def test_default_requirements_used_when_none(self):
        group = _group(_traveler(nationality="IN", visa_records=[]), segments=[_segment("CN")])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_default_requirements_include_country_exemptions(self):
        group = _group(_traveler(nationality="BR", visa_records=[]), segments=[_segment("DE")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_default_lookup_handles_repeated_destinations(self):
        segments = [
            _segment("CN", flight_number="DL1"),
            _segment("CN", flight_number="DL2", departure=date(2030, 6, 10), arrival=date(2030, 6, 11)),
        ]
        group = _group(_traveler(nationality="IN", visa_records=[]), segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for CN",
            "No visa on file for CN",
        ]

    def test_unknown_country_falls_back_to_default_rules(self):
        group = _group(_traveler(nationality="US", visa_records=[]), segments=[_segment("ZZ")])

        result = evaluate_group_itinerary(group)

        assert "No visa on file for ZZ" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL


class TestItineraryChanges:
    """Re-evaluating a changed group itinerary reflects the new segments."""

    def test_destination_change_introduces_visa_alert(self, base_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN")])
        original = _group(traveler, segments=[_segment("CN")])
        changed = replace(original, segments=[_segment("JP")])

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for JP" in after.traveler_summaries[0].visa_result.reasons

    def test_date_change_past_visa_expiry_introduces_alert(self, base_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN", expiry_date=date(2030, 12, 31))])
        original = _group(traveler, segments=[_segment("CN")])
        changed = replace(
            original,
            segments=[_segment("CN", departure=date(2031, 3, 1), arrival=date(2031, 3, 2))],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert "Visa for CN expires before date of travel" in after.traveler_summaries[0].visa_result.reasons
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_added_transit_layover_introduces_alert(self, base_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("JP")])
        original = _group(traveler, segments=[_segment("JP")])
        changed = replace(
            original,
            segments=[_segment("CN", flight_number="DL5", is_layover=True), _segment("JP")],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_removed_problem_segment_clears_alert(self, base_requirements):
        traveler = _traveler(nationality="IN", visa_records=[_visa("JP")])
        original = _group(traveler, segments=[_segment("CN", is_layover=True), _segment("JP")])
        changed = replace(original, segments=[_segment("JP")])

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity == AlertSeverity.CRITICAL
        assert after.group_severity is None
