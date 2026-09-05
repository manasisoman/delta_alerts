"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment


DEPARTURE = date(2028, 9, 1)
ARRIVAL = date(2028, 9, 2)


def make_traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Test",
    last_name: str = "User",
    nationality: str = "US",
    passport_expiry: date | None = date(2035, 1, 1),
    visa_records: list[VisaRecord] | None = None,
) -> SkyMilesProfile:
    """Build a traveler that is clean by default (US national, long-valid passport)."""
    profile = make_profile(
        nationality=nationality,
        passport_expiry=passport_expiry,
        visa_records=visa_records,
    )
    profile.skymiles_number = skymiles_number
    profile.first_name = first_name
    profile.last_name = last_name
    return profile


def make_group(
    *travelers: SkyMilesProfile,
    segments=None,
    confirmation: str = "DL-GROUP",
) -> GroupItinerary:
    """Build a GroupItinerary with a single DE segment by default."""
    travelers = list(travelers) or [make_traveler()]
    if segments is None:
        segments = [make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)]
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments,
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number,
    )


class TestCleanGroup:
    """A group of fully compliant travelers yields no alerts and no severity."""

    def test_single_clean_traveler(self, base_requirements):
        group = make_group(make_traveler())

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_summary_carries_traveler_identity(self, base_requirements):
        traveler = make_traveler(skymiles_number="4242424", first_name="Ada", last_name="Lovelace")
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "4242424"
        assert summary.first_name == "Ada"
        assert summary.last_name == "Lovelace"

    def test_empty_traveler_list(self, base_requirements):
        group = make_group()
        group.travelers = []

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestPerTravelerResults:
    """Each traveler is evaluated independently and in order."""

    def test_summaries_preserve_traveler_order(self, base_requirements):
        group = make_group(
            make_traveler(skymiles_number="1"),
            make_traveler(skymiles_number="2"),
            make_traveler(skymiles_number="3"),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == ["1", "2", "3"]

    def test_only_affected_traveler_flagged(self, base_requirements):
        clean = make_traveler(skymiles_number="1")
        no_passport = make_traveler(skymiles_number="2", passport_expiry=None)
        group = make_group(clean, no_passport)

        result = evaluate_group_itinerary(group, base_requirements)

        clean_summary, flagged_summary = result.traveler_summaries
        assert clean_summary.passport_result.is_alert_required is False
        assert flagged_summary.passport_result.is_alert_required is True
        assert flagged_summary.passport_result.severity == AlertSeverity.CRITICAL

    def test_visa_issue_surfaces_in_visa_result(self, base_requirements):
        # IN national with no DE visa on file -> visa CRITICAL, passport clean
        traveler = make_traveler(nationality="IN")
        group = make_group(traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is True
        assert summary.visa_result.severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers and result types."""

    def test_critical_from_passport(self, base_requirements):
        group = make_group(make_traveler(passport_expiry=None))

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_critical_from_visa(self, base_requirements):
        group = make_group(make_traveler(nationality="IN"))

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_info_when_passport_expires_within_six_months(self, base_requirements):
        # Passport valid for ~5 months past departure: above DE's 3-month min, within 6-month advisory
        group = make_group(make_traveler(passport_expiry=date(2029, 2, 1)))

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_when_visa_expires_on_departure(self, base_requirements):
        visa = VisaRecord(
            country_code="DE",
            visa_type="TOURIST",
            issue_date=date(2027, 1, 1),
            expiry_date=DEPARTURE,
            visa_number="V1",
        )
        group = make_group(make_traveler(nationality="IN", visa_records=[visa]))

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_traveler_outranks_info_traveler(self, base_requirements):
        info_traveler = make_traveler(skymiles_number="1", passport_expiry=date(2029, 2, 1))
        critical_traveler = make_traveler(skymiles_number="2", passport_expiry=None)
        group = make_group(info_traveler, critical_traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_lower_severity_later_does_not_downgrade(self, base_requirements):
        critical_traveler = make_traveler(skymiles_number="1", passport_expiry=None)
        info_traveler = make_traveler(skymiles_number="2", passport_expiry=date(2029, 2, 1))
        group = make_group(critical_traveler, info_traveler)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_equal_severity_across_travelers(self, base_requirements):
        group = make_group(
            make_traveler(skymiles_number="1", passport_expiry=None),
            make_traveler(skymiles_number="2", passport_expiry=None),
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestRequirementsResolution:
    """Requirements are taken from the caller when given, else looked up per destination."""

    def test_default_lookup_when_requirements_omitted(self):
        # DE is Schengen (3-month rule) in default data; passport expiring 2 months after departure -> CRITICAL
        group = make_group(make_traveler(passport_expiry=date(2028, 11, 1)))

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any("3-month" in r for r in result.traveler_summaries[0].passport_result.reasons)

    def test_default_lookup_deduplicates_repeated_destinations(self):
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL100"),
            make_segment(destination="DE", departure=date(2028, 9, 10), arrival=date(2028, 9, 11), flight_number="DL200"),
        ]
        group = make_group(make_traveler(), segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert len(result.traveler_summaries[0].passport_result.segments_evaluated) == 2

    def test_default_lookup_multiple_destinations(self):
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL100"),
            make_segment(destination="JP", departure=date(2028, 9, 10), arrival=date(2028, 9, 11), flight_number="DL200"),
        ]
        group = make_group(make_traveler(), segments=segments)

        result = evaluate_group_itinerary(group)

        assert len(result.traveler_summaries) == 1
        assert result.confirmation_number == "DL-GROUP"

    def test_explicit_requirements_override_defaults(self):
        # Caller says DE needs no visa and only 1 month of passport validity
        lenient = {
            "DE": TravelDocRequirements(
                country_code="DE",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=1,
                visa_exempt_nationalities=[],
            )
        }
        group = make_group(make_traveler(nationality="IN", passport_expiry=date(2029, 6, 1)))

        result = evaluate_group_itinerary(group, lenient)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.is_alert_required is False


class TestSharedItinerary:
    """All travelers are evaluated against the same shared segments."""

    def test_layover_segment_shared_by_all_travelers(self, base_requirements):
        # CN layover requires transit visa; both IN travelers lack one
        segments = [
            make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True, flight_number="DL100"),
            make_segment(destination="JP", departure=date(2028, 9, 3), arrival=date(2028, 9, 4), flight_number="DL200"),
        ]
        group = make_group(
            make_traveler(skymiles_number="1", nationality="IN"),
            make_traveler(skymiles_number="2", nationality="IN"),
            segments=segments,
        )

        result = evaluate_group_itinerary(group, base_requirements)

        for summary in result.traveler_summaries:
            assert summary.visa_result.severity == AlertSeverity.CRITICAL
            assert len(summary.passport_result.segments_evaluated) == 2
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_confirmation_number_propagated(self, base_requirements):
        group = make_group(make_traveler(), confirmation="DL-XYZ789")

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-XYZ789"
