"""AWS Lambda handler for Bedrock Agent Action Group invocations.

This handler is invoked by an Amazon Bedrock Agent when it decides to
call one of the action-group functions defined in the OpenAPI schema.
It parses the incoming request, dispatches to the appropriate service
function, and returns a response in the format Bedrock Agents expect.
"""

import json

from src.handlers.lambda_handler import evaluate_itinerary
from src.services.alert_store import (
    acknowledge_alert,
    get_active_alerts,
    resolve_alert,
)


def handler(event: dict, context: object) -> dict:
    """Bedrock Agent Action Group Lambda entry point.

    Bedrock Agents invoke this Lambda with a structured event containing
    ``apiPath`` and ``parameters`` (or ``requestBody``).  The handler
    maps the API path to the correct business-logic function and returns
    a well-formed Action Group response.

    Args:
        event: The Bedrock Agent action group invocation event.
        context: The Lambda context object (unused).

    Returns:
        A dict conforming to the Bedrock Agent Action Group response schema.
    """
    api_path = event.get("apiPath", "")
    http_method = event.get("httpMethod", "GET")
    parameters = _extract_parameters(event)
    request_body = _extract_request_body(event)

    if api_path == "/getActiveAlerts" and http_method == "GET":
        return _handle_get_active_alerts(parameters)

    if api_path == "/acknowledgeAlert" and http_method == "POST":
        return _handle_acknowledge_alert(request_body)

    if api_path == "/resolveAlert" and http_method == "POST":
        return _handle_resolve_alert(request_body)

    if api_path == "/evaluateItinerary" and http_method == "POST":
        return _handle_evaluate_itinerary(request_body)

    return _build_response(
        event,
        400,
        {"error": f"Unknown action: {http_method} {api_path}"},
    )


# ------------------------------------------------------------------
# Action implementations
# ------------------------------------------------------------------

def _handle_get_active_alerts(params: dict) -> dict:
    skymiles_number = params.get("skymiles_number", "")
    if not skymiles_number:
        return _build_action_response(400, {"error": "skymiles_number is required"})

    alerts = get_active_alerts(skymiles_number)
    return _build_action_response(200, {"alerts": alerts, "count": len(alerts)})


def _handle_acknowledge_alert(body: dict) -> dict:
    skymiles_number = body.get("skymiles_number", "")
    alert_id = body.get("alert_id", "")
    if not skymiles_number or not alert_id:
        return _build_action_response(
            400, {"error": "skymiles_number and alert_id are required"}
        )

    acknowledge_alert(skymiles_number, alert_id)
    return _build_action_response(
        200, {"message": f"Alert {alert_id} acknowledged"}
    )


def _handle_resolve_alert(body: dict) -> dict:
    skymiles_number = body.get("skymiles_number", "")
    alert_id = body.get("alert_id", "")
    resolution = body.get("resolution", "")
    if not skymiles_number or not alert_id or not resolution:
        return _build_action_response(
            400,
            {"error": "skymiles_number, alert_id, and resolution are required"},
        )

    resolve_alert(skymiles_number, alert_id, resolution)
    return _build_action_response(
        200, {"message": f"Alert {alert_id} resolved"}
    )


def _handle_evaluate_itinerary(body: dict) -> dict:
    if "profile" not in body or "itinerary" not in body:
        return _build_action_response(
            400, {"error": "profile and itinerary are required in the request body"}
        )

    result = evaluate_itinerary(body)
    return _build_action_response(200, result.get("body", {}))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _extract_parameters(event: dict) -> dict:
    """Extract path/query parameters from the Bedrock Agent event."""
    params: dict = {}
    for param in event.get("parameters", []):
        params[param["name"]] = param["value"]
    return params


def _extract_request_body(event: dict) -> dict:
    """Extract the JSON request body from the Bedrock Agent event."""
    request_body = event.get("requestBody", {})
    content = request_body.get("content", {})
    json_body = content.get("application/json", {})
    properties = json_body.get("properties", [])

    body: dict = {}
    for prop in properties:
        body[prop["name"]] = prop["value"]
    return body


def _build_action_response(status_code: int, body: dict) -> dict:
    """Build a response in the format Bedrock Agents expect."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": "DeltaConciergeActions",
            "apiPath": "",
            "httpMethod": "",
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body),
                }
            },
        },
    }


def _build_response(event: dict, status_code: int, body: dict) -> dict:
    """Build a response preserving the original event's action group metadata."""
    return {
        "messageVersion": "1.0",
        "response": {
            "actionGroup": event.get("actionGroup", ""),
            "apiPath": event.get("apiPath", ""),
            "httpMethod": event.get("httpMethod", ""),
            "httpStatusCode": status_code,
            "responseBody": {
                "application/json": {
                    "body": json.dumps(body),
                }
            },
        },
    }
