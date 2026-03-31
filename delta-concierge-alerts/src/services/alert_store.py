"""DynamoDB-backed alert persistence for the Delta Concierge Alert system."""

import boto3

from src.config import DYNAMODB_TABLE_NAME
from src.models.types import AlertRecord


dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE_NAME)


def save_alert(alert_record: AlertRecord) -> None:
    """Persist an alert record to the ConciergeAlerts DynamoDB table.

    Uses skymiles_number as the partition key and alert_id as the sort key.

    Args:
        alert_record: The AlertRecord to store.
    """
    table.put_item(
        Item={
            "skymiles_number": alert_record.skymiles_number,
            "alert_id": alert_record.alert_id,
            "alert_type": alert_record.alert_type.value,
            "severity": alert_record.severity.value,
            "reasons": alert_record.reasons,
            "created_at": alert_record.created_at,
            "itinerary_ref": alert_record.itinerary_ref,
        }
    )


def get_alerts_by_member(skymiles_number: str) -> list[dict]:
    """Query all alerts for a SkyMiles member.

    Args:
        skymiles_number: The member's SkyMiles number (partition key).

    Returns:
        A list of alert items from DynamoDB.
    """
    response = table.query(
        KeyConditionExpression=boto3.dynamodb.conditions.Key("skymiles_number").eq(
            skymiles_number
        )
    )
    return response.get("Items", [])
