"""Tests for the group evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    SkyMilesProfile,
    VisaRecord,
)

from tests.conftest import make_segment


def _make_traveler(
    skymiles_number: str = "9999999",
    first_name: str = "Test",
    last_name: str = "User",
    nationality: str = "US",
    passport_expiry: date = date(2030, 1, 1),
    passport_number: str | None = "P123456",
    visa_records: list[VisaRecord] | None = None,
) -> SkyMilesProfile:
    """Build a SkyMilesProfile for group tests with overridable identity."""
    return SkyMilesProfile(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        nationality=nationality,
        passport_number=passport_number,
        passport_expiry=passport_expiry,
        visa_records=visa_records or [],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )


def _make_group(
    travelers: list[SkyMilesProfile],
    segments=None,
    confirmation: str = "GRP-001",
) -> GroupItinerary:
    """Build a GroupItinerary with sensible defaults."""
    if segments is None:
        segments = [make_segment(destination="DE", departure=date(2026, 9, 1))]
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments,
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number,
    )


class TestGroupAllClear:
    """All travelers have valid documents — no alerts raised."""

    def test_no_alerts_for_visa_exempt_travelers(self, base_requirements):
        travelers = [
            _make_traveler(skymiles_number="1001", first_name="Alice", nationality="US"),
            _make_traveler(skymiles_number="1002", first_name="Bob", nationality="GB"),
        ]
        group = _make_group(travelers)

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None
        assert result.confirmation_number == "GRP-001"
        assert len(result.traveler_summaries) == 2
        for summary in result.traveler_summaries:
            assert summary.passport_result.is_alert_required is False
            assert summary.visa_result.is_alert_required is False


class TestGroupWithOneFailingTraveler:
    """One traveler has a document issue — group_severity reflects it."""

    def test_missing_visa_triggers_critical(self, base_requirements):
        good_traveler = _make_traveler(
            skymiles_number="2001", first_name="Alice", nationality="US",
        )
        bad_traveler = _make_traveler(
            skymiles_number="2002", first_name="Raj", nationality="IN",
        )
        group = _make_group([good_traveler, bad_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        alice_summary = result.traveler_summaries[0]
        assert alice_summary.visa_result.is_alert_required is False
        raj_summary = result.traveler_summaries[1]
        assert raj_summary.visa_result.is_alert_required is True
        assert raj_summary.visa_result.severity == AlertSeverity.CRITICAL

    def test_expired_passport_triggers_critical(self, base_requirements):
        good_traveler = _make_traveler(skymiles_number="2010", nationality="US")
        bad_traveler = _make_traveler(
            skymiles_number="2011",
            nationality="US",
            passport_expiry=date(2020, 1, 1),
        )
        group = _make_group([good_traveler, bad_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest across all travelers."""

    def test_critical_overrides_warning(self, base_requirements):
        # Traveler 1: visa expires on departure → WARNING
        warning_traveler = _make_traveler(
            skymiles_number="3001",
            first_name="Wei",
            nationality="IN",
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="TOURIST",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 1),
                    visa_number="V100",
                ),
            ],
        )
        # Traveler 2: no visa on file → CRITICAL
        critical_traveler = _make_traveler(
            skymiles_number="3002",
            first_name="Sato",
            nationality="IN",
        )
        group = _make_group([warning_traveler, critical_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.WARNING
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL

    def test_warning_stays_when_no_critical(self, base_requirements):
        # Traveler with visa expiring on departure → WARNING, no CRITICAL anywhere
        warning_traveler = _make_traveler(
            skymiles_number="3010",
            nationality="IN",
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="TOURIST",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 1),
                    visa_number="V200",
                ),
            ],
        )
        group = _make_group([warning_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_lower_severity_does_not_replace_higher(self, base_requirements):
        # Traveler 1: no visa → CRITICAL
        critical_traveler = _make_traveler(
            skymiles_number="3020",
            first_name="First",
            nationality="IN",
        )
        # Traveler 2: visa expires on departure → WARNING (should not downgrade)
        warning_traveler = _make_traveler(
            skymiles_number="3021",
            first_name="Second",
            nationality="IN",
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="TOURIST",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 1),
                    visa_number="V300",
                ),
            ],
        )
        group = _make_group([critical_traveler, warning_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestGroupWithRequirementsNone:
    """When requirements=None, the function auto-looks up from default data."""

    def test_auto_lookup_for_single_destination(self):
        traveler = _make_traveler(skymiles_number="4001", nationality="IN")
        group = _make_group(
            [traveler],
            segments=[make_segment(destination="DE", departure=date(2026, 9, 1))],
        )

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any("No visa on file" in r for r in result.traveler_summaries[0].visa_result.reasons)

    def test_auto_lookup_for_multiple_destinations(self):
        traveler = _make_traveler(skymiles_number="4002", nationality="US")
        group = _make_group(
            [traveler],
            segments=[
                make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100"),
                make_segment(destination="JP", departure=date(2026, 9, 5), flight_number="DL200"),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None

    def test_auto_lookup_deduplicates_same_destination(self):
        traveler = _make_traveler(skymiles_number="4003", nationality="US")
        group = _make_group(
            [traveler],
            segments=[
                make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100"),
                make_segment(destination="DE", departure=date(2026, 9, 10), flight_number="DL101"),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=None)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1


class TestGroupMultipleTravelers:
    """Per-traveler summaries carry the correct identity fields."""

    def test_summary_identity_fields_match_travelers(self, base_requirements):
        alice = _make_traveler(
            skymiles_number="5001", first_name="Alice", last_name="Smith", nationality="US",
        )
        bob = _make_traveler(
            skymiles_number="5002", first_name="Bob", last_name="Jones", nationality="GB",
        )
        carol = _make_traveler(
            skymiles_number="5003", first_name="Carol", last_name="Lee", nationality="CA",
        )
        group = _make_group([alice, bob, carol])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert len(result.traveler_summaries) == 3
        for traveler, summary in zip([alice, bob, carol], result.traveler_summaries):
            assert summary.skymiles_number == traveler.skymiles_number
            assert summary.first_name == traveler.first_name
            assert summary.last_name == traveler.last_name

    def test_confirmation_number_propagated(self, base_requirements):
        traveler = _make_traveler(skymiles_number="5010", nationality="US")
        group = _make_group([traveler], confirmation="GRP-CUSTOM-42")

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.confirmation_number == "GRP-CUSTOM-42"

    def test_layover_with_transit_visa_required(self, base_requirements):
        # CN requires transit visa; IN national transiting without visa → CRITICAL
        traveler = _make_traveler(skymiles_number="5020", nationality="IN")
        group = _make_group(
            [traveler],
            segments=[
                make_segment(
                    destination="CN",
                    departure=date(2026, 9, 1),
                    is_layover=True,
                ),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any("No visa on file for CN" in r for r in result.traveler_summaries[0].visa_result.reasons)

    def test_layover_without_transit_visa_skipped(self, base_requirements):
        # DE does not require transit visa; IN national transiting → no visa alert
        traveler = _make_traveler(skymiles_number="5021", nationality="IN")
        group = _make_group(
            [traveler],
            segments=[
                make_segment(
                    destination="DE",
                    departure=date(2026, 9, 1),
                    is_layover=True,
                ),
            ],
        )

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
