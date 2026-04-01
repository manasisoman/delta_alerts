"""Tests for the sync_knowledge_base Lambda handler.

Covers the handler end-to-end (S3 uploads via moto), the _build_document
helper with all optional-field branches, and the _COUNTRY_NAMES fallback.
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
def _mock_s3():
    """Activate moto S3 mock and patch the module-level client for every test."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="delta-concierge-kb")

        orig_client = sync_mod.s3_client
        sync_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_client


@pytest.fixture
def s3_client(_mock_s3):
    """Expose the moto S3 client."""
    return _mock_s3


# ------------------------------------------------------------------
# _build_document tests
# ------------------------------------------------------------------

class TestBuildDocument:
    """Unit tests for the _build_document helper."""

    def test_minimal_requirements_no_optional_fields(self):
        """When no optional fields are set, the doc should not contain them."""
        reqs = TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB"],
        )

        doc = _build_document("DE", reqs)

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is False
        assert doc["passport_validity_months"] == 3
        assert doc["visa_exempt_nationalities"] == ["US", "GB"]
        # Optional keys must be absent
        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_all_optional_fields_present(self):
        """When all optional fields are set, the doc should include them."""
        reqs = TravelDocRequirements(
            country_code="IL",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US"],
            embassy_url="https://embassies.gov.il/",
            evisa_portal_url="https://evisa.example.com/",
            estimated_processing_days=14,
            entry_form_url="https://www.gov.il/en/service/request-entry-permit",
        )

        doc = _build_document("IL", reqs)

        assert doc["embassy_url"] == "https://embassies.gov.il/"
        assert doc["evisa_portal_url"] == "https://evisa.example.com/"
        assert doc["estimated_processing_days"] == 14
        assert doc["entry_form_url"] == "https://www.gov.il/en/service/request-entry-permit"
        assert doc["country_name"] == "Israel"

    def test_country_name_fallback_for_unknown_code(self):
        """An unknown country code should fall back to the code itself."""
        reqs = TravelDocRequirements(
            country_code="XX",
            requires_visa=False,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )

        doc = _build_document("XX", reqs)

        assert doc["country_name"] == "XX"

    def test_only_embassy_url_set(self):
        """Only embassy_url is set among optional fields."""
        reqs = TravelDocRequirements(
            country_code="MX",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US"],
            embassy_url="https://embamex.sre.gob.mx/",
        )

        doc = _build_document("MX", reqs)

        assert doc["embassy_url"] == "https://embamex.sre.gob.mx/"
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc

    def test_only_evisa_portal_url_set(self):
        """Only evisa_portal_url is set among optional fields."""
        reqs = TravelDocRequirements(
            country_code="TH",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            evisa_portal_url="https://www.thaievisa.go.th/",
        )

        doc = _build_document("TH", reqs)

        assert doc["evisa_portal_url"] == "https://www.thaievisa.go.th/"
        assert "embassy_url" not in doc

    def test_only_entry_form_url_set(self):
        """Only entry_form_url is set among optional fields."""
        reqs = TravelDocRequirements(
            country_code="DO",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            entry_form_url="https://eticket.migracion.gob.do/",
        )

        doc = _build_document("DO", reqs)

        assert doc["entry_form_url"] == "https://eticket.migracion.gob.do/"
        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc


# ------------------------------------------------------------------
# handler end-to-end tests
# ------------------------------------------------------------------

class TestHandlerEndToEnd:
    """Integration tests for the sync_knowledge_base handler."""

    def test_handler_uploads_all_countries_and_returns_count(self, s3_client):
        """Handler should upload one JSON doc per country and return the count."""
        from src.data.country_requirements import COUNTRY_REQUIREMENTS

        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploaded_objects_are_valid_json(self, s3_client):
        """Each uploaded S3 object should be parseable JSON with expected keys."""
        handler({}, None)

        # Spot-check a known country
        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/DE.json",
        )
        doc = json.loads(obj["Body"].read().decode())

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert "visa_required" in doc
        assert "passport_validity_months" in doc

    def test_uploaded_objects_have_correct_content_type(self, s3_client):
        """S3 objects should be uploaded with application/json content type."""
        handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/CN.json",
        )
        assert obj["ContentType"] == "application/json"

    def test_s3_key_prefix_is_correct(self, s3_client):
        """All uploaded keys should be under the country-requirements/ prefix."""
        handler({}, None)

        listing = s3_client.list_objects_v2(
            Bucket="delta-concierge-kb",
            Prefix="country-requirements/",
        )
        keys = [o["Key"] for o in listing["Contents"]]

        assert all(k.startswith("country-requirements/") for k in keys)
        assert all(k.endswith(".json") for k in keys)

    def test_country_with_optional_fields_persisted(self, s3_client):
        """A country with optional fields (e.g. IL) should have them in S3."""
        handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/IL.json",
        )
        doc = json.loads(obj["Body"].read().decode())

        assert "embassy_url" in doc
        assert "entry_form_url" in doc
        assert "estimated_processing_days" in doc
