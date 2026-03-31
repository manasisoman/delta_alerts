"""Lambda handler that syncs country requirements to S3 for Bedrock Knowledge Base."""

import json
import os

import boto3

from src.data.country_requirements import COUNTRY_REQUIREMENTS

KNOWLEDGE_BASE_BUCKET = os.environ.get("KNOWLEDGE_BASE_BUCKET", "delta-concierge-kb")
S3_PREFIX = "country-requirements"

s3_client = boto3.client("s3")


def handler(event: dict, context: object) -> dict:
    """Sync all country requirement documents to S3.

    Iterates over COUNTRY_REQUIREMENTS and uploads a structured JSON
    document for each country to the configured S3 bucket. These
    documents are then indexed by Amazon Bedrock Knowledge Base for
    retrieval-augmented generation (RAG).

    Args:
        event: The Lambda event payload (unused).
        context: The Lambda context object (unused).

    Returns:
        A response dict with statusCode and the count of synced documents.
    """
    synced = 0

    for country_code, reqs in COUNTRY_REQUIREMENTS.items():
        document = _build_document(country_code, reqs)
        key = f"{S3_PREFIX}/{country_code}.json"
        s3_client.put_object(
            Bucket=KNOWLEDGE_BASE_BUCKET,
            Key=key,
            Body=json.dumps(document, indent=2),
            ContentType="application/json",
        )
        synced += 1

    return {
        "statusCode": 200,
        "body": {"documents_synced": synced},
    }


def _build_document(country_code: str, reqs) -> dict:
    """Build a structured document for a single country's requirements.

    Args:
        country_code: ISO 3166-1 alpha-2 country code.
        reqs: A TravelDocRequirements instance.

    Returns:
        A dict ready for JSON serialization and S3 upload.
    """
    doc: dict = {
        "country_code": reqs.country_code,
        "country_name": _COUNTRY_NAMES.get(country_code, country_code),
        "visa_required": reqs.requires_visa,
        "transit_visa_required": reqs.transit_visa_required,
        "passport_validity_months": reqs.passport_validity_months,
        "visa_exempt_nationalities": reqs.visa_exempt_nationalities,
    }

    if reqs.embassy_url:
        doc["embassy_url"] = reqs.embassy_url
    if reqs.evisa_portal_url:
        doc["evisa_portal_url"] = reqs.evisa_portal_url
    if reqs.estimated_processing_days is not None:
        doc["estimated_processing_days"] = reqs.estimated_processing_days
    if reqs.entry_form_url:
        doc["entry_form_url"] = reqs.entry_form_url

    return doc


_COUNTRY_NAMES: dict[str, str] = {
    "DE": "Germany",
    "FR": "France",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "AT": "Austria",
    "BE": "Belgium",
    "CH": "Switzerland",
    "PT": "Portugal",
    "GR": "Greece",
    "US": "United States",
    "GB": "United Kingdom",
    "JP": "Japan",
    "AU": "Australia",
    "CN": "China",
    "IN": "India",
    "BR": "Brazil",
    "CA": "Canada",
    "MX": "Mexico",
    "KR": "South Korea",
    "SG": "Singapore",
    "TH": "Thailand",
    "AE": "United Arab Emirates",
    "ZA": "South Africa",
    "NG": "Nigeria",
    "EG": "Egypt",
    "KE": "Kenya",
    "AR": "Argentina",
    "CL": "Chile",
    "CO": "Colombia",
    "PE": "Peru",
    "NZ": "New Zealand",
    "PH": "Philippines",
    "VN": "Vietnam",
    "IL": "Israel",
    "TR": "Turkey",
    "SA": "Saudi Arabia",
    "QA": "Qatar",
    "JM": "Jamaica",
    "DO": "Dominican Republic",
    "CR": "Costa Rica",
    "PA": "Panama",
    "GH": "Ghana",
    "SN": "Senegal",
    "TZ": "Tanzania",
}
