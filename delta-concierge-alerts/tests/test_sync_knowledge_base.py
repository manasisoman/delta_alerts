"""Tests for the Bedrock Knowledge Base S3 sync handler.

Covers the handler upload loop (keys, body, content type, count), the
empty-requirements path, and every optional-field branch of
_build_document, including the country-name fallback.
"""

import json

import boto3
from botocore.exceptions import ClientError
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

TEST_BUCKET = "test-kb-bucket"


def _make_reqs(country_code: str = "DE", **overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with no optional fields unless overridden."""
    fields = {
        "country_code": country_code,
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _get_doc(s3, country_code: str) -> dict:
    obj = s3.get_object(
        Bucket=TEST_BUCKET, Key=f"{sync_mod.S3_PREFIX}/{country_code}.json"
    )
    return json.loads(obj["Body"].read())


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the bucket, and swap module-level client/constants."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_BUCKET)

        orig_client = sync_mod.s3_client
        orig_bucket = sync_mod.KNOWLEDGE_BASE_BUCKET
        orig_reqs = sync_mod.COUNTRY_REQUIREMENTS
        sync_mod.s3_client = s3
        sync_mod.KNOWLEDGE_BASE_BUCKET = TEST_BUCKET
        sync_mod.COUNTRY_REQUIREMENTS = {
            "DE": _make_reqs("DE"),
            "JP": _make_reqs(
                "JP",
                passport_validity_months=6,
                embassy_url="https://jp.example/embassy",
                evisa_portal_url="https://jp.example/evisa",
                estimated_processing_days=5,
                entry_form_url="https://jp.example/entry",
            ),
        }
        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_client
            sync_mod.KNOWLEDGE_BASE_BUCKET = orig_bucket
            sync_mod.COUNTRY_REQUIREMENTS = orig_reqs


class TestHandlerSync:
    """The handler uploads one JSON document per country and reports the count."""

    def test_returns_200_with_synced_count(self):
        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 2}}

    def test_uploads_one_object_per_country_under_prefix(self, _mock_aws):
        handler({}, None)

        listing = _mock_aws.list_objects_v2(Bucket=TEST_BUCKET)
        keys = sorted(o["Key"] for o in listing["Contents"])
        assert keys == ["country-requirements/DE.json", "country-requirements/JP.json"]

    def test_objects_are_json_content_type(self, _mock_aws):
        handler({}, None)

        head = _mock_aws.head_object(
            Bucket=TEST_BUCKET, Key="country-requirements/DE.json"
        )
        assert head["ContentType"] == "application/json"

    def test_uploaded_body_matches_built_document(self, _mock_aws):
        handler({}, None)

        doc = _get_doc(_mock_aws, "JP")
        assert doc == _build_document("JP", sync_mod.COUNTRY_REQUIREMENTS["JP"])

    def test_empty_requirements_syncs_nothing(self, _mock_aws):
        sync_mod.COUNTRY_REQUIREMENTS = {}

        result = handler({}, None)

        assert result["body"]["documents_synced"] == 0
        assert _mock_aws.list_objects_v2(Bucket=TEST_BUCKET).get("KeyCount") == 0

    def test_resync_overwrites_existing_document(self, _mock_aws):
        handler({}, None)
        sync_mod.COUNTRY_REQUIREMENTS = {
            "DE": _make_reqs("DE", passport_validity_months=6)
        }

        handler({}, None)

        assert _get_doc(_mock_aws, "DE")["passport_validity_months"] == 6

    def test_missing_bucket_raises(self):
        sync_mod.KNOWLEDGE_BASE_BUCKET = "does-not-exist"

        with pytest.raises(ClientError):
            handler({}, None)


class TestBuildDocumentRequiredFields:
    """Required fields are always present and mapped from the requirements."""

    def test_base_fields(self):
        doc = _build_document("DE", _make_reqs("DE"))

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB"],
        }

    def test_unknown_country_name_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_reqs("ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_transit_visa_flag_propagates(self):
        doc = _build_document("CN", _make_reqs("CN", transit_visa_required=True))

        assert doc["transit_visa_required"] is True
        assert doc["country_name"] == "China"


class TestBuildDocumentOptionalFields:
    """Optional fields are included only when set (processing days when not None)."""

    def test_no_optional_fields_omitted(self):
        doc = _build_document("DE", _make_reqs("DE"))

        for key in ("embassy_url", "evisa_portal_url",
                    "estimated_processing_days", "entry_form_url"):
            assert key not in doc

    def test_embassy_url_included(self):
        doc = _build_document("DE", _make_reqs(embassy_url="https://e.example"))

        assert doc["embassy_url"] == "https://e.example"

    def test_evisa_portal_url_included(self):
        doc = _build_document("DE", _make_reqs(evisa_portal_url="https://v.example"))

        assert doc["evisa_portal_url"] == "https://v.example"

    def test_entry_form_url_included(self):
        doc = _build_document("DE", _make_reqs(entry_form_url="https://f.example"))

        assert doc["entry_form_url"] == "https://f.example"

    def test_processing_days_included(self):
        doc = _build_document("DE", _make_reqs(estimated_processing_days=10))

        assert doc["estimated_processing_days"] == 10

    def test_zero_processing_days_still_included(self):
        doc = _build_document("DE", _make_reqs(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_empty_string_urls_omitted(self):
        doc = _build_document(
            "DE", _make_reqs(embassy_url="", evisa_portal_url="", entry_form_url="")
        )

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc
