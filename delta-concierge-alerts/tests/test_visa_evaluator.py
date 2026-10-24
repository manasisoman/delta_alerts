"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

import pytest

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.data import country_requirements
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)


def _visa(
    country_code: str = "CN",
    expiry_date: date = date(2031, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    """Build a VisaRecord with sensible far-future defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2020, 1, 1),
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


def _requirements(country_code: str = "CN", **overrides) -> TravelDocRequirements:
    """Build TravelDocRequirements that require a visa, with overrides."""
    fields = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": True,
        "passport_validity_months": 6,
        "visa_exempt_nationalities": [],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _evaluate(visa_records=None, requirements=None, nationality="IN", segments=None):
    """Evaluate a single-CN-segment itinerary unless segments are given."""
    profile = make_profile(nationality=nationality, visa_records=visa_records or [])
    segments = segments or [make_segment(destination="CN", departure=DEPARTURE)]
    itinerary = make_itinerary(*segments)
    reqs = requirements if requirements is not None else {"CN": _requirements("CN")}
    return evaluate_visa_requirements(profile, itinerary, reqs)


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

    def test_visa_for_other_country_does_not_match(self):
        visas = [_visa(country_code="JP")]

        result = _evaluate(visa_records=visas)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


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

    def test_visa_expires_one_day_before_departure(self):
        visas = [_visa(expiry_date=DEPARTURE - timedelta(days=1))]

        result = _evaluate(visa_records=visas)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure(self):
        visas = [_visa(expiry_date=DEPARTURE)]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestVisaExpiryWarningWindow:
    """Visa expiring within VISA_EXPIRY_WARNING_DAYS after departure → INFO."""

    def test_visa_expires_day_after_departure(self):
        visas = [_visa(expiry_date=DEPARTURE + timedelta(days=1))]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_visa_expires_exactly_at_warning_threshold(self):
        visas = [_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS))]

        result = _evaluate(visa_records=visas)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expires_one_day_past_warning_threshold(self):
        visas = [_visa(expiry_date=DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1))]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestValidVisaHappyPath:
    """A visa valid well past travel produces no alert."""

    def test_valid_visa_no_alert(self):
        visas = [_visa(expiry_date=date(2035, 1, 1))]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_result_echoes_profile_and_segments(self):
        profile = make_profile(visa_records=[_visa(expiry_date=date(2035, 1, 1))])
        segment = make_segment(destination="CN", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, {"CN": _requirements()})

        assert result.profile is profile
        assert result.segments_evaluated == [segment]

    def test_empty_itinerary_no_alert(self):
        profile = make_profile()
        itinerary = make_itinerary()

        result = evaluate_visa_requirements(profile, itinerary, {})

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestVisaNotRequired:
    """Destinations with requires_visa=False are skipped entirely."""

    def test_country_without_visa_requirement_skipped(self):
        reqs = {"CN": _requirements(requires_visa=False)}

        result = _evaluate(visa_records=[], requirements=reqs)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_visa_not_required_ignores_expired_visa(self):
        reqs = {"CN": _requirements(requires_visa=False)}
        visas = [_visa(expiry_date=date(2020, 1, 1))]

        result = _evaluate(visa_records=visas, requirements=reqs)

        assert result.is_alert_required is False


class TestLayovers:
    """Layovers are only checked when the country requires a transit visa."""

    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL

    def test_layover_without_transit_visa_requirement_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_final_destination_without_transit_requirement_still_checked(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE, is_layover=False)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for DE"]

    def test_transit_layover_with_expired_visa(self):
        segments = [make_segment(destination="CN", departure=DEPARTURE, is_layover=True)]
        visas = [_visa(expiry_date=date(2020, 1, 1))]

        result = _evaluate(visa_records=visas, segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN has expired"]

    def test_skipped_layover_then_final_destination_checked(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        layover = make_segment(destination="JP", departure=DEPARTURE, is_layover=True)
        final = make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200")
        itinerary = make_itinerary(layover, final)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.reasons == ["No visa on file for CN"]


class TestVisaExemptNationality:
    """Travelers whose nationality is exempt are not alerted."""

    def test_exempt_nationality_skipped(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None

    def test_non_exempt_nationality_alerted(self, base_requirements):
        profile = make_profile(nationality="CN", visa_records=[])
        segment = make_segment(destination="DE", departure=DEPARTURE)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL

    def test_exempt_nationality_on_transit_layover_skipped(self):
        reqs = {"CN": _requirements(visa_exempt_nationalities=["US"])}
        segments = [make_segment(destination="CN", departure=DEPARTURE, is_layover=True)]

        result = _evaluate(requirements=reqs, nationality="US", segments=segments)

        assert result.is_alert_required is False


class TestVisaSelection:
    """When several visas match a country, the latest expiry wins."""

    def test_valid_visa_preferred_over_expired(self):
        visas = [
            _visa(expiry_date=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry_date=date(2035, 1, 1), visa_number="NEW"),
        ]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is False

    def test_latest_expiry_selected_regardless_of_order(self):
        visas = [
            _visa(expiry_date=date(2035, 1, 1), visa_number="NEW"),
            _visa(expiry_date=DEPARTURE, visa_number="ON_TRAVEL_DATE"),
        ]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is False

    def test_best_of_multiple_insufficient_visas_used(self):
        visas = [
            _visa(expiry_date=date(2020, 1, 1), visa_number="EXPIRED"),
            _visa(expiry_date=DEPARTURE, visa_number="ON_TRAVEL_DATE"),
        ]

        result = _evaluate(visa_records=visas)

        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestSeverityConsolidation:
    """Consolidated severity is the highest across segments; all reasons kept."""

    @staticmethod
    def _requirements():
        return {"CN": _requirements("CN"), "JP": _requirements("JP")}

    def test_critical_overrides_info(self):
        segments = [
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        ]
        visas = [_visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5))]

        result = _evaluate(visa_records=visas, requirements=self._requirements(), segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            f"Visa for JP expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "No visa on file for CN",
        ]

    def test_critical_not_downgraded_by_later_warning(self):
        segments = [
            make_segment(destination="CN", departure=DEPARTURE),
            make_segment(destination="JP", departure=DEPARTURE, flight_number="DL200"),
        ]
        visas = [_visa(country_code="JP", expiry_date=DEPARTURE)]

        result = _evaluate(visa_records=visas, requirements=self._requirements(), segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_overrides_info(self):
        segments = [
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        ]
        visas = [
            _visa(country_code="JP", expiry_date=DEPARTURE + timedelta(days=5)),
            _visa(country_code="CN", expiry_date=DEPARTURE),
        ]

        result = _evaluate(visa_records=visas, requirements=self._requirements(), segments=segments)

        assert result.severity == AlertSeverity.WARNING

    def test_same_severity_kept(self):
        segments = [
            make_segment(destination="JP", departure=DEPARTURE),
            make_segment(destination="CN", departure=DEPARTURE, flight_number="DL200"),
        ]

        result = _evaluate(visa_records=[], requirements=self._requirements(), segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for JP", "No visa on file for CN"]

    def test_each_segment_uses_its_own_departure_date(self):
        segments = [
            make_segment(destination="CN", departure=date(2030, 6, 1)),
            make_segment(destination="CN", departure=date(2030, 12, 1), flight_number="DL200"),
        ]
        visas = [_visa(expiry_date=date(2030, 9, 1))]

        result = _evaluate(visa_records=visas, segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new segments."""

    def test_later_departure_makes_visa_insufficient(self):
        visas = [_visa(expiry_date=date(2030, 10, 1))]
        original = [make_segment(destination="CN", departure=date(2030, 8, 1))]
        changed = [make_segment(destination="CN", departure=date(2030, 11, 1))]

        before = _evaluate(visa_records=visas, segments=original)
        after = _evaluate(visa_records=visas, segments=changed)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL
        assert after.reasons == ["Visa for CN expires before date of travel"]

    def test_rerouting_away_from_visa_country_clears_alert(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        original = make_itinerary(make_segment(destination="CN", departure=DEPARTURE))
        changed = make_itinerary(make_segment(destination="JP", departure=DEPARTURE))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False

    def test_layover_becomes_final_destination(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        original = make_itinerary(make_segment(destination="JP", departure=DEPARTURE, is_layover=True))
        changed = make_itinerary(make_segment(destination="JP", departure=DEPARTURE, is_layover=False))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, changed, base_requirements)

        assert before.is_alert_required is False
        assert after.reasons == ["No visa on file for JP"]


class TestRequirementsFallback:
    """Destinations missing from the requirements dict fall back to get_requirements."""

    @pytest.fixture(autouse=True)
    def _isolated_country_requirements(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {
            "XA": _requirements("XA", requires_visa=False),
            "XB": _requirements("XB", visa_exempt_nationalities=["IN"]),
        }
        try:
            yield
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

    def test_fallback_country_without_visa_requirement(self):
        segments = [make_segment(destination="XA", departure=DEPARTURE)]

        result = _evaluate(requirements={}, segments=segments)

        assert result.is_alert_required is False

    def test_fallback_country_with_exempt_nationality(self):
        segments = [make_segment(destination="XB", departure=DEPARTURE)]

        result = _evaluate(requirements={}, segments=segments)

        assert result.is_alert_required is False

    def test_unknown_country_uses_default_requirements(self):
        segments = [make_segment(destination="ZZ", departure=DEPARTURE)]

        result = _evaluate(requirements={}, segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for ZZ"]

    def test_unknown_country_layover_skipped_by_default(self):
        segments = [make_segment(destination="ZZ", departure=DEPARTURE, is_layover=True)]

        result = _evaluate(requirements={}, segments=segments)

        assert result.is_alert_required is False

    def test_explicit_requirements_take_precedence(self):
        segments = [make_segment(destination="XA", departure=DEPARTURE)]
        reqs = {"XA": _requirements("XA", requires_visa=True)}

        result = _evaluate(requirements=reqs, segments=segments)

        assert result.reasons == ["No visa on file for XA"]
