# EventBridge Itinerary-Change Trigger

This document describes the EventBridge integration that automatically re-evaluates traveler alerts when an itinerary changes.

## Architecture

```
Booking System
      │
      ▼
  EventBridge
  (ItineraryChanged rule)
      │
      ▼
eventbridge_handler.handler()
      │
      ├──► resolve_alerts_for_itinerary()  ──► DynamoDB (ConciergeAlerts)
      │        (resolve stale ACTIVE/ACKNOWLEDGED alerts)
      │
      └──► lambda_handler.evaluate_itinerary()
               │
               ├──► passport_evaluator
               ├──► visa_evaluator
               ├──► alert_store.save_alert()  ──► DynamoDB
               └──► notification_service       ──► SNS ──► Push Notification
```

## EventBridge Rule Configuration

### Create the rule

```bash
aws events put-rule \
  --name DeltaConciergeItineraryChanged \
  --event-pattern '{"source":["delta.booking-system"],"detail-type":["ItineraryChanged"]}' \
  --state ENABLED
```

### Attach the Lambda target

```bash
aws events put-targets \
  --rule DeltaConciergeItineraryChanged \
  --targets "Id"="EvalItineraryTarget","Arn"="arn:aws:lambda:us-east-1:ACCOUNT_ID:function:eventbridge-itinerary-handler"
```

> **Note:** Replace `ACCOUNT_ID` with your AWS account ID. The Lambda function must also have a resource-based policy allowing EventBridge to invoke it:
>
> ```bash
> aws lambda add-permission \
>   --function-name eventbridge-itinerary-handler \
>   --statement-id AllowEventBridgeInvoke \
>   --action lambda:InvokeFunction \
>   --principal events.amazonaws.com \
>   --source-arn arn:aws:events:us-east-1:ACCOUNT_ID:rule/DeltaConciergeItineraryChanged
> ```

## Event Schema

The booking system publishes events to the `default` event bus with the following structure:

```json
{
  "source": "delta.booking-system",
  "detail-type": "ItineraryChanged",
  "detail": {
    "profile": {
      "skymiles_number": "1234567890",
      "first_name": "Jane",
      "last_name": "Doe",
      "nationality": "US",
      "passport_number": "AB1234567",
      "passport_expiry": "2028-06-15",
      "visa_records": [],
      "endpoint_arn": "arn:aws:sns:us-east-1:ACCOUNT_ID:endpoint/APNS/DeltaApp/device-token"
    },
    "itinerary": {
      "confirmation_number": "GKXYZ1",
      "segments": [
        {
          "flight_number": "DL100",
          "origin": "ATL",
          "destination": "CDG",
          "departure_date": "2026-07-01",
          "arrival_date": "2026-07-02",
          "is_layover": false
        }
      ]
    },
    "change_type": "DATE_CHANGE"
  }
}
```

### Supported `change_type` values

| Value             | Description                                          |
| ----------------- | ---------------------------------------------------- |
| `DATE_CHANGE`     | One or more segment dates were modified.             |
| `SEGMENT_ADDED`   | A new segment was added to the itinerary.            |
| `SEGMENT_REMOVED` | A segment was removed from the itinerary.            |
| `ROUTE_CHANGE`    | The origin or destination of a segment was modified. |

## Stale Alert Cleanup

Before re-evaluating the updated itinerary, the handler resolves all existing `ACTIVE` and `ACKNOWLEDGED` alerts for the affected itinerary. This prevents duplicate or outdated alerts from persisting after the itinerary has changed.

The cleanup process:

1. **Query** the `ConciergeAlerts` DynamoDB table using `skymiles_number` as the partition key with a `FilterExpression` matching `itinerary_ref = <confirmation_number>` and `status IN (ACTIVE, ACKNOWLEDGED)`.
2. **Resolve** each matching alert by calling `resolve_alert()`, which sets the status to `RESOLVED`, records a `resolved_at` timestamp, and stores the resolution reason: `"Auto-resolved: itinerary changed, re-evaluating"`.
3. **Re-evaluate** the updated itinerary through the standard evaluation pipeline (`evaluate_itinerary()`), which may generate new alerts based on the changed flight details.

## Response Format

The handler returns a response dict:

```json
{
  "statusCode": 200,
  "body": {
    "change_type": "DATE_CHANGE",
    "resolved_alerts": 2,
    "evaluation_result": {
      "statusCode": 200,
      "body": {
        "alerts_sent": 1,
        "passport_status": "WARNING",
        "visa_status": "OK"
      }
    }
  }
}
```

## Observability

The handler logs the following at `INFO` level for each invocation:

- `change_type`, `skymiles_number`, and `confirmation_number` on event receipt.
- Count of resolved stale alerts after cleanup.

Use CloudWatch Logs Insights to query these events:

```
fields @timestamp, @message
| filter @message like /ItineraryChanged/
| sort @timestamp desc
| limit 50
```
