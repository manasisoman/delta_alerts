"""Tests for the knowledge base sync handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and create the knowledge base bucket for every test."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)

        orig_s3_client = sync_mod.s3_client
        sync_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client


@pytest.fixture
def s3(_mock_aws):
    """Expose the moto S3 client created by _mock_aws."""
    return _mock_aws


@pytest.fixture(autouse=True)
def _patch_requirements():
    """Replace production requirements with a small fixture set for every test."""
    requirements = {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB"],
        ),
        "XX": TravelDocRequirements(
            country_code="XX",
            requires_visa=False,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            embassy_url="https://embassy.example/xx",
            evisa_portal_url="https://evisa.example/xx",
            estimated_processing_days=10,
            entry_form_url="https://entry.example/xx",
        ),
    }

    orig = sync_mod.COUNTRY_REQUIREMENTS
    sync_mod.COUNTRY_REQUIREMENTS = requirements

    try:
        yield requirements
    finally:
        sync_mod.COUNTRY_REQUIREMENTS = orig


def _requirements(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults."""
    fields = {
        "country_code": "DE",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB"],
    }
    fields.update(overrides)

    return TravelDocRequirements(**fields)


class TestSyncUploadsDocuments:
    """Every country in COUNTRY_REQUIREMENTS is uploaded exactly once."""

    def test_returns_success_with_document_count(self, _patch_requirements):
        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"] == {"documents_synced": len(_patch_requirements)}

    def test_uploads_one_object_per_country_under_prefix(self, s3, _patch_requirements):
        handler({}, None)

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)["Contents"]
        }
        assert keys == {
            f"{sync_mod.S3_PREFIX}/{code}.json" for code in _patch_requirements
        }

    def test_uploaded_object_is_json_with_content_type(self, s3):
        handler({}, None)

        obj = s3.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key=f"{sync_mod.S3_PREFIX}/DE.json",
        )
        body = json.loads(obj["Body"].read())

        assert obj["ContentType"] == "application/json"
        assert body["country_code"] == "DE"
        assert body["country_name"] == "Germany"

    def test_empty_requirements_syncs_nothing(self, s3):
        sync_mod.COUNTRY_REQUIREMENTS = {}

        response = handler({}, None)

        assert response["body"] == {"documents_synced": 0}
        assert "Contents" not in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)

    def test_syncs_full_production_requirements(self, s3):
        sync_mod.COUNTRY_REQUIREMENTS = COUNTRY_REQUIREMENTS

        response = handler({}, None)

        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)
        assert (
            s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)["KeyCount"]
            == len(COUNTRY_REQUIREMENTS)
        )


class TestDocumentRequiredFields:
    """Core requirement fields are always present in the built document."""

    def test_maps_requirement_fields(self):
        doc = _build_document("CN", _requirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ))

        assert doc == {
            "country_code": "CN",
            "country_name": "China",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": [],
        }

    def test_unknown_country_code_falls_back_to_code_as_name(self):
        doc = _build_document("ZZ", _requirements(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_visa_not_required_is_reported(self):
        doc = _build_document("US", _requirements(country_code="US", requires_visa=False))

        assert doc["visa_required"] is False


class TestDocumentOptionalFields:
    """Optional URL and processing fields are only included when populated."""

    def test_optional_fields_omitted_when_unset(self):
        doc = _build_document("DE", _requirements())

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_embassy_url_included_when_set(self):
        doc = _build_document("DE", _requirements(embassy_url="https://embassy.example/de"))

        assert doc["embassy_url"] == "https://embassy.example/de"

    def test_evisa_portal_url_included_when_set(self):
        doc = _build_document("IN", _requirements(
            country_code="IN", evisa_portal_url="https://evisa.example/in",
        ))

        assert doc["evisa_portal_url"] == "https://evisa.example/in"

    def test_entry_form_url_included_when_set(self):
        doc = _build_document("SG", _requirements(
            country_code="SG", entry_form_url="https://entry.example/sg",
        ))

        assert doc["entry_form_url"] == "https://entry.example/sg"

    def test_estimated_processing_days_included_when_set(self):
        doc = _build_document("CN", _requirements(
            country_code="CN", estimated_processing_days=14,
        ))

        assert doc["estimated_processing_days"] == 14

    def test_zero_processing_days_is_included(self):
        doc = _build_document("CA", _requirements(
            country_code="CA", estimated_processing_days=0,
        ))

        assert doc["estimated_processing_days"] == 0
