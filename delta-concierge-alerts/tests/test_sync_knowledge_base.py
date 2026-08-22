"""Tests for the knowledge base sync handler that uploads country docs to S3."""

import json

import boto3
import botocore.exceptions
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import handler
from src.models.types import TravelDocRequirements

BUCKET = sync_mod.KNOWLEDGE_BASE_BUCKET


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks, create the KB bucket, and patch the module-level S3 client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

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


def make_requirements(
    country_code: str = "DE",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    passport_validity_months: int = 3,
    visa_exempt_nationalities: list[str] | None = None,
    **optional,
) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=(
            ["US", "GB"] if visa_exempt_nationalities is None else visa_exempt_nationalities
        ),
        **optional,
    )


def _read_document(s3, country_code: str) -> dict:
    """Fetch and decode the synced document for a country."""
    body = s3.get_object(
        Bucket=BUCKET, Key=f"{sync_mod.S3_PREFIX}/{country_code}.json"
    )["Body"].read()
    return json.loads(body)


class TestSyncHappyPath:
    """Every country in the requirements table is uploaded exactly once."""

    def test_uploads_one_document_per_country(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"DE": make_requirements("DE"), "CN": make_requirements("CN")},
        )

        response = handler({}, None)

        assert response == {"statusCode": 200, "body": {"documents_synced": 2}}
        keys = {o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET)["Contents"]}
        assert keys == {
            "country-requirements/DE.json",
            "country-requirements/CN.json",
        }

    def test_uploaded_object_is_json_content_type(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")})

        handler({}, None)

        obj = s3.get_object(Bucket=BUCKET, Key="country-requirements/DE.json")
        assert obj["ContentType"] == "application/json"

    def test_syncs_full_production_requirements_table(self, s3):
        response = handler({}, None)

        expected = len(sync_mod.COUNTRY_REQUIREMENTS)
        assert response["body"]["documents_synced"] == expected
        assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == expected

    def test_event_and_context_are_ignored(self, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")})

        with_payload = handler({"detail": {"unused": True}}, object())
        empty = handler({}, None)

        assert with_payload == empty


class TestDocumentContents:
    """Required document fields mirror the source requirements."""

    def test_core_fields_are_serialized(self, s3, monkeypatch):
        reqs = make_requirements(
            "CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"CN": reqs})

        handler({}, None)

        assert _read_document(s3, "CN") == {
            "country_code": "CN",
            "country_name": "China",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": [],
        }

    def test_visa_free_country_records_no_visa_requirement(self, s3, monkeypatch):
        reqs = make_requirements("JM", requires_visa=False, passport_validity_months=6)
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"JM": reqs})

        handler({}, None)

        doc = _read_document(s3, "JM")
        assert doc["visa_required"] is False
        assert doc["passport_validity_months"] == 6

    def test_unknown_country_code_falls_back_to_code_as_name(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"ZZ": make_requirements("ZZ")})

        handler({}, None)

        assert _read_document(s3, "ZZ")["country_name"] == "ZZ"


class TestOptionalDocumentFields:
    """Optional URL and processing-time fields appear only when populated."""

    def test_omitted_when_unset(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")})

        handler({}, None)

        doc = _read_document(s3, "DE")
        for key in (
            "embassy_url",
            "evisa_portal_url",
            "estimated_processing_days",
            "entry_form_url",
        ):
            assert key not in doc

    def test_included_when_populated(self, s3, monkeypatch):
        reqs = make_requirements(
            "IN",
            embassy_url="https://embassy.example/in",
            evisa_portal_url="https://evisa.example/in",
            estimated_processing_days=5,
            entry_form_url="https://forms.example/in",
        )
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"IN": reqs})

        handler({}, None)

        doc = _read_document(s3, "IN")
        assert doc["embassy_url"] == "https://embassy.example/in"
        assert doc["evisa_portal_url"] == "https://evisa.example/in"
        assert doc["estimated_processing_days"] == 5
        assert doc["entry_form_url"] == "https://forms.example/in"

    def test_zero_processing_days_is_included(self, s3, monkeypatch):
        reqs = make_requirements("AE", estimated_processing_days=0)
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"AE": reqs})

        handler({}, None)

        assert _read_document(s3, "AE")["estimated_processing_days"] == 0

    def test_empty_urls_are_omitted(self, s3, monkeypatch):
        reqs = make_requirements("TR", embassy_url="", evisa_portal_url="", entry_form_url="")
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"TR": reqs})

        handler({}, None)

        doc = _read_document(s3, "TR")
        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc


class TestEmptyRequirements:
    """An empty requirements table is a successful no-op."""

    def test_no_uploads_and_zero_count(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {})

        response = handler({}, None)

        assert response == {"statusCode": 200, "body": {"documents_synced": 0}}
        assert s3.list_objects_v2(Bucket=BUCKET)["KeyCount"] == 0


class TestSyncFailures:
    """S3 failures surface to the caller instead of reporting a partial success."""

    def test_missing_bucket_raises_client_error(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")})
        monkeypatch.setattr(sync_mod, "KNOWLEDGE_BASE_BUCKET", "no-such-kb-bucket")

        with pytest.raises(botocore.exceptions.ClientError) as excinfo:
            handler({}, None)

        assert excinfo.value.response["Error"]["Code"] == "NoSuchBucket"

    def test_failure_midway_does_not_return_a_count(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"DE": make_requirements("DE"), "FR": make_requirements("FR")},
        )
        s3.delete_bucket(Bucket=BUCKET)

        with pytest.raises(botocore.exceptions.ClientError):
            handler({}, None)
