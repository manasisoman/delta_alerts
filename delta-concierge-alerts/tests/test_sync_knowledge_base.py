"""Tests for the sync_knowledge_base Lambda handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.models.types import TravelDocRequirements


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto S3 mock, create the bucket, and monkey-patch the module client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="delta-concierge-kb")
        orig_s3_client = sync_mod.s3_client
        sync_mod.s3_client = s3
        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client


@pytest.fixture
def s3_client(_mock_aws):
    """Expose the moto S3 client for assertions."""
    return _mock_aws


class TestSyncHandler:
    """Handler syncs all country requirement documents to S3."""

    def test_documents_synced_count_matches_requirements(self, s3_client):
        """Handler returns documents_synced equal to COUNTRY_REQUIREMENTS length."""
        result = sync_mod.handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_s3_objects_written(self, s3_client):
        """Every country in COUNTRY_REQUIREMENTS has a corresponding S3 object."""
        sync_mod.handler({}, None)

        objects = s3_client.list_objects_v2(
            Bucket="delta-concierge-kb", Prefix="country-requirements/"
        )
        keys = {obj["Key"] for obj in objects["Contents"]}

        for code in COUNTRY_REQUIREMENTS:
            assert f"country-requirements/{code}.json" in keys

    def test_uploaded_document_content(self, s3_client):
        """An uploaded document contains the expected fields and values."""
        sync_mod.handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb", Key="country-requirements/DE.json"
        )
        doc = json.loads(obj["Body"].read())

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is False
        assert doc["passport_validity_months"] == 3
        assert "US" in doc["visa_exempt_nationalities"]


class TestBuildDocument:
    """_build_document produces correct dicts with optional fields handled."""

    def test_all_optional_fields_present(self):
        """When all optional fields are set, they appear in the output."""
        reqs = TravelDocRequirements(
            country_code="XX",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=["US"],
            embassy_url="https://embassy.example.com",
            evisa_portal_url="https://evisa.example.com",
            estimated_processing_days=10,
            entry_form_url="https://entry.example.com",
        )

        doc = sync_mod._build_document("XX", reqs)

        assert doc["country_code"] == "XX"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is True
        assert doc["passport_validity_months"] == 6
        assert doc["visa_exempt_nationalities"] == ["US"]
        assert doc["embassy_url"] == "https://embassy.example.com"
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert doc["estimated_processing_days"] == 10
        assert doc["entry_form_url"] == "https://entry.example.com"

    def test_no_optional_fields(self):
        """When optional fields are None/empty, they are absent from the output."""
        reqs = TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB"],
        )

        doc = sync_mod._build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_known_country_name_lookup(self):
        """A known country code maps to its full name."""
        reqs = TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        )

        doc = sync_mod._build_document("DE", reqs)

        assert doc["country_name"] == "Germany"

    def test_unknown_country_code_falls_back(self):
        """An unknown country code uses the code itself as the name."""
        reqs = TravelDocRequirements(
            country_code="ZZ",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )

        doc = sync_mod._build_document("ZZ", reqs)

        assert doc["country_name"] == "ZZ"

    def test_partial_optional_fields_embassy_only(self):
        """Only embassy_url set — it appears, others are absent."""
        reqs = TravelDocRequirements(
            country_code="CA",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            embassy_url="https://embassy.example.com",
        )

        doc = sync_mod._build_document("CA", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_partial_optional_fields_evisa_only(self):
        """Only evisa_portal_url set — it appears, others are absent."""
        reqs = TravelDocRequirements(
            country_code="TR",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            evisa_portal_url="https://evisa.example.com",
        )

        doc = sync_mod._build_document("TR", reqs)

        assert "embassy_url" not in doc
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_partial_optional_fields_entry_form_only(self):
        """Only entry_form_url set — it appears, others are absent."""
        reqs = TravelDocRequirements(
            country_code="DO",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            entry_form_url="https://entry.example.com",
        )

        doc = sync_mod._build_document("DO", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert doc["entry_form_url"] == "https://entry.example.com"

    def test_estimated_processing_days_zero_is_included(self):
        """estimated_processing_days=0 is not None, so it should appear."""
        reqs = TravelDocRequirements(
            country_code="XX",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            estimated_processing_days=0,
        )

        doc = sync_mod._build_document("XX", reqs)

        assert doc["estimated_processing_days"] == 0
