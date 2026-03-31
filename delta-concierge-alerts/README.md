# Delta Concierge Proactive Alert System

A mock implementation of the Delta Air Lines Concierge proactive alert system, built as AWS Lambda functions in Python 3.11. The system evaluates travelers' passport and visa status against their upcoming itineraries and sends push notifications when action is required.

## Project Structure

```
delta-concierge-alerts/
├── src/
│   ├── __init__.py
│   ├── models/
│   │   ├── __init__.py
│   │   └── types.py              # Dataclasses and enums for all domain objects
│   ├── evaluators/
│   │   ├── __init__.py
│   │   ├── passport_evaluator.py # Passport expiry evaluation logic
│   │   └── visa_evaluator.py     # Visa requirements evaluation logic
│   ├── services/
│   │   ├── __init__.py
│   │   ├── notification_service.py  # SNS push notification publishing
│   │   └── alert_store.py          # DynamoDB alert persistence
│   ├── handlers/
│   │   ├── __init__.py
│   │   └── lambda_handler.py     # AWS Lambda entry point
│   ├── data/
│   │   ├── __init__.py
│   │   └── country_requirements.py  # Country-specific travel doc requirements
│   └── config.py                 # Configuration constants
├── requirements.txt
└── README.md
```

## How It Works

1. The Lambda handler receives an event containing a **SkyMiles profile** (with passport and visa details) and a **travel itinerary** (with flight segments).
2. The **passport evaluator** checks whether the traveler's passport meets each destination country's validity requirements.
3. The **visa evaluator** checks whether the traveler has valid visas for countries that require them, accounting for nationality exemptions and transit rules.
4. Any alerts are persisted to **DynamoDB** and delivered as **push notifications** via Amazon SNS.

## Alert Severity Levels

| Severity | Meaning |
|----------|---------|
| **CRITICAL** | Missing documents, expired passport/visa, or passport expires before travel |
| **WARNING** | Passport doesn't meet destination's validity window, or visa expires on travel date |
| **INFO** | Passport expiring within 6 months, or visa expiring within 30 days of travel |

## Invoking the Lambda Handler Locally

```python
from src.handlers.lambda_handler import handler

event = {
    "profile": {
        "skymiles_number": "1234567890",
        "first_name": "Jane",
        "last_name": "Doe",
        "nationality": "US",
        "passport_number": "P12345678",
        "passport_expiry": "2026-09-15",
        "endpoint_arn": "arn:aws:sns:us-east-1:123456789012:endpoint/APNS/DeltaApp/abc123",
        "visa_records": [
            {
                "country_code": "CN",
                "visa_type": "TOURIST",
                "issue_date": "2025-01-10",
                "expiry_date": "2026-01-10",
                "visa_number": "V98765432"
            }
        ]
    },
    "itinerary": {
        "confirmation_number": "DL-ABC123",
        "segments": [
            {
                "flight_number": "DL100",
                "origin": "ATL",
                "destination": "DE",
                "departure_date": "2026-06-15",
                "arrival_date": "2026-06-16",
                "is_layover": false
            },
            {
                "flight_number": "DL200",
                "origin": "DE",
                "destination": "CN",
                "departure_date": "2026-06-20",
                "arrival_date": "2026-06-21",
                "is_layover": false
            }
        ]
    },
    "requirements_override": null
}

response = handler(event, None)
print(response)
```

### Example Response

```json
{
    "statusCode": 200,
    "body": {
        "alerts_sent": 1,
        "passport_status": "INFO",
        "visa_status": "CRITICAL"
    }
}
```

## Event Schema

| Field | Type | Description |
|-------|------|-------------|
| `profile.skymiles_number` | string | SkyMiles member ID |
| `profile.first_name` | string | Member's first name |
| `profile.last_name` | string | Member's last name |
| `profile.nationality` | string | ISO country code |
| `profile.passport_number` | string or null | Passport number |
| `profile.passport_expiry` | string (YYYY-MM-DD) or null | Passport expiration date |
| `profile.endpoint_arn` | string | SNS platform endpoint ARN for push notifications |
| `profile.visa_records` | array | List of visa records on file |
| `itinerary.confirmation_number` | string | Booking confirmation number |
| `itinerary.segments` | array | Flight segments with origin, destination, dates |
| `requirements_override` | object or null | Optional country requirement overrides |

## Configuration

Edit `src/config.py` to adjust system-wide settings:

| Constant | Default | Description |
|----------|---------|-------------|
| `DEFAULT_PASSPORT_VALIDITY_MONTHS` | 6 | Fallback passport validity requirement (months) |
| `VISA_EXPIRY_WARNING_DAYS` | 30 | Days before travel to warn about expiring visas |
| `DYNAMODB_TABLE_NAME` | `"ConciergeAlerts"` | DynamoDB table name for alert storage |

## Dependencies

- Python 3.11+
- `boto3` — AWS SDK for DynamoDB and SNS access
- `python-dateutil` — Date arithmetic with `relativedelta`

Install dependencies:

```bash
pip install -r requirements.txt
```

## AWS Resources Required

- **DynamoDB table** `ConciergeAlerts` with partition key `skymiles_number` (String) and sort key `alert_id` (String)
- **SNS platform application** configured for APNS and/or GCM
- **IAM role** for the Lambda function with permissions for DynamoDB and SNS
