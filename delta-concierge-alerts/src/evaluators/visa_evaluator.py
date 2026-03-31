"""Visa requirements evaluation logic for Delta Concierge Alerts."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.data.country_requirements import get_requirements
from src.models.types import (
    AlertSeverity,
    Itinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaEvaluation,
    VisaRecord,
    ValidationError,
)


_SEVERITY_RANK = {
    AlertSeverity.INFO: 1,
    AlertSeverity.WARNING: 2,
    AlertSeverity.CRITICAL: 3,
}


def evaluate_visa_requirements(
    profile: SkyMilesProfile,
    itinerary: Itinerary,
    requirements: dict[str, TravelDocRequirements],
) -> VisaEvaluation:
    """Evaluate visa requirements against every segment in an itinerary.

    Iterates over all segments and checks whether the traveler has valid
    visas for each destination that requires one.

    Args:
        profile: The SkyMiles member's profile with visa records.
        itinerary: The travel itinerary to evaluate.
        requirements: Mapping of country codes to TravelDocRequirements.
            Falls back to the default from country_requirements if a
            destination is not present.

    Returns:
        A VisaEvaluation with consolidated severity and reasons.
    """
    reasons: list[str] = []
    validation_errors: list[ValidationError] = []
    highest_severity: AlertSeverity | None = None
    today = date.today()

    for segment in itinerary.segments:
        destination = segment.destination
        country_reqs = requirements.get(destination) or get_requirements(destination)

        # Skip if country does not require a visa
        if not country_reqs.requires_visa:
            continue

        # Skip layovers where transit visa is not required
        if segment.is_layover and not country_reqs.transit_visa_required:
            continue

        # Skip if traveler's nationality is visa-exempt
        if profile.nationality in country_reqs.visa_exempt_nationalities:
            continue

        # Find matching visa record
        matching_visa = _find_matching_visa(profile.visa_records, destination)

        if matching_visa is None:
            reasons.append(f"No visa on file for {destination}")
            highest_severity = _max_severity(highest_severity, AlertSeverity.CRITICAL)
            continue

        # Visa already expired
        if matching_visa.expiry_date < today:
            reasons.append(f"Visa for {destination} has expired")
            highest_severity = _max_severity(highest_severity, AlertSeverity.CRITICAL)
            continue

        # Visa expires before travel date
        if matching_visa.expiry_date < segment.departure_date:
            reasons.append(f"Visa for {destination} expires before date of travel")
            highest_severity = _max_severity(highest_severity, AlertSeverity.CRITICAL)
            continue

        # Visa expires on travel date
        if matching_visa.expiry_date == segment.departure_date:
            reasons.append(f"Visa for {destination} expires on date of travel")
            highest_severity = _max_severity(highest_severity, AlertSeverity.WARNING)
            continue

        # Visa expires within warning window of travel date
        warning_threshold = segment.departure_date + timedelta(days=VISA_EXPIRY_WARNING_DAYS)
        if matching_visa.expiry_date <= warning_threshold:
            reasons.append(
                f"Visa for {destination} expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            )
            highest_severity = _max_severity(highest_severity, AlertSeverity.INFO)

    is_alert_required = len(reasons) > 0

    return VisaEvaluation(
        profile=profile,
        segments_evaluated=itinerary.segments,
        is_alert_required=is_alert_required,
        severity=highest_severity,
        reasons=reasons,
        validation_errors=validation_errors,
    )


def _find_matching_visa(
    visa_records: list[VisaRecord], country_code: str
) -> VisaRecord | None:
    """Find the best visa record matching the given country code.

    Returns the record with the latest expiry date to avoid selecting
    an expired visa when a valid one exists.
    """
    matches = [r for r in visa_records if r.country_code == country_code]
    if not matches:
        return None
    return max(matches, key=lambda r: r.expiry_date)


def _max_severity(
    current: AlertSeverity | None, new: AlertSeverity
) -> AlertSeverity:
    """Return the higher severity between the current and new values."""
    if current is None:
        return new
    if _SEVERITY_RANK[new] > _SEVERITY_RANK[current]:
        return new
    return current
