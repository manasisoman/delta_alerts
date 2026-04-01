"""Group itinerary evaluation logic for Delta Concierge Alerts."""

from src.data.country_requirements import get_requirements
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import (
    AlertSeverity,
    GroupEvaluationResult,
    GroupItinerary,
    Itinerary,
    TravelerAlertSummary,
    TravelDocRequirements,
)


_SEVERITY_RANK = {
    AlertSeverity.INFO: 1,
    AlertSeverity.WARNING: 2,
    AlertSeverity.CRITICAL: 3,
}


def evaluate_group_itinerary(
    group: GroupItinerary,
    requirements: dict[str, TravelDocRequirements] | None = None,
) -> GroupEvaluationResult:
    """Evaluate travel document requirements for all travelers in a group.

    For each traveler, runs passport and visa evaluations against all segments.
    Returns per-traveler results plus an aggregate group severity (the highest
    severity found across all travelers).

    Args:
        group: The group itinerary containing shared segments and all travelers.
        requirements: Optional mapping of country codes to TravelDocRequirements.
            When ``None``, requirements are looked up from the default data.

    Returns:
        A GroupEvaluationResult with per-traveler summaries and the highest
        severity across the entire group.
    """
    summaries: list[TravelerAlertSummary] = []
    highest_severity = AlertSeverity.INFO

    # Build a shared itinerary object for the segments
    itinerary = Itinerary(
        confirmation_number=group.confirmation_number,
        segments=group.segments,
    )

    # Build requirements dict from default data if not provided
    if requirements is None:
        requirements = {}
        for segment in group.segments:
            dest = segment.destination
            if dest not in requirements:
                requirements[dest] = get_requirements(dest)

    for traveler in group.travelers:
        passport_result = evaluate_passport_expiry(traveler, itinerary, requirements)
        visa_result = evaluate_visa_requirements(traveler, itinerary, requirements)

        # Track highest severity across all travelers
        for result in [passport_result, visa_result]:
            if result.severity is not None:
                if _SEVERITY_RANK.get(result.severity, 0) > _SEVERITY_RANK.get(highest_severity, 0):
                    highest_severity = result.severity

        summaries.append(TravelerAlertSummary(
            skymiles_number=traveler.skymiles_number,
            first_name=traveler.first_name,
            last_name=traveler.last_name,
            passport_result=passport_result,
            visa_result=visa_result,
        ))

    return GroupEvaluationResult(
        confirmation_number=group.confirmation_number,
        traveler_summaries=summaries,
        group_severity=highest_severity,
    )
