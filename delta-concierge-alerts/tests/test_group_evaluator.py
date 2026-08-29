"""Tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, SkyMilesProfile, VisaRecord

from tests.conftest import make_group, make_profile, make_segment


def _clean_traveler(**overrides) -> SkyMilesProfile:
    """US national with a long-valid passport — no passport or visa issues for DE."""
    defaults = dict(
        nationality="US",
        passport_expiry=date(2035, 1, 1),
        skymiles_number="1000001",
        first_name="Clean",
        last_name="Traveler",
    )
    defaults.update(overrides)
    return make_profile(**defaults)


class TestNoIssues:
    """A group of fully compliant travelers produces no group severity."""

    def test_clean_group_has_no_severity(self, base_requirements):
        group = make_group(
            travelers=[_clean_traveler()],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 1
        summary = result.traveler_summaries[0]
        assert summary.passport_result.is_alert_required is False
        assert summary.visa_result.is_alert_required is False

    def test_empty_traveler_list_returns_empty_summaries(self, base_requirements):
        group = make_group(
            travelers=[],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None
        assert result.confirmation_number == "DL-GROUP"


class TestPerTravelerResults:
    """Each traveler is evaluated independently against the shared segments."""

    def test_summary_carries_traveler_identity(self, base_requirements):
        group = make_group(
            travelers=[
                _clean_traveler(skymiles_number="2000001", first_name="Ada", last_name="Lovelace"),
                _clean_traveler(skymiles_number="2000002", first_name="Alan", last_name="Turing"),
            ],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == ["2000001", "2000002"]
        assert [(s.first_name, s.last_name) for s in result.traveler_summaries] == [
            ("Ada", "Lovelace"),
            ("Alan", "Turing"),
        ]

    def test_one_traveler_critical_others_clean(self, base_requirements):
        group = make_group(
            travelers=[
                _clean_traveler(skymiles_number="3000001"),
                _clean_traveler(
                    skymiles_number="3000002",
                    passport_number=None,
                    passport_expiry=None,
                ),
            ],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL

    def test_shared_segments_evaluated_for_every_traveler(self, base_requirements):
        segments = [
            make_segment(destination="DE", departure=date(2030, 6, 1), flight_number="DL100"),
            make_segment(destination="JP", departure=date(2030, 6, 10), flight_number="DL200"),
        ]
        group = make_group(
            travelers=[_clean_traveler(skymiles_number="4000001"), _clean_traveler(skymiles_number="4000002")],
            segments=segments,
        )

        result = evaluate_group_itinerary(group, base_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestConsolidatedSeverity:
    """Group severity is the highest severity across all travelers and segments."""

    def test_info_only_group(self, base_requirements):
        # Passport expires 4 months after departure to DE: meets the 3-month
        # minimum but falls inside the 6-month advisory window.
        group = make_group(
            travelers=[_clean_traveler(passport_expiry=date(2030, 10, 1))],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO

    def test_warning_beats_info_across_travelers(self, base_requirements):
        visa_on_departure = VisaRecord(
            country_code="DE",
            visa_type="SCHENGEN",
            issue_date=date(2029, 1, 1),
            expiry_date=date(2030, 6, 1),
            visa_number="V-1",
        )
        group = make_group(
            travelers=[
                _clean_traveler(skymiles_number="5000001", passport_expiry=date(2030, 10, 1)),
                make_profile(
                    skymiles_number="5000002",
                    nationality="IN",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[visa_on_departure],
                ),
            ],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_beats_warning_across_travelers(self, base_requirements):
        visa_on_departure = VisaRecord(
            country_code="DE",
            visa_type="SCHENGEN",
            issue_date=date(2029, 1, 1),
            expiry_date=date(2030, 6, 1),
            visa_number="V-2",
        )
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="6000001",
                    nationality="IN",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[visa_on_departure],
                ),
                make_profile(
                    skymiles_number="6000002",
                    nationality="IN",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[],
                ),
            ],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.WARNING
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_critical_from_second_segment(self, base_requirements):
        # DE is clean for a US national; CN requires a visa with no exemptions.
        group = make_group(
            travelers=[_clean_traveler()],
            segments=[
                make_segment(destination="DE", departure=date(2030, 6, 1), flight_number="DL100"),
                make_segment(destination="CN", departure=date(2030, 6, 10), flight_number="DL200"),
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for CN" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )


class TestLayoverSegments:
    """Layovers only raise visa issues when a transit visa is required."""

    def test_layover_without_transit_visa_is_skipped(self, base_requirements):
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="7000001",
                    nationality="IN",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[],
                )
            ],
            segments=[
                make_segment(
                    destination="DE",
                    departure=date(2030, 6, 1),
                    is_layover=True,
                    flight_number="DL100",
                )
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.reasons == []

    def test_layover_requiring_transit_visa_is_critical(self, base_requirements):
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="7000002",
                    nationality="US",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[],
                )
            ],
            segments=[
                make_segment(
                    destination="CN",
                    departure=date(2030, 6, 1),
                    is_layover=True,
                    flight_number="DL300",
                )
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for CN" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )

    def test_layover_passport_validity_still_enforced(self, base_requirements):
        # Layover in CN (6-month requirement); passport expires 2 months after travel.
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="7000003",
                    nationality="US",
                    passport_expiry=date(2030, 8, 1),
                    visa_records=[
                        VisaRecord(
                            country_code="CN",
                            visa_type="TRANSIT",
                            issue_date=date(2029, 1, 1),
                            expiry_date=date(2035, 1, 1),
                            visa_number="V-3",
                        )
                    ],
                )
            ],
            segments=[
                make_segment(
                    destination="CN",
                    departure=date(2030, 6, 1),
                    is_layover=True,
                    flight_number="DL400",
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
    """When no requirements are supplied, defaults are looked up per destination."""

    def test_requirements_resolved_from_country_data(self):
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="8000001",
                    nationality="IN",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[],
                )
            ],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for DE" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )

    def test_unknown_destination_falls_back_to_default(self):
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="8000002",
                    nationality="US",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[],
                )
            ],
            segments=[make_segment(destination="ZZ", departure=date(2030, 6, 1))],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "No visa on file for ZZ" in r
            for r in result.traveler_summaries[0].visa_result.reasons
        )

    def test_repeated_destination_is_looked_up_once(self):
        group = make_group(
            travelers=[
                make_profile(
                    skymiles_number="8000003",
                    nationality="US",
                    passport_expiry=date(2035, 1, 1),
                    visa_records=[],
                )
            ],
            segments=[
                make_segment(destination="DE", departure=date(2030, 6, 1), flight_number="DL100"),
                make_segment(destination="DE", departure=date(2030, 6, 20), flight_number="DL101"),
            ],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.reasons == []


class TestItineraryChanged:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_added_segment_raises_group_severity(self, base_requirements):
        traveler = _clean_traveler(skymiles_number="9000001")
        original = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
        )

        before = evaluate_group_itinerary(original, base_requirements)

        changed = make_group(
            travelers=[traveler],
            segments=[
                make_segment(destination="DE", departure=date(2030, 6, 1), flight_number="DL100"),
                make_segment(destination="CN", departure=date(2030, 6, 10), flight_number="DL200"),
            ],
        )
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_confirmation_number_propagated(self, base_requirements):
        group = make_group(
            travelers=[_clean_traveler()],
            segments=[make_segment(destination="DE", departure=date(2030, 6, 1))],
            confirmation="DL-CHANGED",
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-CHANGED"
