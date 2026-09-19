"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V001",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


DEPARTURE = date(2030, 6, 1)
ARRIVAL = date(2030, 6, 2)


def make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    issue_date: date = date(2025, 1, 1),
    visa_number: str = "V100",
    visa_type: str = "TOURIST",
) -> VisaRecord:
    """Helper to build a VisaRecord with a far-future expiry by default."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


class TestHappyPath:
    """Valid visa well beyond the travel date → no alert."""

    def test_valid_visa_produces_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa()])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == [segment]
        assert result.profile is profile

    def test_empty_itinerary_produces_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestSkipRules:
    """Segments that do not require a visa check are skipped entirely."""

    def test_country_not_requiring_visa_is_skipped(self, base_requirements):
        base_requirements["CN"].requires_visa = False
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_non_layover_to_transit_exempt_country_still_evaluated(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=False
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert "No visa on file for DE" in result.reasons

    def test_visa_exempt_nationality_is_skipped(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []


class TestRequirementsFallback:
    """Destinations absent from the requirements dict fall back to defaults."""

    def test_unknown_destination_uses_default_requirements(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="ZZ", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons

    def test_unknown_destination_layover_uses_default_transit_rule(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="ZZ", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_empty_requirements_dict_falls_back_to_static_data(self):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="FR", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestVisaExpiryWindows:
    """Visa expiry relative to departure drives severity."""

    def test_visa_expires_before_departure_is_critical(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE - timedelta(days=1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]

    def test_visa_expires_on_departure_is_warning(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]

    def test_visa_expires_within_warning_window_is_info(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_visa_expires_exactly_at_warning_threshold_is_info(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_just_beyond_warning_threshold_is_clean(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaRecordSelection:
    """When several visas match a country, the latest-expiring one wins."""

    def test_latest_expiry_selected_over_expired_record(self, base_requirements):
        expired = make_visa(expiry_date=date(2020, 1, 1), visa_number="OLD")
        valid = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        other = make_visa(country_code="JP", expiry_date=date(2035, 1, 1))
        profile = make_profile(nationality="IN", visa_records=[other])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


class TestMultiSegmentConsolidation:
    """Severity is the highest across segments; reasons accumulate per segment."""

    def test_highest_severity_wins_across_segments(self, base_requirements):
        info_visa = make_visa(
            country_code="CN", expiry_date=DEPARTURE + timedelta(days=5), visa_number="V1"
        )
        warning_visa = make_visa(country_code="JP", expiry_date=DEPARTURE, visa_number="V2")
        profile = make_profile(nationality="IN", visa_records=[info_visa, warning_visa])
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        seg_jp = make_segment(destination="JP", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "Visa for JP expires on date of travel",
        ]
        assert result.segments_evaluated == [seg_cn, seg_jp]

    def test_critical_not_downgraded_by_later_segment(self, base_requirements):
        jp_visa = make_visa(country_code="JP", expiry_date=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        seg_jp = make_segment(destination="JP", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_skipped_and_clean_segments_do_not_add_reasons(self, base_requirements):
        cn_visa = make_visa(country_code="CN")
        profile = make_profile(nationality="IN", visa_records=[cn_visa])
        seg_layover = make_segment(
            destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(seg_layover, seg_cn)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.segments_evaluated == [seg_layover, seg_cn]


class TestItineraryChange:
    """Re-evaluating after the itinerary changes reflects the new dates."""

    def test_moving_departure_past_visa_expiry_escalates(self, base_requirements):
        visa = make_visa(expiry_date=date(2030, 12, 31))
        profile = make_profile(nationality="IN", visa_records=[visa])
        original = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        )
        rebooked = make_itinerary(
            make_segment(
                destination="CN", departure=date(2031, 1, 15), arrival=date(2031, 1, 16)
            )
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, rebooked, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["Visa for CN expires before date of travel"]

    def test_layover_promoted_to_stopover_requires_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        original = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True)
        )
        rebooked = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=False)
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, rebooked, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL


class TestFindMatchingVisa:
    """_find_matching_visa returns None or the latest-expiring match."""

    def test_returns_none_when_no_match(self):
        records = [make_visa(country_code="JP")]

        result = _find_matching_visa(records, "CN")

        assert result is None

    def test_returns_latest_expiry_among_matches(self):
        older = make_visa(expiry_date=date(2031, 1, 1), visa_number="A")
        newer = make_visa(expiry_date=date(2033, 1, 1), visa_number="B")
        records = [older, newer, make_visa(country_code="JP", visa_number="C")]

        result = _find_matching_visa(records, "CN")

        assert result is newer


class TestMaxSeverity:
    """_max_severity keeps the higher of the two severities."""

    def test_none_current_returns_new(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_higher_new_replaces_current(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.CRITICAL) == AlertSeverity.CRITICAL

    def test_lower_new_keeps_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.WARNING) == AlertSeverity.CRITICAL

    def test_equal_severity_keeps_current(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING
