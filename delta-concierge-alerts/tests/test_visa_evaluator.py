"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

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
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
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


DEPARTURE = date(2030, 9, 1)


def _visa(country_code="CN", expiry=date(2031, 1, 1), visa_number="V100"):
    return VisaRecord(
        country_code=country_code, visa_type="TOURIST",
        issue_date=date(2025, 1, 1), expiry_date=expiry,
        visa_number=visa_number,
    )


def _no_visa_reqs(country_code="MX"):
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=False,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=[],
    )


class TestHappyPath:
    """Valid visa well beyond the warning window → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa()])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.segments_evaluated == itinerary.segments
        assert result.profile is profile


class TestVisaNotRequired:
    """Destinations that do not require a visa are skipped entirely."""

    def test_country_without_visa_requirement_skipped(self, base_requirements):
        requirements = {**base_requirements, "MX": _no_visa_reqs("MX")}
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="MX", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestLayoverRules:
    """Layovers only need a visa when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_non_layover_same_country_still_alerts(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, is_layover=False)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons

    def test_transit_layover_with_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa("CN")])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_skipped_layover_does_not_mask_final_destination(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="DE", departure=DEPARTURE, is_layover=True),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


class TestVisaExemption:
    """Visa-exempt nationalities never trigger a visa alert."""

    def test_exempt_nationality_skipped(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_exemption_is_per_country(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestVisaRecordMatching:
    """Only visas for the destination count; the latest-expiring match wins."""

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa("JP")])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_latest_expiry_selected_over_expired(self, base_requirements):
        expired = _visa("CN", expiry=date(2020, 1, 1), visa_number="OLD")
        valid = _visa("CN", expiry=date(2031, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_selection_independent_of_record_order(self, base_requirements):
        expired = _visa("CN", expiry=date(2020, 1, 1), visa_number="OLD")
        valid = _visa("CN", expiry=date(2031, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[valid, expired])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_all_matches_expired_reports_expired(self, base_requirements):
        older = _visa("CN", expiry=date(2019, 1, 1), visa_number="A")
        newer = _visa("CN", expiry=date(2020, 1, 1), visa_number="B")
        profile = make_profile(nationality="IN", visa_records=[older, newer])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN has expired"]


class TestVisaExpiryRelativeToTravel:
    """Expiry on / near the departure date maps to WARNING / INFO."""

    def test_expires_one_day_before_departure_critical(self, base_requirements):
        visa = _visa("CN", expiry=DEPARTURE - timedelta(days=1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]

    def test_expires_on_departure_warning(self, base_requirements):
        visa = _visa("CN", expiry=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]

    def test_expires_day_after_departure_info(self, base_requirements):
        visa = _visa("CN", expiry=DEPARTURE + timedelta(days=1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_expires_exactly_at_warning_threshold_info(self, base_requirements):
        visa = _visa("CN", expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_expires_just_past_warning_threshold_no_alert(self, base_requirements):
        visa = _visa("CN", expiry=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_transit_layover_visa_expiring_on_departure_warning(self, base_requirements):
        visa = _visa("CN", expiry=DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING


class TestSeverityConsolidation:
    """Consolidated severity is the highest across all segments."""

    def test_info_then_critical_escalates(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa("CN", expiry=DEPARTURE + timedelta(days=10))],
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_warning_stays_critical(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa("CN", expiry=DEPARTURE)],
        )
        itinerary = make_itinerary(
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "No visa on file for JP",
            "Visa for CN expires on date of travel",
        ]

    def test_warning_then_info_stays_warning(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa("CN", expiry=DEPARTURE),
                _visa("JP", expiry=DEPARTURE + timedelta(days=5)),
            ],
        )
        itinerary = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE, flight_number="DL200"),
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert len(result.reasons) == 2

    def test_empty_itinerary_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestRequirementsFallback:
    """Destinations absent from the passed requirements use country defaults."""

    def test_unknown_country_uses_default_visa_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="ZZ", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons

    def test_unknown_country_layover_uses_default_no_transit_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(
            make_segment(destination="ZZ", departure=DEPARTURE, is_layover=True)
        )

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_override_takes_precedence(self, base_requirements):
        requirements = {**base_requirements, "CN": _no_visa_reqs("CN")}
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments/dates."""

    def test_departure_moved_past_visa_expiry_becomes_critical(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa("CN", expiry=date(2030, 12, 1))]
        )
        original = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="CN", departure=date(2031, 1, 1)))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["Visa for CN expires before date of travel"]

    def test_departure_moved_near_visa_expiry_becomes_info(self, base_requirements):
        profile = make_profile(
            nationality="IN", visa_records=[_visa("CN", expiry=date(2030, 12, 1))]
        )
        changed = make_itinerary(make_segment(destination="CN", departure=date(2030, 11, 20)))

        result = evaluate_visa_requirements(profile, changed, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_rerouted_to_exempt_destination_clears_alert(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        original = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="JP", departure=DEPARTURE))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False

    def test_added_transit_layover_introduces_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa("JP")])
        original = make_itinerary(make_segment(destination="JP", departure=DEPARTURE))
        changed = make_itinerary(
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True),
            make_segment(destination="JP", departure=DEPARTURE, flight_number="DL200"),
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["No visa on file for CN"]
