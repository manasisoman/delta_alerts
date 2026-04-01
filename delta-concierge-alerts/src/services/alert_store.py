"""DynamoDB-backed alert persistence for the Delta Concierge Alert system."""

from datetime import datetime

import boto3
from boto3.dynamodb.conditions import Attr, Key

from src.config import DYNAMODB_TABLE_NAME
from src.models.types import AlertRecord, AlertStatus


dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE_NAME)


def save_alert(alert_record: AlertRecord) -> None:
    """Persist an alert record to the ConciergeAlerts DynamoDB table.

    Uses skymiles_number as the partition key and alert_id as the sort key.
    Stores the alert status and optional TTL for automatic DynamoDB expiry.

    Args:
        alert_record: The AlertRecord to store.
    """
    item: dict = {
        "skymiles_number": alert_record.skymiles_number,
        "alert_id": alert_record.alert_id,
        "alert_type": alert_record.alert_type.value,
        "severity": alert_record.severity.value,
        "reasons": alert_record.reasons,
        "created_at": alert_record.created_at,
        "itinerary_ref": alert_record.itinerary_ref,
        "status": alert_record.status.value,
    }
    if alert_record.ttl is not None:
        item["ttl"] = alert_record.ttl
    if alert_record.resolved_at is not None:
        item["resolved_at"] = alert_record.resolved_at
    if alert_record.resolution is not None:
        item["resolution"] = alert_record.resolution
    table.put_item(Item=item)


def get_alerts_by_member(skymiles_number: str) -> list[dict]:
    """Query all alerts for a SkyMiles member.

    Args:
        skymiles_number: The member's SkyMiles number (partition key).

    Returns:
        A list of alert items from DynamoDB.
    """
    response = table.query(
        KeyConditionExpression=Key("skymiles_number").eq(skymiles_number)
    )
    return response.get("Items", [])


def acknowledge_alert(skymiles_number: str, alert_id: str) -> None:
    """Mark an alert as acknowledged.

    Args:
        skymiles_number: The member's SkyMiles number (partition key).
        alert_id: The alert's unique identifier (sort key).
    """
    table.update_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id},
        UpdateExpression="SET #s = :status",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":status": AlertStatus.ACKNOWLEDGED.value},
    )


def resolve_alert(
    skymiles_number: str, alert_id: str, resolution: str
) -> None:
    """Mark an alert as resolved with a resolution description.

    Args:
        skymiles_number: The member's SkyMiles number (partition key).
        alert_id: The alert's unique identifier (sort key).
        resolution: A human-readable description of how the alert was resolved.
    """
    table.update_item(
        Key={"skymiles_number": skymiles_number, "alert_id": alert_id},
        UpdateExpression="SET #s = :status, resolved_at = :resolved_at, resolution = :resolution",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={
            ":status": AlertStatus.RESOLVED.value,
            ":resolved_at": datetime.utcnow().isoformat(),
            ":resolution": resolution,
        },
    )


def get_active_alerts(skymiles_number: str) -> list[dict]:
    """Query alerts that are still actionable (ACTIVE or ACKNOWLEDGED).

    Args:
        skymiles_number: The member's SkyMiles number (partition key).

    Returns:
        A list of alert items with status ACTIVE or ACKNOWLEDGED.
    """
    response = table.query(
        KeyConditionExpression=Key("skymiles_number").eq(skymiles_number),
        FilterExpression=Attr("status").is_in(
            [AlertStatus.ACTIVE.value, AlertStatus.ACKNOWLEDGED.value]
        ),
    )
    return response.get("Items", [])


def resolve_alerts_for_itinerary(
    skymiles_number: str, confirmation_number: str, resolution: str
) -> int:
    """Resolve all active/acknowledged alerts for a specific itinerary.

    Queries the member's alerts by partition key and filters for those
    matching the given ``confirmation_number`` (stored as ``itinerary_ref``)
    with a status of ACTIVE or ACKNOWLEDGED.  Each matching alert is
    individually resolved via :func:`resolve_alert`.

    Args:
        skymiles_number: The member's SkyMiles number (partition key).
        confirmation_number: The itinerary confirmation number to match
            against the ``itinerary_ref`` attribute.
        resolution: A human-readable description applied to each resolved
            alert (e.g. ``"Auto-resolved: itinerary changed, re-evaluating"``).

    Returns:
        The number of alerts that were resolved.
    """
    response = table.query(
        KeyConditionExpression=Key("skymiles_number").eq(skymiles_number),
        FilterExpression=(
            Attr("itinerary_ref").eq(confirmation_number)
            & Attr("status").is_in(
                [AlertStatus.ACTIVE.value, AlertStatus.ACKNOWLEDGED.value]
            )
        ),
    )
    matching_alerts = response.get("Items", [])
    resolved_count = 0
    for alert in matching_alerts:
        resolve_alert(skymiles_number, alert["alert_id"], resolution)
        resolved_count += 1
    return resolved_count


def get_alerts_by_itinerary(confirmation_number: str) -> list[dict]:
    """Query all alerts for a given itinerary confirmation number.

    Uses a GSI named ``itinerary-ref-index`` on the ``itinerary_ref``
    attribute to find alerts across all travelers sharing the same booking.

    Note:
        This requires a DynamoDB Global Secondary Index named
        ``itinerary-ref-index`` with partition key ``itinerary_ref``.
    """
    response = table.query(
        IndexName="itinerary-ref-index",
        KeyConditionExpression=Key("itinerary_ref").eq(confirmation_number),
    )
    return response.get("Items", [])


def get_group_alert_summary(confirmation_number: str) -> dict[str, list[dict]]:
    """Get alerts grouped by skymiles_number for a booking.

    Args:
        confirmation_number: The itinerary confirmation number to query.

    Returns:
        A dict mapping each traveler's skymiles_number to their list of alerts.
    """
    all_alerts = get_alerts_by_itinerary(confirmation_number)
    grouped: dict[str, list[dict]] = {}
    for alert in all_alerts:
        sm = alert["skymiles_number"]
        grouped.setdefault(sm, []).append(alert)
    return grouped


def expire_stale_alerts(skymiles_number: str) -> int:
    """Mark alerts past their TTL as EXPIRED.

    Scans a member's alerts and transitions any whose ``ttl`` epoch
    timestamp has passed to the EXPIRED status.  DynamoDB's native TTL
    feature will eventually delete these items, but this function
    provides an immediate, explicit status change.

    Args:
        skymiles_number: The member's SkyMiles number (partition key).

    Returns:
        The number of alerts that were marked EXPIRED.
    """
    now_epoch = int(datetime.utcnow().timestamp())
    alerts = get_alerts_by_member(skymiles_number)
    expired_count = 0
    for alert in alerts:
        alert_ttl = alert.get("ttl")
        if alert_ttl is None:
            continue
        if (
            int(alert_ttl) <= now_epoch
            and alert.get("status") in (AlertStatus.ACTIVE.value, AlertStatus.ACKNOWLEDGED.value)
        ):
            table.update_item(
                Key={
                    "skymiles_number": skymiles_number,
                    "alert_id": alert["alert_id"],
                },
                UpdateExpression="SET #s = :status",
                ExpressionAttributeNames={"#s": "status"},
                ExpressionAttributeValues={":status": AlertStatus.EXPIRED.value},
            )
            expired_count += 1
    return expired_count
