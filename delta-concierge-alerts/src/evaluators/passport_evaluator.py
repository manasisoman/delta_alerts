"""Passport expiry evaluation logic for Delta Concierge Alerts."""

from datetime import date
from dateutil.relativedelta import relativedelta

from src.data.country_requirements import get_requirements
from src.models.types import (
    AlertSeverity,
    FlightSegment,
    Itinerary,
    PassportEvaluation,
    SkyMilesProfile,
    TravelDocRequirements,
    ValidationError,
)


_SEVERITY_RANK = {
    AlertSeverity.INFO: 1,
    AlertSeverity.WARNING: 2,
    AlertSeverity.CRITICAL: 3,
}


def evaluate_passport_expiry(
    profile: SkyMilesProfile,
    itinerary: Itinerary,
    requirements: dict[str, TravelDocRequirements],
) -> PassportEvaluation:
    """Evaluate passport validity against every segment in an itinerary.

    Iterates over all segments (including layovers) and checks whether the
    traveler's passport meets each destination country's validity requirements.

    Args:
        profile: The SkyMiles member's profile with passport details.
        itinerary: The travel itinerary to evaluate.
        requirements: Mapping of country codes to TravelDocRequirements.
            Falls back to the default from country_requirements if a
            destination is not present.

    Returns:
        A PassportEvaluation with consolidated severity and reasons.
    """
    reasons: list[str] = []
    validation_errors: list[ValidationError] = []
    highest_severity: AlertSeverity | None = None
    today = date.today()

    # Check for missing passport information
    if profile.passport_number is None or profile.passport_expiry is None:
        return PassportEvaluation(
            profile=profile,
            segments_evaluated=itinerary.segments,
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport information is incomplete"],
            validation_errors=[
                ValidationError(
                    field="passport_number" if profile.passport_number is None else "passport_expiry",
                    message="Passport information is incomplete",
                    code="MISSING_PASSPORT_INFO",
                )
            ],
        )

    # Check if passport has already expired
    if profile.passport_expiry < today:
        return PassportEvaluation(
            profile=profile,
            segments_evaluated=itinerary.segments,
            is_alert_required=True,
            severity=AlertSeverity.CRITICAL,
            reasons=["Passport has expired"],
            validation_errors=[],
        )

    for segment in itinerary.segments:
        destination = segment.destination
        country_reqs = requirements.get(destination) or get_requirements(destination)
        required_months = country_reqs.passport_validity_months

        # Passport expires before departure
        if profile.passport_expiry < segment.departure_date:
            reasons.append(
                f"Passport expires before departure on {segment.flight_number} to {destination}"
            )
            highest_severity = _max_severity(highest_severity, AlertSeverity.CRITICAL)
            continue

        # Calculate months of validity remaining at time of travel
        validity_threshold = segment.departure_date + relativedelta(months=required_months)

        if profile.passport_expiry < validity_threshold:
            reasons.append(
                f"Passport does not meet {destination}'s {required_months}-month "
                f"validity requirement for {segment.flight_number}"
            )
            highest_severity = _max_severity(highest_severity, AlertSeverity.WARNING)
            continue

        # Passport is valid but expiring within 6 months of travel
        six_month_threshold = segment.departure_date + relativedelta(months=6)
        if profile.passport_expiry < six_month_threshold:
            reasons.append(
                f"Passport expiring within 6 months — consider renewing before "
                f"{segment.flight_number} to {destination}"
            )
            highest_severity = _max_severity(highest_severity, AlertSeverity.INFO)

    is_alert_required = len(reasons) > 0

    return PassportEvaluation(
        profile=profile,
        segments_evaluated=itinerary.segments,
        is_alert_required=is_alert_required,
        severity=highest_severity,
        reasons=reasons,
        validation_errors=validation_errors,
    )


def _max_severity(
    current: AlertSeverity | None, new: AlertSeverity
) -> AlertSeverity:
    """Return the higher severity between the current and new values."""
    if current is None:
        return new
    if _SEVERITY_RANK[new] > _SEVERITY_RANK[current]:
        return new
    return current
