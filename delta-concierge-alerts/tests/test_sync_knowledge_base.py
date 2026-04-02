"""Tests for the sync_knowledge_base handler — S3 document upload for Bedrock KB."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


BUCKET = "delta-concierge-kb"


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto S3 mock and monkey-patch the module-level s3_client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        orig_s3_client = sync_mod.s3_client
        sync_mod.s3_client = s3
        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client


# ---------------------------------------------------------------------------
# Handler tests
# ---------------------------------------------------------------------------

class TestHandlerUploadsDocuments:
    """handler() uploads one JSON document per country and returns the count."""

    def test_documents_synced_count_matches_country_requirements(self, _mock_aws):
        """documents_synced in the response equals len(COUNTRY_REQUIREMENTS)."""
        s3 = _mock_aws

        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_s3_objects_created_at_correct_keys(self, _mock_aws):
        """Each country produces an object at country-requirements/{code}.json."""
        s3 = _mock_aws

        handler({}, None)

        objects = s3.list_objects_v2(Bucket=BUCKET, Prefix="country-requirements/")
        keys = {obj["Key"] for obj in objects["Contents"]}

        for code in COUNTRY_REQUIREMENTS:
            assert f"country-requirements/{code}.json" in keys

    def test_uploaded_document_content_for_known_country(self, _mock_aws):
        """Verify JSON content for CA which has all optional fields."""
        s3 = _mock_aws

        handler({}, None)

        resp = s3.get_object(Bucket=BUCKET, Key="country-requirements/CA.json")
        doc = json.loads(resp["Body"].read())

        assert doc["country_code"] == "CA"
        assert doc["country_name"] == "Canada"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is True
        assert doc["passport_validity_months"] == 6
        assert "US" in doc["visa_exempt_nationalities"]
        # CA has all optional fields
        assert doc["embassy_url"] == "https://www.canada.ca/en/immigration-refugees-citizenship.html"
        assert doc["evisa_portal_url"] == "https://www.canada.ca/en/immigration-refugees-citizenship/services/visit-canada/eta.html"
        assert doc["estimated_processing_days"] == 14

    def test_uploaded_document_for_country_without_optional_fields(self, _mock_aws):
        """DE has no optional fields; those keys should be absent from JSON."""
        s3 = _mock_aws

        handler({}, None)

        resp = s3.get_object(Bucket=BUCKET, Key="country-requirements/DE.json")
        doc = json.loads(resp["Body"].read())

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc


# ---------------------------------------------------------------------------
# _build_document tests
# ---------------------------------------------------------------------------

class TestBuildDocumentAllOptionalFields:
    """_build_document includes optional keys when the reqs object has them."""

    def test_all_optional_fields_present(self):
        reqs = TravelDocRequirements(
            country_code="XX",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US"],
            embassy_url="https://embassy.example.com",
            evisa_portal_url="https://evisa.example.com",
            estimated_processing_days=10,
            entry_form_url="https://entry.example.com",
        )

        doc = _build_document("XX", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert doc["estimated_processing_days"] == 10
        assert doc["entry_form_url"] == "https://entry.example.com"

    def test_base_fields_always_present(self):
        reqs = TravelDocRequirements(
            country_code="XX",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US"],
            embassy_url="https://embassy.example.com",
            evisa_portal_url="https://evisa.example.com",
            estimated_processing_days=10,
            entry_form_url="https://entry.example.com",
        )

        doc = _build_document("XX", reqs)

        assert doc["country_code"] == "XX"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is False
        assert doc["passport_validity_months"] == 6
        assert doc["visa_exempt_nationalities"] == ["US"]


class TestBuildDocumentNoOptionalFields:
    """_build_document omits optional keys when the reqs object lacks them."""

    def test_optional_keys_absent(self):
        reqs = TravelDocRequirements(
            country_code="YY",
            requires_visa=False,
            transit_visa_required=True,
            passport_validity_months=3,
            visa_exempt_nationalities=[],
        )

        doc = _build_document("YY", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc


class TestBuildDocumentCountryNameFallback:
    """_build_document falls back to country_code when not in _COUNTRY_NAMES."""

    def test_unknown_country_code_uses_code_as_name(self):
        reqs = TravelDocRequirements(
            country_code="ZZ",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )

        doc = _build_document("ZZ", reqs)

        assert doc["country_name"] == "ZZ"

    def test_known_country_code_uses_human_name(self):
        reqs = TravelDocRequirements(
            country_code="JP",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US"],
        )

        doc = _build_document("JP", reqs)

        assert doc["country_name"] == "Japan"
