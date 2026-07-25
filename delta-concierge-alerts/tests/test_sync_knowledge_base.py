"""Tests for the knowledge base sync handler.

Covers the S3 upload loop over COUNTRY_REQUIREMENTS, the response
payload, and every optional-field branch of ``_build_document``
(embassy URL, e-visa portal, processing days, entry form) plus the
country-name lookup fallback.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import (
    KNOWLEDGE_BASE_BUCKET,
    S3_PREFIX,
    _build_document,
    handler,
)
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.models.types import TravelDocRequirements


@pytest.fixture(autouse=True)
def s3():
    """Activate moto, create the KB bucket, and patch the module client."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=KNOWLEDGE_BASE_BUCKET)

        orig_client = sync_mod.s3_client
        sync_mod.s3_client = client

        try:
            yield client
        finally:
            sync_mod.s3_client = orig_client


def make_requirements(
    country_code: str = "DE",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    passport_validity_months: int = 3,
    visa_exempt_nationalities: list[str] | None = None,
    embassy_url: str | None = None,
    evisa_portal_url: str | None = None,
    estimated_processing_days: int | None = None,
    entry_form_url: str | None = None,
) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=(
            ["US", "GB"] if visa_exempt_nationalities is None else visa_exempt_nationalities
        ),
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


def _get_document(s3_client, country_code: str) -> dict:
    """Fetch and parse an uploaded country document from S3."""
    body = s3_client.get_object(
        Bucket=KNOWLEDGE_BASE_BUCKET,
        Key=f"{S3_PREFIX}/{country_code}.json",
    )["Body"].read()
    return json.loads(body)


class TestHandlerHappyPath:
    """The handler uploads one document per country and reports the count."""

    def test_returns_200_with_synced_count(self, s3):
        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploads_one_object_per_country(self, s3):
        handler({}, None)

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=KNOWLEDGE_BASE_BUCKET)["Contents"]
        }

        assert keys == {f"{S3_PREFIX}/{code}.json" for code in COUNTRY_REQUIREMENTS}

    def test_uploaded_object_is_json_content_type(self, s3):
        handler({}, None)

        head = s3.head_object(
            Bucket=KNOWLEDGE_BASE_BUCKET, Key=f"{S3_PREFIX}/DE.json"
        )

        assert head["ContentType"] == "application/json"

    def test_uploaded_document_matches_source_requirements(self, s3):
        handler({}, None)

        doc = _get_document(s3, "DE")
        reqs = COUNTRY_REQUIREMENTS["DE"]

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert doc["visa_required"] == reqs.requires_visa
        assert doc["transit_visa_required"] == reqs.transit_visa_required
        assert doc["passport_validity_months"] == reqs.passport_validity_months
        assert doc["visa_exempt_nationalities"] == reqs.visa_exempt_nationalities


class TestDocumentBaseFields:
    """Every document carries the core requirement fields."""

    def test_base_fields_present(self):
        reqs = make_requirements(country_code="JP", passport_validity_months=6)

        doc = _build_document("JP", reqs)

        assert doc == {
            "country_code": "JP",
            "country_name": "Japan",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": ["US", "GB"],
        }

    def test_unknown_country_code_falls_back_to_code_as_name(self):
        reqs = make_requirements(country_code="XX")

        doc = _build_document("XX", reqs)

        assert doc["country_name"] == "XX"

    def test_country_code_comes_from_requirements_not_key(self):
        reqs = make_requirements(country_code="FR")

        doc = _build_document("DE", reqs)

        assert doc["country_code"] == "FR"
        assert doc["country_name"] == "Germany"


class TestOptionalDocumentFields:
    """Optional URLs and processing days are only emitted when populated."""

    def test_embassy_url_included_when_set(self):
        reqs = make_requirements(embassy_url="https://embassy.example")

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example"

    def test_evisa_portal_url_included_when_set(self):
        reqs = make_requirements(evisa_portal_url="https://evisa.example")

        doc = _build_document("DE", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_estimated_processing_days_included_when_set(self):
        reqs = make_requirements(estimated_processing_days=14)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 14

    def test_zero_processing_days_included(self):
        reqs = make_requirements(estimated_processing_days=0)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included_when_set(self):
        reqs = make_requirements(entry_form_url="https://form.example")

        doc = _build_document("DE", reqs)

        assert doc["entry_form_url"] == "https://form.example"

    def test_optional_fields_omitted_when_unset(self):
        reqs = make_requirements()

        doc = _build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_empty_string_urls_omitted(self):
        reqs = make_requirements(
            embassy_url="", evisa_portal_url="", entry_form_url=""
        )

        doc = _build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc


class TestUploadedOptionalFields:
    """Countries with optional data round-trip through S3 intact."""

    def test_canada_document_includes_optional_fields(self, s3):
        handler({}, None)

        doc = _get_document(s3, "CA")
        reqs = COUNTRY_REQUIREMENTS["CA"]

        assert doc["embassy_url"] == reqs.embassy_url
        assert doc["evisa_portal_url"] == reqs.evisa_portal_url
        assert doc["estimated_processing_days"] == reqs.estimated_processing_days
