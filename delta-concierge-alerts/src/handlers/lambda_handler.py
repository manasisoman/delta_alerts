"""AWS Lambda handler for the Delta Concierge Alert system."""

import uuid
from datetime import date, datetime, timedelta

from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import (
    AlertRecord,
    AlertSeverity,
    AlertStatus,
    AlertType,
    FlightSegment,
    Itinerary,
    NotificationPayload,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)
from src.services.alert_store import save_alert
from src.services.notification_service import send_push_notification

_TTL_DAYS_AFTER_ARRIVAL = 30


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for evaluating travel document alerts.

    Deserializes the incoming event into domain objects, runs passport and
    visa evaluations, persists any alerts to DynamoDB, and sends push
    notifications for actionable alerts.

    Args:
        event: The Lambda event payload matching the expected schema.
        context: The Lambda context object (unused).

    Returns:
        A response dict with statusCode and a body summarizing results.
    """
    profile = _parse_profile(event["profile"])
    itinerary = _parse_itinerary(event["itinerary"])

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

    # Run evaluations
    passport_eval = evaluate_passport_expiry(profile, itinerary, requirements)
    visa_eval = evaluate_visa_requirements(profile, itinerary, requirements)

    # Compute TTL: 30 days after the last segment's arrival date
    ttl = _compute_ttl(itinerary)

    alerts_sent = 0

    # Process passport evaluation
    if passport_eval.is_alert_required and passport_eval.severity is not None:
        alert_record = AlertRecord(
            alert_id=str(uuid.uuid4()),
            skymiles_number=profile.skymiles_number,
            alert_type=AlertType.PASSPORT,
            severity=passport_eval.severity,
            reasons=passport_eval.reasons,
            created_at=datetime.utcnow().isoformat(),
            itinerary_ref=itinerary.confirmation_number,
            status=AlertStatus.ACTIVE,
            ttl=ttl,
        )
        save_alert(alert_record)

        notification = NotificationPayload(
            endpoint_arn=profile.endpoint_arn,
            alert_record=alert_record,
            title="Delta Concierge — Passport Alert",
            body="; ".join(passport_eval.reasons),
            push_data={
                "alert_type": AlertType.PASSPORT.value,
                "severity": passport_eval.severity.value,
                "confirmation_number": itinerary.confirmation_number,
            },
        )
        send_push_notification(notification)
        alerts_sent += 1

    # Process visa evaluation
    if visa_eval.is_alert_required and visa_eval.severity is not None:
        alert_record = AlertRecord(
            alert_id=str(uuid.uuid4()),
            skymiles_number=profile.skymiles_number,
            alert_type=AlertType.VISA,
            severity=visa_eval.severity,
            reasons=visa_eval.reasons,
            created_at=datetime.utcnow().isoformat(),
            itinerary_ref=itinerary.confirmation_number,
            status=AlertStatus.ACTIVE,
            ttl=ttl,
        )
        save_alert(alert_record)

        notification = NotificationPayload(
            endpoint_arn=profile.endpoint_arn,
            alert_record=alert_record,
            title="Delta Concierge — Visa Alert",
            body="; ".join(visa_eval.reasons),
            push_data={
                "alert_type": AlertType.VISA.value,
                "severity": visa_eval.severity.value,
                "confirmation_number": itinerary.confirmation_number,
            },
        )
        send_push_notification(notification)
        alerts_sent += 1

    passport_status = passport_eval.severity.value if passport_eval.severity else "OK"
    visa_status = visa_eval.severity.value if visa_eval.severity else "OK"

    return {
        "statusCode": 200,
        "body": {
            "alerts_sent": alerts_sent,
            "passport_status": passport_status,
            "visa_status": visa_status,
        },
    }


def _compute_ttl(itinerary: Itinerary) -> int:
    """Compute a DynamoDB TTL epoch timestamp 30 days after the last arrival."""
    last_arrival = (
        max(seg.arrival_date for seg in itinerary.segments)
        if itinerary.segments
        else date.today()
    )
    expiry_dt = datetime.combine(last_arrival, datetime.min.time()) + timedelta(
        days=_TTL_DAYS_AFTER_ARRIVAL
    )
    return int(expiry_dt.timestamp())


def evaluate_itinerary(event: dict) -> dict:
    """Shared evaluation logic usable by both the Lambda handler and Bedrock Agent.

    Accepts a raw event dict and returns the handler response. This is
    factored out so the Bedrock Action Group handler can reuse it.

    Args:
        event: The Lambda event payload matching the expected schema.

    Returns:
        A response dict with statusCode and a body summarizing results.
    """
    return handler(event, None)


def _parse_profile(data: dict) -> SkyMilesProfile:
    """Deserialize a profile dict from the event payload."""
    visa_records = [
        VisaRecord(
            country_code=vr["country_code"],
            visa_type=vr["visa_type"],
            issue_date=date.fromisoformat(vr["issue_date"]),
            expiry_date=date.fromisoformat(vr["expiry_date"]),
            visa_number=vr["visa_number"],
        )
        for vr in data.get("visa_records", [])
    ]

    passport_expiry = None
    if data.get("passport_expiry"):
        passport_expiry = date.fromisoformat(data["passport_expiry"])

    return SkyMilesProfile(
        skymiles_number=data["skymiles_number"],
        first_name=data["first_name"],
        last_name=data["last_name"],
        nationality=data["nationality"],
        passport_number=data.get("passport_number"),
        passport_expiry=passport_expiry,
        visa_records=visa_records,
        endpoint_arn=data["endpoint_arn"],
    )


def _parse_itinerary(data: dict) -> Itinerary:
    """Deserialize an itinerary dict from the event payload."""
    segments = [
        FlightSegment(
            flight_number=seg["flight_number"],
            origin=seg["origin"],
            destination=seg["destination"],
            departure_date=date.fromisoformat(seg["departure_date"]),
            arrival_date=date.fromisoformat(seg["arrival_date"]),
            is_layover=seg.get("is_layover", False),
        )
        for seg in data["segments"]
    ]

    return Itinerary(
        confirmation_number=data["confirmation_number"],
        segments=segments,
    )
