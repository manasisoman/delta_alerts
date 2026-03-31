# DynamoDB Global Secondary Index — `status-created_at-index`

## Purpose

This GSI enables efficient querying of alerts across **all** SkyMiles members by their lifecycle status. The base table uses `skymiles_number` as the partition key, which means querying "all active alerts system-wide" requires a full table scan. The GSI eliminates this by re-partitioning on `status`.

## Schema

| Attribute      | Key Type   | Type   |
|----------------|-----------|--------|
| `status`       | HASH (PK) | String |
| `created_at`   | RANGE (SK)| String |

## Projected Attributes

Use `ALL` projection so that downstream consumers have access to every field without a second read against the base table.

## Creation via AWS CLI

```bash
aws dynamodb update-table \
  --table-name ConciergeAlerts \
  --attribute-definitions \
    AttributeName=status,AttributeType=S \
    AttributeName=created_at,AttributeType=S \
  --global-secondary-index-updates '[
    {
      "Create": {
        "IndexName": "status-created_at-index",
        "KeySchema": [
          {"AttributeName": "status", "KeyType": "HASH"},
          {"AttributeName": "created_at", "KeyType": "RANGE"}
        ],
        "Projection": {"ProjectionType": "ALL"}
      }
    }
  ]'
```

## CloudFormation / SAM Snippet

```yaml
ConciergeAlertsTable:
  Type: AWS::DynamoDB::Table
  Properties:
    TableName: ConciergeAlerts
    BillingMode: PAY_PER_REQUEST
    KeySchema:
      - AttributeName: skymiles_number
        KeyType: HASH
      - AttributeName: alert_id
        KeyType: RANGE
    AttributeDefinitions:
      - AttributeName: skymiles_number
        AttributeType: S
      - AttributeName: alert_id
        AttributeType: S
      - AttributeName: status
        AttributeType: S
      - AttributeName: created_at
        AttributeType: S
    GlobalSecondaryIndexes:
      - IndexName: status-created_at-index
        KeySchema:
          - AttributeName: status
            KeyType: HASH
          - AttributeName: created_at
            KeyType: RANGE
        Projection:
          ProjectionType: ALL
    TimeToLiveSpecification:
      AttributeName: ttl
      Enabled: true
```

## Usage Example

```python
from boto3.dynamodb.conditions import Key

response = table.query(
    IndexName="status-created_at-index",
    KeyConditionExpression=Key("status").eq("ACTIVE"),
    ScanIndexForward=False,  # newest first
    Limit=50,
)
```

## Notes

- The `ttl` attribute on the base table should have **Time to Live** enabled in the DynamoDB console or via the `TimeToLiveSpecification` in CloudFormation. DynamoDB will automatically delete items whose `ttl` epoch has passed (with a delay of up to 48 hours).
- The GSI is eventually consistent with the base table. For the alert lifecycle use-case this is acceptable.
