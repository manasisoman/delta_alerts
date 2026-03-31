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

## Running the Mock Demo (No AWS Account Needed)

The `demo_mock.py` script uses [moto](https://github.com/getmoto/moto) to spin up
in-memory DynamoDB and SNS backends, then runs 7 scenarios through the full Lambda
handler — including alert persistence and push notification delivery.

```bash
cd delta-concierge-alerts
pip install -r requirements.txt "moto[dynamodb,sns]"
python demo_mock.py
```

### Included Scenarios

| # | Scenario | Expected Passport | Expected Visa |
|---|----------|-------------------|---------------|
| 1 | Expired passport | CRITICAL | CRITICAL |
| 2 | Passport below DE's 3-month rule | WARNING | OK |
| 3 | Missing visa for China (Indian national) | OK | CRITICAL |
| 4 | Visa-exempt: US national → Germany | OK | OK |
| 5 | Multi-segment: DE layover → CN → JP | OK | CRITICAL |
| 6 | Visa expires before travel date | OK | CRITICAL |
| 7 | No passport information on file | CRITICAL | OK |

The script also prints all persisted DynamoDB alert records at the end.

## AWS Setup for Real Deployment

To deploy this as a real Lambda function talking to actual AWS services, you need
three resources: a DynamoDB table, an SNS platform application, and an IAM role.

### 1. Create the DynamoDB Table

```bash
aws dynamodb create-table \
  --table-name ConciergeAlerts \
  --key-schema \
    AttributeName=skymiles_number,KeyType=HASH \
    AttributeName=alert_id,KeyType=RANGE \
  --attribute-definitions \
    AttributeName=skymiles_number,AttributeType=S \
    AttributeName=alert_id,AttributeType=S \
  --billing-mode PAY_PER_REQUEST \
  --region us-east-1
```

### 2. Create the SNS Platform Application

For **iOS (APNS)**:
```bash
aws sns create-platform-application \
  --name DeltaConciergeAPNS \
  --platform APNS \
  --attributes \
    PlatformCredential=<YOUR_APNS_PRIVATE_KEY>,\
    PlatformPrincipal=<YOUR_APNS_CERTIFICATE> \
  --region us-east-1
```

For **Android (FCM/GCM)**:
```bash
aws sns create-platform-application \
  --name DeltaConciergeGCM \
  --platform GCM \
  --attributes PlatformCredential=<YOUR_FCM_SERVER_KEY> \
  --region us-east-1
```

Then register device endpoints:
```bash
aws sns create-platform-endpoint \
  --platform-application-arn <PLATFORM_APP_ARN> \
  --token <DEVICE_TOKEN> \
  --region us-east-1
```

The returned `EndpointArn` is what goes into `profile.endpoint_arn` in the event payload.

### 3. Create the Lambda IAM Role

```bash
# Create the role
aws iam create-role \
  --role-name DeltaConciergeAlertRole \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": {"Service": "lambda.amazonaws.com"},
      "Action": "sts:AssumeRole"
    }]
  }'

# Attach basic Lambda execution (CloudWatch Logs)
aws iam attach-role-policy \
  --role-name DeltaConciergeAlertRole \
  --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Create and attach inline policy for DynamoDB + SNS
aws iam put-role-policy \
  --role-name DeltaConciergeAlertRole \
  --policy-name ConciergeAlertAccess \
  --policy-document '{
    "Version": "2012-10-17",
    "Statement": [
      {
        "Effect": "Allow",
        "Action": [
          "dynamodb:PutItem",
          "dynamodb:Query"
        ],
        "Resource": "arn:aws:dynamodb:us-east-1:*:table/ConciergeAlerts"
      },
      {
        "Effect": "Allow",
        "Action": "sns:Publish",
        "Resource": "*"
      }
    ]
  }'
```

### 4. Deploy the Lambda Function

Package and deploy:
```bash
cd delta-concierge-alerts
pip install -r requirements.txt -t package/
cp -r src/ package/
cd package && zip -r ../lambda.zip . && cd ..

aws lambda create-function \
  --function-name DeltaConciergeAlerts \
  --runtime python3.11 \
  --role arn:aws:iam::<ACCOUNT_ID>:role/DeltaConciergeAlertRole \
  --handler src.handlers.lambda_handler.handler \
  --zip-file fileb://lambda.zip \
  --timeout 30 \
  --memory-size 256 \
  --region us-east-1
```

### 5. Test with a Real Invocation

```bash
aws lambda invoke \
  --function-name DeltaConciergeAlerts \
  --payload '{
    "profile": {
      "skymiles_number": "9876543210",
      "first_name": "Test",
      "last_name": "User",
      "nationality": "IN",
      "passport_number": "P999999",
      "passport_expiry": "2026-09-01",
      "endpoint_arn": "<YOUR_ENDPOINT_ARN>",
      "visa_records": []
    },
    "itinerary": {
      "confirmation_number": "DL-TEST",
      "segments": [{
        "flight_number": "DL999",
        "origin": "JFK",
        "destination": "CN",
        "departure_date": "2026-08-15",
        "arrival_date": "2026-08-16",
        "is_layover": false
      }]
    },
    "requirements_override": null
  }' \
  response.json

cat response.json
```
