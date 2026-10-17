"""Tests for the Bedrock Knowledge Base sync handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as kb_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

TEST_BUCKET = "test-kb-bucket"


def _make_reqs(country_code: str = "DE", **overrides) -> TravelDocRequirements:
    """Build TravelDocRequirements with sensible defaults and optional overrides."""
    fields = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB", "CA"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _fixture_requirements() -> dict[str, TravelDocRequirements]:
    """Requirements covering Schengen, transit-visa, and unnamed countries."""
    return {
        "DE": _make_reqs("DE"),
        "CN": _make_reqs(
            "CN",
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            embassy_url="https://cn.example/embassy",
            evisa_portal_url="https://cn.example/evisa",
            estimated_processing_days=10,
            entry_form_url="https://cn.example/entry",
        ),
        "ZZ": _make_reqs("ZZ", requires_visa=False, passport_validity_months=0),
    }


@pytest.fixture(autouse=True)
def _mock_s3():
    """Activate moto, create the KB bucket, and swap module-level client/constants."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_BUCKET)

        orig_client = kb_mod.s3_client
        orig_bucket = kb_mod.KNOWLEDGE_BASE_BUCKET
        orig_reqs = kb_mod.COUNTRY_REQUIREMENTS
        kb_mod.s3_client = s3
        kb_mod.KNOWLEDGE_BASE_BUCKET = TEST_BUCKET
        kb_mod.COUNTRY_REQUIREMENTS = _fixture_requirements()
        try:
            yield s3
        finally:
            kb_mod.s3_client = orig_client
            kb_mod.KNOWLEDGE_BASE_BUCKET = orig_bucket
            kb_mod.COUNTRY_REQUIREMENTS = orig_reqs


def _read_doc(s3, country_code: str) -> dict:
    obj = s3.get_object(Bucket=TEST_BUCKET, Key=f"country-requirements/{country_code}.json")
    return json.loads(obj["Body"].read())


class TestBuildDocumentCoreFields:
    """Every document carries the core requirement fields straight from the requirements."""

    def test_happy_path_core_fields(self):
        reqs = _make_reqs("DE")

        doc = _build_document("DE", reqs)

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB", "CA"],
        }

    def test_schengen_three_month_passport_validity_preserved(self):
        reqs = _make_reqs("FR", passport_validity_months=3)

        doc = _build_document("FR", reqs)

        assert doc["passport_validity_months"] == 3

    def test_six_month_passport_validity_preserved(self):
        reqs = _make_reqs("JP", passport_validity_months=6)

        doc = _build_document("JP", reqs)

        assert doc["passport_validity_months"] == 6

    def test_transit_visa_flag_for_layover_country(self):
        reqs = _make_reqs("CN", transit_visa_required=True)

        doc = _build_document("CN", reqs)

        assert doc["transit_visa_required"] is True

    def test_visa_not_required(self):
        reqs = _make_reqs("MX", requires_visa=False)

        doc = _build_document("MX", reqs)

        assert doc["visa_required"] is False

    def test_empty_visa_exempt_nationalities(self):
        reqs = _make_reqs("CN", visa_exempt_nationalities=[])

        doc = _build_document("CN", reqs)

        assert doc["visa_exempt_nationalities"] == []


class TestCountryNameLookup:
    """Country names resolve from the lookup table, falling back to the code."""

    def test_known_country_name(self):
        doc = _build_document("JP", _make_reqs("JP"))

        assert doc["country_name"] == "Japan"

    def test_unknown_country_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_reqs("ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_country_code_field_comes_from_requirements(self):
        doc = _build_document("XX", _make_reqs("DE"))

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "XX"


class TestOptionalFields:
    """Optional fields are included only when set (processing days included even if 0)."""

    def test_optional_fields_omitted_by_default(self):
        doc = _build_document("DE", _make_reqs("DE"))

        for key in ("embassy_url", "evisa_portal_url", "estimated_processing_days", "entry_form_url"):
            assert key not in doc

    def test_embassy_url_included(self):
        doc = _build_document("DE", _make_reqs("DE", embassy_url="https://e.example"))

        assert doc["embassy_url"] == "https://e.example"

    def test_evisa_portal_url_included(self):
        doc = _build_document("IN", _make_reqs("IN", evisa_portal_url="https://v.example"))

        assert doc["evisa_portal_url"] == "https://v.example"

    def test_estimated_processing_days_included(self):
        doc = _build_document("IN", _make_reqs("IN", estimated_processing_days=5))

        assert doc["estimated_processing_days"] == 5

    def test_zero_processing_days_still_included(self):
        doc = _build_document("IN", _make_reqs("IN", estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        doc = _build_document("JM", _make_reqs("JM", entry_form_url="https://f.example"))

        assert doc["entry_form_url"] == "https://f.example"

    def test_empty_string_urls_omitted(self):
        reqs = _make_reqs("DE", embassy_url="", evisa_portal_url="", entry_form_url="")

        doc = _build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc


class TestHandlerSync:
    """The handler uploads one JSON document per country and reports the count."""

    def test_returns_200_with_synced_count(self):
        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 3}}

    def test_uploads_one_object_per_country(self, _mock_s3):
        handler({}, None)

        keys = sorted(
            o["Key"] for o in _mock_s3.list_objects_v2(Bucket=TEST_BUCKET)["Contents"]
        )
        assert keys == [
            "country-requirements/CN.json",
            "country-requirements/DE.json",
            "country-requirements/ZZ.json",
        ]

    def test_uploaded_document_matches_build_document(self, _mock_s3):
        handler({}, None)

        assert _read_doc(_mock_s3, "CN") == _build_document("CN", kb_mod.COUNTRY_REQUIREMENTS["CN"])

    def test_uploaded_document_includes_transit_and_optional_fields(self, _mock_s3):
        handler({}, None)

        doc = _read_doc(_mock_s3, "CN")
        assert doc["transit_visa_required"] is True
        assert doc["passport_validity_months"] == 6
        assert doc["estimated_processing_days"] == 10

    def test_content_type_is_json(self, _mock_s3):
        handler({}, None)

        head = _mock_s3.head_object(Bucket=TEST_BUCKET, Key="country-requirements/DE.json")
        assert head["ContentType"] == "application/json"

    def test_empty_requirements_syncs_nothing(self, _mock_s3):
        kb_mod.COUNTRY_REQUIREMENTS = {}

        result = handler({}, None)

        assert result["body"]["documents_synced"] == 0
        assert _mock_s3.list_objects_v2(Bucket=TEST_BUCKET)["KeyCount"] == 0

    def test_resync_after_requirement_change_overwrites_document(self, _mock_s3):
        handler({}, None)
        kb_mod.COUNTRY_REQUIREMENTS["DE"] = _make_reqs("DE", passport_validity_months=6)

        handler({}, None)

        assert _read_doc(_mock_s3, "DE")["passport_validity_months"] == 6

    def test_missing_bucket_raises(self):
        kb_mod.KNOWLEDGE_BASE_BUCKET = "does-not-exist"

        with pytest.raises(Exception):
            handler({}, None)
