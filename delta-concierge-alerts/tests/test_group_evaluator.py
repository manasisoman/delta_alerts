"""Tests for the group itinerary evaluator."""

from dataclasses import replace
from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment


def make_traveler(
    skymiles_number: str = "1111111",
    first_name: str = "Test",
    last_name: str = "User",
    **profile_kwargs,
) -> SkyMilesProfile:
    """Build a traveler profile with an identifiable name/number."""
    return replace(
        make_profile(**profile_kwargs),
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
    )


def make_group(
    *travelers: SkyMilesProfile,
    segments: list[FlightSegment] | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupItinerary:
    """Build a GroupItinerary from travelers and segments."""
    seg_list = segments if segments is not None else [make_segment()]
    traveler_list = list(travelers)
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=seg_list,
        travelers=traveler_list,
        primary_traveler=traveler_list[0].skymiles_number if traveler_list else "",
    )


def make_visa(
    country_code: str = "DE",
    expiry: date = date(2030, 1, 1),
    issue: date = date(2020, 1, 1),
    visa_number: str = "V1",
) -> VisaRecord:
    """Build a VisaRecord with sensible defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=issue,
        expiry_date=expiry,
        visa_number=visa_number,
    )


class TestCleanGroup:
    """A group whose travelers have fully valid documents raises no alerts."""

    def test_no_severity_when_all_travelers_are_clean(self, base_requirements):
        traveler = make_traveler(
            nationality="US",  # visa-exempt for DE
            passport_expiry=date(2030, 6, 1),
        )
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[0].visa_result.is_alert_required is False

    def test_summary_carries_traveler_identity_and_confirmation(self, base_requirements):
        traveler = make_traveler(
            skymiles_number="4242424",
            first_name="Ada",
            last_name="Lovelace",
            nationality="US",
            passport_expiry=date(2030, 6, 1),
        )
        group = make_group(
            traveler,
            segments=[make_segment(destination="DE")],
            confirmation="DL-ABC123",
        )

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert result.confirmation_number == "DL-ABC123"
        assert (summary.skymiles_number, summary.first_name, summary.last_name) == (
            "4242424",
            "Ada",
            "Lovelace",
        )
        assert summary.passport_result.profile is traveler
        assert summary.visa_result.profile is traveler


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self, base_requirements):
        group = make_group(segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across every traveler."""

    def test_critical_from_one_traveler_wins_over_clean_others(self, base_requirements):
        clean = make_traveler(
            skymiles_number="1111111", nationality="US", passport_expiry=date(2030, 6, 1)
        )
        # No visa on file for DE and nationality is not exempt → CRITICAL
        no_visa = make_traveler(
            skymiles_number="2222222", nationality="IN", passport_expiry=date(2030, 6, 1)
        )
        group = make_group(clean, no_visa, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.severity is None
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL

    def test_info_only_group(self, base_requirements):
        # Passport valid past DE's 3-month minimum but under 6 months → INFO
        traveler = make_traveler(
            nationality="US", passport_expiry=date(2027, 1, 1)
        )
        group = make_group(
            traveler, segments=[make_segment(destination="DE", departure=date(2026, 9, 1))]
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_beats_info(self, base_requirements):
        # Traveler A: passport expiring within 6 months of travel → INFO
        info_traveler = make_traveler(
            skymiles_number="1111111", nationality="US", passport_expiry=date(2027, 1, 1)
        )
        # Traveler B: visa expires exactly on departure date → WARNING
        warning_traveler = make_traveler(
            skymiles_number="2222222",
            nationality="IN",
            passport_expiry=date(2030, 6, 1),
            visa_records=[make_visa(country_code="DE", expiry=date(2026, 9, 1))],
        )
        group = make_group(
            info_traveler,
            warning_traveler,
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_first_traveler_is_not_downgraded(self, base_requirements):
        # CRITICAL traveler comes first, INFO traveler second — order must not matter
        critical_traveler = make_traveler(
            skymiles_number="1111111", nationality="IN", passport_expiry=date(2030, 6, 1)
        )
        info_traveler = make_traveler(
            skymiles_number="2222222", nationality="US", passport_expiry=date(2027, 1, 1)
        )
        group = make_group(
            critical_traveler,
            info_traveler,
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_summary_order_matches_traveler_order(self, base_requirements):
        first = make_traveler(skymiles_number="1111111", nationality="US")
        second = make_traveler(skymiles_number="2222222", nationality="US")
        third = make_traveler(skymiles_number="3333333", nationality="US")
        group = make_group(first, second, third)

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == [
            "1111111",
            "2222222",
            "3333333",
        ]


class TestSharedSegments:
    """All travelers are evaluated against the same shared segment list."""

    def test_every_traveler_sees_all_segments(self, base_requirements):
        segments = [
            make_segment(destination="DE", flight_number="DL100"),
            make_segment(
                destination="JP", flight_number="DL200", departure=date(2026, 9, 10)
            ),
        ]
        travelers = [
            make_traveler(skymiles_number="1111111", nationality="US"),
            make_traveler(skymiles_number="2222222", nationality="US"),
        ]
        group = make_group(*travelers, segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments

    def test_multi_segment_severity_uses_worst_destination(self, base_requirements):
        # IN national: DE requires a visa (not exempt) → CRITICAL on the DE segment
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(
            traveler,
            segments=[
                make_segment(destination="JP", flight_number="DL100"),
                make_segment(
                    destination="DE", flight_number="DL200", departure=date(2026, 9, 10)
                ),
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for DE" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )


class TestLayoverSegments:
    """Layover handling flows through to the per-traveler visa results."""

    def test_layover_without_transit_visa_is_skipped(self, base_requirements):
        # DE layover, transit_visa_required=False → no visa alert despite no visa on file
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(
            traveler, segments=[make_segment(destination="DE", is_layover=True)]
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.is_alert_required is False

    def test_layover_requiring_transit_visa_still_alerts(self, base_requirements):
        # CN layover, transit_visa_required=True → CRITICAL with no visa on file
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(
            traveler, segments=[make_segment(destination="CN", is_layover=True)]
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for CN" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )

    def test_layover_passport_minimum_still_enforced(self, base_requirements):
        # CN layover needs 6 months of passport validity; this passport has ~2
        traveler = make_traveler(nationality="CN", passport_expiry=date(2026, 11, 1))
        group = make_group(
            traveler,
            segments=[
                make_segment(
                    destination="CN", departure=date(2026, 9, 1), is_layover=True
                )
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "does not meet CN's 6-month" in r
            for r in result.traveler_summaries[0].passport_result.reasons
        )


class TestDefaultRequirementsLookup:
    """With no requirements passed in, defaults are looked up per destination."""

    def test_defaults_used_when_requirements_is_none(self):
        # JP exempts US nationals from visas in the real requirements data
        traveler = make_traveler(nationality="US", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="JP")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_defaults_flag_missing_visa(self):
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="JP")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for JP" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )

    def test_repeated_destination_is_looked_up_once(self):
        # Two segments to the same country exercise the dedupe path
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(
            traveler,
            segments=[
                make_segment(destination="JP", flight_number="DL100"),
                make_segment(
                    destination="JP", flight_number="DL200", departure=date(2026, 9, 10)
                ),
            ],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for JP",
            "No visa on file for JP",
        ]

    def test_unknown_destination_falls_back_to_default(self):
        # ZZ is not in the requirements data → default (visa required, 6 months)
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="ZZ")])

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for ZZ" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )


class TestRequirementsOverride:
    """An explicit requirements mapping overrides the default country data."""

    def test_override_dropping_visa_requirement_clears_alert(self, base_requirements):
        overrides = dict(base_requirements)
        overrides["DE"] = replace(base_requirements["DE"], requires_visa=False)
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, overrides)

        assert result.group_severity is None

    def test_override_adding_nationality_exemption_clears_alert(self, base_requirements):
        overrides = dict(base_requirements)
        overrides["DE"] = replace(
            base_requirements["DE"], visa_exempt_nationalities=["IN"]
        )
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, overrides)

        assert result.group_severity is None

    def test_override_raising_passport_validity_months_creates_alert(
        self, base_requirements
    ):
        overrides = dict(base_requirements)
        overrides["DE"] = replace(
            base_requirements["DE"],
            passport_validity_months=48,
            visa_exempt_nationalities=["IN"],
        )
        traveler = make_traveler(nationality="IN", passport_expiry=date(2030, 6, 1))
        group = make_group(
            traveler, segments=[make_segment(destination="DE", departure=date(2026, 9, 1))]
        )

        result = evaluate_group_itinerary(group, overrides)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "does not meet DE's 48-month" in r
            for r in result.traveler_summaries[0].passport_result.reasons
        )


class TestVisaSelection:
    """The latest-expiring visa for a country is the one evaluated."""

    def test_latest_expiry_visa_wins(self, base_requirements):
        traveler = make_traveler(
            nationality="IN",
            passport_expiry=date(2030, 6, 1),
            visa_records=[
                make_visa(country_code="DE", expiry=date(2020, 1, 1), visa_number="OLD"),
                make_visa(country_code="DE", expiry=date(2030, 1, 1), visa_number="NEW"),
            ],
        )
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None

    def test_visa_for_other_country_does_not_count(self, base_requirements):
        traveler = make_traveler(
            nationality="IN",
            passport_expiry=date(2030, 6, 1),
            visa_records=[make_visa(country_code="JP", expiry=date(2030, 1, 1))],
        )
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestMissingPassportInfo:
    """Missing passport data escalates the whole group to CRITICAL."""

    def test_missing_passport_number(self, base_requirements):
        traveler = make_traveler(nationality="US", passport_number=None)
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.reasons == [
            "Passport information is incomplete"
        ]

    def test_expired_passport(self, base_requirements):
        traveler = make_traveler(nationality="US", passport_expiry=date(2020, 1, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.reasons == [
            "Passport has expired"
        ]


class TestItineraryChange:
    """Re-evaluating a group after an itinerary change reflects the new segments."""

    def test_added_destination_introduces_alert(self, base_requirements):
        traveler = make_traveler(
            nationality="US",
            passport_expiry=date(2030, 6, 1),
            visa_records=[make_visa(country_code="CN", expiry=date(2030, 1, 1))],
        )
        original = make_group(traveler, segments=[make_segment(destination="DE")])

        before = evaluate_group_itinerary(original, base_requirements)

        # Itinerary changes: CN leg added, and the traveler's CN visa is expired
        stale_visa_traveler = make_traveler(
            nationality="US",
            passport_expiry=date(2030, 6, 1),
            visa_records=[make_visa(country_code="CN", expiry=date(2020, 1, 1))],
        )
        changed = make_group(
            stale_visa_traveler,
            segments=[
                make_segment(destination="DE", flight_number="DL100"),
                make_segment(
                    destination="CN", flight_number="DL200", departure=date(2026, 9, 10)
                ),
            ],
        )

        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert any(
            "Visa for CN has expired" in r
            for r in after.traveler_summaries[0].visa_result.reasons
        )

    def test_removed_destination_clears_alert(self, base_requirements):
        traveler = make_traveler(nationality="US", passport_expiry=date(2030, 6, 1))
        original = make_group(
            traveler,
            segments=[
                make_segment(destination="DE", flight_number="DL100"),
                make_segment(
                    destination="CN", flight_number="DL200", departure=date(2026, 9, 10)
                ),
            ],
        )

        before = evaluate_group_itinerary(original, base_requirements)

        changed = make_group(traveler, segments=[make_segment(destination="DE")])

        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity == AlertSeverity.CRITICAL
        assert after.group_severity is None
