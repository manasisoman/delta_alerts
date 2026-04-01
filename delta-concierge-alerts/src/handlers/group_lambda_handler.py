"""AWS Lambda handler for group itinerary evaluations in Delta Concierge Alerts."""

import uuid
from datetime import date, datetime

from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.handlers.lambda_handler import _compute_ttl, _parse_profile
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
    FlightSegment,
    GroupItinerary,
    Itinerary,
    NotificationPayload,
    SkyMilesProfile,
    TravelDocRequirements,
)
from src.services.alert_store import save_alert
from src.services.notification_service import (
    send_group_notification,
    send_push_notification,
)


def group_handler(event: dict, context: object) -> dict:
    """Lambda entry point for evaluating group travel document alerts.

    Deserializes the incoming event into a GroupItinerary with multiple traveler
    profiles, runs evaluations for all travelers, persists per-traveler alerts
    to DynamoDB, and sends push notifications for actionable alerts.

    Args:
        event: The Lambda event payload containing travelers and segments.
        context: The Lambda context object (unused).

    Returns:
        A response dict with statusCode and a body summarizing per-traveler results.
    """
    group = _parse_group_itinerary(event)

    # Build requirements dict, merging defaults with any overrides
    requirements = dict(COUNTRY_REQUIREMENTS)
    overrides = event.get("requirements_override")
    if overrides:
        for code, reqs_data in overrides.items():
            requirements[code] = TravelDocRequirements(
                country_code=reqs_data.get("country_code", code),
                requires_visa=reqs_data.get("requires_visa", True),
                transit_visa_required=reqs_data.get("transit_visa_required", False),
                passport_validity_months=reqs_data.get("passport_validity_months", 6),
                visa_exempt_nationalities=reqs_data.get("visa_exempt_nationalities", []),
            )

    group_result = evaluate_group_itinerary(group, requirements)

    # Compute TTL from the shared itinerary
    itinerary = Itinerary(
        confirmation_number=group.confirmation_number,
        segments=group.segments,
    )
    ttl = _compute_ttl(itinerary)

    alerts_sent = 0
    traveler_results = []

    for summary in group_result.traveler_summaries:
        traveler = _find_traveler(group, summary.skymiles_number)
        traveler_alerts = 0

        # Process passport evaluation
        if summary.passport_result.is_alert_required and summary.passport_result.severity is not None:
            alert_record = AlertRecord(
                alert_id=str(uuid.uuid4()),
                skymiles_number=summary.skymiles_number,
                alert_type=AlertType.PASSPORT,
                severity=summary.passport_result.severity,
                reasons=summary.passport_result.reasons,
                created_at=datetime.utcnow().isoformat(),
                itinerary_ref=group.confirmation_number,
                status=AlertStatus.ACTIVE,
                ttl=ttl,
            )
            save_alert(alert_record)

            if traveler is not None:
                notification = NotificationPayload(
                    endpoint_arn=traveler.endpoint_arn,
                    alert_record=alert_record,
                    title="Delta Concierge \u2014 Passport Alert",
                    body="; ".join(summary.passport_result.reasons),
                    push_data={
                        "alert_type": AlertType.PASSPORT.value,
                        "severity": summary.passport_result.severity.value,
                        "confirmation_number": group.confirmation_number,
                    },
                )
                send_push_notification(notification)

            alerts_sent += 1
            traveler_alerts += 1

        # Process visa evaluation
        if summary.visa_result.is_alert_required and summary.visa_result.severity is not None:
            alert_record = AlertRecord(
                alert_id=str(uuid.uuid4()),
                skymiles_number=summary.skymiles_number,
                alert_type=AlertType.VISA,
                severity=summary.visa_result.severity,
                reasons=summary.visa_result.reasons,
                created_at=datetime.utcnow().isoformat(),
                itinerary_ref=group.confirmation_number,
                status=AlertStatus.ACTIVE,
                ttl=ttl,
            )
            save_alert(alert_record)

            if traveler is not None:
                notification = NotificationPayload(
                    endpoint_arn=traveler.endpoint_arn,
                    alert_record=alert_record,
                    title="Delta Concierge \u2014 Visa Alert",
                    body="; ".join(summary.visa_result.reasons),
                    push_data={
                        "alert_type": AlertType.VISA.value,
                        "severity": summary.visa_result.severity.value,
                        "confirmation_number": group.confirmation_number,
                    },
                )
                send_push_notification(notification)

            alerts_sent += 1
            traveler_alerts += 1

        passport_status = (
            summary.passport_result.severity.value
            if summary.passport_result.severity
            else "OK"
        )
        visa_status = (
            summary.visa_result.severity.value
            if summary.visa_result.severity
            else "OK"
        )
        traveler_results.append({
            "skymiles_number": summary.skymiles_number,
            "first_name": summary.first_name,
            "last_name": summary.last_name,
            "passport_status": passport_status,
            "visa_status": visa_status,
            "alerts_sent": traveler_alerts,
        })

    # Send a group summary notification to the primary traveler
    primary_traveler = _find_traveler(group, group.primary_traveler)
    if primary_traveler is not None:
        send_group_notification(primary_traveler.endpoint_arn, group_result)

    return {
        "statusCode": 200,
        "body": {
            "confirmation_number": group.confirmation_number,
            "total_alerts_sent": alerts_sent,
            "group_severity": group_result.group_severity.value,
            "traveler_results": traveler_results,
        },
    }


def _parse_group_itinerary(event: dict) -> GroupItinerary:
    """Deserialize a group itinerary from the Lambda event payload."""
    segments = [
        FlightSegment(
            flight_number=seg["flight_number"],
            origin=seg["origin"],
            destination=seg["destination"],
            departure_date=date.fromisoformat(seg["departure_date"]),
            arrival_date=date.fromisoformat(seg["arrival_date"]),
            is_layover=seg.get("is_layover", False),
        )
        for seg in event["segments"]
    ]

    travelers = [_parse_profile(t) for t in event["travelers"]]

    return GroupItinerary(
        confirmation_number=event["confirmation_number"],
        segments=segments,
        travelers=travelers,
        primary_traveler=event["primary_traveler"],
    )


def _find_traveler(
    group: GroupItinerary, skymiles_number: str
) -> SkyMilesProfile | None:
    """Find a traveler in the group by their SkyMiles number."""
    for traveler in group.travelers:
        if traveler.skymiles_number == skymiles_number:
            return traveler
    return None
