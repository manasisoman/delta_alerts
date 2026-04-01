"""SNS push notification service for the Delta Concierge Alert system."""

import json

import boto3

from src.models.types import AlertSeverity, GroupEvaluationResult, NotificationPayload


sns_client = boto3.client("sns")


def send_push_notification(payload: NotificationPayload) -> dict:
    """Send a push notification via Amazon SNS.

    Publishes a platform-specific message to the endpoint ARN associated
    with the traveler's device. Supports both APNS (iOS) and GCM (Android).

    Args:
        payload: The NotificationPayload containing endpoint ARN,
            alert details, title, body, and custom push data.

    Returns:
        The SNS publish response dict.
    """
    apns_message = json.dumps(
        {
            "aps": {
                "alert": {
                    "title": payload.title,
                    "body": payload.body,
                },
                "sound": "default",
            },
            **payload.push_data,
        }
    )

    gcm_message = json.dumps(
        {
            "notification": {
                "title": payload.title,
                "body": payload.body,
            },
            "data": payload.push_data,
        }
    )

    json_message = json.dumps(
        {
            "default": payload.body,
            "APNS": apns_message,
            "GCM": gcm_message,
        }
    )

    response = sns_client.publish(
        TargetArn=payload.endpoint_arn,
        Message=json_message,
        MessageStructure="json",
    )

    return response


def send_group_notification(
    primary_endpoint_arn: str, group_result: GroupEvaluationResult
) -> dict:
    """Send a summary push notification to the primary traveler about group alerts.

    Summarizes which travelers have issues (e.g., "2 of 4 travelers need action:
    Jane - passport expiring, Tim - visa required for China").  Individual
    travelers who have their own ``endpoint_arn`` should also receive personal
    alerts via the existing per-traveler notification flow.

    Args:
        primary_endpoint_arn: The SNS endpoint ARN of the primary traveler.
        group_result: The aggregated evaluation results for the group.

    Returns:
        The SNS publish response dict.
    """
    action_items: list[str] = []
    for summary in group_result.traveler_summaries:
        issues: list[str] = []
        if (
            summary.passport_result.is_alert_required
            and summary.passport_result.severity is not None
            and summary.passport_result.severity
            in (AlertSeverity.WARNING, AlertSeverity.CRITICAL)
        ):
            issues.append("; ".join(summary.passport_result.reasons))
        if (
            summary.visa_result.is_alert_required
            and summary.visa_result.severity is not None
            and summary.visa_result.severity
            in (AlertSeverity.WARNING, AlertSeverity.CRITICAL)
        ):
            issues.append("; ".join(summary.visa_result.reasons))
        if issues:
            action_items.append(
                f"{summary.first_name} {summary.last_name} - {', '.join(issues)}"
            )

    total = len(group_result.traveler_summaries)
    need_action = len(action_items)

    if need_action == 0:
        body = f"All {total} travelers are cleared for travel."
    else:
        body = (
            f"{need_action} of {total} travelers need action: "
            + "; ".join(action_items)
        )

    title = "Delta Concierge \u2014 Group Travel Alert"

    apns_message = json.dumps(
        {
            "aps": {
                "alert": {"title": title, "body": body},
                "sound": "default",
            },
            "confirmation_number": group_result.confirmation_number,
            "group_severity": group_result.group_severity.value,
        }
    )

    gcm_message = json.dumps(
        {
            "notification": {"title": title, "body": body},
            "data": {
                "confirmation_number": group_result.confirmation_number,
                "group_severity": group_result.group_severity.value,
            },
        }
    )

    json_message = json.dumps(
        {
            "default": body,
            "APNS": apns_message,
            "GCM": gcm_message,
        }
    )

    response = sns_client.publish(
        TargetArn=primary_endpoint_arn,
        Message=json_message,
        MessageStructure="json",
    )

    return response
