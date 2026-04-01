"""EventBridge handler for itinerary-change events in the Delta Concierge Alert system."""

import logging

from src.handlers.lambda_handler import evaluate_itinerary
from src.services.alert_store import resolve_alerts_for_itinerary

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def handler(event: dict, context: object) -> dict:
    """Lambda entry point for EventBridge ``ItineraryChanged`` events.

    Receives an EventBridge event envelope, resolves any stale alerts for the
    affected itinerary, and then delegates to :func:`evaluate_itinerary` to
    re-evaluate the updated itinerary against the traveler's documents.

    Expected EventBridge event structure::

        {
            "source": "delta.booking-system",
            "detail-type": "ItineraryChanged",
            "detail": {
                "profile": { ... },
                "itinerary": { ... },
                "change_type": "DATE_CHANGE" | "SEGMENT_ADDED" | "SEGMENT_REMOVED" | "ROUTE_CHANGE"
            }
        }

    Args:
        event: The EventBridge event envelope.
        context: The Lambda context object (unused).

    Returns:
        A response dict with ``statusCode``, count of resolved old alerts,
        and the evaluation result from the re-evaluation.
    """
    detail = event["detail"]
    change_type = detail.get("change_type", "UNKNOWN")

    skymiles_number = detail["profile"]["skymiles_number"]
    confirmation_number = detail["itinerary"]["confirmation_number"]

    logger.info(
        "Processing ItineraryChanged event: change_type=%s, skymiles=%s, confirmation=%s",
        change_type,
        skymiles_number,
        confirmation_number,
    )

    # Resolve stale alerts for this itinerary before re-evaluation
    resolved_count = resolve_alerts_for_itinerary(
        skymiles_number=skymiles_number,
        confirmation_number=confirmation_number,
        resolution="Auto-resolved: itinerary changed, re-evaluating",
    )

    logger.info(
        "Resolved %d stale alert(s) for itinerary %s",
        resolved_count,
        confirmation_number,
    )

    # Re-evaluate the updated itinerary
    evaluation_result = evaluate_itinerary(detail)

    return {
        "statusCode": 200,
        "body": {
            "change_type": change_type,
            "resolved_alerts": resolved_count,
            "evaluation_result": evaluation_result,
        },
    }
