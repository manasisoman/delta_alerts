"""Tests for the group itinerary evaluator."""

from datetime import date
from unittest.mock import patch

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)

from tests.conftest import make_segment


def _make_group_profile(
    skymiles_number: str = "9999999",
    first_name: str = "Test",
    last_name: str = "User",
    nationality: str = "US",
    passport_number: str | None = "P123456",
    passport_expiry: date | None = date(2030, 1, 1),
    visa_records: list[VisaRecord] | None = None,
) -> SkyMilesProfile:
    """Build a SkyMilesProfile with a configurable skymiles_number."""
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
    primary_traveler: str | None = None,
) -> GroupItinerary:
    """Build a GroupItinerary with sensible defaults."""
    if segments is None:
        segments = [make_segment(destination="DE", departure=date(2026, 9, 1))]
    return GroupItinerary(
        confirmation_number=confirmation,
        segments=segments,
        travelers=travelers,
        primary_traveler=primary_traveler or travelers[0].skymiles_number,
    )


class TestAllTravelersClean:
    """All travelers have valid documents — group severity should be None."""

    def test_single_clean_traveler(self, base_requirements):
        traveler = _make_group_profile(
            skymiles_number="1111111",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        group = _make_group([traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.confirmation_number == "GRP-001"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "1111111"
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_multiple_clean_travelers(self, base_requirements):
        traveler_a = _make_group_profile(
            skymiles_number="1111111",
            first_name="Alice",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        traveler_b = _make_group_profile(
            skymiles_number="2222222",
            first_name="Bob",
            nationality="GB",
            passport_expiry=date(2031, 6, 1),
        )
        group = _make_group([traveler_a, traveler_b])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2
        for summary in result.traveler_summaries:
            assert summary.passport_result.is_alert_required is False
            assert summary.visa_result.is_alert_required is False


class TestExpiredPassportCritical:
    """One traveler with an expired passport → group_severity is CRITICAL."""

    def test_expired_passport_sets_critical(self, base_requirements):
        traveler = _make_group_profile(
            skymiles_number="3333333",
            nationality="US",
            passport_expiry=date(2020, 1, 1),  # already expired
        )
        group = _make_group([traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL

    def test_missing_passport_number_sets_critical(self, base_requirements):
        traveler = _make_group_profile(
            skymiles_number="3333333",
            nationality="US",
            passport_number=None,
        )
        group = _make_group([traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestMixedGroupSeverity:
    """Mixed group: one clean traveler, one with a visa issue → group severity matches worst."""

    def test_one_clean_one_visa_missing(self, base_requirements):
        clean_traveler = _make_group_profile(
            skymiles_number="1111111",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        # IN nationality needs visa for DE, no visa on file → CRITICAL
        visa_issue_traveler = _make_group_profile(
            skymiles_number="2222222",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[],
        )
        group = _make_group([clean_traveler, visa_issue_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        # Clean traveler should have no alerts
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        # Visa-issue traveler should have visa alert
        assert result.traveler_summaries[1].visa_result.is_alert_required is True
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL

    def test_one_clean_one_visa_warning(self, base_requirements):
        """Visa expires on departure date → WARNING; group_severity = WARNING."""
        clean_traveler = _make_group_profile(
            skymiles_number="1111111",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        # IN nationality with visa that expires exactly on departure → WARNING
        warning_traveler = _make_group_profile(
            skymiles_number="2222222",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="Tourist",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 1),  # same as departure
                    visa_number="V111",
                ),
            ],
        )
        group = _make_group([clean_traveler, warning_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.WARNING


class TestRequirementsAutoBuilt:
    """requirements=None path: function auto-builds requirements from get_requirements()."""

    def test_auto_builds_requirements_from_destinations(self):
        traveler = _make_group_profile(
            skymiles_number="4444444",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        segment_de = make_segment(destination="DE", departure=date(2026, 9, 1))
        group = _make_group([traveler], segments=[segment_de])

        # requirements=None triggers auto-lookup via get_requirements
        result = evaluate_group_itinerary(group, requirements=None)

        assert result.confirmation_number == "GRP-001"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1

    def test_auto_builds_deduplicates_destinations(self):
        """Two segments to same destination should only call get_requirements once."""
        traveler = _make_group_profile(
            skymiles_number="4444444",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        seg1 = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg2 = make_segment(destination="DE", departure=date(2026, 9, 10), flight_number="DL200")
        group = _make_group([traveler], segments=[seg1, seg2])

        with patch("src.evaluators.group_evaluator.get_requirements") as mock_get:
            mock_get.return_value = TravelDocRequirements(
                country_code="DE",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=["US"],
            )

            result = evaluate_group_itinerary(group, requirements=None)

            # get_requirements called once for DE, not twice
            mock_get.assert_called_once_with("DE")
        assert result.group_severity is None

    def test_auto_builds_multiple_destinations(self):
        """Multiple unique destinations each get their own requirements lookup."""
        traveler = _make_group_profile(
            skymiles_number="4444444",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_jp = make_segment(destination="JP", departure=date(2026, 9, 5), flight_number="DL200")
        group = _make_group([traveler], segments=[seg_de, seg_jp])

        with patch("src.evaluators.group_evaluator.get_requirements") as mock_get:
            mock_get.side_effect = lambda dest: TravelDocRequirements(
                country_code=dest,
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=["US"],
            )

            evaluate_group_itinerary(group, requirements=None)

            assert mock_get.call_count == 2


class TestRequirementsProvidedExplicitly:
    """When requirements are provided, the function uses them instead of auto-building."""

    def test_uses_explicit_requirements(self, base_requirements):
        # IN nationality needs visa for DE per base_requirements
        traveler = _make_group_profile(
            skymiles_number="5555555",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[],
        )
        group = _make_group([traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        # Should use the provided base_requirements (DE requires visa, IN not exempt)
        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.CRITICAL

    def test_explicit_requirements_not_call_get_requirements(self, base_requirements):
        """Passing requirements explicitly should NOT call get_requirements."""
        traveler = _make_group_profile(
            skymiles_number="5555555",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        group = _make_group([traveler])

        with patch("src.evaluators.group_evaluator.get_requirements") as mock_get:
            evaluate_group_itinerary(group, requirements=base_requirements)

            mock_get.assert_not_called()


class TestMultipleTravelersSeverityPrecedence:
    """Multiple travelers with different severity issues → highest wins."""

    def test_critical_beats_warning(self, base_requirements):
        """One traveler WARNING, one CRITICAL → group_severity = CRITICAL."""
        # IN traveler with visa expiring on departure → WARNING
        warning_traveler = _make_group_profile(
            skymiles_number="6666666",
            first_name="Warn",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="Tourist",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 1),  # same as departure → WARNING
                    visa_number="V222",
                ),
            ],
        )
        # IN traveler with expired passport → CRITICAL
        critical_traveler = _make_group_profile(
            skymiles_number="7777777",
            first_name="Crit",
            nationality="IN",
            passport_expiry=date(2020, 1, 1),
        )
        group = _make_group([warning_traveler, critical_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_warning_beats_info(self, base_requirements):
        """One traveler INFO, one WARNING → group_severity = WARNING."""
        # IN traveler with visa expiring within 30 days of travel → INFO
        info_traveler = _make_group_profile(
            skymiles_number="6666666",
            first_name="Info",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="Tourist",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 15),  # within 30 days of departure → INFO
                    visa_number="V333",
                ),
            ],
        )
        # IN traveler with visa expiring on departure → WARNING
        warning_traveler = _make_group_profile(
            skymiles_number="7777777",
            first_name="Warn",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="Tourist",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 1),  # same as departure → WARNING
                    visa_number="V444",
                ),
            ],
        )
        group = _make_group([info_traveler, warning_traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_info_only_when_all_info(self, base_requirements):
        """Both travelers have INFO-level issues → group_severity = INFO."""
        info_traveler_a = _make_group_profile(
            skymiles_number="6666666",
            first_name="InfoA",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="Tourist",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 20),  # within 30 days → INFO
                    visa_number="V555",
                ),
            ],
        )
        info_traveler_b = _make_group_profile(
            skymiles_number="7777777",
            first_name="InfoB",
            nationality="IN",
            passport_expiry=date(2030, 1, 1),
            visa_records=[
                VisaRecord(
                    country_code="DE",
                    visa_type="Tourist",
                    issue_date=date(2025, 1, 1),
                    expiry_date=date(2026, 9, 25),  # within 30 days → INFO
                    visa_number="V666",
                ),
            ],
        )
        group = _make_group([info_traveler_a, info_traveler_b])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        assert result.group_severity == AlertSeverity.INFO


class TestTravelerSummaryFields:
    """Verify TravelerAlertSummary fields are correctly populated."""

    def test_summary_contains_correct_traveler_info(self, base_requirements):
        traveler = _make_group_profile(
            skymiles_number="8888888",
            first_name="Jane",
            last_name="Doe",
            nationality="US",
            passport_expiry=date(2030, 1, 1),
        )
        group = _make_group([traveler])

        result = evaluate_group_itinerary(group, requirements=base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "8888888"
        assert summary.first_name == "Jane"
        assert summary.last_name == "Doe"
        assert summary.passport_result.profile == traveler
        assert summary.visa_result.profile == traveler
