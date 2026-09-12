"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    TravelDocRequirements,
    VisaRecord,
)

from tests.conftest import make_profile, make_segment


def make_group(
    *travelers,
    segments=None,
    confirmation: str = "DL-GROUP",
    primary: str | None = None,
) -> GroupItinerary:
    """Helper to build a GroupItinerary with sensible defaults."""
    travelers = list(travelers)
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=list(segments) if segments is not None else [make_segment()],
        travelers=travelers,
        primary_traveler=primary or (travelers[0].skymiles_number if travelers else ""),
    )


def make_named_profile(skymiles: str, first: str, last: str, **overrides):
    """Build a profile with a distinct identity so summaries can be told apart."""
    profile = make_profile(**overrides)
    profile.skymiles_number = skymiles
    profile.first_name = first
    profile.last_name = last
    return profile


class TestCleanGroup:
    """A group where every traveler is compliant produces no severity."""

    def test_single_compliant_traveler_has_no_severity(self, base_requirements):
        # US traveler is visa-exempt for DE and has a far-future passport
        traveler = make_named_profile("1000001", "Alice", "Smith", nationality="US")
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "1000001"
        assert summary.first_name == "Alice"
        assert summary.last_name == "Smith"
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_empty_traveler_list_yields_empty_result(self, base_requirements):
        group = make_group(segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestPerTravelerSummaries:
    """Each traveler is evaluated independently and reported in order."""

    def test_summaries_preserve_traveler_order_and_identity(self, base_requirements):
        alice = make_named_profile("1000001", "Alice", "Smith", nationality="US")
        bob = make_named_profile("1000002", "Bob", "Jones", nationality="US")
        group = make_group(alice, bob, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == ["1000001", "1000002"]
        assert [s.first_name for s in result.traveler_summaries] == ["Alice", "Bob"]
        assert [s.last_name for s in result.traveler_summaries] == ["Smith", "Jones"]

    def test_one_flagged_traveler_does_not_affect_the_other(self, base_requirements):
        # Alice is clean; Bob has no passport on file → CRITICAL for Bob only
        alice = make_named_profile("1000001", "Alice", "Smith", nationality="US")
        bob = make_named_profile(
            "1000002", "Bob", "Jones", nationality="US", passport_number=None, passport_expiry=None
        )
        group = make_group(alice, bob, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, base_requirements)

        alice_summary, bob_summary = result.traveler_summaries
        assert alice_summary.passport_result.is_alert_required is False
        assert alice_summary.passport_result.severity is None
        assert bob_summary.passport_result.is_alert_required is True
        assert bob_summary.passport_result.severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across all travelers and result types."""

    def test_info_only_group_reports_info(self, base_requirements):
        # Passport meets DE's 3-month minimum but expires within 6 months of travel → INFO
        traveler = make_named_profile(
            "1000001", "Alice", "Smith", nationality="US", passport_expiry=date(2027, 7, 1)
        )
        segment = make_segment(
            destination="DE", departure=date(2027, 3, 1), arrival=date(2027, 3, 2)
        )
        group = make_group(traveler, segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].visa_result.severity is None
        assert result.group_severity == AlertSeverity.INFO

    def test_warning_visa_outranks_info_passport_within_one_traveler(self, base_requirements):
        # Passport → INFO; visa expiring on departure date → WARNING; WARNING wins
        visa = VisaRecord(
            country_code="DE",
            visa_type="TOURIST",
            issue_date=date(2026, 1, 1),
            expiry_date=date(2027, 3, 1),
            visa_number="V1",
        )
        traveler = make_named_profile(
            "1000001",
            "Alice",
            "Smith",
            nationality="IN",
            passport_expiry=date(2027, 7, 1),
            visa_records=[visa],
        )
        segment = make_segment(
            destination="DE", departure=date(2027, 3, 1), arrival=date(2027, 3, 2)
        )
        group = make_group(traveler, segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_traveler_outranks_warning_traveler(self, base_requirements):
        # Alice: visa expires on departure → WARNING. Bob: missing visa → CRITICAL.
        visa = VisaRecord(
            country_code="DE",
            visa_type="TOURIST",
            issue_date=date(2026, 1, 1),
            expiry_date=date(2027, 3, 1),
            visa_number="V1",
        )
        alice = make_named_profile("1000001", "Alice", "Smith", nationality="IN", visa_records=[visa])
        bob = make_named_profile("1000002", "Bob", "Jones", nationality="IN")
        segment = make_segment(
            destination="DE", departure=date(2027, 3, 1), arrival=date(2027, 3, 2)
        )
        group = make_group(alice, bob, segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        alice_summary, bob_summary = result.traveler_summaries
        assert alice_summary.visa_result.severity == AlertSeverity.WARNING
        assert bob_summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_lower_severity_later_traveler_does_not_downgrade_group(self, base_requirements):
        # Bob (CRITICAL) is listed first, Alice (INFO) second; group stays CRITICAL
        bob = make_named_profile("1000002", "Bob", "Jones", nationality="IN")
        alice = make_named_profile(
            "1000001", "Alice", "Smith", nationality="US", passport_expiry=date(2027, 7, 1)
        )
        segment = make_segment(
            destination="DE", departure=date(2027, 3, 1), arrival=date(2027, 3, 2)
        )
        group = make_group(bob, alice, segments=[segment])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.CRITICAL


class TestDefaultRequirementsLookup:
    """When no requirements are provided, they are resolved from the default country data."""

    def test_requirements_resolved_from_default_data(self):
        # IN traveler with no visa on file → DE requires a visa by default → CRITICAL
        traveler = make_named_profile("1000001", "Alice", "Smith", nationality="IN")
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.is_alert_required is True
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_repeated_destinations_are_evaluated_once_per_country(self):
        # Two DE segments plus a JP layover; US traveler is clean everywhere
        traveler = make_named_profile("1000001", "Alice", "Smith", nationality="US")
        segments = [
            make_segment(destination="DE", flight_number="DL100"),
            make_segment(
                destination="JP",
                is_layover=True,
                flight_number="DL200",
                departure=date(2026, 9, 3),
                arrival=date(2026, 9, 4),
            ),
            make_segment(
                destination="DE",
                flight_number="DL300",
                departure=date(2026, 9, 5),
                arrival=date(2026, 9, 6),
            ),
        ]
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group)

        assert len(result.traveler_summaries) == 1
        assert result.group_severity is None

    def test_explicit_requirements_take_precedence_over_defaults(self):
        # Override says DE needs no visa at all, so the IN traveler is clean
        overrides = {
            "DE": TravelDocRequirements(
                country_code="DE",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=0,
                visa_exempt_nationalities=[],
            )
        }
        traveler = make_named_profile("1000001", "Alice", "Smith", nationality="IN")
        group = make_group(traveler, segments=[make_segment(destination="DE")])

        result = evaluate_group_itinerary(group, overrides)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None
