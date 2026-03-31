#!/usr/bin/env python3
"""Demo script that exercises the full Lambda handler using moto-mocked AWS services.

Usage:
    cd delta-concierge-alerts
    pip install -r requirements.txt "moto[dynamodb,sns]"
    python demo_mock.py

No real AWS credentials or resources are needed — moto intercepts all boto3
calls and provides in-memory DynamoDB and SNS backends.
"""

import json
import os
import sys

# Ensure the project root is on the Python path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Set a dummy AWS region before any boto3 import (moto needs it)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")

import boto3
import moto


# ---------------------------------------------------------------------------
# Helper: set up mock AWS resources
# ---------------------------------------------------------------------------

def _create_dynamodb_table() -> None:
    """Create the ConciergeAlerts DynamoDB table in the moto mock."""
    ddb = boto3.resource("dynamodb", region_name="us-east-1")
    ddb.create_table(
        TableName="ConciergeAlerts",
        KeySchema=[
            {"AttributeName": "skymiles_number", "KeyType": "HASH"},
            {"AttributeName": "alert_id", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "skymiles_number", "AttributeType": "S"},
            {"AttributeName": "alert_id", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_sns_endpoint() -> str:
    """Create a mock SNS platform application + endpoint and return the endpoint ARN."""
    sns = boto3.client("sns", region_name="us-east-1")
    app_response = sns.create_platform_application(
        Name="DeltaApp",
        Platform="GCM",
        Attributes={"PlatformCredential": "mock-api-key"},
    )
    app_arn = app_response["PlatformApplicationArn"]

    endpoint_response = sns.create_platform_endpoint(
        PlatformApplicationArn=app_arn,
        Token="mock-device-token",
    )
    return endpoint_response["EndpointArn"]


# ---------------------------------------------------------------------------
# Scenario definitions
# ---------------------------------------------------------------------------

def scenario_expired_passport(endpoint_arn: str) -> dict:
    """Passport expired last year — should trigger CRITICAL."""
    return {
        "profile": {
            "skymiles_number": "1000000001",
            "first_name": "Alice",
            "last_name": "Johnson",
            "nationality": "IN",
            "passport_number": "P100001",
            "passport_expiry": "2025-06-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-EXP001",
            "segments": [
                {
                    "flight_number": "DL100",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }


def scenario_passport_below_country_minimum(endpoint_arn: str) -> dict:
    """Passport has only 2 months validity left but Germany requires 3 — WARNING."""
    return {
        "profile": {
            "skymiles_number": "1000000002",
            "first_name": "Bob",
            "last_name": "Smith",
            "nationality": "IN",
            "passport_number": "P100002",
            "passport_expiry": "2026-11-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [
                {
                    "country_code": "DE",
                    "visa_type": "TOURIST",
                    "issue_date": "2026-01-01",
                    "expiry_date": "2027-01-01",
                    "visa_number": "V200001",
                }
            ],
        },
        "itinerary": {
            "confirmation_number": "DL-LOW002",
            "segments": [
                {
                    "flight_number": "DL200",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-09-15",
                    "arrival_date": "2026-09-16",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }


def scenario_missing_visa_for_china(endpoint_arn: str) -> dict:
    """Indian national traveling to China with no visa on file — CRITICAL."""
    return {
        "profile": {
            "skymiles_number": "1000000003",
            "first_name": "Carol",
            "last_name": "Patel",
            "nationality": "IN",
            "passport_number": "P100003",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-VIS003",
            "segments": [
                {
                    "flight_number": "DL300",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2026-10-01",
                    "arrival_date": "2026-10-02",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }


def scenario_visa_exempt_us_to_germany(endpoint_arn: str) -> dict:
    """US national traveling to Germany — visa exempt, no visa alert expected."""
    return {
        "profile": {
            "skymiles_number": "1000000004",
            "first_name": "Dave",
            "last_name": "Wilson",
            "nationality": "US",
            "passport_number": "P100004",
            "passport_expiry": "2030-06-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-EXM004",
            "segments": [
                {
                    "flight_number": "DL400",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-08-01",
                    "arrival_date": "2026-08-02",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }


def scenario_multi_segment_mixed_alerts(endpoint_arn: str) -> dict:
    """Multi-segment trip: ATL→DE (layover) →CN→JP.

    - DE layover: transit visa not required → skipped
    - CN: Indian national, no visa → CRITICAL
    - JP: Indian national, visa expiring within 30 days of travel → INFO
    """
    return {
        "profile": {
            "skymiles_number": "1000000005",
            "first_name": "Eve",
            "last_name": "Kumar",
            "nationality": "IN",
            "passport_number": "P100005",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [
                {
                    "country_code": "JP",
                    "visa_type": "TOURIST",
                    "issue_date": "2026-01-01",
                    "expiry_date": "2026-11-10",
                    "visa_number": "V500001",
                }
            ],
        },
        "itinerary": {
            "confirmation_number": "DL-MIX005",
            "segments": [
                {
                    "flight_number": "DL510",
                    "origin": "ATL",
                    "destination": "DE",
                    "departure_date": "2026-10-20",
                    "arrival_date": "2026-10-21",
                    "is_layover": True,
                },
                {
                    "flight_number": "DL520",
                    "origin": "DE",
                    "destination": "CN",
                    "departure_date": "2026-10-21",
                    "arrival_date": "2026-10-22",
                    "is_layover": False,
                },
                {
                    "flight_number": "DL530",
                    "origin": "CN",
                    "destination": "JP",
                    "departure_date": "2026-10-25",
                    "arrival_date": "2026-10-26",
                    "is_layover": False,
                },
            ],
        },
        "requirements_override": None,
    }


def scenario_visa_expires_before_travel(endpoint_arn: str) -> dict:
    """Visa for China expires before departure date — CRITICAL."""
    return {
        "profile": {
            "skymiles_number": "1000000006",
            "first_name": "Frank",
            "last_name": "Chen",
            "nationality": "IN",
            "passport_number": "P100006",
            "passport_expiry": "2030-01-01",
            "endpoint_arn": endpoint_arn,
            "visa_records": [
                {
                    "country_code": "CN",
                    "visa_type": "TOURIST",
                    "issue_date": "2025-01-01",
                    "expiry_date": "2026-07-01",
                    "visa_number": "V600001",
                }
            ],
        },
        "itinerary": {
            "confirmation_number": "DL-VEX006",
            "segments": [
                {
                    "flight_number": "DL600",
                    "origin": "JFK",
                    "destination": "CN",
                    "departure_date": "2026-10-01",
                    "arrival_date": "2026-10-02",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }


def scenario_no_passport_info(endpoint_arn: str) -> dict:
    """Profile has no passport number or expiry — CRITICAL."""
    return {
        "profile": {
            "skymiles_number": "1000000007",
            "first_name": "Grace",
            "last_name": "Lee",
            "nationality": "US",
            "passport_number": None,
            "passport_expiry": None,
            "endpoint_arn": endpoint_arn,
            "visa_records": [],
        },
        "itinerary": {
            "confirmation_number": "DL-NOP007",
            "segments": [
                {
                    "flight_number": "DL700",
                    "origin": "LAX",
                    "destination": "JP",
                    "departure_date": "2026-12-01",
                    "arrival_date": "2026-12-02",
                    "is_layover": False,
                }
            ],
        },
        "requirements_override": None,
    }


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

SCENARIOS = [
    ("Expired passport", scenario_expired_passport),
    ("Passport below country minimum (DE 3-month rule)", scenario_passport_below_country_minimum),
    ("Missing visa for China (Indian national)", scenario_missing_visa_for_china),
    ("Visa-exempt: US national → Germany", scenario_visa_exempt_us_to_germany),
    ("Multi-segment mixed alerts (DE layover → CN → JP)", scenario_multi_segment_mixed_alerts),
    ("Visa expires before travel date", scenario_visa_expires_before_travel),
    ("No passport information on file", scenario_no_passport_info),
]


@moto.mock_aws
def main() -> None:
    _create_dynamodb_table()
    endpoint_arn = _create_sns_endpoint()

    # Force-reload the service modules so they pick up the moto-patched boto3
    import importlib
    import src.services.alert_store as alert_store_mod
    import src.services.notification_service as notif_mod

    alert_store_mod.dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    alert_store_mod.table = alert_store_mod.dynamodb.Table("ConciergeAlerts")
    notif_mod.sns_client = boto3.client("sns", region_name="us-east-1")

    from src.handlers.lambda_handler import handler

    print("=" * 72)
    print("  Delta Concierge Alert System — Mock Demo")
    print("=" * 72)

    for i, (name, scenario_fn) in enumerate(SCENARIOS, 1):
        event = scenario_fn(endpoint_arn)
        print(f"\n{'─' * 72}")
        print(f"  Scenario {i}: {name}")
        print(f"{'─' * 72}")
        print(f"  Traveler : {event['profile']['first_name']} {event['profile']['last_name']}")
        print(f"  Nationality : {event['profile']['nationality']}")
        print(f"  SkyMiles# : {event['profile']['skymiles_number']}")
        segments = event["itinerary"]["segments"]
        route = " → ".join(seg["origin"] for seg in segments) + " → " + segments[-1]["destination"]
        print(f"  Route : {route}")
        print()

        response = handler(event, None)
        body = response["body"]

        print(f"  Passport Status : {body['passport_status']}")
        print(f"  Visa Status     : {body['visa_status']}")
        print(f"  Alerts Sent     : {body['alerts_sent']}")

    # Show persisted alerts
    print(f"\n{'=' * 72}")
    print("  Persisted Alerts in DynamoDB")
    print(f"{'=' * 72}")

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("ConciergeAlerts")
    scan = table.scan()
    items = scan.get("Items", [])
    print(f"\n  Total alert records: {len(items)}\n")
    for item in items:
        print(f"  [{item['severity']}] {item['alert_type']} — SkyMiles# {item['skymiles_number']}")
        for reason in item.get("reasons", []):
            print(f"        → {reason}")
        print()


if __name__ == "__main__":
    main()
