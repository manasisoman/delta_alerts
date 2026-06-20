"""Tests for the sync_knowledge_base handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.models.types import TravelDocRequirements


@pytest.fixture(autouse=True)
def _mock_aws(monkeypatch):
    """Activate moto S3 mock and monkey-patch the module-level client."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="delta-concierge-kb")

        orig_client = sync_mod.s3_client
        sync_mod.s3_client = s3

        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_client


@pytest.fixture
def s3_client(_mock_aws):
    """Expose the mocked S3 client."""
    return _mock_aws


def _base_reqs(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and optional overrides."""
    defaults = {
        "country_code": "DE",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB"],
    }
    defaults.update(overrides)
    return TravelDocRequirements(**defaults)


class TestHandlerSyncsAllCountries:
    """handler() iterates COUNTRY_REQUIREMENTS and returns the correct count."""

    def test_synced_count_matches_country_requirements(self):
        response = sync_mod.handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploads_objects_to_s3(self, s3_client):
        sync_mod.handler({}, None)

        objects = s3_client.list_objects_v2(
            Bucket="delta-concierge-kb",
            Prefix="country-requirements/",
        )
        assert objects["KeyCount"] == len(COUNTRY_REQUIREMENTS)

    def test_each_object_key_matches_country_code(self, s3_client):
        sync_mod.handler({}, None)

        objects = s3_client.list_objects_v2(
            Bucket="delta-concierge-kb",
            Prefix="country-requirements/",
        )
        uploaded_codes = {
            obj["Key"].split("/")[1].replace(".json", "")
            for obj in objects["Contents"]
        }
        assert uploaded_codes == set(COUNTRY_REQUIREMENTS.keys())

    def test_uploaded_content_is_valid_json(self, s3_client):
        sync_mod.handler({}, None)

        first_code = next(iter(COUNTRY_REQUIREMENTS))
        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key=f"country-requirements/{first_code}.json",
        )
        doc = json.loads(obj["Body"].read())

        assert doc["country_code"] == first_code


class TestBuildDocumentBaseFields:
    """_build_document includes all required base fields."""

    def test_includes_country_code(self):
        reqs = _base_reqs(country_code="DE")

        doc = sync_mod._build_document("DE", reqs)

        assert doc["country_code"] == "DE"

    def test_includes_country_name_from_lookup(self):
        reqs = _base_reqs(country_code="DE")

        doc = sync_mod._build_document("DE", reqs)

        assert doc["country_name"] == "Germany"

    def test_includes_visa_required(self):
        reqs = _base_reqs(requires_visa=True)

        doc = sync_mod._build_document("DE", reqs)

        assert doc["visa_required"] is True

    def test_includes_transit_visa_required(self):
        reqs = _base_reqs(transit_visa_required=True)

        doc = sync_mod._build_document("DE", reqs)

        assert doc["transit_visa_required"] is True

    def test_includes_passport_validity_months(self):
        reqs = _base_reqs(passport_validity_months=6)

        doc = sync_mod._build_document("DE", reqs)

        assert doc["passport_validity_months"] == 6

    def test_includes_visa_exempt_nationalities(self):
        reqs = _base_reqs(visa_exempt_nationalities=["US", "CA"])

        doc = sync_mod._build_document("DE", reqs)

        assert doc["visa_exempt_nationalities"] == ["US", "CA"]


class TestBuildDocumentOptionalFields:
    """_build_document conditionally includes optional fields."""

    def test_includes_embassy_url_when_present(self):
        reqs = _base_reqs(embassy_url="https://embassy.example.com")

        doc = sync_mod._build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"

    def test_excludes_embassy_url_when_none(self):
        reqs = _base_reqs()

        doc = sync_mod._build_document("DE", reqs)

        assert "embassy_url" not in doc

    def test_includes_evisa_portal_url_when_present(self):
        reqs = _base_reqs(evisa_portal_url="https://evisa.example.com")

        doc = sync_mod._build_document("DE", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example.com"

    def test_excludes_evisa_portal_url_when_none(self):
        reqs = _base_reqs()

        doc = sync_mod._build_document("DE", reqs)

        assert "evisa_portal_url" not in doc

    def test_includes_estimated_processing_days_when_present(self):
        reqs = _base_reqs(estimated_processing_days=14)

        doc = sync_mod._build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 14

    def test_excludes_estimated_processing_days_when_none(self):
        reqs = _base_reqs()

        doc = sync_mod._build_document("DE", reqs)

        assert "estimated_processing_days" not in doc

    def test_includes_entry_form_url_when_present(self):
        reqs = _base_reqs(entry_form_url="https://entry.example.com")

        doc = sync_mod._build_document("DE", reqs)

        assert doc["entry_form_url"] == "https://entry.example.com"

    def test_excludes_entry_form_url_when_none(self):
        reqs = _base_reqs()

        doc = sync_mod._build_document("DE", reqs)

        assert "entry_form_url" not in doc

    def test_includes_all_optional_fields_together(self):
        reqs = _base_reqs(
            embassy_url="https://embassy.example.com",
            evisa_portal_url="https://evisa.example.com",
            estimated_processing_days=7,
            entry_form_url="https://entry.example.com",
        )

        doc = sync_mod._build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert doc["estimated_processing_days"] == 7
        assert doc["entry_form_url"] == "https://entry.example.com"


class TestCountryNameFallback:
    """_build_document falls back to the country code when name is unknown."""

    def test_known_code_uses_mapped_name(self):
        reqs = _base_reqs(country_code="JP")

        doc = sync_mod._build_document("JP", reqs)

        assert doc["country_name"] == "Japan"

    def test_unknown_code_falls_back_to_code(self):
        reqs = _base_reqs(country_code="ZZ")

        doc = sync_mod._build_document("ZZ", reqs)

        assert doc["country_name"] == "ZZ"
