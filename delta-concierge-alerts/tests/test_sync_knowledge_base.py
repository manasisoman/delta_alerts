"""Tests for the sync_knowledge_base Lambda handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_kb_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.models.types import TravelDocRequirements


def _base_reqs(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and optional overrides."""
    defaults = dict(
        country_code="DE",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB"],
    )
    defaults.update(overrides)
    return TravelDocRequirements(**defaults)


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto S3 mock and monkey-patch the module-level s3_client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="delta-concierge-kb")

        orig_client = sync_kb_mod.s3_client
        sync_kb_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_kb_mod.s3_client = orig_client


@pytest.fixture
def s3_client(_mock_aws):
    """Expose the moto S3 client for assertions."""
    return _mock_aws


class TestHandler:
    """handler() iterates COUNTRY_REQUIREMENTS and uploads each as JSON to S3."""

    def test_happy_path_response_shape(self):
        """Returns 200 with documents_synced equal to the number of countries."""
        result = sync_kb_mod.handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_correct_number_of_s3_objects(self, s3_client):
        """Creates one S3 object per country in COUNTRY_REQUIREMENTS."""
        sync_kb_mod.handler({}, None)

        objects = s3_client.list_objects_v2(Bucket="delta-concierge-kb")
        assert objects["KeyCount"] == len(COUNTRY_REQUIREMENTS)

    def test_s3_key_format(self, s3_client):
        """Each S3 object key follows the pattern country-requirements/{CC}.json."""
        sync_kb_mod.handler({}, None)

        objects = s3_client.list_objects_v2(Bucket="delta-concierge-kb")
        keys = {obj["Key"] for obj in objects["Contents"]}

        for cc in COUNTRY_REQUIREMENTS:
            assert f"country-requirements/{cc}.json" in keys

    def test_uploaded_document_is_valid_json(self, s3_client):
        """Each uploaded object is parseable JSON with expected required fields."""
        sync_kb_mod.handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/DE.json",
        )
        doc = json.loads(obj["Body"].read())

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert "visa_required" in doc
        assert "transit_visa_required" in doc
        assert "passport_validity_months" in doc
        assert "visa_exempt_nationalities" in doc


class TestBuildDocumentRequiredFields:
    """_build_document() always includes the six required fields."""

    def test_required_fields_present(self):
        """All required fields appear in the output."""
        reqs = _base_reqs()

        doc = sync_kb_mod._build_document("DE", reqs)

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is False
        assert doc["passport_validity_months"] == 3
        assert doc["visa_exempt_nationalities"] == ["US", "GB"]

    def test_no_optional_fields_when_none(self):
        """Optional fields are absent when not set on TravelDocRequirements."""
        reqs = _base_reqs()

        doc = sync_kb_mod._build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc


class TestBuildDocumentOptionalFields:
    """_build_document() includes optional fields only when they are not None."""

    def test_all_optional_fields_included(self):
        """When all optional fields are set, they all appear in the output."""
        reqs = _base_reqs(
            embassy_url="https://embassy.example.com",
            evisa_portal_url="https://evisa.example.com",
            estimated_processing_days=14,
            entry_form_url="https://entryform.example.com",
        )

        doc = sync_kb_mod._build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert doc["estimated_processing_days"] == 14
        assert doc["entry_form_url"] == "https://entryform.example.com"

    def test_only_embassy_url(self):
        """Only embassy_url appears when only it is set."""
        reqs = _base_reqs(embassy_url="https://embassy.example.com")

        doc = sync_kb_mod._build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_only_evisa_portal_url(self):
        """Only evisa_portal_url appears when only it is set."""
        reqs = _base_reqs(evisa_portal_url="https://evisa.example.com")

        doc = sync_kb_mod._build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_only_estimated_processing_days(self):
        """Only estimated_processing_days appears when only it is set."""
        reqs = _base_reqs(estimated_processing_days=7)

        doc = sync_kb_mod._build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert doc["estimated_processing_days"] == 7
        assert "entry_form_url" not in doc

    def test_only_entry_form_url(self):
        """Only entry_form_url appears when only it is set."""
        reqs = _base_reqs(entry_form_url="https://entryform.example.com")

        doc = sync_kb_mod._build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert doc["entry_form_url"] == "https://entryform.example.com"


class TestBuildDocumentCountryNameLookup:
    """_build_document() resolves country_code to a human-readable name."""

    def test_known_country_code(self):
        """Known code maps to the full country name."""
        reqs = _base_reqs(country_code="JP")

        doc = sync_kb_mod._build_document("JP", reqs)

        assert doc["country_name"] == "Japan"

    def test_unknown_country_code_falls_back(self):
        """Unknown code falls back to the raw country_code string."""
        reqs = _base_reqs(country_code="ZZ")

        doc = sync_kb_mod._build_document("ZZ", reqs)

        assert doc["country_name"] == "ZZ"
