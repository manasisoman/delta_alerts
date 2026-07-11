"""Tests for the sync_knowledge_base Lambda handler.

Covers the S3 sync loop (keys, content types, serialized bodies, document
count, response payload), the country-name lookup for both known and unknown
codes, and every optional document field branch in ``_build_document``.

Follows the project's moto + module-client monkeypatching pattern: an autouse
fixture activates ``moto.mock_aws()``, creates the target bucket, and patches
the module-level ``s3_client``, restoring the original in a finally block.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_kb_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.models.types import TravelDocRequirements


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto S3 mocks, create the KB bucket, and patch s3_client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=sync_kb_mod.KNOWLEDGE_BASE_BUCKET)

        orig_s3_client = sync_kb_mod.s3_client
        sync_kb_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_kb_mod.s3_client = orig_s3_client


@pytest.fixture
def s3(_mock_aws):
    """Expose the mocked S3 client created by ``_mock_aws``."""
    return _mock_aws


def _make_reqs(
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
    """Build a TravelDocRequirements varying one field at a time."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=visa_exempt_nationalities or [],
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


def _list_keys(s3) -> list[str]:
    """Return all object keys currently in the KB bucket."""
    resp = s3.list_objects_v2(Bucket=sync_kb_mod.KNOWLEDGE_BASE_BUCKET)
    return [obj["Key"] for obj in resp.get("Contents", [])]


def _get_document(s3, key: str) -> dict:
    """Fetch and JSON-decode an uploaded document body."""
    body = s3.get_object(Bucket=sync_kb_mod.KNOWLEDGE_BASE_BUCKET, Key=key)["Body"].read()
    return json.loads(body)


# ------------------------------------------------------------------
# 1. Handler response and document count
# ------------------------------------------------------------------

class TestHandlerResponse:
    """The handler returns 200 and the count of synced documents."""

    def test_returns_200_with_document_count(self, s3):
        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploads_one_object_per_country(self, s3):
        handler({}, None)

        assert len(_list_keys(s3)) == len(COUNTRY_REQUIREMENTS)


# ------------------------------------------------------------------
# 2. S3 keys and content types
# ------------------------------------------------------------------

class TestUploadKeysAndContentType:
    """Each object is stored under the country-requirements prefix as JSON."""

    def test_key_uses_prefix_and_country_code(self, s3):
        handler({}, None)

        keys = _list_keys(s3)
        for country_code in COUNTRY_REQUIREMENTS:
            assert f"country-requirements/{country_code}.json" in keys

    def test_content_type_is_application_json(self, s3):
        handler({}, None)

        head = s3.head_object(
            Bucket=sync_kb_mod.KNOWLEDGE_BASE_BUCKET,
            Key="country-requirements/DE.json",
        )
        assert head["ContentType"] == "application/json"


# ------------------------------------------------------------------
# 3. Serialized document body
# ------------------------------------------------------------------

class TestSerializedDocument:
    """Uploaded bodies are valid JSON carrying the required base fields."""

    def test_body_is_valid_json_with_base_fields(self, s3):
        handler({}, None)

        doc = _get_document(s3, "country-requirements/DE.json")
        reqs = COUNTRY_REQUIREMENTS["DE"]
        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert doc["visa_required"] == reqs.requires_visa
        assert doc["transit_visa_required"] == reqs.transit_visa_required
        assert doc["passport_validity_months"] == reqs.passport_validity_months
        assert doc["visa_exempt_nationalities"] == reqs.visa_exempt_nationalities


# ------------------------------------------------------------------
# 4. Country-name lookup
# ------------------------------------------------------------------

class TestCountryNameLookup:
    """_build_document maps known codes to names and falls back to the code."""

    def test_known_country_name(self):
        doc = _build_document("JP", _make_reqs(country_code="JP"))

        assert doc["country_name"] == "Japan"

    def test_unknown_country_name_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_reqs(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"


# ------------------------------------------------------------------
# 5. Optional document fields
# ------------------------------------------------------------------

class TestOptionalFields:
    """Optional fields appear only when set on the requirements object."""

    def test_all_optional_fields_omitted_by_default(self):
        doc = _build_document("DE", _make_reqs())

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_embassy_url_included_when_set(self):
        doc = _build_document("CA", _make_reqs(embassy_url="https://example.gov"))

        assert doc["embassy_url"] == "https://example.gov"

    def test_evisa_portal_url_included_when_set(self):
        doc = _build_document("KR", _make_reqs(evisa_portal_url="https://evisa.example"))

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_estimated_processing_days_included_when_set(self):
        doc = _build_document("CA", _make_reqs(estimated_processing_days=14))

        assert doc["estimated_processing_days"] == 14

    def test_estimated_processing_days_zero_is_included(self):
        doc = _build_document("CA", _make_reqs(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included_when_set(self):
        doc = _build_document("DO", _make_reqs(entry_form_url="https://form.example"))

        assert doc["entry_form_url"] == "https://form.example"

    def test_all_optional_fields_included_together(self):
        doc = _build_document(
            "CA",
            _make_reqs(
                embassy_url="https://embassy.example",
                evisa_portal_url="https://evisa.example",
                estimated_processing_days=7,
                entry_form_url="https://form.example",
            ),
        )

        assert doc["embassy_url"] == "https://embassy.example"
        assert doc["evisa_portal_url"] == "https://evisa.example"
        assert doc["estimated_processing_days"] == 7
        assert doc["entry_form_url"] == "https://form.example"
