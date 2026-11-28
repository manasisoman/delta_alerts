"""Tests for the Bedrock Knowledge Base S3 sync handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


def _make_requirements(country_code: str = "XA", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and no optional fields."""
    fields = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


@pytest.fixture
def requirements_override():
    """Swap the handler's COUNTRY_REQUIREMENTS for test data; restore afterwards."""
    original = sync_mod.COUNTRY_REQUIREMENTS
    replacement: dict[str, TravelDocRequirements] = {}
    sync_mod.COUNTRY_REQUIREMENTS = replacement
    try:
        yield replacement
    finally:
        sync_mod.COUNTRY_REQUIREMENTS = original


@pytest.fixture(autouse=True)
def _mock_aws():
    """Provide a moto S3 bucket and patch the module-level s3_client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)

        orig_s3_client = sync_mod.s3_client
        sync_mod.s3_client = s3
        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client


def _list_keys(s3) -> list[str]:
    resp = s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)
    return sorted(obj["Key"] for obj in resp.get("Contents", []))


def _get_document(s3, key: str) -> dict:
    obj = s3.get_object(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key=key)
    return json.loads(obj["Body"].read())


class TestBuildDocumentRequiredFields:
    """Every document carries the core requirement fields."""

    def test_core_fields_copied_from_requirements(self):
        reqs = _make_requirements(
            "XB",
            requires_visa=False,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=["CA"],
        )

        doc = _build_document("XB", reqs)

        assert doc == {
            "country_code": "XB",
            "country_name": "XB",
            "visa_required": False,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": ["CA"],
        }

    def test_known_country_code_resolves_to_name(self):
        reqs = _make_requirements("DE")

        doc = _build_document("DE", reqs)

        assert doc["country_name"] == "Germany"

    def test_unknown_country_code_falls_back_to_code(self):
        reqs = _make_requirements("XA")

        doc = _build_document("XA", reqs)

        assert doc["country_name"] == "XA"


class TestBuildDocumentOptionalFields:
    """Optional fields appear only when they are set on the requirements."""

    def test_optional_fields_omitted_when_unset(self):
        reqs = _make_requirements()

        doc = _build_document("XA", reqs)

        for key in (
            "embassy_url",
            "evisa_portal_url",
            "estimated_processing_days",
            "entry_form_url",
        ):
            assert key not in doc

    def test_embassy_url_included(self):
        reqs = _make_requirements(embassy_url="https://embassy.example/xa")

        doc = _build_document("XA", reqs)

        assert doc["embassy_url"] == "https://embassy.example/xa"

    def test_evisa_portal_url_included(self):
        reqs = _make_requirements(evisa_portal_url="https://evisa.example/xa")

        doc = _build_document("XA", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example/xa"

    def test_estimated_processing_days_included(self):
        reqs = _make_requirements(estimated_processing_days=10)

        doc = _build_document("XA", reqs)

        assert doc["estimated_processing_days"] == 10

    def test_zero_processing_days_still_included(self):
        reqs = _make_requirements(estimated_processing_days=0)

        doc = _build_document("XA", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        reqs = _make_requirements(entry_form_url="https://form.example/xa")

        doc = _build_document("XA", reqs)

        assert doc["entry_form_url"] == "https://form.example/xa"

    def test_empty_string_urls_omitted(self):
        reqs = _make_requirements(
            embassy_url="", evisa_portal_url="", entry_form_url=""
        )

        doc = _build_document("XA", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc


class TestHandlerSync:
    """The handler uploads one JSON document per country and reports the count."""

    def test_uploads_one_document_per_country(self, _mock_aws, requirements_override):
        requirements_override["XA"] = _make_requirements("XA")
        requirements_override["XB"] = _make_requirements(
            "XB", passport_validity_months=6
        )

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 2}}
        assert _list_keys(_mock_aws) == [
            "country-requirements/XA.json",
            "country-requirements/XB.json",
        ]

    def test_uploaded_body_matches_built_document(self, _mock_aws, requirements_override):
        reqs = _make_requirements(
            "XA", embassy_url="https://embassy.example/xa", estimated_processing_days=5
        )
        requirements_override["XA"] = reqs

        handler({}, None)

        assert _get_document(_mock_aws, "country-requirements/XA.json") == _build_document(
            "XA", reqs
        )

    def test_uploaded_object_has_json_content_type(self, _mock_aws, requirements_override):
        requirements_override["XA"] = _make_requirements("XA")

        handler({}, None)

        head = _mock_aws.head_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key="country-requirements/XA.json"
        )
        assert head["ContentType"] == "application/json"

    def test_empty_requirements_syncs_nothing(self, _mock_aws, requirements_override):
        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 0}}
        assert _list_keys(_mock_aws) == []

    def test_syncs_real_country_requirements(self, _mock_aws):
        result = handler({}, None)

        expected = len(sync_mod.COUNTRY_REQUIREMENTS)
        assert result["body"]["documents_synced"] == expected
        assert len(_list_keys(_mock_aws)) == expected
