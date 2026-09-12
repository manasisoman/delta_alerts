"""Tests for the Knowledge Base sync handler (S3 upload of country requirements)."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import (
    KNOWLEDGE_BASE_BUCKET,
    S3_PREFIX,
    _build_document,
    handler,
)
from src.models.types import TravelDocRequirements


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the KB bucket, and patch the module-level S3 client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=KNOWLEDGE_BASE_BUCKET)

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


def _make_requirements(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with only the required fields set by default."""
    fields = {
        "country_code": "DE",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


class TestHandlerSync:
    """handler uploads one JSON document per country and reports the count."""

    def test_returns_200_with_document_count(self):
        # Arrange
        expected = len(COUNTRY_REQUIREMENTS)

        # Act
        result = handler({}, None)

        # Assert
        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == expected

    def test_uploads_one_object_per_country(self, s3):
        # Arrange
        expected_keys = {f"{S3_PREFIX}/{code}.json" for code in COUNTRY_REQUIREMENTS}

        # Act
        handler({}, None)

        # Assert
        paginator = s3.get_paginator("list_objects_v2")
        keys = {
            obj["Key"]
            for page in paginator.paginate(Bucket=KNOWLEDGE_BASE_BUCKET)
            for obj in page.get("Contents", [])
        }
        assert keys == expected_keys

    def test_uploaded_object_is_json_with_content_type(self, s3):
        # Arrange
        code = next(iter(COUNTRY_REQUIREMENTS))
        reqs = COUNTRY_REQUIREMENTS[code]

        # Act
        handler({}, None)

        # Assert
        obj = s3.get_object(Bucket=KNOWLEDGE_BASE_BUCKET, Key=f"{S3_PREFIX}/{code}.json")
        assert obj["ContentType"] == "application/json"
        body = json.loads(obj["Body"].read())
        assert body == _build_document(code, reqs)
        assert body["country_code"] == reqs.country_code

    def test_uses_patched_requirements(self, s3, monkeypatch):
        # Arrange
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"XX": _make_requirements(country_code="XX")},
        )

        # Act
        result = handler({}, None)

        # Assert
        assert result["body"]["documents_synced"] == 1
        body = json.loads(
            s3.get_object(Bucket=KNOWLEDGE_BASE_BUCKET, Key=f"{S3_PREFIX}/XX.json")["Body"].read()
        )
        assert body["country_code"] == "XX"
        assert body["country_name"] == "XX"


class TestBuildDocumentBaseFields:
    """_build_document always emits the core fields and resolves country names."""

    def test_core_fields_present(self):
        # Arrange
        reqs = _make_requirements()

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB"],
        }

    def test_unknown_country_falls_back_to_code(self):
        # Arrange
        reqs = _make_requirements(country_code="ZZ")

        # Act
        doc = _build_document("ZZ", reqs)

        # Assert
        assert doc["country_name"] == "ZZ"

    def test_no_optional_fields_when_unset(self):
        # Arrange
        reqs = _make_requirements()

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        for key in (
            "embassy_url",
            "evisa_portal_url",
            "estimated_processing_days",
            "entry_form_url",
        ):
            assert key not in doc


class TestBuildDocumentOptionalFields:
    """Each optional field is included only when set on the requirements."""

    def test_embassy_url_included(self):
        # Arrange
        reqs = _make_requirements(embassy_url="https://embassy.example")

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc["embassy_url"] == "https://embassy.example"
        assert "evisa_portal_url" not in doc

    def test_evisa_portal_url_included(self):
        # Arrange
        reqs = _make_requirements(evisa_portal_url="https://evisa.example")

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc["evisa_portal_url"] == "https://evisa.example"
        assert "embassy_url" not in doc

    def test_estimated_processing_days_included(self):
        # Arrange
        reqs = _make_requirements(estimated_processing_days=14)

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc["estimated_processing_days"] == 14

    def test_estimated_processing_days_zero_is_included(self):
        # Arrange
        reqs = _make_requirements(estimated_processing_days=0)

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        # Arrange
        reqs = _make_requirements(entry_form_url="https://entry.example")

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc["entry_form_url"] == "https://entry.example"

    def test_empty_string_urls_are_omitted(self):
        # Arrange
        reqs = _make_requirements(embassy_url="", evisa_portal_url="", entry_form_url="")

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc

    def test_all_optional_fields_included(self):
        # Arrange
        reqs = _make_requirements(
            embassy_url="https://embassy.example",
            evisa_portal_url="https://evisa.example",
            estimated_processing_days=7,
            entry_form_url="https://entry.example",
        )

        # Act
        doc = _build_document("DE", reqs)

        # Assert
        assert doc["embassy_url"] == "https://embassy.example"
        assert doc["evisa_portal_url"] == "https://evisa.example"
        assert doc["estimated_processing_days"] == 7
        assert doc["entry_form_url"] == "https://entry.example"
