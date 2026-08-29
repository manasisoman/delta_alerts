"""Tests for the knowledge base sync handler.

Covers the S3 upload happy path, the empty-requirements branch, and
every optional-field branch of the document builder.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks and create the knowledge base bucket."""
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
    """Expose the mocked S3 client."""
    return _mock_aws


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
    """Helper to build TravelDocRequirements with sensible defaults."""
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


def _read_key(s3, key: str) -> dict:
    """Fetch and JSON-decode an object from the mocked bucket."""
    body = s3.get_object(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key=key)["Body"].read()
    return json.loads(body)


# ------------------------------------------------------------------
# 1. Happy path — documents uploaded for every country
# ------------------------------------------------------------------

class TestSyncHappyPath:
    """Every country in COUNTRY_REQUIREMENTS is uploaded under the S3 prefix."""

    def test_uploads_one_document_per_country(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"DE": make_requirements("DE"), "JP": make_requirements("JP")},
        )

        response = handler({}, None)

        assert response == {"statusCode": 200, "body": {"documents_synced": 2}}
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)["Contents"]
        }
        assert keys == {
            f"{sync_mod.S3_PREFIX}/DE.json",
            f"{sync_mod.S3_PREFIX}/JP.json",
        }

    def test_uploaded_document_contains_requirement_fields(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {
                "CN": make_requirements(
                    "CN",
                    transit_visa_required=True,
                    passport_validity_months=6,
                    visa_exempt_nationalities=[],
                )
            },
        )

        handler({}, None)

        document = _read_key(s3, f"{sync_mod.S3_PREFIX}/CN.json")
        assert document == {
            "country_code": "CN",
            "country_name": "China",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": [],
        }

    def test_uploads_with_json_content_type(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")}
        )

        handler({}, None)

        head = s3.head_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key=f"{sync_mod.S3_PREFIX}/DE.json"
        )
        assert head["ContentType"] == "application/json"

    def test_syncs_full_production_requirements(self, s3):
        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(sync_mod.COUNTRY_REQUIREMENTS)


# ------------------------------------------------------------------
# 2. Empty requirements — nothing uploaded
# ------------------------------------------------------------------

class TestSyncEmptyRequirements:
    """An empty requirements mapping uploads nothing but still succeeds."""

    def test_no_documents_synced(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {})

        response = handler({}, None)

        assert response == {"statusCode": 200, "body": {"documents_synced": 0}}
        assert "Contents" not in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)


# ------------------------------------------------------------------
# 3. Missing bucket — S3 error propagates
# ------------------------------------------------------------------

class TestSyncMissingBucket:
    """Upload failures are not swallowed by the handler."""

    def test_raises_when_bucket_absent(self, s3, monkeypatch):
        s3.delete_bucket(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)
        monkeypatch.setattr(
            sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")}
        )

        with pytest.raises(s3.exceptions.NoSuchBucket):
            handler({}, None)


# ------------------------------------------------------------------
# 4. Document builder — optional field branches
# ------------------------------------------------------------------

class TestBuildDocumentOptionalFields:
    """Optional requirement fields only appear in the document when set."""

    def test_optional_fields_omitted_when_unset(self):
        document = _build_document("DE", make_requirements("DE"))

        assert "embassy_url" not in document
        assert "evisa_portal_url" not in document
        assert "estimated_processing_days" not in document
        assert "entry_form_url" not in document

    def test_embassy_url_included(self):
        document = _build_document(
            "DE", make_requirements("DE", embassy_url="https://embassy.example/de")
        )

        assert document["embassy_url"] == "https://embassy.example/de"

    def test_evisa_portal_url_included(self):
        document = _build_document(
            "IN", make_requirements("IN", evisa_portal_url="https://evisa.example/in")
        )

        assert document["evisa_portal_url"] == "https://evisa.example/in"

    def test_estimated_processing_days_included(self):
        document = _build_document(
            "CN", make_requirements("CN", estimated_processing_days=10)
        )

        assert document["estimated_processing_days"] == 10

    def test_zero_processing_days_included(self):
        document = _build_document(
            "SG", make_requirements("SG", estimated_processing_days=0)
        )

        assert document["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        document = _build_document(
            "GB", make_requirements("GB", entry_form_url="https://entry.example/gb")
        )

        assert document["entry_form_url"] == "https://entry.example/gb"


# ------------------------------------------------------------------
# 5. Document builder — country name lookup
# ------------------------------------------------------------------

class TestBuildDocumentCountryName:
    """Country names resolve from the lookup table, falling back to the code."""

    def test_known_code_resolves_to_name(self):
        document = _build_document("FR", make_requirements("FR"))

        assert document["country_name"] == "France"

    def test_unknown_code_falls_back_to_code(self):
        document = _build_document("ZZ", make_requirements("ZZ"))

        assert document["country_name"] == "ZZ"
