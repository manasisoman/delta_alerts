"""Critical-path tests for the visa evaluator."""

from datetime import date

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


# ---------------------------------------------------------------------------
# Helpers for extended coverage. Far-future dates keep these tests stable.
# ---------------------------------------------------------------------------

FUTURE_DEPARTURE = date(2030, 6, 1)


def _visa(
    country_code: str = "CN",
    expiry_date: date = date(2035, 1, 1),
    issue_date: date = date(2025, 1, 1),
    visa_type: str = "TOURIST",
    visa_number: str = "V100",
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code, visa_type=visa_type,
        issue_date=issue_date, expiry_date=expiry_date,
        visa_number=visa_number,
    )


def _requirements(
    country_code: str = "XX",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    passport_validity_months: int = 6,
    visa_exempt_nationalities: list[str] | None = None,
) -> dict[str, TravelDocRequirements]:
    return {
        country_code: TravelDocRequirements(
            country_code=country_code,
            requires_visa=requires_visa,
            transit_visa_required=transit_visa_required,
            passport_validity_months=passport_validity_months,
            visa_exempt_nationalities=visa_exempt_nationalities or [],
        )
    }


class TestValidVisaHappyPath:
    """Valid visa well beyond the warning window → no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa()])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []
        assert result.profile is profile
        assert result.segments_evaluated == itinerary.segments

    def test_empty_itinerary_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN")
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestVisaNotRequired:
    """Destination with requires_visa=False is skipped entirely."""

    def test_country_not_requiring_visa_skipped(self):
        requirements = _requirements(country_code="XX", requires_visa=False)
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="XX", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestLayoverTransitRules:
    """Layovers only require a visa when the country mandates a transit visa."""

    def test_layover_without_transit_visa_requirement_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=FUTURE_DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_non_layover_same_country_still_alerts(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=FUTURE_DEPARTURE, is_layover=False)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons

    def test_transit_visa_country_layover_with_valid_visa_no_alert(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa(country_code="CN")])
        segment = make_segment(destination="CN", departure=FUTURE_DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_layover_skipped_but_final_destination_evaluated(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        layover = make_segment(destination="DE", departure=FUTURE_DEPARTURE, is_layover=True)
        final = make_segment(destination="JP", departure=FUTURE_DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(layover, final)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.reasons == ["No visa on file for JP"]
        assert result.severity == AlertSeverity.CRITICAL


class TestVisaExemptNationality:
    """Travelers whose nationality is exempt for a destination are skipped."""

    def test_exempt_nationality_no_alert(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_exemption_is_per_country(self, base_requirements):
        """US is exempt for JP but not CN (empty exempt list)."""
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestVisaRecordCountryMatching:
    """A visa only counts for the country it was issued for."""

    def test_visa_for_other_country_does_not_satisfy(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[_visa(country_code="JP")])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestVisaExpiryRelativeToDeparture:
    """Visa expiry vs departure date: before → CRITICAL, on → WARNING, ≤30 days → INFO."""

    def test_expires_before_departure_but_after_today_critical(self, base_requirements):
        visa = _visa(expiry_date=date(2030, 5, 31))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]

    def test_expires_on_departure_warning(self, base_requirements):
        visa = _visa(expiry_date=FUTURE_DEPARTURE)
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]

    def test_expires_one_day_after_departure_info(self, base_requirements):
        visa = _visa(expiry_date=date(2030, 6, 2))
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert result.reasons == ["Visa for CN expires within 30 days of travel"]

    def test_expires_exactly_at_warning_threshold_info(self, base_requirements):
        visa = _visa(expiry_date=date(2030, 7, 1))  # departure + 30 days
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO
        assert result.reasons == ["Visa for CN expires within 30 days of travel"]

    def test_expires_one_day_past_warning_threshold_no_alert(self, base_requirements):
        visa = _visa(expiry_date=date(2030, 7, 2))  # departure + 31 days
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestBestVisaSelection:
    """With multiple visas for one country, the latest expiry wins."""

    def test_valid_visa_chosen_over_expired(self, base_requirements):
        expired = _visa(expiry_date=date(2020, 1, 1), visa_number="OLD")
        valid = _visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_order_of_records_does_not_matter(self, base_requirements):
        expired = _visa(expiry_date=date(2020, 1, 1), visa_number="OLD")
        valid = _visa(expiry_date=date(2035, 1, 1), visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[valid, expired])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_latest_visa_drives_severity(self, base_requirements):
        """Latest visa expires on departure → WARNING, not CRITICAL from older one."""
        older = _visa(expiry_date=date(2030, 1, 1), visa_number="OLD")
        latest = _visa(expiry_date=FUTURE_DEPARTURE, visa_number="NEW")
        profile = make_profile(nationality="IN", visa_records=[latest, older])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestSeverityConsolidation:
    """Overall severity is the highest across segments; reasons accumulate in order."""

    def test_info_then_critical_escalates(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(country_code="CN", expiry_date=date(2030, 6, 10))],
        )
        info_seg = make_segment(destination="CN", departure=FUTURE_DEPARTURE)
        critical_seg = make_segment(destination="JP", departure=FUTURE_DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(info_seg, critical_seg)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Visa for CN expires within 30 days of travel",
            "No visa on file for JP",
        ]

    def test_critical_then_info_stays_critical(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(country_code="CN", expiry_date=date(2030, 6, 10))],
        )
        critical_seg = make_segment(destination="JP", departure=FUTURE_DEPARTURE)
        info_seg = make_segment(destination="CN", departure=FUTURE_DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(critical_seg, info_seg)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_then_info_stays_warning(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(country_code="CN", expiry_date=FUTURE_DEPARTURE),
                _visa(country_code="JP", expiry_date=date(2030, 6, 10)),
            ],
        )
        warning_seg = make_segment(destination="CN", departure=FUTURE_DEPARTURE)
        info_seg = make_segment(destination="JP", departure=FUTURE_DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(warning_seg, info_seg)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == [
            "Visa for CN expires on date of travel",
            "Visa for JP expires within 30 days of travel",
        ]

    def test_info_then_warning_escalates(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[
                _visa(country_code="CN", expiry_date=date(2030, 6, 10)),
                _visa(country_code="JP", expiry_date=FUTURE_DEPARTURE),
            ],
        )
        info_seg = make_segment(destination="CN", departure=FUTURE_DEPARTURE)
        warning_seg = make_segment(destination="JP", departure=FUTURE_DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(info_seg, warning_seg)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING

    def test_round_trip_same_country_evaluated_per_segment(self, base_requirements):
        """Same visa checked against each segment's own departure date."""
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(country_code="CN", expiry_date=date(2030, 6, 15))],
        )
        outbound = make_segment(destination="CN", departure=FUTURE_DEPARTURE)
        later = make_segment(destination="CN", departure=date(2030, 7, 1), flight_number="DL200")
        itinerary = make_itinerary(outbound, later)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "Visa for CN expires within 30 days of travel",
            "Visa for CN expires before date of travel",
        ]


class TestRequirementsFallback:
    """Destinations missing from the requirements arg fall back to country_requirements."""

    def test_unknown_country_uses_default_requiring_visa(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="ZZ", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for ZZ" in result.reasons

    def test_unknown_country_layover_skipped_by_default(self, base_requirements):
        """Default requirements do not mandate a transit visa."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="ZZ", departure=FUTURE_DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_override_takes_precedence_over_defaults(self):
        """Requirements override marks CN as visa-free → no alert despite no visa."""
        requirements = _requirements(country_code="CN", requires_visa=False)
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False

    def test_empty_requirements_falls_back_for_every_segment(self):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new dates/destinations."""

    def test_rebooked_later_past_visa_expiry_becomes_critical(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(country_code="CN", expiry_date=date(2030, 12, 31))],
        )
        original = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))
        changed = make_itinerary(make_segment(destination="CN", departure=date(2031, 2, 1)))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["Visa for CN expires before date of travel"]

    def test_rebooked_earlier_clears_alert(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[_visa(country_code="CN", expiry_date=date(2030, 12, 31))],
        )
        original = make_itinerary(make_segment(destination="CN", departure=date(2031, 2, 1)))
        changed = make_itinerary(make_segment(destination="CN", departure=FUTURE_DEPARTURE))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False

    def test_reroute_through_transit_visa_country_adds_alert(self, base_requirements):
        """Layover moved from DE (no transit visa) to CN (transit visa) → CRITICAL."""
        profile = make_profile(nationality="IN", visa_records=[_visa(country_code="JP")])
        final = make_segment(destination="JP", departure=FUTURE_DEPARTURE, flight_number="DL200")
        original = make_itinerary(
            make_segment(destination="DE", departure=FUTURE_DEPARTURE, is_layover=True), final,
        )
        changed = make_itinerary(
            make_segment(destination="CN", departure=FUTURE_DEPARTURE, is_layover=True), final,
        )

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["No visa on file for CN"]
