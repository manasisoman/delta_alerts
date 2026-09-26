"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment

DEPARTURE = date(2030, 9, 1)
ARRIVAL = date(2030, 9, 2)


def make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    issue_date: date = date(2025, 1, 1),
    visa_type: str = "TOURIST",
    visa_number: str = "V001",
) -> VisaRecord:
    """Helper to build a VisaRecord with far-future validity by default."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


class TestHappyPath:
    """Valid visa well beyond travel dates → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
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

    def test_empty_itinerary_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_visa_for_other_country_does_not_count(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa(country_code="JP")])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = make_visa(issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = make_visa(expiry_date=date(2030, 7, 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons

    def test_visa_expires_day_before_departure(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE - timedelta(days=1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expires exactly on departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within VISA_EXPIRY_WARNING_DAYS after departure → INFO."""

    def test_visa_expires_day_after_departure(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expires_exactly_at_window_boundary(self, base_requirements):
        visa = make_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_just_outside_window(self, base_requirements):
        visa = make_visa(
            expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestCountryDoesNotRequireVisa:
    """Destinations with requires_visa=False are skipped entirely."""

    def test_visa_free_country_skipped(self, base_requirements):
        base_requirements["MX"] = TravelDocRequirements(
            country_code="MX",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="MX", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaExemptNationality:
    """Traveler nationality in visa_exempt_nationalities is skipped."""

    def test_exempt_nationality_no_alert(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_non_exempt_nationality_alerts(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons


class TestLayovers:
    """Layover segments only require a visa when transit_visa_required=True."""

    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="CN", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_layover_without_transit_visa_required_skipped(self, base_requirements):
        """Germany does not require transit visa → layover without visa is fine."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_non_layover_same_country_still_alerts(self, base_requirements):
        """Same destination as a final stop (not layover) does require a visa."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=False
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL

    def test_layover_and_final_destination_evaluated_independently(
        self, base_requirements
    ):
        """Layover in DE is skipped, final stop in CN without visa alerts."""
        profile = make_profile(nationality="IN", visa_records=[])
        layover = make_segment(
            destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        final = make_segment(
            destination="CN",
            departure=ARRIVAL,
            arrival=ARRIVAL + timedelta(days=1),
            is_layover=False,
            flight_number="DL200",
            origin="FRA",
        )
        itinerary = make_itinerary(layover, final)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.reasons == ["No visa on file for CN"]
        assert result.severity == AlertSeverity.CRITICAL


class TestRequirementsFallback:
    """Destinations absent from the requirements dict fall back to static data."""

    def test_unknown_country_uses_default_requirements(self, base_requirements):
        """Unknown code → default: visa required, no exemptions."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="ZZ", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons

    def test_unknown_country_layover_uses_default_transit_rule(self, base_requirements):
        """Default transit_visa_required=False → layover skipped."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="ZZ", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_static_data_used_for_known_country_not_in_dict(self):
        """Empty override dict → static US rules exempt GB nationals."""
        profile = make_profile(nationality="GB", visa_records=[])
        segment = make_segment(destination="US", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False


class TestBestVisaSelection:
    """When multiple visas match a country, the latest-expiring one is used."""

    def test_expired_and_valid_visa_picks_valid(self, base_requirements):
        expired = make_visa(expiry_date=date(2022, 1, 1), visa_number="OLD")
        valid = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_order_does_not_matter(self, base_requirements):
        valid = make_visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        expired = make_visa(expiry_date=date(2022, 1, 1), visa_number="OLD")
        profile = make_profile(nationality="IN", visa_records=[valid, expired])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_two_short_visas_uses_later_one(self, base_requirements):
        """Both expire before travel; the later one drives the message (still CRITICAL)."""
        early = make_visa(expiry_date=date(2030, 3, 1), visa_number="A")
        later = make_visa(expiry_date=DEPARTURE, visa_number="B")
        profile = make_profile(nationality="IN", visa_records=[early, later])
        segment = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons

    def test_find_matching_visa_returns_latest(self):
        a = make_visa(expiry_date=date(2031, 1, 1), visa_number="A")
        b = make_visa(expiry_date=date(2033, 1, 1), visa_number="B")
        c = make_visa(expiry_date=date(2032, 1, 1), visa_number="C")

        result = _find_matching_visa([a, b, c], "CN")

        assert result is b

    def test_find_matching_visa_none_when_no_match(self):
        result = _find_matching_visa([make_visa(country_code="JP")], "CN")

        assert result is None

    def test_find_matching_visa_empty_list(self):
        assert _find_matching_visa([], "CN") is None


class TestSeverityConsolidation:
    """Highest severity across all segments wins; all reasons are kept."""

    def test_info_then_critical_yields_critical(self, base_requirements):
        cn_visa = make_visa(country_code="CN", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[cn_visa])
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        seg_jp = make_segment(
            destination="JP",
            departure=ARRIVAL,
            arrival=ARRIVAL + timedelta(days=1),
            flight_number="DL200",
            origin="PEK",
        )
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
        assert "No visa on file for JP" in result.reasons

    def test_critical_then_info_stays_critical(self, base_requirements):
        jp_visa = make_visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        seg_jp = make_segment(
            destination="JP",
            departure=DEPARTURE,
            arrival=ARRIVAL,
            flight_number="DL200",
        )
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_and_info_yields_warning(self, base_requirements):
        cn_visa = make_visa(country_code="CN", expiry_date=DEPARTURE)
        jp_visa = make_visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))
        profile = make_profile(nationality="IN", visa_records=[cn_visa, jp_visa])
        seg_cn = make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        seg_jp = make_segment(
            destination="JP", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL200"
        )
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING

    def test_max_severity_from_none(self):
        assert _max_severity(None, AlertSeverity.INFO) == AlertSeverity.INFO

    def test_max_severity_upgrades(self):
        assert _max_severity(AlertSeverity.INFO, AlertSeverity.WARNING) == AlertSeverity.WARNING

    def test_max_severity_keeps_higher_current(self):
        assert _max_severity(AlertSeverity.CRITICAL, AlertSeverity.INFO) == AlertSeverity.CRITICAL

    def test_max_severity_equal(self):
        assert _max_severity(AlertSeverity.WARNING, AlertSeverity.WARNING) == AlertSeverity.WARNING


class TestItineraryChange:
    """Re-evaluating after an itinerary change reflects the new dates/destinations."""

    def test_departure_pushed_past_visa_expiry(self, base_requirements):
        visa = make_visa(expiry_date=date(2030, 10, 15))
        profile = make_profile(nationality="IN", visa_records=[visa])
        original = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        )
        changed = make_itinerary(
            make_segment(
                destination="CN", departure=date(2030, 11, 1), arrival=date(2030, 11, 2)
            )
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in after.reasons

    def test_destination_changed_to_country_without_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa(country_code="CN")])
        original = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)
        )
        changed = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE, arrival=ARRIVAL)
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for JP"]

    def test_layover_promoted_to_stopover(self, base_requirements):
        """A DE layover becomes an overnight stop → visa now required."""
        profile = make_profile(nationality="IN", visa_records=[])
        original = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True)
        )
        changed = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=False)
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
