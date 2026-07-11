"""Tests for group itinerary evaluation."""

from dataclasses import replace

import src.evaluators.group_evaluator as group_evaluator
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    GroupItinerary,
    Itinerary,
    PassportEvaluation,
    SkyMilesProfile,
    VisaEvaluation,
)

from tests.conftest import make_profile, make_segment


def _make_group(
    *travelers: SkyMilesProfile,
    segments: list[FlightSegment] | None = None,
) -> GroupItinerary:
    group_segments = segments or [make_segment()]
    primary_traveler = travelers[0].skymiles_number if travelers else ""
    return GroupItinerary(
        confirmation_number="DL-GROUP",
        segments=group_segments,
        travelers=list(travelers),
        primary_traveler=primary_traveler,
    )


def _passport_result(
    profile: SkyMilesProfile,
    itinerary: Itinerary,
    severity: AlertSeverity | None = None,
) -> PassportEvaluation:
    return PassportEvaluation(
        profile=profile,
        segments_evaluated=itinerary.segments,
        is_alert_required=severity is not None,
        severity=severity,
        reasons=[],
        validation_errors=[],
    )


def _visa_result(
    profile: SkyMilesProfile,
    itinerary: Itinerary,
    severity: AlertSeverity | None = None,
) -> VisaEvaluation:
    return VisaEvaluation(
        profile=profile,
        segments_evaluated=itinerary.segments,
        is_alert_required=severity is not None,
        severity=severity,
        reasons=[],
        validation_errors=[],
    )


def _install_evaluator_results(
    monkeypatch,
    passport_severities: dict[str, AlertSeverity | None],
    visa_severities: dict[str, AlertSeverity | None],
) -> None:
    def evaluate_passport(profile, itinerary, requirements):
        return _passport_result(
            profile,
            itinerary,
            passport_severities[profile.skymiles_number],
        )

    def evaluate_visa(profile, itinerary, requirements):
        return _visa_result(
            profile,
            itinerary,
            visa_severities[profile.skymiles_number],
        )

    monkeypatch.setattr(
        group_evaluator,
        "evaluate_passport_expiry",
        evaluate_passport,
    )
    monkeypatch.setattr(
        group_evaluator,
        "evaluate_visa_requirements",
        evaluate_visa,
    )


class TestDefaultRequirementsConstruction:
    """Default requirements are built once per destination and shared."""

    def test_builds_unique_destination_requirements(
        self,
        monkeypatch,
        base_requirements,
    ):
        traveler = make_profile()
        segments = [
            make_segment(destination="DE", flight_number="DL100"),
            make_segment(destination="DE", flight_number="DL101"),
            make_segment(destination="CN", flight_number="DL200"),
        ]
        group = _make_group(traveler, segments=segments)
        lookup_calls = []
        evaluator_calls = []

        def get_requirements(country_code):
            lookup_calls.append(country_code)
            return base_requirements[country_code]

        def evaluate_passport(profile, itinerary, requirements):
            evaluator_calls.append(("passport", itinerary, requirements))
            return _passport_result(profile, itinerary)

        def evaluate_visa(profile, itinerary, requirements):
            evaluator_calls.append(("visa", itinerary, requirements))
            return _visa_result(profile, itinerary)

        monkeypatch.setattr(group_evaluator, "get_requirements", get_requirements)
        monkeypatch.setattr(
            group_evaluator,
            "evaluate_passport_expiry",
            evaluate_passport,
        )
        monkeypatch.setattr(
            group_evaluator,
            "evaluate_visa_requirements",
            evaluate_visa,
        )

        result = evaluate_group_itinerary(group)

        built_itinerary = evaluator_calls[0][1]
        built_requirements = evaluator_calls[0][2]
        assert lookup_calls == ["DE", "CN"]
        assert [call[0] for call in evaluator_calls] == ["passport", "visa"]
        assert built_itinerary.confirmation_number == "DL-GROUP"
        assert built_itinerary.segments == segments
        assert evaluator_calls[1][1] is built_itinerary
        assert built_requirements == {
            "DE": base_requirements["DE"],
            "CN": base_requirements["CN"],
        }
        assert evaluator_calls[1][2] is built_requirements
        assert result.group_severity is None


class TestTravelerSummaries:
    """Each traveler receives evaluations and an identity-preserving summary."""

    def test_returns_one_summary_per_traveler(
        self,
        monkeypatch,
        base_requirements,
    ):
        first_traveler = replace(
            make_profile(),
            skymiles_number="1111111",
            first_name="Ada",
            last_name="Lovelace",
        )
        second_traveler = replace(
            make_profile(),
            skymiles_number="2222222",
            first_name="Grace",
            last_name="Hopper",
        )
        group = _make_group(first_traveler, second_traveler)
        evaluator_calls = []

        def evaluate_passport(profile, itinerary, requirements):
            evaluator_calls.append(("passport", profile, requirements))
            return _passport_result(profile, itinerary)

        def evaluate_visa(profile, itinerary, requirements):
            evaluator_calls.append(("visa", profile, requirements))
            return _visa_result(profile, itinerary)

        def unexpected_lookup(country_code):
            raise AssertionError(f"Unexpected default lookup for {country_code}")

        monkeypatch.setattr(group_evaluator, "get_requirements", unexpected_lookup)
        monkeypatch.setattr(
            group_evaluator,
            "evaluate_passport_expiry",
            evaluate_passport,
        )
        monkeypatch.setattr(
            group_evaluator,
            "evaluate_visa_requirements",
            evaluate_visa,
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-GROUP"
        assert [
            (
                summary.skymiles_number,
                summary.first_name,
                summary.last_name,
            )
            for summary in result.traveler_summaries
        ] == [
            ("1111111", "Ada", "Lovelace"),
            ("2222222", "Grace", "Hopper"),
        ]
        assert [
            (call[0], call[1].skymiles_number)
            for call in evaluator_calls
        ] == [
            ("passport", "1111111"),
            ("visa", "1111111"),
            ("passport", "2222222"),
            ("visa", "2222222"),
        ]
        assert all(call[2] is base_requirements for call in evaluator_calls)
        assert result.traveler_summaries[0].passport_result.profile is first_traveler
        assert result.traveler_summaries[1].visa_result.profile is second_traveler


class TestAggregateSeverity:
    """Group severity is the highest traveler document severity."""

    def test_clean_results_leave_group_severity_unset(
        self,
        monkeypatch,
        base_requirements,
    ):
        traveler = make_profile()
        group = _make_group(traveler)
        _install_evaluator_results(
            monkeypatch,
            {traveler.skymiles_number: None},
            {traveler.skymiles_number: None},
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None

    def test_warning_replaces_info(
        self,
        monkeypatch,
        base_requirements,
    ):
        traveler = make_profile()
        group = _make_group(traveler)
        _install_evaluator_results(
            monkeypatch,
            {traveler.skymiles_number: AlertSeverity.INFO},
            {traveler.skymiles_number: AlertSeverity.WARNING},
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING

    def test_critical_wins_across_travelers_and_is_not_downgraded(
        self,
        monkeypatch,
        base_requirements,
    ):
        first_traveler = replace(make_profile(), skymiles_number="1111111")
        second_traveler = replace(make_profile(), skymiles_number="2222222")
        group = _make_group(first_traveler, second_traveler)
        _install_evaluator_results(
            monkeypatch,
            {
                first_traveler.skymiles_number: AlertSeverity.WARNING,
                second_traveler.skymiles_number: AlertSeverity.CRITICAL,
            },
            {
                first_traveler.skymiles_number: None,
                second_traveler.skymiles_number: AlertSeverity.INFO,
            },
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
