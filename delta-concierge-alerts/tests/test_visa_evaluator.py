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
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
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
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
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
            issue_date=date(2025, 1, 1), expiry_date=date(2030, 6, 1),
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
        segment = make_segment(destination="CN", departure=date(2030, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


DEPARTURE = date(2030, 9, 1)


def _visa(
    country_code: str = "CN",
    expiry: date = date(2035, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code, visa_type="TOURIST",
        issue_date=date(2025, 1, 1), expiry_date=expiry,
        visa_number=visa_number,
    )


def _requirements(
    country_code: str = "XX",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    visa_exempt_nationalities: list[str] | None = None,
) -> TravelDocRequirements:
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=6,
        visa_exempt_nationalities=visa_exempt_nationalities or [],
    )


def _evaluate(requirements, visa_records=None, nationality="IN", segments=None):
    profile = make_profile(nationality=nationality, visa_records=visa_records or [])
    if segments is None:
        segments = [make_segment(destination="CN", departure=DEPARTURE)]
    return evaluate_visa_requirements(profile, make_itinerary(*segments), requirements)


class TestValidVisaHappyPath:
    """A visa valid well beyond the warning window produces no alert."""

    def test_valid_visa_no_alert(self, base_requirements):
        result = _evaluate(base_requirements, visa_records=[_visa()])

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_result_echoes_profile_and_segments(self, base_requirements):
        segment = make_segment(destination="CN", departure=DEPARTURE)
        profile = make_profile(visa_records=[_visa()])

        result = evaluate_visa_requirements(profile, make_itinerary(segment), base_requirements)

        assert result.profile is profile
        assert result.segments_evaluated == [segment]

    def test_empty_itinerary_no_alert(self, base_requirements):
        result = _evaluate(base_requirements, segments=[])

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestVisaNotRequired:
    """Destinations with requires_visa=False are skipped entirely."""

    def test_country_without_visa_requirement_skipped(self):
        reqs = {"MX": _requirements("MX", requires_visa=False)}
        segment = make_segment(destination="MX", departure=DEPARTURE)

        result = _evaluate(reqs, segments=[segment])

        assert result.is_alert_required is False
        assert result.severity is None

    def test_no_visa_required_even_on_layover_with_transit_flag(self):
        reqs = {"MX": _requirements("MX", requires_visa=False, transit_visa_required=True)}
        segment = make_segment(destination="MX", departure=DEPARTURE, is_layover=True)

        result = _evaluate(reqs, segments=[segment])

        assert result.is_alert_required is False


class TestLayovers:
    """Layovers are only evaluated when the country requires a transit visa."""

    def test_layover_without_transit_requirement_skipped(self, base_requirements):
        segment = make_segment(destination="JP", departure=DEPARTURE, is_layover=True)

        result = _evaluate(base_requirements, segments=[segment])

        assert result.is_alert_required is False
        assert result.severity is None

    def test_same_country_as_final_destination_requires_visa(self, base_requirements):
        segment = make_segment(destination="JP", departure=DEPARTURE, is_layover=False)

        result = _evaluate(base_requirements, segments=[segment])

        assert result.is_alert_required is True
        assert "No visa on file for JP" in result.reasons

    def test_transit_visa_layover_with_valid_visa_no_alert(self, base_requirements):
        segment = make_segment(destination="CN", departure=DEPARTURE, is_layover=True)

        result = _evaluate(base_requirements, visa_records=[_visa()], segments=[segment])

        assert result.is_alert_required is False

    def test_transit_visa_layover_with_expired_visa_alerts(self, base_requirements):
        segment = make_segment(destination="CN", departure=DEPARTURE, is_layover=True)

        result = _evaluate(
            base_requirements, visa_records=[_visa(expiry=date(2020, 1, 1))], segments=[segment]
        )

        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons

    def test_skipped_layover_does_not_mask_final_destination_alert(self, base_requirements):
        layover = make_segment(destination="JP", departure=DEPARTURE, is_layover=True)
        final = make_segment(destination="CN", departure=DEPARTURE + timedelta(days=1))

        result = _evaluate(base_requirements, segments=[layover, final])

        assert result.reasons == ["No visa on file for CN"]
        assert result.severity == AlertSeverity.CRITICAL


class TestVisaExemptNationality:
    """Travelers whose nationality is in the exempt list are skipped."""

    def test_exempt_nationality_no_alert(self, base_requirements):
        segment = make_segment(destination="DE", departure=DEPARTURE)

        result = _evaluate(base_requirements, nationality="US", segments=[segment])

        assert result.is_alert_required is False
        assert result.severity is None

    def test_non_exempt_nationality_alerts(self, base_requirements):
        segment = make_segment(destination="DE", departure=DEPARTURE)

        result = _evaluate(base_requirements, nationality="IN", segments=[segment])

        assert result.is_alert_required is True
        assert "No visa on file for DE" in result.reasons

    def test_exempt_nationality_on_transit_layover_skipped(self):
        reqs = {"CN": _requirements("CN", transit_visa_required=True, visa_exempt_nationalities=["SG"])}
        segment = make_segment(destination="CN", departure=DEPARTURE, is_layover=True)

        result = _evaluate(reqs, nationality="SG", segments=[segment])

        assert result.is_alert_required is False


class TestVisaExpiryRelativeToTravel:
    """Visa expiry relative to departure drives severity: before → CRITICAL,
    same day → WARNING, within the warning window → INFO, beyond → none."""

    def test_expires_day_before_departure_is_critical(self, base_requirements):
        result = _evaluate(base_requirements, visa_records=[_visa(expiry=DEPARTURE - timedelta(days=1))])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]

    def test_expires_on_departure_is_warning(self, base_requirements):
        result = _evaluate(base_requirements, visa_records=[_visa(expiry=DEPARTURE)])

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]

    def test_expires_day_after_departure_is_info(self, base_requirements):
        result = _evaluate(base_requirements, visa_records=[_visa(expiry=DEPARTURE + timedelta(days=1))])

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_expires_exactly_at_warning_threshold_is_info(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)

        result = _evaluate(base_requirements, visa_records=[_visa(expiry=expiry)])

        assert result.severity == AlertSeverity.INFO

    def test_expires_day_after_warning_threshold_no_alert(self, base_requirements):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)

        result = _evaluate(base_requirements, visa_records=[_visa(expiry=expiry)])

        assert result.is_alert_required is False
        assert result.severity is None

    def test_already_expired_takes_precedence_over_before_travel(self, base_requirements):
        result = _evaluate(base_requirements, visa_records=[_visa(expiry=date(2020, 1, 1))])

        assert result.reasons == ["Visa for CN has expired"]


class TestVisaMatching:
    """Only visas for the destination country match; latest expiry wins."""

    def test_visa_for_other_country_does_not_match(self, base_requirements):
        result = _evaluate(base_requirements, visa_records=[_visa(country_code="JP")])

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_latest_expiry_selected_over_expired_visa(self, base_requirements):
        visas = [
            _visa(expiry=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry=date(2035, 1, 1), visa_number="NEW"),
        ]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.is_alert_required is False

    def test_latest_expiry_selected_regardless_of_order(self, base_requirements):
        visas = [
            _visa(expiry=date(2035, 1, 1), visa_number="NEW"),
            _visa(expiry=DEPARTURE, visa_number="SAME_DAY"),
        ]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.is_alert_required is False

    def test_latest_expiry_still_short_reports_that_visa(self, base_requirements):
        visas = [
            _visa(expiry=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry=DEPARTURE, visa_number="SAME_DAY"),
        ]

        result = _evaluate(base_requirements, visa_records=visas)

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestRequirementsFallback:
    """Destinations missing from the requirements mapping fall back to
    country_requirements data (or the default for unknown countries)."""

    def test_unknown_country_uses_default_visa_required(self):
        segment = make_segment(destination="ZZ", departure=DEPARTURE)

        result = _evaluate({}, segments=[segment])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for ZZ"]

    def test_unknown_country_default_skips_layover(self):
        segment = make_segment(destination="ZZ", departure=DEPARTURE, is_layover=True)

        result = _evaluate({}, segments=[segment])

        assert result.is_alert_required is False

    def test_known_country_uses_static_exempt_list(self):
        segment = make_segment(destination="GB", departure=DEPARTURE)

        result = _evaluate({}, nationality="US", segments=[segment])

        assert result.is_alert_required is False

    def test_override_takes_precedence_over_static_data(self):
        reqs = {"GB": _requirements("GB", requires_visa=True, visa_exempt_nationalities=[])}
        segment = make_segment(destination="GB", departure=DEPARTURE)

        result = _evaluate(reqs, nationality="US", segments=[segment])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for GB"]


class TestSeverityConsolidation:
    """The overall severity is the highest across segments; reasons accumulate."""

    def _segments(self):
        return [
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE + timedelta(days=5)),
        ]

    def test_critical_then_info_stays_critical(self, base_requirements):
        visas = [_visa("JP", expiry=DEPARTURE + timedelta(days=10))]

        result = _evaluate(base_requirements, visa_records=visas, segments=self._segments())

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "No visa on file for CN",
            f"Visa for JP expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
        ]

    def test_info_then_warning_escalates_to_warning(self, base_requirements):
        visas = [
            _visa("CN", expiry=DEPARTURE + timedelta(days=10)),
            _visa("JP", expiry=DEPARTURE + timedelta(days=5)),
        ]

        result = _evaluate(base_requirements, visa_records=visas, segments=self._segments())

        assert result.severity == AlertSeverity.WARNING
        assert len(result.reasons) == 2

    def test_warning_then_info_stays_warning(self, base_requirements):
        visas = [
            _visa("CN", expiry=DEPARTURE),
            _visa("JP", expiry=DEPARTURE + timedelta(days=10)),
        ]

        result = _evaluate(base_requirements, visa_records=visas, segments=self._segments())

        assert result.severity == AlertSeverity.WARNING

    def test_info_then_critical_escalates_to_critical(self, base_requirements):
        visas = [_visa("CN", expiry=DEPARTURE + timedelta(days=10))]

        result = _evaluate(base_requirements, visa_records=visas, segments=self._segments())

        assert result.severity == AlertSeverity.CRITICAL

    def test_one_alerting_segment_among_clean_segments(self, base_requirements):
        visas = [_visa("CN"), _visa("JP", expiry=DEPARTURE + timedelta(days=5))]

        result = _evaluate(base_requirements, visa_records=visas, segments=self._segments())

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for JP expires on date of travel"]


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new dates/destinations."""

    def test_departure_moved_past_visa_expiry_now_alerts(self, base_requirements):
        visas = [_visa(expiry=date(2030, 12, 31))]
        original = [make_segment(destination="CN", departure=date(2030, 10, 1))]
        changed = [make_segment(destination="CN", departure=date(2031, 2, 1))]

        before = _evaluate(base_requirements, visa_records=visas, segments=original)
        after = _evaluate(base_requirements, visa_records=visas, segments=changed)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["Visa for CN expires before date of travel"]

    def test_departure_moved_earlier_clears_alert(self, base_requirements):
        visas = [_visa(expiry=date(2030, 12, 31))]
        original = [make_segment(destination="CN", departure=date(2030, 12, 31))]
        changed = [make_segment(destination="CN", departure=date(2030, 10, 1))]

        before = _evaluate(base_requirements, visa_records=visas, segments=original)
        after = _evaluate(base_requirements, visa_records=visas, segments=changed)

        assert before.severity == AlertSeverity.WARNING
        assert after.is_alert_required is False

    def test_destination_changed_to_country_without_visa_on_file(self, base_requirements):
        visas = [_visa("CN")]
        original = [make_segment(destination="CN", departure=DEPARTURE)]
        changed = [make_segment(destination="DE", departure=DEPARTURE)]

        before = _evaluate(base_requirements, visa_records=visas, segments=original)
        after = _evaluate(base_requirements, visa_records=visas, segments=changed)

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for DE"]

    def test_added_transit_layover_introduces_alert(self, base_requirements):
        visas = [_visa("DE")]
        original = [make_segment(destination="DE", departure=DEPARTURE)]
        changed = [
            make_segment(destination="CN", departure=DEPARTURE, is_layover=True),
            make_segment(destination="DE", departure=DEPARTURE + timedelta(days=1)),
        ]

        before = _evaluate(base_requirements, visa_records=visas, segments=original)
        after = _evaluate(base_requirements, visa_records=visas, segments=changed)

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for CN"]
        assert after.severity == AlertSeverity.CRITICAL
