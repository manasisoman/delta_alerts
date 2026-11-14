"""Tests for the sync_knowledge_base Lambda handler (S3 sync for Bedrock KB)."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as kb_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

TEST_BUCKET = "test-kb-bucket"


def _make_reqs(country_code: str = "DE", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and no optional URLs."""
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
def s3():
    """Moto S3 bucket with module-level client, bucket name and requirements swapped."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)

        orig_client = kb_mod.s3_client
        orig_bucket = kb_mod.KNOWLEDGE_BASE_BUCKET
        orig_reqs = kb_mod.COUNTRY_REQUIREMENTS
        kb_mod.s3_client = client
        kb_mod.KNOWLEDGE_BASE_BUCKET = TEST_BUCKET
        try:
            yield client
        finally:
            kb_mod.s3_client = orig_client
            kb_mod.KNOWLEDGE_BASE_BUCKET = orig_bucket
            kb_mod.COUNTRY_REQUIREMENTS = orig_reqs


def _set_requirements(reqs: dict) -> None:
    kb_mod.COUNTRY_REQUIREMENTS = reqs


def _list_keys(client) -> list[str]:
    resp = client.list_objects_v2(Bucket=TEST_BUCKET)
    return sorted(obj["Key"] for obj in resp.get("Contents", []))


def _get_doc(client, key: str) -> dict:
    return json.loads(client.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read())


class TestHandlerSync:
    """Handler uploads one JSON document per country and reports the count."""

    def test_uploads_one_document_per_country(self, s3):
        _set_requirements({"DE": _make_reqs("DE"), "JP": _make_reqs("JP")})

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 2}}
        assert _list_keys(s3) == [
            "country-requirements/DE.json",
            "country-requirements/JP.json",
        ]

    def test_empty_requirements_syncs_nothing(self, s3):
        _set_requirements({})

        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == 0
        assert _list_keys(s3) == []

    def test_uploaded_body_matches_built_document(self, s3):
        reqs = _make_reqs("DE", embassy_url="https://de.example")
        _set_requirements({"DE": reqs})

        handler({}, None)

        assert _get_doc(s3, "country-requirements/DE.json") == _build_document("DE", reqs)

    def test_uploaded_object_has_json_content_type(self, s3):
        _set_requirements({"DE": _make_reqs("DE")})

        handler({}, None)

        head = s3.head_object(Bucket=TEST_BUCKET, Key="country-requirements/DE.json")
        assert head["ContentType"] == "application/json"

    def test_uploaded_body_is_indented_json(self, s3):
        _set_requirements({"DE": _make_reqs("DE")})

        handler({}, None)

        raw = s3.get_object(Bucket=TEST_BUCKET, Key="country-requirements/DE.json")["Body"].read().decode()
        assert raw.startswith("{\n  ")

    def test_event_and_context_are_ignored(self, s3):
        _set_requirements({"DE": _make_reqs("DE")})

        result = handler({"anything": "here"}, object())

        assert result["body"]["documents_synced"] == 1

    def test_rerun_overwrites_existing_documents(self, s3):
        _set_requirements({"DE": _make_reqs("DE", passport_validity_months=3)})
        handler({}, None)
        _set_requirements({"DE": _make_reqs("DE", passport_validity_months=6)})

        handler({}, None)

        assert _list_keys(s3) == ["country-requirements/DE.json"]
        assert _get_doc(s3, "country-requirements/DE.json")["passport_validity_months"] == 6

    def test_syncs_all_real_country_requirements(self, s3):
        from src.data.country_requirements import COUNTRY_REQUIREMENTS

        _set_requirements(COUNTRY_REQUIREMENTS)

        result = handler({}, None)

        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)
        assert len(_list_keys(s3)) == len(COUNTRY_REQUIREMENTS)

    def test_missing_bucket_raises(self, s3):
        _set_requirements({"DE": _make_reqs("DE")})
        kb_mod.KNOWLEDGE_BASE_BUCKET = "does-not-exist"

        with pytest.raises(s3.exceptions.NoSuchBucket):
            handler({}, None)


class TestBuildDocumentRequiredFields:
    """Required fields are always present and mapped from the requirements."""

    def test_required_fields_mapped(self):
        reqs = _make_reqs(
            "CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )

        doc = _build_document("CN", reqs)

        assert doc == {
            "country_code": "CN",
            "country_name": "China",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": [],
        }

    def test_known_code_uses_country_name(self):
        doc = _build_document("DE", _make_reqs("DE"))

        assert doc["country_name"] == "Germany"

    def test_unknown_code_falls_back_to_code(self):
        doc = _build_document("XA", _make_reqs("XA"))

        assert doc["country_name"] == "XA"

    def test_country_code_comes_from_requirements(self):
        doc = _build_document("DE", _make_reqs("XB"))

        assert doc["country_code"] == "XB"
        assert doc["country_name"] == "Germany"


class TestBuildDocumentOptionalFields:
    """Optional fields are included only when set (truthy / not None)."""

    def test_no_optional_fields_when_unset(self):
        doc = _build_document("DE", _make_reqs("DE"))

        for key in ("embassy_url", "evisa_portal_url", "estimated_processing_days", "entry_form_url"):
            assert key not in doc

    def test_embassy_url_included(self):
        doc = _build_document("DE", _make_reqs("DE", embassy_url="https://de.example"))

        assert doc["embassy_url"] == "https://de.example"

    def test_evisa_portal_url_included(self):
        doc = _build_document("DE", _make_reqs("DE", evisa_portal_url="https://evisa.example"))

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_estimated_processing_days_included(self):
        doc = _build_document("DE", _make_reqs("DE", estimated_processing_days=10))

        assert doc["estimated_processing_days"] == 10

    def test_zero_processing_days_included(self):
        doc = _build_document("DE", _make_reqs("DE", estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        doc = _build_document("DE", _make_reqs("DE", entry_form_url="https://form.example"))

        assert doc["entry_form_url"] == "https://form.example"

    @pytest.mark.parametrize("field", ["embassy_url", "evisa_portal_url", "entry_form_url"])
    def test_empty_string_urls_excluded(self, field):
        doc = _build_document("DE", _make_reqs("DE", **{field: ""}))

        assert field not in doc

    def test_all_optional_fields_included(self):
        reqs = _make_reqs(
            "DE",
            embassy_url="https://de.example",
            evisa_portal_url="https://evisa.example",
            estimated_processing_days=5,
            entry_form_url="https://form.example",
        )

        doc = _build_document("DE", reqs)

        assert len(doc) == 10
