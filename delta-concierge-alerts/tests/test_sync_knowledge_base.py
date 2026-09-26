"""Tests for the Knowledge Base S3 sync handler."""

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
    """Activate moto, create the KB bucket, and patch the module-level S3 client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)

        orig_s3_client = sync_mod.s3_client
        sync_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client


def make_requirements(
    country_code: str = "XX",
    requires_visa: bool = True,
    transit_visa_required: bool = False,
    passport_validity_months: int = 6,
    visa_exempt_nationalities: list[str] | None = None,
    embassy_url: str | None = None,
    evisa_portal_url: str | None = None,
    estimated_processing_days: int | None = None,
    entry_form_url: str | None = None,
) -> TravelDocRequirements:
    """Build a TravelDocRequirements with only the required fields populated by default."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=visa_exempt_nationalities or [],
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


def _list_keys(s3) -> list[str]:
    resp = s3.list_objects_v2(
        Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Prefix=sync_mod.S3_PREFIX,
    )
    return [obj["Key"] for obj in resp.get("Contents", [])]


class TestHandlerSyncsAllCountries:
    """handler() uploads one JSON document per COUNTRY_REQUIREMENTS entry."""

    def test_returns_200_with_synced_count(self):
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"] == {"documents_synced": len(COUNTRY_REQUIREMENTS)}

    def test_uploads_one_object_per_country_under_prefix(self, _mock_aws):
        handler({}, None)

        keys = _list_keys(_mock_aws)

        assert len(keys) == len(COUNTRY_REQUIREMENTS)
        assert sorted(keys) == sorted(
            f"{sync_mod.S3_PREFIX}/{code}.json" for code in COUNTRY_REQUIREMENTS
        )

    def test_uploaded_object_is_json_with_correct_content_type(self, _mock_aws):
        handler({}, None)

        obj = _mock_aws.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key=f"{sync_mod.S3_PREFIX}/DE.json",
        )
        body = json.loads(obj["Body"].read())

        assert obj["ContentType"] == "application/json"
        assert body["country_code"] == "DE"
        assert body["country_name"] == "Germany"
        assert body["passport_validity_months"] == COUNTRY_REQUIREMENTS["DE"].passport_validity_months

    def test_uploaded_body_matches_build_document(self, _mock_aws):
        handler({}, None)

        for code, reqs in COUNTRY_REQUIREMENTS.items():
            obj = _mock_aws.get_object(
                Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
                Key=f"{sync_mod.S3_PREFIX}/{code}.json",
            )
            assert json.loads(obj["Body"].read()) == _build_document(code, reqs)

    def test_rerun_is_idempotent(self, _mock_aws):
        handler({}, None)

        result = handler({"detail-type": "Scheduled Event"}, object())

        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)
        assert len(_list_keys(_mock_aws)) == len(COUNTRY_REQUIREMENTS)


class TestBuildDocumentRequiredFields:
    """_build_document always emits the core requirement fields."""

    def test_core_fields_present_with_no_optional_fields(self):
        reqs = make_requirements(
            country_code="XX",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=["US", "GB"],
        )

        doc = _build_document("XX", reqs)

        assert doc == {
            "country_code": "XX",
            "country_name": "XX",
            "visa_required": True,
            "transit_visa_required": True,
            "passport_validity_months": 6,
            "visa_exempt_nationalities": ["US", "GB"],
        }

    def test_visa_not_required_is_reflected(self):
        reqs = make_requirements(requires_visa=False)

        doc = _build_document("XX", reqs)

        assert doc["visa_required"] is False

    def test_transit_visa_required_is_reflected(self):
        reqs = make_requirements(transit_visa_required=True)

        doc = _build_document("XX", reqs)

        assert doc["transit_visa_required"] is True

    def test_country_code_comes_from_requirements_not_key(self):
        reqs = make_requirements(country_code="DE")

        doc = _build_document("XX", reqs)

        assert doc["country_code"] == "DE"


class TestBuildDocumentCountryName:
    """country_name resolves from the lookup table, falling back to the code."""

    def test_known_code_resolves_to_full_name(self):
        doc = _build_document("JP", make_requirements(country_code="JP"))

        assert doc["country_name"] == "Japan"

    def test_unknown_code_falls_back_to_code(self):
        doc = _build_document("ZZ", make_requirements(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_every_production_country_has_a_name(self):
        for code in COUNTRY_REQUIREMENTS:
            assert code in sync_mod._COUNTRY_NAMES, f"missing name for {code}"


class TestBuildDocumentOptionalFields:
    """Optional URL/processing fields are only included when populated."""

    def test_embassy_url_included_when_set(self):
        reqs = make_requirements(embassy_url="https://embassy.example")

        doc = _build_document("XX", reqs)

        assert doc["embassy_url"] == "https://embassy.example"

    def test_embassy_url_omitted_when_none(self):
        doc = _build_document("XX", make_requirements(embassy_url=None))

        assert "embassy_url" not in doc

    def test_embassy_url_omitted_when_empty_string(self):
        doc = _build_document("XX", make_requirements(embassy_url=""))

        assert "embassy_url" not in doc

    def test_evisa_portal_url_included_when_set(self):
        reqs = make_requirements(evisa_portal_url="https://evisa.example")

        doc = _build_document("XX", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_evisa_portal_url_omitted_when_none(self):
        doc = _build_document("XX", make_requirements(evisa_portal_url=None))

        assert "evisa_portal_url" not in doc

    def test_estimated_processing_days_included_when_set(self):
        reqs = make_requirements(estimated_processing_days=14)

        doc = _build_document("XX", reqs)

        assert doc["estimated_processing_days"] == 14

    def test_estimated_processing_days_zero_is_included(self):
        reqs = make_requirements(estimated_processing_days=0)

        doc = _build_document("XX", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_estimated_processing_days_omitted_when_none(self):
        doc = _build_document("XX", make_requirements(estimated_processing_days=None))

        assert "estimated_processing_days" not in doc

    def test_entry_form_url_included_when_set(self):
        reqs = make_requirements(entry_form_url="https://entry.example")

        doc = _build_document("XX", reqs)

        assert doc["entry_form_url"] == "https://entry.example"

    def test_entry_form_url_omitted_when_none(self):
        doc = _build_document("XX", make_requirements(entry_form_url=None))

        assert "entry_form_url" not in doc

    def test_all_optional_fields_included_together(self):
        reqs = make_requirements(
            embassy_url="https://embassy.example",
            evisa_portal_url="https://evisa.example",
            estimated_processing_days=7,
            entry_form_url="https://entry.example",
        )

        doc = _build_document("XX", reqs)

        assert doc["embassy_url"] == "https://embassy.example"
        assert doc["evisa_portal_url"] == "https://evisa.example"
        assert doc["estimated_processing_days"] == 7
        assert doc["entry_form_url"] == "https://entry.example"

    def test_document_is_json_serializable(self):
        reqs = make_requirements(
            visa_exempt_nationalities=["US"],
            embassy_url="https://embassy.example",
            estimated_processing_days=3,
        )

        doc = _build_document("XX", reqs)

        assert json.loads(json.dumps(doc)) == doc
