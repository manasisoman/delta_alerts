"""Tests for the group itinerary evaluator."""

from datetime import date

from src.data.country_requirements import get_requirements
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_group, make_profile, make_segment


DEPARTURE = date(2027, 6, 1)


def _visa(country: str, expiry: date, number: str = "V001") -> VisaRecord:
    return VisaRecord(
        country_code=country, visa_type="TOURIST",
        issue_date=date(2025, 1, 1), expiry_date=expiry, visa_number=number,
    )


def _clean_traveler(skymiles: str = "1000001", **overrides):
    """A US traveler with a far-future passport: visa-exempt for DE, clean everywhere."""
    defaults = dict(
        nationality="US", passport_expiry=date(2035, 1, 1),
        skymiles_number=skymiles, first_name="Clean", last_name="Traveler",
    )
    defaults.update(overrides)
    return make_profile(**defaults)


class TestHappyPath:
    """All travelers clean → per-traveler results clean and group severity None."""

    def test_two_clean_travelers(self, base_requirements):
        group = make_group(
            _clean_traveler("1000001", first_name="Ann", last_name="Lee"),
            _clean_traveler("1000002", first_name="Bob", last_name="Ray"),
            segments=[make_segment(destination="DE", departure=DEPARTURE)],
            confirmation="DL-ABC123",
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-ABC123"
        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2
        assert [s.skymiles_number for s in result.traveler_summaries] == ["1000001", "1000002"]
        assert result.traveler_summaries[0].first_name == "Ann"
        assert result.traveler_summaries[0].last_name == "Lee"
        for summary in result.traveler_summaries:
            assert summary.passport_result.is_alert_required is False
            assert summary.passport_result.severity is None
            assert summary.visa_result.is_alert_required is False
            assert summary.visa_result.severity is None

    def test_traveler_order_is_preserved(self, base_requirements):
        group = make_group(
            _clean_traveler("3"), _clean_traveler("1"), _clean_traveler("2"),
            segments=[make_segment(destination="DE", departure=DEPARTURE)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert [s.skymiles_number for s in result.traveler_summaries] == ["3", "1", "2"]


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self, base_requirements):
        group = make_group(segments=[make_segment(destination="DE", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None

    def test_no_segments(self, base_requirements):
        group = make_group(_clean_traveler(), segments=[])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert result.traveler_summaries[0].passport_result.segments_evaluated == []
        assert result.traveler_summaries[0].visa_result.segments_evaluated == []


class TestSharedItinerary:
    """The group's segments are passed through to every traveler's evaluation."""

    def test_segments_shared_across_travelers(self, base_requirements):
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, flight_number="DL100"),
            make_segment(destination="JP", departure=date(2027, 6, 5), flight_number="DL200"),
        ]
        group = make_group(_clean_traveler("1"), _clean_traveler("2"), segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        for summary in result.traveler_summaries:
            assert summary.passport_result.segments_evaluated == segments
            assert summary.visa_result.segments_evaluated == segments


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers and result types."""

    def test_one_traveler_critical_passport_raises_group_to_critical(self, base_requirements):
        # Missing passport number → CRITICAL for one traveler only
        bad = _clean_traveler("2", passport_number=None)
        group = make_group(
            _clean_traveler("1"), bad,
            segments=[make_segment(destination="DE", departure=DEPARTURE)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.severity is None
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL
        assert "Passport information is incomplete" in result.traveler_summaries[1].passport_result.reasons

    def test_expired_passport_is_critical(self, base_requirements):
        expired = _clean_traveler("2", passport_expiry=date(2020, 1, 1))
        group = make_group(
            _clean_traveler("1"), expired,
            segments=[make_segment(destination="DE", departure=DEPARTURE)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert "Passport has expired" in result.traveler_summaries[1].passport_result.reasons

    def test_info_only_group(self, base_requirements):
        # Passport expires 4 months after departure: above DE's 3-month minimum,
        # but within the 6-month advisory window → INFO
        advisory = _clean_traveler("1", passport_expiry=date(2027, 10, 1))
        group = make_group(
            advisory, _clean_traveler("2"),
            segments=[make_segment(destination="DE", departure=DEPARTURE)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[1].passport_result.severity is None

    def test_warning_from_visa_beats_info_from_passport(self, base_requirements):
        cn_departure = date(2027, 3, 15)
        # Traveler 1 (US): passport 2027-10-01 clears CN's 6-month minimum in March
        # and DE's 3-month minimum in June, but is within DE's 6-month advisory → INFO
        info = _clean_traveler(
            "1", passport_expiry=date(2027, 10, 1), visa_records=[_visa("CN", date(2030, 1, 1))],
        )
        # Traveler 2 (IN): CN visa expiring on CN departure day → WARNING
        warn = _clean_traveler(
            "2", nationality="IN",
            visa_records=[_visa("CN", cn_departure, "V-CN"), _visa("DE", date(2030, 1, 1), "V-DE")],
        )
        group = make_group(
            info, warn,
            segments=[
                make_segment(destination="CN", departure=cn_departure, flight_number="DL100"),
                make_segment(destination="DE", departure=DEPARTURE, flight_number="DL200"),
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].visa_result.severity is None
        assert result.traveler_summaries[1].passport_result.severity is None
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_beats_warning(self, base_requirements):
        warn = _clean_traveler(
            "1", nationality="IN", visa_records=[_visa("CN", DEPARTURE)],
        )
        # No CN visa on file → CRITICAL
        crit = _clean_traveler("2", nationality="IN", visa_records=[])
        group = make_group(
            warn, crit,
            segments=[make_segment(destination="CN", departure=DEPARTURE)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.WARNING
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_critical_first_is_not_lowered_by_later_travelers(self, base_requirements):
        crit = _clean_traveler("1", nationality="IN", visa_records=[])
        info = _clean_traveler("2", passport_expiry=date(2027, 10, 1))
        group = make_group(
            crit, info, _clean_traveler("3"),
            segments=[make_segment(destination="CN", departure=DEPARTURE)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_and_visa_both_alert_for_same_traveler(self, base_requirements):
        # Passport under CN's 6-month minimum (CRITICAL) and no CN visa (CRITICAL)
        traveler = _clean_traveler(
            "1", nationality="IN", passport_expiry=date(2027, 9, 1), visa_records=[],
        )
        group = make_group(traveler, segments=[make_segment(destination="CN", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL
        assert any("6-month" in r for r in summary.passport_result.reasons)
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in summary.visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL


class TestLayovers:
    """Layover segments follow the destination's transit-visa rules for every traveler."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        # DE: transit_visa_required=False → IN national with no DE visa is fine
        traveler = _clean_traveler("1", nationality="IN", visa_records=[_visa("JP", date(2030, 1, 1))])
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, is_layover=True, flight_number="DL100"),
            make_segment(destination="JP", departure=date(2027, 6, 2), flight_number="DL200"),
        ]
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_with_transit_visa_requirement_alerts(self, base_requirements):
        # CN: transit_visa_required=True → IN national needs a CN visa even on layover
        traveler = _clean_traveler("1", nationality="IN", visa_records=[_visa("JP", date(2030, 1, 1))])
        segments = [
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True, flight_number="DL100"),
            make_segment(destination="JP", departure=date(2027, 6, 2), flight_number="DL200"),
        ]
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        assert "No visa on file for CN" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_passport_minimum_still_enforced(self, base_requirements):
        # Passport valid 4 months past departure: fine for DE (3) but below CN layover's 6
        traveler = _clean_traveler("1", passport_expiry=date(2027, 10, 1))
        segments = [
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True, flight_number="DL100"),
            make_segment(destination="DE", departure=date(2027, 6, 2), flight_number="DL200"),
        ]
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("CN's 6-month" in r and "DL100" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL


class TestVisaValidityLimits:
    """Visa expiry relative to travel dates is evaluated per traveler within the group."""

    def test_visa_expired_before_today(self, base_requirements):
        traveler = _clean_traveler("1", nationality="IN", visa_records=[_visa("CN", date(2020, 1, 1))])
        group = make_group(traveler, segments=[make_segment(destination="CN", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        assert "Visa for CN has expired" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expires_before_departure(self, base_requirements):
        traveler = _clean_traveler("1", nationality="IN", visa_records=[_visa("CN", date(2027, 5, 1))])
        group = make_group(traveler, segments=[make_segment(destination="CN", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        assert "Visa for CN expires before date of travel" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expires_within_30_days_is_info(self, base_requirements):
        traveler = _clean_traveler("1", nationality="IN", visa_records=[_visa("CN", date(2027, 6, 20))])
        group = make_group(traveler, segments=[make_segment(destination="CN", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.INFO

    def test_latest_visa_record_wins(self, base_requirements):
        traveler = _clean_traveler(
            "1", nationality="IN",
            visa_records=[_visa("CN", date(2020, 1, 1), "OLD"), _visa("CN", date(2030, 1, 1), "NEW")],
        )
        group = make_group(traveler, segments=[make_segment(destination="CN", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_mixed_nationalities_same_destination(self, base_requirements):
        # US is exempt for JP; IN is not and has no visa
        us = _clean_traveler("1", nationality="US")
        india = _clean_traveler("2", nationality="IN", visa_records=[])
        group = make_group(us, india, segments=[make_segment(destination="JP", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert "No visa on file for JP" in result.traveler_summaries[1].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL


class TestItineraryChange:
    """Re-evaluating the same travelers after the itinerary changes updates the outcome."""

    def test_new_destination_introduces_visa_alert(self, base_requirements):
        traveler = _clean_traveler("1", nationality="IN", visa_records=[_visa("JP", date(2030, 1, 1))])
        original = make_group(traveler, segments=[make_segment(destination="JP", departure=DEPARTURE)])
        changed = make_group(
            traveler,
            segments=[
                make_segment(destination="JP", departure=DEPARTURE, flight_number="DL100"),
                make_segment(destination="CN", departure=date(2027, 6, 10), flight_number="DL200"),
            ],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in after.traveler_summaries[0].visa_result.reasons

    def test_later_departure_pushes_passport_below_minimum(self, base_requirements):
        # Passport expires 2028-01-01: fine for June 2027 travel to CN (6-month min),
        # but a rebooking to September 2027 leaves under 6 months.
        traveler = _clean_traveler(
            "1", passport_expiry=date(2028, 1, 1), visa_records=[_visa("CN", date(2030, 1, 1))],
        )
        original = make_group(traveler, segments=[make_segment(destination="CN", departure=DEPARTURE)])
        changed = make_group(traveler, segments=[make_segment(destination="CN", departure=date(2027, 9, 1))])

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(changed, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert any("CN's 6-month" in r for r in after.traveler_summaries[0].passport_result.reasons)

    def test_removing_traveler_clears_group_alert(self, base_requirements):
        clean = _clean_traveler("1")
        bad = _clean_traveler("2", passport_number=None)
        segments = [make_segment(destination="DE", departure=DEPARTURE)]

        before = evaluate_group_itinerary(make_group(clean, bad, segments=segments), base_requirements)
        after = evaluate_group_itinerary(make_group(clean, segments=segments), base_requirements)

        assert before.group_severity == AlertSeverity.CRITICAL
        assert after.group_severity is None
        assert len(after.traveler_summaries) == 1


class TestDefaultRequirementsLookup:
    """When no requirements dict is given, defaults are built from country data."""

    def test_defaults_used_when_requirements_none(self):
        # US national to DE: visa-exempt in default data; passport far-future → clean
        traveler = _clean_traveler("1")
        group = make_group(traveler, segments=[make_segment(destination="DE", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.is_alert_required is False

    def test_defaults_enforce_country_passport_minimum(self):
        # Default DE rule is a 3-month minimum; passport expiring 2 months after departure
        traveler = _clean_traveler("1", passport_expiry=date(2027, 8, 1))
        group = make_group(traveler, segments=[make_segment(destination="DE", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group)

        months = get_requirements("DE").passport_validity_months
        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(f"DE's {months}-month" in r for r in result.traveler_summaries[0].passport_result.reasons)

    def test_defaults_deduplicate_repeated_destinations(self):
        # Same destination twice (outbound + return leg) resolves once and evaluates both
        traveler = _clean_traveler("1", nationality="IN", visa_records=[])
        segments = [
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL100"),
            make_segment(destination="CN", departure=date(2027, 6, 15), flight_number="DL200"),
        ]
        group = make_group(traveler, segments=segments)

        result = evaluate_group_itinerary(group)

        visa = result.traveler_summaries[0].visa_result
        assert visa.reasons.count("No visa on file for CN") == 2
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_unknown_country_falls_back_to_default_rules(self):
        # Unknown code → default: visa required, no exemptions, 6-month passport minimum
        traveler = _clean_traveler("1", visa_records=[])
        group = make_group(traveler, segments=[make_segment(destination="ZZ", departure=DEPARTURE)])

        result = evaluate_group_itinerary(group)

        assert "No visa on file for ZZ" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_explicit_requirements_override_defaults(self):
        # Override makes DE require no visa at all for an IN national with no visas
        traveler = _clean_traveler("1", nationality="IN", visa_records=[])
        override = {
            "DE": TravelDocRequirements(
                country_code="DE", requires_visa=False, transit_visa_required=False,
                passport_validity_months=3, visa_exempt_nationalities=[],
            )
        }
        group = make_group(traveler, segments=[make_segment(destination="DE", departure=DEPARTURE)])

        with_defaults = evaluate_group_itinerary(group)
        with_override = evaluate_group_itinerary(group, override)

        assert with_defaults.group_severity == AlertSeverity.CRITICAL
        assert with_override.group_severity is None
