"""Tests for the knowledge base sync Lambda handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as kb_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

TEST_BUCKET = "test-kb-bucket"


def _make_requirements(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and optional overrides."""
    fields = {
        "country_code": "XA",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


@pytest.fixture
def s3():
    """Activate moto, create the KB bucket, and patch module-level S3 settings."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)

        orig_client = kb_mod.s3_client
        orig_bucket = kb_mod.KNOWLEDGE_BASE_BUCKET
        orig_requirements = kb_mod.COUNTRY_REQUIREMENTS

        kb_mod.s3_client = client
        kb_mod.KNOWLEDGE_BASE_BUCKET = TEST_BUCKET

        try:
            yield client
        finally:
            kb_mod.s3_client = orig_client
            kb_mod.KNOWLEDGE_BASE_BUCKET = orig_bucket
            kb_mod.COUNTRY_REQUIREMENTS = orig_requirements


def _get_document(client, key: str) -> dict:
    """Read and decode a JSON document from the test bucket."""
    return json.loads(client.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read())


class TestHandlerSyncsDocuments:
    """The handler uploads one JSON document per country and reports the count."""

    def test_uploads_each_country_and_returns_count(self, s3):
        kb_mod.COUNTRY_REQUIREMENTS = {
            "XA": _make_requirements(country_code="XA"),
            "XB": _make_requirements(country_code="XB", passport_validity_months=6),
        }

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 2}}
        keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=TEST_BUCKET)["Contents"])
        assert keys == ["country-requirements/XA.json", "country-requirements/XB.json"]

    def test_uploaded_body_matches_built_document(self, s3):
        reqs = _make_requirements(country_code="XA")
        kb_mod.COUNTRY_REQUIREMENTS = {"XA": reqs}

        handler({}, None)

        assert _get_document(s3, "country-requirements/XA.json") == _build_document("XA", reqs)

    def test_uploads_with_json_content_type(self, s3):
        kb_mod.COUNTRY_REQUIREMENTS = {"XA": _make_requirements()}

        handler({}, None)

        head = s3.head_object(Bucket=TEST_BUCKET, Key="country-requirements/XA.json")
        assert head["ContentType"] == "application/json"

    def test_empty_requirements_syncs_nothing(self, s3):
        kb_mod.COUNTRY_REQUIREMENTS = {}

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 0}}
        assert s3.list_objects_v2(Bucket=TEST_BUCKET)["KeyCount"] == 0

    def test_syncs_all_real_country_requirements(self, s3):
        result = handler({}, None)

        assert result["body"]["documents_synced"] == len(kb_mod.COUNTRY_REQUIREMENTS)
        assert s3.list_objects_v2(Bucket=TEST_BUCKET)["KeyCount"] == len(kb_mod.COUNTRY_REQUIREMENTS)


class TestBuildDocumentRequiredFields:
    """Core requirement fields are always present in the document."""

    def test_includes_core_fields(self):
        reqs = _make_requirements(
            country_code="XA",
            requires_visa=False,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=["CA"],
        )

        doc = _build_document("XA", reqs)

        assert doc == {
            "country_code": "XA",
            "country_name": "XA",
            "visa_required": False,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": ["CA"],
        }


class TestBuildDocumentCountryName:
    """Country name comes from the lookup table, falling back to the code."""

    def test_known_country_uses_display_name(self):
        reqs = _make_requirements(country_code="DE")

        doc = _build_document("DE", reqs)

        assert doc["country_name"] == "Germany"

    def test_unknown_country_falls_back_to_code(self):
        reqs = _make_requirements(country_code="XA")

        doc = _build_document("XA", reqs)

        assert doc["country_name"] == "XA"


class TestBuildDocumentOptionalFields:
    """Optional fields are included only when set."""

    def test_omits_optional_fields_when_unset(self):
        reqs = _make_requirements()

        doc = _build_document("XA", reqs)

        for field in ("embassy_url", "evisa_portal_url", "estimated_processing_days", "entry_form_url"):
            assert field not in doc

    def test_includes_embassy_url(self):
        reqs = _make_requirements(embassy_url="https://embassy.example")

        doc = _build_document("XA", reqs)

        assert doc["embassy_url"] == "https://embassy.example"

    def test_includes_evisa_portal_url(self):
        reqs = _make_requirements(evisa_portal_url="https://evisa.example")

        doc = _build_document("XA", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_includes_estimated_processing_days(self):
        reqs = _make_requirements(estimated_processing_days=10)

        doc = _build_document("XA", reqs)

        assert doc["estimated_processing_days"] == 10

    def test_includes_zero_processing_days(self):
        reqs = _make_requirements(estimated_processing_days=0)

        doc = _build_document("XA", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_includes_entry_form_url(self):
        reqs = _make_requirements(entry_form_url="https://form.example")

        doc = _build_document("XA", reqs)

        assert doc["entry_form_url"] == "https://form.example"

    def test_omits_empty_string_urls(self):
        reqs = _make_requirements(embassy_url="", evisa_portal_url="", entry_form_url="")

        doc = _build_document("XA", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc
