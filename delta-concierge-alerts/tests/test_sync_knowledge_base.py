"""Tests for the Bedrock Knowledge Base sync handler."""

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


def make_requirements(**overrides) -> TravelDocRequirements:
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


class TestHandlerSync:
    """The handler uploads one JSON document per country and reports the count."""

    def test_returns_200_and_count_of_all_countries(self, s3):
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploads_one_object_per_country_under_prefix(self, s3):
        handler({}, None)

        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)["Contents"]
        }
        assert keys == {
            f"{sync_mod.S3_PREFIX}/{code}.json" for code in COUNTRY_REQUIREMENTS
        }

    def test_uploaded_object_is_json_with_country_content_type(self, s3):
        handler({}, None)

        obj = s3.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key=f"{sync_mod.S3_PREFIX}/DE.json",
        )
        document = json.loads(obj["Body"].read())

        assert obj["ContentType"] == "application/json"
        assert document["country_code"] == "DE"
        assert document["country_name"] == "Germany"
        assert document["passport_validity_months"] == 3

    def test_schengen_and_six_month_countries_keep_their_validity_windows(self, s3):
        handler({}, None)

        def fetch(code: str) -> dict:
            body = s3.get_object(
                Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
                Key=f"{sync_mod.S3_PREFIX}/{code}.json",
            )["Body"].read()
            return json.loads(body)

        assert fetch("FR")["passport_validity_months"] == 3
        for code in ("US", "GB", "JP", "AU", "CN", "IN", "BR"):
            assert fetch(code)["passport_validity_months"] == 6

    def test_ignores_event_and_context_payloads(self, s3):
        result = handler({"detail": {"unused": True}}, object())

        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_rerun_overwrites_existing_documents(self, s3):
        handler({}, None)
        handler({}, None)

        objects = s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)["Contents"]
        assert len(objects) == len(COUNTRY_REQUIREMENTS)


class TestBuildDocumentRequiredFields:
    """Required requirement fields are always present in the document."""

    def test_maps_core_fields(self):
        doc = _build_document("CN", make_requirements(
            country_code="CN",
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

    def test_visa_required_false_is_preserved(self):
        doc = _build_document("US", make_requirements(country_code="US", requires_visa=False))

        assert doc["visa_required"] is False

    def test_transit_visa_flag_marks_layover_sensitive_countries(self):
        doc = _build_document("CN", make_requirements(
            country_code="CN", transit_visa_required=True,
        ))

        assert doc["transit_visa_required"] is True

    def test_exempt_nationalities_are_copied_through(self):
        doc = _build_document("DE", make_requirements(
            visa_exempt_nationalities=["US", "GB", "CA"],
        ))

        assert doc["visa_exempt_nationalities"] == ["US", "GB", "CA"]

    def test_unknown_country_code_falls_back_to_the_code_as_name(self):
        doc = _build_document("ZZ", make_requirements(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_country_code_comes_from_requirements_not_the_key(self):
        doc = _build_document("DE", make_requirements(country_code="FR"))

        assert doc["country_code"] == "FR"
        assert doc["country_name"] == "Germany"


class TestBuildDocumentOptionalFields:
    """Optional fields are included only when populated."""

    def test_omits_all_optional_fields_when_unset(self):
        doc = _build_document("DE", make_requirements())

        for key in (
            "embassy_url",
            "evisa_portal_url",
            "estimated_processing_days",
            "entry_form_url",
        ):
            assert key not in doc

    def test_includes_embassy_url(self):
        doc = _build_document("DE", make_requirements(embassy_url="https://embassy.de"))

        assert doc["embassy_url"] == "https://embassy.de"

    def test_includes_evisa_portal_url(self):
        doc = _build_document("IN", make_requirements(
            country_code="IN", evisa_portal_url="https://evisa.in",
        ))

        assert doc["evisa_portal_url"] == "https://evisa.in"

    def test_includes_entry_form_url(self):
        doc = _build_document("JP", make_requirements(
            country_code="JP", entry_form_url="https://entry.jp",
        ))

        assert doc["entry_form_url"] == "https://entry.jp"

    def test_includes_zero_processing_days(self):
        doc = _build_document("DE", make_requirements(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_includes_positive_processing_days(self):
        doc = _build_document("CN", make_requirements(
            country_code="CN", estimated_processing_days=14,
        ))

        assert doc["estimated_processing_days"] == 14

    def test_empty_string_urls_are_omitted(self):
        doc = _build_document("DE", make_requirements(
            embassy_url="", evisa_portal_url="", entry_form_url="",
        ))

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc

    def test_all_optional_fields_together(self):
        doc = _build_document("DE", make_requirements(
            embassy_url="https://embassy.de",
            evisa_portal_url="https://evisa.de",
            estimated_processing_days=5,
            entry_form_url="https://entry.de",
        ))

        assert doc["embassy_url"] == "https://embassy.de"
        assert doc["evisa_portal_url"] == "https://evisa.de"
        assert doc["estimated_processing_days"] == 5
        assert doc["entry_form_url"] == "https://entry.de"
