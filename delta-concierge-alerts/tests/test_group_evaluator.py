"""Tests for group itinerary evaluation logic."""

from datetime import date

import pytest

import src.data.country_requirements as country_requirements
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)

DEPARTURE = date(2031, 1, 1)
ARRIVAL = date(2031, 1, 2)


def _requirements(**overrides) -> dict[str, TravelDocRequirements]:
    reqs = {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB", "CA"],
        ),
        "FR": TravelDocRequirements(
            country_code="FR",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        ),
    }
    reqs.update(overrides)
    return reqs


def _segment(
    destination: str = "DE",
    departure: date = DEPARTURE,
    arrival: date = ARRIVAL,
    is_layover: bool = False,
    flight_number: str = "DL100",
) -> FlightSegment:
    return FlightSegment(
        flight_number=flight_number,
        origin="ATL",
        destination=destination,
        departure_date=departure,
        arrival_date=arrival,
        is_layover=is_layover,
    )


def _traveler(
    skymiles_number: str = "1000001",
    first_name: str = "Ada",
    last_name: str = "Lovelace",
    nationality: str = "US",
    passport_number: str | None = "P123456",
    passport_expiry: date | None = date(2035, 1, 1),
    visa_records: list[VisaRecord] | None = None,
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


def _visa(country_code: str = "DE", expiry: date = date(2035, 1, 1)) -> VisaRecord:
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2029, 1, 1),
        expiry_date=expiry,
        visa_number="V0001",
    )


def _group(
    travelers: list[SkyMilesProfile] | None = None,
    segments: list[FlightSegment] | None = None,
    confirmation_number: str = "DL-GROUP",
) -> GroupItinerary:
    travelers = [_traveler()] if travelers is None else travelers
    return GroupItinerary(
        confirmation_number=confirmation_number,
        segments=[_segment()] if segments is None else segments,
        travelers=travelers,
        primary_traveler=travelers[0].skymiles_number if travelers else "",
    )


# Travelers producing a single known severity against _requirements() / DE
def _clean_traveler(n: str = "1") -> SkyMilesProfile:
    return _traveler(skymiles_number=n)


def _info_traveler(n: str = "2") -> SkyMilesProfile:
    # Meets DE 3-month rule but expires within 6 months of travel
    return _traveler(skymiles_number=n, passport_expiry=date(2031, 5, 1))


def _warning_traveler(n: str = "3") -> SkyMilesProfile:
    # Non-exempt nationality with visa expiring on departure date
    return _traveler(skymiles_number=n, nationality="IN", visa_records=[_visa(expiry=DEPARTURE)])


def _critical_traveler(n: str = "4") -> SkyMilesProfile:
    # Non-exempt nationality with no visa on file
    return _traveler(skymiles_number=n, nationality="IN")


@pytest.fixture
def swap_country_requirements():
    """Swap the module-level COUNTRY_REQUIREMENTS and restore it afterwards."""
    original = country_requirements.COUNTRY_REQUIREMENTS

    def _swap(new: dict[str, TravelDocRequirements]) -> None:
        country_requirements.COUNTRY_REQUIREMENTS = new

    try:
        yield _swap
    finally:
        country_requirements.COUNTRY_REQUIREMENTS = original


class TestHappyPath:
    """A fully compliant group yields per-traveler summaries and no group severity."""

    def test_clean_group_has_no_severity(self):
        group = _group(travelers=[_clean_traveler("1"), _clean_traveler("2")])

        result = evaluate_group_itinerary(group, _requirements())

        assert result.group_severity is None
        assert all(not s.passport_result.is_alert_required for s in result.traveler_summaries)
        assert all(not s.visa_result.is_alert_required for s in result.traveler_summaries)

    def test_confirmation_number_propagated(self):
        group = _group(confirmation_number="DL-XYZ123")

        result = evaluate_group_itinerary(group, _requirements())

        assert result.confirmation_number == "DL-XYZ123"

    def test_summary_carries_traveler_identity(self):
        traveler = _traveler(skymiles_number="555", first_name="Grace", last_name="Hopper")

        result = evaluate_group_itinerary(_group(travelers=[traveler]), _requirements())

        summary = result.traveler_summaries[0]
        assert (summary.skymiles_number, summary.first_name, summary.last_name) == ("555", "Grace", "Hopper")
        assert summary.passport_result.profile is traveler
        assert summary.visa_result.profile is traveler

    def test_summaries_preserve_traveler_order(self):
        travelers = [_clean_traveler("a"), _critical_traveler("b"), _info_traveler("c")]

        result = evaluate_group_itinerary(_group(travelers=travelers), _requirements())

        assert [s.skymiles_number for s in result.traveler_summaries] == ["a", "b", "c"]

    def test_all_segments_evaluated_for_each_traveler(self):
        segments = [_segment("DE", flight_number="DL1"), _segment("FR", flight_number="DL2")]

        result = evaluate_group_itinerary(_group(segments=segments), _requirements())

        summary = result.traveler_summaries[0]
        assert summary.passport_result.segments_evaluated == segments
        assert summary.visa_result.segments_evaluated == segments


class TestEmptyGroup:
    """A group with no travelers yields no summaries and no severity."""

    def test_no_travelers(self):
        result = evaluate_group_itinerary(_group(travelers=[]), _requirements())

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestPerTravelerResults:
    """Each traveler is evaluated independently against the shared segments."""

    def test_only_offending_traveler_flagged(self):
        travelers = [_clean_traveler("1"), _critical_traveler("2")]

        result = evaluate_group_itinerary(_group(travelers=travelers), _requirements())

        clean, bad = result.traveler_summaries
        assert not clean.visa_result.is_alert_required
        assert bad.visa_result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in bad.visa_result.reasons

    def test_expired_passport_is_critical(self):
        traveler = _traveler(passport_expiry=date(2020, 1, 1))

        result = evaluate_group_itinerary(_group(travelers=[traveler]), _requirements())

        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_missing_passport_is_critical(self):
        traveler = _traveler(passport_number=None)

        result = evaluate_group_itinerary(_group(travelers=[traveler]), _requirements())

        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_expired_visa_is_critical(self):
        traveler = _traveler(nationality="IN", visa_records=[_visa(expiry=date(2020, 1, 1))])

        result = evaluate_group_itinerary(_group(travelers=[traveler]), _requirements())

        assert "Visa for DE has expired" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_layover_without_transit_visa_requirement_skipped(self):
        segments = [_segment("DE", is_layover=True)]

        result = evaluate_group_itinerary(
            _group(travelers=[_critical_traveler()], segments=segments), _requirements()
        )

        assert not result.traveler_summaries[0].visa_result.is_alert_required
        assert result.group_severity is None

    def test_layover_with_transit_visa_requirement_flagged(self):
        reqs = _requirements(
            CN=TravelDocRequirements(
                country_code="CN",
                requires_visa=True,
                transit_visa_required=True,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            )
        )
        segments = [_segment("CN", is_layover=True)]

        result = evaluate_group_itinerary(_group(travelers=[_traveler()], segments=segments), reqs)

        assert "No visa on file for CN" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL


class TestGroupSeverityAggregation:
    """Group severity is the highest severity across all travelers and checks."""

    @pytest.mark.parametrize(
        "factory, expected",
        [
            (_info_traveler, AlertSeverity.INFO),
            (_warning_traveler, AlertSeverity.WARNING),
            (_critical_traveler, AlertSeverity.CRITICAL),
        ],
    )
    def test_single_traveler_severity(self, factory, expected):
        result = evaluate_group_itinerary(_group(travelers=[factory()]), _requirements())

        assert result.group_severity == expected

    def test_higher_severity_later_replaces_lower(self):
        travelers = [_info_traveler(), _warning_traveler(), _critical_traveler()]

        result = evaluate_group_itinerary(_group(travelers=travelers), _requirements())

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_lower_severity_later_does_not_downgrade(self):
        travelers = [_critical_traveler(), _warning_traveler(), _info_traveler()]

        result = evaluate_group_itinerary(_group(travelers=travelers), _requirements())

        assert result.group_severity == AlertSeverity.CRITICAL

    def test_equal_severity_kept(self):
        travelers = [_warning_traveler("a"), _warning_traveler("b")]

        result = evaluate_group_itinerary(_group(travelers=travelers), _requirements())

        assert result.group_severity == AlertSeverity.WARNING

    def test_clean_traveler_does_not_reset_severity(self):
        travelers = [_warning_traveler(), _clean_traveler()]

        result = evaluate_group_itinerary(_group(travelers=travelers), _requirements())

        assert result.group_severity == AlertSeverity.WARNING

    def test_visa_severity_overrides_lower_passport_severity_same_traveler(self):
        traveler = _traveler(nationality="IN", passport_expiry=date(2031, 5, 1))

        result = evaluate_group_itinerary(_group(travelers=[traveler]), _requirements())

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.INFO
        assert summary.visa_result.severity == AlertSeverity.CRITICAL
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_passport_severity_kept_over_lower_visa_severity_same_traveler(self):
        traveler = _traveler(
            nationality="IN",
            passport_expiry=date(2020, 1, 1),
            visa_records=[_visa(expiry=DEPARTURE)],
        )

        result = evaluate_group_itinerary(_group(travelers=[traveler]), _requirements())

        summary = result.traveler_summaries[0]
        assert summary.passport_result.severity == AlertSeverity.CRITICAL
        assert summary.visa_result.severity == AlertSeverity.WARNING
        assert result.group_severity == AlertSeverity.CRITICAL


class TestDefaultRequirementsLookup:
    """When requirements is None, rules are looked up from COUNTRY_REQUIREMENTS."""

    def test_uses_country_requirements_data(self, swap_country_requirements):
        swap_country_requirements({
            "ZZ": TravelDocRequirements(
                country_code="ZZ",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            )
        })
        group = _group(travelers=[_critical_traveler()], segments=[_segment("ZZ")])

        result = evaluate_group_itinerary(group, None)

        assert result.group_severity is None

    def test_unknown_country_falls_back_to_default(self, swap_country_requirements):
        swap_country_requirements({})
        group = _group(travelers=[_critical_traveler()], segments=[_segment("QQ")])

        result = evaluate_group_itinerary(group)

        assert "No visa on file for QQ" in result.traveler_summaries[0].visa_result.reasons
        assert result.group_severity == AlertSeverity.CRITICAL

    def test_repeated_destination_looked_up_once(self, swap_country_requirements):
        lookups: list[str] = []

        class _CountingDict(dict):
            def get(self, key, default=None):
                lookups.append(key)
                return super().get(key, default)

        swap_country_requirements(_CountingDict(_requirements()))
        segments = [
            _segment("DE", flight_number="DL1"),
            _segment("FR", flight_number="DL2"),
            _segment("DE", departure=date(2031, 2, 1), arrival=date(2031, 2, 2), flight_number="DL3"),
        ]

        result = evaluate_group_itinerary(_group(travelers=[_critical_traveler()], segments=segments))

        assert lookups == ["DE", "FR"]
        assert result.traveler_summaries[0].visa_result.reasons == [
            "No visa on file for DE",
            "No visa on file for DE",
        ]

    def test_explicit_requirements_override_defaults(self, swap_country_requirements):
        swap_country_requirements({})
        reqs = _requirements(
            QQ=TravelDocRequirements(
                country_code="QQ",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=3,
                visa_exempt_nationalities=[],
            )
        )
        group = _group(travelers=[_critical_traveler()], segments=[_segment("QQ")])

        result = evaluate_group_itinerary(group, reqs)

        assert result.group_severity is None
