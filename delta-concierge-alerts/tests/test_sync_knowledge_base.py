"""Tests for the Knowledge Base sync handler (S3 document upload)."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

BUCKET = sync_mod.KNOWLEDGE_BASE_BUCKET


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto and point the module-level S3 client at a fake bucket."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=BUCKET)

        orig_s3_client = sync_mod.s3_client
        sync_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client


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
    """Helper to build a TravelDocRequirements with sensible defaults."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=(
            visa_exempt_nationalities if visa_exempt_nationalities is not None else ["US"]
        ),
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


class TestHandlerSyncsAllCountries:
    """The handler uploads one JSON document per country and reports the count."""

    def test_returns_200_with_document_count(self):
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploads_one_object_per_country_under_prefix(self, _mock_aws):
        handler({}, None)

        listing = _mock_aws.list_objects_v2(Bucket=BUCKET, Prefix=sync_mod.S3_PREFIX)
        keys = {obj["Key"] for obj in listing["Contents"]}

        assert keys == {
            f"{sync_mod.S3_PREFIX}/{code}.json" for code in COUNTRY_REQUIREMENTS
        }

    def test_uploaded_object_is_json_with_content_type(self, _mock_aws):
        handler({}, None)

        obj = _mock_aws.get_object(Bucket=BUCKET, Key=f"{sync_mod.S3_PREFIX}/DE.json")
        body = json.loads(obj["Body"].read())

        assert obj["ContentType"] == "application/json"
        assert body["country_code"] == "DE"
        assert body["country_name"] == "Germany"
        assert body == _build_document("DE", COUNTRY_REQUIREMENTS["DE"])

    def test_handler_is_idempotent_on_rerun(self, _mock_aws):
        handler({}, None)
        result = handler({}, None)

        listing = _mock_aws.list_objects_v2(Bucket=BUCKET, Prefix=sync_mod.S3_PREFIX)

        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)
        assert listing["KeyCount"] == len(COUNTRY_REQUIREMENTS)


class TestBuildDocumentBaseFields:
    """Required fields are always present and optional fields are omitted when unset."""

    def test_happy_path_only_required_fields(self):
        reqs = make_requirements()

        doc = _build_document("DE", reqs)

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US"],
        }

    def test_transit_visa_and_no_visa_flags_pass_through(self):
        reqs = make_requirements(
            country_code="CN",
            requires_visa=False,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )

        doc = _build_document("CN", reqs)

        assert doc["visa_required"] is False
        assert doc["transit_visa_required"] is True
        assert doc["passport_validity_months"] == 6
        assert doc["visa_exempt_nationalities"] == []


class TestBuildDocumentCountryName:
    """Country name resolves from the lookup table, falling back to the code."""

    def test_known_country_uses_display_name(self):
        doc = _build_document("JP", make_requirements(country_code="JP"))

        assert doc["country_name"] == "Japan"

    def test_unknown_country_falls_back_to_code(self):
        doc = _build_document("XX", make_requirements(country_code="XX"))

        assert doc["country_name"] == "XX"


class TestBuildDocumentOptionalFields:
    """Each optional field is included only when it carries a value."""

    def test_embassy_url_included_when_set(self):
        doc = _build_document("DE", make_requirements(embassy_url="https://embassy.example"))

        assert doc["embassy_url"] == "https://embassy.example"

    def test_evisa_portal_url_included_when_set(self):
        doc = _build_document("DE", make_requirements(evisa_portal_url="https://evisa.example"))

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_estimated_processing_days_included_when_set(self):
        doc = _build_document("DE", make_requirements(estimated_processing_days=10))

        assert doc["estimated_processing_days"] == 10

    def test_estimated_processing_days_zero_is_included(self):
        doc = _build_document("DE", make_requirements(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included_when_set(self):
        doc = _build_document("DE", make_requirements(entry_form_url="https://entry.example"))

        assert doc["entry_form_url"] == "https://entry.example"

    def test_empty_string_urls_are_omitted(self):
        doc = _build_document(
            "DE",
            make_requirements(embassy_url="", evisa_portal_url="", entry_form_url=""),
        )

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "entry_form_url" not in doc

    def test_all_optional_fields_together(self):
        reqs = make_requirements(
            embassy_url="https://embassy.example",
            evisa_portal_url="https://evisa.example",
            estimated_processing_days=5,
            entry_form_url="https://entry.example",
        )

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example"
        assert doc["evisa_portal_url"] == "https://evisa.example"
        assert doc["estimated_processing_days"] == 5
        assert doc["entry_form_url"] == "https://entry.example"
