"""Tests for the Bedrock Knowledge Base sync handler.

Covers the S3 upload loop (happy path, empty and multi-country
requirement sets) and every optional-field branch of the document
builder.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
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
    """Expose the moto S3 client."""
    return _mock_aws


def make_requirements(
    country_code: str = "DE",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    passport_validity_months: int = 3,
    visa_exempt_nationalities: list[str] | None = None,
    embassy_url: str | None = None,
    evisa_portal_url: str | None = None,
    estimated_processing_days: int | None = None,
    entry_form_url: str | None = None,
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
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


def _get_document(s3_client, country_code: str) -> dict:
    """Read back a synced document from the mocked bucket."""
    body = s3_client.get_object(
        Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
        Key=f"{sync_mod.S3_PREFIX}/{country_code}.json",
    )["Body"].read()
    return json.loads(body)


class TestHandlerSync:
    """The handler uploads one JSON document per country and reports the count."""

    def test_uploads_document_per_country(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"DE": make_requirements("DE"), "CN": make_requirements("CN")},
        )

        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == 2
        keys = {
            obj["Key"]
            for obj in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)["Contents"]
        }
        assert keys == {
            "country-requirements/DE.json",
            "country-requirements/CN.json",
        }

    def test_uploaded_object_is_json_with_expected_fields(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"JP": make_requirements("JP", passport_validity_months=6)},
        )

        handler({}, None)

        document = _get_document(s3, "JP")
        assert document["country_code"] == "JP"
        assert document["country_name"] == "Japan"
        assert document["passport_validity_months"] == 6

    def test_uploaded_object_has_json_content_type(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"DE": make_requirements("DE")})

        handler({}, None)

        head = s3.head_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key="country-requirements/DE.json",
        )
        assert head["ContentType"] == "application/json"

    def test_empty_requirements_syncs_nothing(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {})

        response = handler({}, None)

        assert response["body"]["documents_synced"] == 0
        assert "Contents" not in s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)

    def test_syncs_full_production_dataset(self, s3):
        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(sync_mod.COUNTRY_REQUIREMENTS)


class TestBuildDocumentCoreFields:
    """Core fields always come from the requirements record."""

    def test_core_fields_mirror_requirements(self):
        reqs = make_requirements(
            "CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )

        document = _build_document("CN", reqs)

        assert document == {
            "country_code": "CN",
            "country_name": "China",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": [],
        }

    def test_unknown_country_code_used_as_name(self):
        document = _build_document("ZZ", make_requirements("ZZ"))

        assert document["country_name"] == "ZZ"

    def test_country_name_looked_up_by_key_not_record(self):
        document = _build_document("FR", make_requirements("DE"))

        assert document["country_name"] == "France"
        assert document["country_code"] == "DE"


class TestBuildDocumentOptionalFields:
    """Optional fields are included only when present on the requirements."""

    def test_optional_fields_omitted_when_unset(self):
        document = _build_document("DE", make_requirements("DE"))

        assert "embassy_url" not in document
        assert "evisa_portal_url" not in document
        assert "estimated_processing_days" not in document
        assert "entry_form_url" not in document

    def test_embassy_url_included(self):
        reqs = make_requirements("DE", embassy_url="https://embassy.example/de")

        document = _build_document("DE", reqs)

        assert document["embassy_url"] == "https://embassy.example/de"

    def test_evisa_portal_url_included(self):
        reqs = make_requirements("IN", evisa_portal_url="https://evisa.example/in")

        document = _build_document("IN", reqs)

        assert document["evisa_portal_url"] == "https://evisa.example/in"

    def test_estimated_processing_days_included(self):
        reqs = make_requirements("IN", estimated_processing_days=10)

        document = _build_document("IN", reqs)

        assert document["estimated_processing_days"] == 10

    def test_zero_processing_days_included(self):
        reqs = make_requirements("IN", estimated_processing_days=0)

        document = _build_document("IN", reqs)

        assert document["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        reqs = make_requirements("TH", entry_form_url="https://entry.example/th")

        document = _build_document("TH", reqs)

        assert document["entry_form_url"] == "https://entry.example/th"

    def test_all_optional_fields_included_together(self):
        reqs = make_requirements(
            "AE",
            embassy_url="https://embassy.example/ae",
            evisa_portal_url="https://evisa.example/ae",
            estimated_processing_days=5,
            entry_form_url="https://entry.example/ae",
        )

        document = _build_document("AE", reqs)

        assert document["embassy_url"] == "https://embassy.example/ae"
        assert document["evisa_portal_url"] == "https://evisa.example/ae"
        assert document["estimated_processing_days"] == 5
        assert document["entry_form_url"] == "https://entry.example/ae"
