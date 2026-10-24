"""Tests for the group itinerary evaluator."""

from datetime import date, timedelta

import pytest

import src.data.country_requirements as country_requirements_mod
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)


DEPARTURE = date(2031, 6, 1)
FAR_FUTURE_EXPIRY = date(2040, 1, 1)


def _requirements() -> dict[str, TravelDocRequirements]:
    return {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "JP": TravelDocRequirements(
            country_code="JP",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US", "GB"],
        ),
        "MX": TravelDocRequirements(
            country_code="MX",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=0,
            visa_exempt_nationalities=[],
        ),
    }


def _traveler(
    skymiles_number: str = "1000001",
    nationality: str = "US",
    passport_number: str | None = "P123456",
    passport_expiry: date | None = FAR_FUTURE_EXPIRY,
    visa_records: list[VisaRecord] | None = None,
    first_name: str = "Test",
    last_name: str = "Traveler",
) -> SkyMilesProfile:
    return SkyMilesProfile(
        skymiles_number=skymiles_number,
        first_name=first_name,
        last_name=last_name,
        nationality=nationality,
        passport_number=passport_number,
        passport_expiry=passport_expiry,
        visa_records=visa_records or [],
        endpoint_arn=f"arn:aws:sns:us-east-1:000:endpoint/{skymiles_number}",
    )


def _segment(
    destination: str = "DE",
    departure: date = DEPARTURE,
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> FlightSegment:
    return FlightSegment(
        flight_number=flight_number,
        origin=origin,
        destination=destination,
        departure_date=departure,
        arrival_date=departure + timedelta(days=1),
        is_layover=is_layover,
    )


def _visa(country_code: str, expiry: date, visa_number: str = "V001") -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2025, 1, 1),
        expiry_date=expiry,
        visa_number=visa_number,
    )


def _group(
    segments: list[FlightSegment] | None = None,
    travelers: list[SkyMilesProfile] | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupItinerary:
    travelers = travelers if travelers is not None else [_traveler()]
    return GroupItinerary(
        confirmation_number=confirmation_number,
        segments=segments if segments is not None else [_segment()],
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


def _summary_for(result, skymiles_number):
    return next(s for s in result.traveler_summaries if s.skymiles_number == skymiles_number)


@pytest.fixture
def default_requirements():
    """Swap the production COUNTRY_REQUIREMENTS for fixture data."""
    original = country_requirements_mod.COUNTRY_REQUIREMENTS
    country_requirements_mod.COUNTRY_REQUIREMENTS = _requirements()
    try:
        yield country_requirements_mod.COUNTRY_REQUIREMENTS
    finally:
        country_requirements_mod.COUNTRY_REQUIREMENTS = original


class TestHappyPath:
    """A fully compliant group yields one clean summary per traveler and no group severity."""

    def test_clean_group_has_no_severity(self):
        group = _group(travelers=[
            _traveler(skymiles_number="1000001", first_name="Ann", last_name="Lee"),
            _traveler(skymiles_number="1000002", first_name="Bob", last_name="Ray"),
        ])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.confirmation_number == "DL-GROUP"
        assert result.group_severity is None
        assert [s.skymiles_number for s in result.traveler_summaries] == ["1000001", "1000002"]
        assert [(s.first_name, s.last_name) for s in result.traveler_summaries] == [
            ("Ann", "Lee"), ("Bob", "Ray"),
        ]
        for summary in result.traveler_summaries:
            assert summary.passport_result.is_alert_required is False
            assert summary.visa_result.is_alert_required is False

    def test_each_summary_evaluates_shared_segments(self):
        segments = [_segment("DE", flight_number="DL100"), _segment("JP", flight_number="DL200")]
        group = _group(segments=segments)

        result = evaluate_group_itinerary(group, _requirements())

        summary = result.traveler_summaries[0]
        assert summary.passport_result.segments_evaluated == segments
        assert summary.visa_result.segments_evaluated == segments
        assert summary.passport_result.profile is group.travelers[0]

    def test_empty_group_returns_no_summaries(self):
        group = _group(travelers=[])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestGroupSeverityAggregation:
    """The group severity is the highest severity found across all travelers and checks."""

    def test_single_info_traveler_sets_info(self):
        info = _traveler(skymiles_number="1000002", passport_expiry=DEPARTURE + timedelta(days=150))
        group = _group(travelers=[_traveler(), info])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.INFO
        assert _summary_for(result, "1000001").passport_result.severity is None
        assert _summary_for(result, "1000002").passport_result.severity == AlertSeverity.INFO

    def test_warning_escalates_over_earlier_info(self):
        info = _traveler(skymiles_number="1000001", passport_expiry=DEPARTURE + timedelta(days=150))
        warning = _traveler(
            skymiles_number="1000002", nationality="IN",
            visa_records=[_visa("DE", DEPARTURE)],
        )
        group = _group(travelers=[info, warning])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_escalates_over_earlier_warning(self):
        warning = _traveler(
            skymiles_number="1000001", nationality="IN",
            visa_records=[_visa("DE", DEPARTURE)],
        )
        critical = _traveler(skymiles_number="1000002", nationality="IN")
        group = _group(travelers=[warning, critical])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_later_lower_severity_does_not_downgrade(self):
        critical = _traveler(skymiles_number="1000001", nationality="IN")
        info = _traveler(skymiles_number="1000002", passport_expiry=DEPARTURE + timedelta(days=150))
        group = _group(travelers=[critical, info])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity == AlertSeverity.CRITICAL
        assert _summary_for(result, "1000002").passport_result.severity == AlertSeverity.INFO

    def test_visa_severity_outranks_same_travelers_passport_severity(self):
        traveler = _traveler(nationality="IN", passport_expiry=DEPARTURE + timedelta(days=150))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_one_noncompliant_traveler_does_not_affect_others(self):
        group = _group(travelers=[
            _traveler(skymiles_number="1000001"),
            _traveler(skymiles_number="1000002", passport_expiry=date(2020, 1, 1)),
        ])

        result = evaluate_group_itinerary(group, _requirements())

        assert _summary_for(result, "1000001").passport_result.is_alert_required is False
        assert _summary_for(result, "1000002").passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL


class TestPassportLimits:
    """Each traveler's passport is checked against the destination's validity window."""

    def test_below_schengen_3_month_requirement_is_critical(self):
        traveler = _traveler(passport_expiry=DEPARTURE + timedelta(days=60))
        group = _group(segments=[_segment("DE")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_meets_schengen_but_under_6_months_is_info(self):
        traveler = _traveler(passport_expiry=DEPARTURE + timedelta(days=120))
        group = _group(segments=[_segment("DE")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.INFO
        assert any("expiring within 6 months" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.INFO

    def test_same_passport_fails_6_month_country(self):
        traveler = _traveler(passport_expiry=DEPARTURE + timedelta(days=120))
        group = _group(segments=[_segment("JP")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert any("does not meet JP's 6-month" in r for r in passport.reasons)

    def test_passport_expires_before_departure_is_critical(self):
        traveler = _traveler(passport_expiry=DEPARTURE - timedelta(days=1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert any("expires before departure on DL100 to DE" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expired_passport_is_critical(self):
        traveler = _traveler(passport_expiry=date(2020, 1, 1))
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_missing_passport_is_critical(self):
        traveler = _traveler(passport_number=None)
        group = _group(travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert passport.severity == AlertSeverity.CRITICAL
        assert passport.validation_errors[0].field == "passport_number"
        assert result.group_severity == AlertSeverity.CRITICAL


class TestVisaLimits:
    """Visa rules are applied per traveler nationality against each destination."""

    def test_exempt_nationality_needs_no_visa(self):
        group = _group(segments=[_segment("DE")], travelers=[_traveler(nationality="US")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_mixed_nationalities_only_flag_non_exempt_traveler(self):
        group = _group(segments=[_segment("DE")], travelers=[
            _traveler(skymiles_number="1000001", nationality="US"),
            _traveler(skymiles_number="1000002", nationality="IN"),
        ])

        result = evaluate_group_itinerary(group, _requirements())

        assert _summary_for(result, "1000001").visa_result.reasons == []
        assert _summary_for(result, "1000002").visa_result.reasons == ["No visa on file for DE"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_country_without_visa_requirement_is_skipped(self):
        group = _group(segments=[_segment("MX")], travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False

    def test_visa_expires_on_travel_date_is_warning(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN", DEPARTURE)])
        group = _group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.reasons == [
            "Visa for CN expires on date of travel"
        ]
        assert result.group_severity == AlertSeverity.WARNING

    def test_visa_expires_before_travel_date_is_critical(self):
        traveler = _traveler(
            nationality="IN", visa_records=[_visa("CN", DEPARTURE - timedelta(days=1))],
        )
        group = _group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.reasons == [
            "Visa for CN expires before date of travel"
        ]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expired_before_today_is_critical(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa("CN", date(2020, 1, 1))])
        group = _group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.reasons == ["Visa for CN has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_visa_expiring_within_warning_window_is_info(self):
        traveler = _traveler(
            nationality="IN", visa_records=[_visa("CN", DEPARTURE + timedelta(days=10))],
        )
        group = _group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.severity == AlertSeverity.INFO
        assert result.group_severity == AlertSeverity.INFO

    def test_latest_matching_visa_is_selected(self):
        traveler = _traveler(nationality="IN", visa_records=[
            _visa("CN", date(2020, 1, 1), visa_number="OLD"),
            _visa("CN", FAR_FUTURE_EXPIRY, visa_number="NEW"),
        ])
        group = _group(segments=[_segment("CN")], travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None


class TestLayovers:
    """Layover segments skip visa checks unless a transit visa is required."""

    def test_layover_without_transit_visa_requirement_is_skipped(self):
        segments = [
            _segment("JP", is_layover=True, flight_number="DL100"),
            _segment("MX", flight_number="DL200"),
        ]
        group = _group(segments=segments, travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None

    def test_layover_requiring_transit_visa_is_critical(self):
        segments = [
            _segment("CN", is_layover=True, flight_number="DL100"),
            _segment("MX", flight_number="DL200"),
        ]
        group = _group(segments=segments, travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_still_enforces_passport_validity(self):
        segments = [
            _segment("JP", is_layover=True, flight_number="DL100"),
            _segment("DE", flight_number="DL200"),
        ]
        traveler = _traveler(passport_expiry=DEPARTURE + timedelta(days=120))
        group = _group(segments=segments, travelers=[traveler])

        result = evaluate_group_itinerary(group, _requirements())

        passport = result.traveler_summaries[0].passport_result
        assert any("does not meet JP's 6-month" in r and "DL100" in r for r in passport.reasons)
        assert result.group_severity == AlertSeverity.CRITICAL


class TestItineraryChanges:
    """Re-evaluating a changed itinerary reflects the new segments for every traveler."""

    def test_later_departure_pushes_passport_out_of_validity(self):
        traveler = _traveler(passport_expiry=DEPARTURE + timedelta(days=200))
        original = _group(segments=[_segment("JP", departure=DEPARTURE)], travelers=[traveler])
        changed = _group(
            segments=[_segment("JP", departure=DEPARTURE + timedelta(days=60))],
            travelers=[traveler],
        )

        before = evaluate_group_itinerary(original, _requirements())
        after = evaluate_group_itinerary(changed, _requirements())

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_added_destination_introduces_visa_alert(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", FAR_FUTURE_EXPIRY)])
        original = _group(segments=[_segment("DE")], travelers=[traveler])
        changed = _group(
            segments=[_segment("DE"), _segment("CN", flight_number="DL200")],
            travelers=[traveler],
        )

        before = evaluate_group_itinerary(original, _requirements())
        after = evaluate_group_itinerary(changed, _requirements())

        assert before.group_severity is None
        assert after.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_removed_destination_clears_alert(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa("DE", FAR_FUTURE_EXPIRY)])
        original = _group(
            segments=[_segment("DE"), _segment("CN", flight_number="DL200")],
            travelers=[traveler],
        )
        changed = _group(segments=[_segment("DE")], travelers=[traveler])

        before = evaluate_group_itinerary(original, _requirements())
        after = evaluate_group_itinerary(changed, _requirements())

        assert before.group_severity == AlertSeverity.CRITICAL
        assert after.group_severity is None


class TestRequirementsLookup:
    """Without explicit requirements, each destination is looked up from the default data."""

    def test_defaults_used_when_requirements_is_none(self, default_requirements):
        group = _group(segments=[_segment("CN")], travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_repeated_destination_is_looked_up_once(self, default_requirements):
        segments = [
            _segment("DE", flight_number="DL100"),
            _segment("DE", departure=DEPARTURE + timedelta(days=7), flight_number="DL200"),
        ]
        group = _group(segments=segments, travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group)

        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for DE", "No visa on file for DE",
        ]

    def test_unknown_country_falls_back_to_default_requirements(self, default_requirements):
        traveler = _traveler(passport_expiry=DEPARTURE + timedelta(days=120))
        group = _group(segments=[_segment("ZZ")], travelers=[traveler])

        result = evaluate_group_itinerary(group)

        summary = result.traveler_summaries[0]
        assert any("does not meet ZZ's 6-month" in r for r in summary.passport_result.reasons)
        assert summary.visa_result.reasons == ["No visa on file for ZZ"]

    def test_explicit_requirements_take_precedence_over_defaults(self, default_requirements):
        relaxed = {
            "CN": TravelDocRequirements(
                country_code="CN",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            ),
        }
        group = _group(segments=[_segment("CN")], travelers=[_traveler(nationality="IN")])

        result = evaluate_group_itinerary(group, relaxed)

        assert result.traveler_summaries[0].visa_result.is_alert_required is False
        assert result.group_severity is None
