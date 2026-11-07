"""Additional coverage tests for the visa evaluator decision paths."""

from datetime import date, timedelta

import pytest

import src.data.country_requirements as country_requirements
from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import (
    _find_matching_visa,
    _max_severity,
    evaluate_visa_requirements,
)
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)


def _visa(
    country_code: str = "CN",
    expiry_date: date = date(2032, 1, 1),
    issue_date: date = date(2029, 1, 1),
    visa_number: str = "V100",
) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=issue_date,
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


def _requirements(
    country_code: str = "XA",
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


def _evaluate(visa_records=None, requirements=None, nationality="IN", segments=None):
    profile = make_profile(nationality=nationality, visa_records=visa_records or [])
    itinerary = make_itinerary(*(segments or [make_segment(destination="CN", departure=DEPARTURE)]))
    reqs = requirements if requirements is not None else {"CN": _requirements("CN", transit_visa_required=True)}
    return evaluate_visa_requirements(profile, itinerary, reqs)


class TestValidVisaHappyPath:
    """A visa valid well beyond the warning window produces no alert."""

    def test_valid_visa_no_alert(self):
        result = _evaluate(visa_records=[_visa(expiry_date=date(2032, 1, 1))])

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []
        assert result.validation_errors == []

    def test_result_carries_profile_and_segments(self):
        segment = make_segment(destination="CN", departure=DEPARTURE)

        result = _evaluate(visa_records=[_visa()], segments=[segment])

        assert result.profile.nationality == "IN"
        assert result.segments_evaluated == [segment]

    def test_empty_itinerary_no_alert(self):
        profile = make_profile(visa_records=[])

        result = evaluate_visa_requirements(profile, make_itinerary(), {})

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.segments_evaluated == []


class TestVisaNotRequired:
    """Destinations that do not require a visa are skipped entirely."""

    def test_country_without_visa_requirement_skipped(self):
        reqs = {"XA": _requirements("XA", requires_visa=False)}
        segment = make_segment(destination="XA", departure=DEPARTURE)

        result = _evaluate(requirements=reqs, segments=[segment])

        assert result.is_alert_required is False
        assert result.severity is None


class TestLayovers:
    """Layovers only require a visa when the country mandates a transit visa."""

    def test_layover_without_transit_requirement_skipped(self):
        reqs = {"XA": _requirements("XA", transit_visa_required=False)}
        segment = make_segment(destination="XA", departure=DEPARTURE, is_layover=True)

        result = _evaluate(requirements=reqs, segments=[segment])

        assert result.is_alert_required is False
        assert result.severity is None

    def test_layover_with_transit_requirement_and_valid_visa_no_alert(self):
        reqs = {"XA": _requirements("XA", transit_visa_required=True)}
        segment = make_segment(destination="XA", departure=DEPARTURE, is_layover=True)

        result = _evaluate(
            visa_records=[_visa(country_code="XA")], requirements=reqs, segments=[segment]
        )

        assert result.is_alert_required is False

    def test_layover_skipped_but_final_destination_alerts(self):
        reqs = {
            "XA": _requirements("XA", transit_visa_required=False),
            "XB": _requirements("XB"),
        }
        layover = make_segment(destination="XA", departure=DEPARTURE, is_layover=True)
        final = make_segment(destination="XB", departure=DEPARTURE + timedelta(days=1))

        result = _evaluate(requirements=reqs, segments=[layover, final])

        assert result.reasons == ["No visa on file for XB"]
        assert result.severity == AlertSeverity.CRITICAL

    def test_non_layover_requires_visa_even_without_transit_rule(self):
        reqs = {"XA": _requirements("XA", transit_visa_required=False)}
        segment = make_segment(destination="XA", departure=DEPARTURE, is_layover=False)

        result = _evaluate(requirements=reqs, segments=[segment])

        assert result.is_alert_required is True
        assert "No visa on file for XA" in result.reasons


class TestVisaExemptNationality:
    """Travelers whose nationality is exempt are skipped."""

    def test_exempt_nationality_skipped(self):
        reqs = {"XA": _requirements("XA", visa_exempt_nationalities=["US", "GB"])}
        segment = make_segment(destination="XA", departure=DEPARTURE)

        result = _evaluate(nationality="US", requirements=reqs, segments=[segment])

        assert result.is_alert_required is False

    def test_non_exempt_nationality_alerts(self):
        reqs = {"XA": _requirements("XA", visa_exempt_nationalities=["US", "GB"])}
        segment = make_segment(destination="XA", departure=DEPARTURE)

        result = _evaluate(nationality="IN", requirements=reqs, segments=[segment])

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestMissingVisa:
    """Visa records for other countries do not satisfy the destination."""

    def test_visa_for_other_country_does_not_match(self):
        result = _evaluate(visa_records=[_visa(country_code="JP")])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


class TestVisaExpiryWindows:
    """Expiry relative to today and departure determines severity."""

    def test_expired_visa_critical(self):
        result = _evaluate(visa_records=[_visa(expiry_date=date(2020, 1, 1))])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN has expired"]

    def test_expires_before_departure_critical(self):
        result = _evaluate(visa_records=[_visa(expiry_date=DEPARTURE - timedelta(days=1))])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]

    def test_expires_on_departure_warning(self):
        result = _evaluate(visa_records=[_visa(expiry_date=DEPARTURE)])

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]

    def test_expires_day_after_departure_info(self):
        result = _evaluate(visa_records=[_visa(expiry_date=DEPARTURE + timedelta(days=1))])

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
        ]

    def test_expires_exactly_at_warning_threshold_info(self):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS)

        result = _evaluate(visa_records=[_visa(expiry_date=expiry)])

        assert result.severity == AlertSeverity.INFO

    def test_expires_one_day_past_warning_threshold_no_alert(self):
        expiry = DEPARTURE + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1)

        result = _evaluate(visa_records=[_visa(expiry_date=expiry)])

        assert result.is_alert_required is False
        assert result.severity is None


class TestBestVisaSelection:
    """When several visas match a country, the latest expiry is used."""

    def test_valid_visa_preferred_over_expired(self):
        visas = [
            _visa(expiry_date=date(2020, 1, 1), visa_number="OLD"),
            _visa(expiry_date=date(2032, 1, 1), visa_number="NEW"),
        ]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is False

    def test_order_of_records_does_not_matter(self):
        visas = [
            _visa(expiry_date=date(2032, 1, 1), visa_number="NEW"),
            _visa(expiry_date=date(2020, 1, 1), visa_number="OLD"),
        ]

        result = _evaluate(visa_records=visas)

        assert result.is_alert_required is False

    def test_find_matching_visa_returns_latest_expiry(self):
        visas = [
            _visa(expiry_date=date(2031, 1, 1), visa_number="A"),
            _visa(expiry_date=date(2033, 1, 1), visa_number="B"),
            _visa(country_code="JP", expiry_date=date(2040, 1, 1), visa_number="C"),
        ]

        match = _find_matching_visa(visas, "CN")

        assert match.visa_number == "B"

    def test_find_matching_visa_none_when_no_match(self):
        assert _find_matching_visa([_visa(country_code="JP")], "CN") is None

    def test_find_matching_visa_none_for_empty_list(self):
        assert _find_matching_visa([], "CN") is None


class TestSeverityConsolidation:
    """Highest severity across segments wins; all reasons are collected."""

    def _reqs(self):
        return {code: _requirements(code) for code in ("XA", "XB", "XC")}

    def test_info_then_critical_escalates(self):
        visas = [_visa(country_code="XA", expiry_date=DEPARTURE + timedelta(days=5))]
        segments = [
            make_segment(destination="XA", departure=DEPARTURE),
            make_segment(destination="XB", departure=DEPARTURE),
        ]

        result = _evaluate(visa_records=visas, requirements=self._reqs(), segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_critical_then_info_stays_critical(self):
        visas = [_visa(country_code="XB", expiry_date=DEPARTURE + timedelta(days=5))]
        segments = [
            make_segment(destination="XA", departure=DEPARTURE),
            make_segment(destination="XB", departure=DEPARTURE),
        ]

        result = _evaluate(visa_records=visas, requirements=self._reqs(), segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            "No visa on file for XA",
            f"Visa for XB expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
        ]

    def test_warning_then_info_stays_warning(self):
        visas = [
            _visa(country_code="XA", expiry_date=DEPARTURE),
            _visa(country_code="XB", expiry_date=DEPARTURE + timedelta(days=5)),
        ]
        segments = [
            make_segment(destination="XA", departure=DEPARTURE),
            make_segment(destination="XB", departure=DEPARTURE),
        ]

        result = _evaluate(visa_records=visas, requirements=self._reqs(), segments=segments)

        assert result.severity == AlertSeverity.WARNING
        assert len(result.reasons) == 2

    def test_info_then_warning_escalates(self):
        visas = [
            _visa(country_code="XA", expiry_date=DEPARTURE + timedelta(days=5)),
            _visa(country_code="XB", expiry_date=DEPARTURE),
        ]
        segments = [
            make_segment(destination="XA", departure=DEPARTURE),
            make_segment(destination="XB", departure=DEPARTURE),
        ]

        result = _evaluate(visa_records=visas, requirements=self._reqs(), segments=segments)

        assert result.severity == AlertSeverity.WARNING

    def test_same_country_twice_uses_each_segment_departure(self):
        visa = _visa(country_code="XA", expiry_date=date(2030, 10, 1))
        segments = [
            make_segment(destination="XA", departure=date(2030, 9, 1)),
            make_segment(destination="XA", departure=date(2030, 12, 1)),
        ]

        result = _evaluate(visa_records=[visa], requirements=self._reqs(), segments=segments)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == [
            f"Visa for XA expires within {VISA_EXPIRY_WARNING_DAYS} days of travel",
            "Visa for XA expires before date of travel",
        ]


class TestItineraryChange:
    """Re-evaluating a changed itinerary reflects the new dates/destinations."""

    def test_rescheduled_departure_after_visa_expiry_becomes_critical(self):
        visa = _visa(country_code="XA", expiry_date=date(2031, 1, 1))
        reqs = {"XA": _requirements("XA")}
        original = [make_segment(destination="XA", departure=date(2030, 9, 1))]
        changed = [make_segment(destination="XA", departure=date(2031, 2, 1))]

        before = _evaluate(visa_records=[visa], requirements=reqs, segments=original)
        after = _evaluate(visa_records=[visa], requirements=reqs, segments=changed)

        assert before.is_alert_required is False
        assert after.severity == AlertSeverity.CRITICAL

    def test_rerouted_to_exempt_country_clears_alert(self):
        reqs = {
            "XA": _requirements("XA"),
            "XB": _requirements("XB", visa_exempt_nationalities=["IN"]),
        }
        original = [make_segment(destination="XA", departure=DEPARTURE)]
        changed = [make_segment(destination="XB", departure=DEPARTURE)]

        before = _evaluate(requirements=reqs, segments=original)
        after = _evaluate(requirements=reqs, segments=changed)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False


class TestRequirementsFallback:
    """Destinations missing from the requirements dict use get_requirements()."""

    @pytest.fixture(autouse=True)
    def _swap_country_requirements(self):
        original = country_requirements.COUNTRY_REQUIREMENTS
        country_requirements.COUNTRY_REQUIREMENTS = {
            "XA": _requirements("XA", requires_visa=False),
            "XB": _requirements("XB", visa_exempt_nationalities=["IN"]),
        }
        try:
            yield
        finally:
            country_requirements.COUNTRY_REQUIREMENTS = original

    def test_fallback_country_not_requiring_visa_skipped(self):
        segment = make_segment(destination="XA", departure=DEPARTURE)

        result = _evaluate(requirements={}, segments=[segment])

        assert result.is_alert_required is False

    def test_fallback_exempt_nationality_skipped(self):
        segment = make_segment(destination="XB", departure=DEPARTURE)

        result = _evaluate(requirements={}, segments=[segment])

        assert result.is_alert_required is False

    def test_unknown_country_uses_default_requiring_visa(self):
        segment = make_segment(destination="ZZ", departure=DEPARTURE)

        result = _evaluate(requirements={}, segments=[segment])

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for ZZ"]

    def test_unknown_country_layover_skipped_by_default(self):
        segment = make_segment(destination="ZZ", departure=DEPARTURE, is_layover=True)

        result = _evaluate(requirements={}, segments=[segment])

        assert result.is_alert_required is False

    def test_explicit_requirements_take_precedence_over_fallback(self):
        segment = make_segment(destination="XA", departure=DEPARTURE)

        result = _evaluate(requirements={"XA": _requirements("XA")}, segments=[segment])

        assert result.severity == AlertSeverity.CRITICAL


class TestMaxSeverity:
    """_max_severity returns the higher-ranked severity."""

    @pytest.mark.parametrize(
        "current, new, expected",
        [
            (None, AlertSeverity.INFO, AlertSeverity.INFO),
            (None, AlertSeverity.CRITICAL, AlertSeverity.CRITICAL),
            (AlertSeverity.INFO, AlertSeverity.WARNING, AlertSeverity.WARNING),
            (AlertSeverity.WARNING, AlertSeverity.CRITICAL, AlertSeverity.CRITICAL),
            (AlertSeverity.CRITICAL, AlertSeverity.INFO, AlertSeverity.CRITICAL),
            (AlertSeverity.WARNING, AlertSeverity.INFO, AlertSeverity.WARNING),
            (AlertSeverity.WARNING, AlertSeverity.WARNING, AlertSeverity.WARNING),
        ],
    )
    def test_max_severity(self, current, new, expected):
        assert _max_severity(current, new) == expected
