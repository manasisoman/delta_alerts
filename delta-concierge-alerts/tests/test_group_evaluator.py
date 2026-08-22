"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, GroupItinerary, SkyMilesProfile, VisaRecord

from tests.conftest import make_segment


def make_traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Group",
    last_name: str = "Traveler",
    nationality: str = "US",
    passport_number: str | None = "P999999",
    passport_expiry: date | None = date(2031, 6, 1),
    visa_records: list[VisaRecord] | None = None,
) -> SkyMilesProfile:
    """Build a SkyMilesProfile that is clean by default (no alerts expected)."""
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


def make_visa(
    country_code: str = "DE",
    expiry_date: date = date(2031, 1, 1),
    issue_date: date = date(2024, 1, 1),
    visa_type: str = "TOURIST",
    visa_number: str = "V123",
) -> VisaRecord:
    """Build a VisaRecord that is valid by default."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


def make_group(
    *travelers: SkyMilesProfile,
    segments: list | None = None,
    confirmation: str = "DL-GROUP",
) -> GroupItinerary:
    """Build a GroupItinerary sharing one DE segment by default."""
    seg_list = segments if segments is not None else [
        make_segment(destination="DE", departure=date(2030, 1, 1), arrival=date(2030, 1, 2))
    ]
    traveler_list = list(travelers)
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=seg_list,
        travelers=traveler_list,
        primary_traveler=traveler_list[0].skymiles_number if traveler_list else "",
    )


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self, base_requirements):
        group = make_group()

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestSingleTraveler:
    """A single clean traveler produces one summary and no group severity."""

    def test_clean_traveler_has_no_severity(self, base_requirements):
        traveler = make_traveler()
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == traveler.skymiles_number
        assert summary.first_name == "Group"
        assert summary.last_name == "Traveler"
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_missing_passport_yields_critical(self, base_requirements):
        traveler = make_traveler(passport_number=None, passport_expiry=None)
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL
        assert summary.passport_result.validation_errors[0].code == "MISSING_PASSPORT_INFO"
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expired_passport_yields_critical(self, base_requirements):
        traveler = make_traveler(passport_expiry=date(2020, 1, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across all travelers."""

    def test_info_only_group(self, base_requirements):
        # Visa expires 19 days after departure — inside the 30-day window → INFO
        traveler = make_traveler(
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2030, 1, 20))],
        )
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_beats_info(self, base_requirements):
        # Traveler 1: visa expires on departure date → WARNING
        # Traveler 2: visa expires inside the 30-day window → INFO
        warning_traveler = make_traveler(
            skymiles_number="1000002",
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2030, 1, 1))],
        )
        info_traveler = make_traveler(
            skymiles_number="1000003",
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2030, 1, 20))],
        )
        group = make_group(warning_traveler, info_traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert len(result.traveler_summaries) == 2
        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_beats_warning_and_clean(self, base_requirements):
        # Traveler 1: clean, Traveler 2: WARNING visa, Traveler 3: no visa on file → CRITICAL
        clean = make_traveler(skymiles_number="1000001")
        warning = make_traveler(
            skymiles_number="1000002",
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2030, 1, 1))],
        )
        critical = make_traveler(skymiles_number="1000003", nationality="IN")
        group = make_group(clean, warning, critical)

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == [
            "1000001",
            "1000002",
            "1000003",
        ]
        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.traveler_summaries[2].visa_result.reasons == ["No visa on file for DE"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_earlier_higher_severity_is_not_downgraded(self, base_requirements):
        # Traveler 1: CRITICAL, Traveler 2: INFO — order must not lower the group severity
        critical = make_traveler(skymiles_number="1000001", nationality="IN")
        info = make_traveler(
            skymiles_number="1000002",
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2030, 1, 20))],
        )
        group = make_group(critical, info)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestPassportValidityAgainstCountryRequirements:
    """Per-traveler passport validity is checked against each destination's minimum."""

    def test_below_destination_minimum(self, base_requirements):
        # DE requires 3 months of validity; passport expires 2 months after departure
        traveler = make_traveler(passport_expiry=date(2030, 3, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert any("does not meet DE's 3-month" in r for r in summary.passport_result.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expiring_within_six_months_is_info(self, base_requirements):
        # Passport meets DE's 3-month minimum but expires within 6 months of travel
        traveler = make_traveler(passport_expiry=date(2030, 5, 1))
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.INFO


class TestLayoverSegments:
    """Layovers are evaluated per the destination's transit visa rules."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        # DE does not require a transit visa, so the layover raises no visa alert
        traveler = make_traveler(nationality="IN")
        layover = make_segment(
            destination="DE",
            departure=date(2030, 1, 1),
            arrival=date(2030, 1, 1),
            is_layover=True,
            flight_number="DL500",
        )
        group = make_group(traveler, segments=[layover])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_requiring_transit_visa_yields_critical(self, base_requirements):
        # CN requires a transit visa, so a layover without a visa is CRITICAL
        traveler = make_traveler(nationality="IN")
        layover = make_segment(
            destination="CN",
            departure=date(2030, 1, 1),
            arrival=date(2030, 1, 1),
            is_layover=True,
            flight_number="DL600",
        )
        group = make_group(traveler, segments=[layover])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_multi_segment_itinerary_evaluates_every_destination(self, base_requirements):
        # Layover in DE (no transit visa needed) then final destination CN (visa needed)
        traveler = make_traveler(nationality="IN")
        layover = make_segment(
            destination="DE",
            departure=date(2030, 1, 1),
            arrival=date(2030, 1, 1),
            is_layover=True,
            flight_number="DL700",
        )
        final = make_segment(
            destination="CN",
            departure=date(2030, 1, 2),
            arrival=date(2030, 1, 3),
            flight_number="DL701",
        )
        group = make_group(traveler, segments=[layover, final])

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.visa_result.reasons == ["No visa on file for CN"]
        assert summary.passport_result.segments_evaluated == [layover, final]
        assert result.group_severity == AlertSeverity.CRITICAL


class TestDefaultRequirementsLookup:
    """When no requirements are supplied, they are looked up from the static data."""

    def test_requirements_default_to_country_data(self):
        # DE exempts US nationals in the shipped country data → clean result
        traveler = make_traveler(nationality="US")
        group = make_group(traveler)

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_unknown_destination_falls_back_to_default_requirements(self):
        # ZZ is not in the country data → default requires a visa for every nationality
        traveler = make_traveler(nationality="US")
        segment = make_segment(
            destination="ZZ", departure=date(2030, 1, 1), arrival=date(2030, 1, 2)
        )
        group = make_group(traveler, segments=[segment])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for ZZ"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_repeated_destination_is_looked_up_once(self):
        # Two segments to the same country exercise the dedupe branch
        traveler = make_traveler(nationality="IN")
        outbound = make_segment(
            destination="DE",
            departure=date(2030, 1, 1),
            arrival=date(2030, 1, 2),
            flight_number="DL800",
        )
        inbound = make_segment(
            destination="DE",
            departure=date(2030, 1, 10),
            arrival=date(2030, 1, 11),
            flight_number="DL801",
        )
        group = make_group(traveler, segments=[outbound, inbound])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for DE",
            "No visa on file for DE",
        ]
        assert result.group_severity == AlertSeverity.CRITICAL


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_added_destination_changes_group_severity(self, base_requirements):
        traveler = make_traveler(
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2031, 1, 1))],
        )
        original = make_segment(
            destination="DE", departure=date(2030, 1, 1), arrival=date(2030, 1, 2)
        )
        group = make_group(traveler, segments=[original])

        before = evaluate_group_itinerary(group, base_requirements)

        added = make_segment(
            destination="CN",
            departure=date(2030, 1, 5),
            arrival=date(2030, 1, 6),
            flight_number="DL900",
        )
        changed_group = make_group(traveler, segments=[original, added])

        after = evaluate_group_itinerary(changed_group, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert after.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]

    def test_confirmation_number_is_propagated_to_shared_itinerary(self, base_requirements):
        traveler = make_traveler()
        group = make_group(traveler, confirmation="DL-CHANGED")

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-CHANGED"
        assert result.traveler_summaries[0].passport_result.segments_evaluated == group.segments


class TestVisaSelectionPerTraveler:
    """The latest-expiring visa is used for each traveler in the group."""

    def test_latest_expiring_visa_wins(self, base_requirements):
        traveler = make_traveler(
            nationality="IN",
            visa_records=[
                make_visa(country_code="DE", expiry_date=date(2020, 1, 1), visa_number="V-OLD"),
                make_visa(country_code="DE", expiry_date=date(2031, 1, 1), visa_number="V-NEW"),
            ],
        )
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_expired_visa_yields_critical(self, base_requirements):
        traveler = make_traveler(
            nationality="IN",
            visa_records=[make_visa(country_code="DE", expiry_date=date(2020, 1, 1))],
        )
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.reasons == ["Visa for DE has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL
