"""SNS push notification service for the Delta Concierge Alert system."""

import json

import boto3

from src.models.types import NotificationPayload


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
