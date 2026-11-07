"""Tests for the Bedrock Knowledge Base S3 sync handler.

Covers the handler's upload loop (keys, bodies, content type, counts),
the empty-requirements path, and every optional-field branch in
_build_document, including country-name fallback for unknown codes.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as kb_mod
from src.handlers.sync_knowledge_base import S3_PREFIX, _build_document, handler
from src.models.types import TravelDocRequirements


TEST_BUCKET = "test-concierge-kb"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_reqs(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with all optional fields populated."""
    defaults = dict(
        country_code="JP",
        requires_visa=False,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=["US", "GB"],
        embassy_url="https://jp.example.gov",
        evisa_portal_url="https://evisa.jp.example.gov",
        estimated_processing_days=5,
        entry_form_url="https://entry.jp.example.gov",
    )
    defaults.update(overrides)
    return TravelDocRequirements(**defaults)


def _minimal_reqs(country_code: str = "XA") -> TravelDocRequirements:
    """Build a TravelDocRequirements with no optional fields."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=True,
        transit_visa_required=True,
        passport_validity_months=3,
        visa_exempt_nationalities=[],
    )


def _get_object_json(s3, key: str) -> dict:
    body = s3.get_object(Bucket=TEST_BUCKET, Key=key)["Body"].read()
    return json.loads(body)


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture
def s3():
    """Activate moto S3, create the bucket, and swap the module client."""
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


# ------------------------------------------------------------------
# handler
# ------------------------------------------------------------------

class TestHandlerUploads:
    """The handler uploads one JSON document per country and reports the count."""

    def test_returns_200_with_synced_count(self, s3):
        _set_requirements({"JP": _make_reqs(), "XA": _minimal_reqs()})

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 2}}

    def test_uploads_one_object_per_country_under_prefix(self, s3):
        _set_requirements({"JP": _make_reqs(), "XA": _minimal_reqs()})

        handler({}, None)

        keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket=TEST_BUCKET)["Contents"])
        assert keys == [f"{S3_PREFIX}/JP.json", f"{S3_PREFIX}/XA.json"]

    def test_uploaded_body_matches_built_document(self, s3):
        reqs = _make_reqs()
        _set_requirements({"JP": reqs})

        handler({}, None)

        assert _get_object_json(s3, f"{S3_PREFIX}/JP.json") == _build_document("JP", reqs)

    def test_uploaded_body_is_indented_json(self, s3):
        _set_requirements({"JP": _make_reqs()})

        handler({}, None)

        body = s3.get_object(Bucket=TEST_BUCKET, Key=f"{S3_PREFIX}/JP.json")["Body"].read().decode()
        assert body == json.dumps(_build_document("JP", _make_reqs()), indent=2)

    def test_content_type_is_json(self, s3):
        _set_requirements({"JP": _make_reqs()})

        handler({}, None)

        head = s3.head_object(Bucket=TEST_BUCKET, Key=f"{S3_PREFIX}/JP.json")
        assert head["ContentType"] == "application/json"

    def test_resync_overwrites_existing_document(self, s3):
        _set_requirements({"JP": _make_reqs(passport_validity_months=6)})
        handler({}, None)
        _set_requirements({"JP": _make_reqs(passport_validity_months=3)})

        handler({}, None)

        assert _get_object_json(s3, f"{S3_PREFIX}/JP.json")["passport_validity_months"] == 3

    def test_event_and_context_are_ignored(self, s3):
        _set_requirements({"JP": _make_reqs()})

        result = handler({"anything": "here"}, object())

        assert result["body"]["documents_synced"] == 1


class TestHandlerEmptyRequirements:
    """With no requirements the handler uploads nothing and reports zero."""

    def test_returns_zero_synced(self, s3):
        _set_requirements({})

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 0}}

    def test_bucket_remains_empty(self, s3):
        _set_requirements({})

        handler({}, None)

        assert s3.list_objects_v2(Bucket=TEST_BUCKET)["KeyCount"] == 0


class TestHandlerProductionData:
    """The handler syncs every entry of the real COUNTRY_REQUIREMENTS table."""

    def test_syncs_all_production_countries(self, s3):
        result = handler({}, None)

        expected = len(kb_mod.COUNTRY_REQUIREMENTS)
        assert result["body"]["documents_synced"] == expected
        assert s3.list_objects_v2(Bucket=TEST_BUCKET)["KeyCount"] == expected


# ------------------------------------------------------------------
# _build_document
# ------------------------------------------------------------------

class TestBuildDocumentRequiredFields:
    """Required fields are always copied from the requirements record."""

    def test_maps_core_fields(self):
        reqs = _minimal_reqs("XA")

        doc = _build_document("XA", reqs)

        assert doc == {
            "country_code": "XA",
            "country_name": "XA",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": [],
        }

    def test_country_code_comes_from_reqs_not_key(self):
        reqs = _minimal_reqs("XB")

        doc = _build_document("XA", reqs)

        assert doc["country_code"] == "XB"


class TestBuildDocumentCountryName:
    """Known codes resolve to a display name; unknown codes fall back to the code."""

    def test_known_code_uses_display_name(self):
        doc = _build_document("JP", _make_reqs())

        assert doc["country_name"] == "Japan"

    def test_unknown_code_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_reqs(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"


class TestBuildDocumentOptionalFields:
    """Optional fields are included only when set."""

    def test_all_optional_fields_present_when_set(self):
        doc = _build_document("JP", _make_reqs())

        assert doc["embassy_url"] == "https://jp.example.gov"
        assert doc["evisa_portal_url"] == "https://evisa.jp.example.gov"
        assert doc["estimated_processing_days"] == 5
        assert doc["entry_form_url"] == "https://entry.jp.example.gov"

    def test_all_optional_fields_omitted_when_unset(self):
        doc = _build_document("XA", _minimal_reqs())

        for field in ("embassy_url", "evisa_portal_url", "estimated_processing_days", "entry_form_url"):
            assert field not in doc

    @pytest.mark.parametrize(
        "field",
        ["embassy_url", "evisa_portal_url", "estimated_processing_days", "entry_form_url"],
    )
    def test_single_optional_field_omitted_when_none(self, field):
        doc = _build_document("JP", _make_reqs(**{field: None}))

        assert field not in doc

    @pytest.mark.parametrize("field", ["embassy_url", "evisa_portal_url", "entry_form_url"])
    def test_empty_string_url_is_omitted(self, field):
        doc = _build_document("JP", _make_reqs(**{field: ""}))

        assert field not in doc

    def test_zero_processing_days_is_included(self):
        doc = _build_document("JP", _make_reqs(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_document_is_json_serializable(self):
        doc = _build_document("JP", _make_reqs())

        assert json.loads(json.dumps(doc)) == doc
