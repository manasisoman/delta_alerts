"""Tests for the sync_knowledge_base handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


def _base_reqs(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and optional overrides."""
    defaults = dict(
        country_code="DE",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB"],
    )
    defaults.update(overrides)
    return TravelDocRequirements(**defaults)


@pytest.fixture(autouse=True)
def _mock_s3():
    """Activate moto S3 mock and monkey-patch the module-level client."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="delta-concierge-kb")

        orig_client = sync_mod.s3_client
        sync_mod.s3_client = client

        try:
            yield client
        finally:
            sync_mod.s3_client = orig_client


class TestHandlerUpload:
    """Handler iterates COUNTRY_REQUIREMENTS and uploads one document per country."""

    def test_uploads_one_document_per_country(self, _mock_s3):
        """Each country in COUNTRY_REQUIREMENTS gets a corresponding S3 object."""
        handler({}, None)

        objects = _mock_s3.list_objects_v2(Bucket="delta-concierge-kb", Prefix="country-requirements/")
        keys = [obj["Key"] for obj in objects["Contents"]]

        for code in COUNTRY_REQUIREMENTS:
            assert f"country-requirements/{code}.json" in keys

    def test_returns_correct_count(self):
        """Response body contains the exact count of synced documents."""
        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_uploaded_content_is_valid_json(self, _mock_s3):
        """Each uploaded object is valid JSON matching the built document."""
        handler({}, None)

        code = next(iter(COUNTRY_REQUIREMENTS))
        obj = _mock_s3.get_object(Bucket="delta-concierge-kb", Key=f"country-requirements/{code}.json")
        body = json.loads(obj["Body"].read())

        assert body["country_code"] == code


class TestBuildDocumentRequiredFields:
    """_build_document always includes the required fields."""

    def test_includes_country_code(self):
        reqs = _base_reqs(country_code="DE")

        doc = _build_document("DE", reqs)

        assert doc["country_code"] == "DE"

    def test_includes_country_name_from_lookup(self):
        reqs = _base_reqs(country_code="DE")

        doc = _build_document("DE", reqs)

        assert doc["country_name"] == "Germany"

    def test_includes_visa_required(self):
        reqs = _base_reqs(requires_visa=True)

        doc = _build_document("DE", reqs)

        assert doc["visa_required"] is True

    def test_includes_transit_visa_required(self):
        reqs = _base_reqs(transit_visa_required=False)

        doc = _build_document("DE", reqs)

        assert doc["transit_visa_required"] is False

    def test_includes_passport_validity_months(self):
        reqs = _base_reqs(passport_validity_months=3)

        doc = _build_document("DE", reqs)

        assert doc["passport_validity_months"] == 3

    def test_includes_visa_exempt_nationalities(self):
        reqs = _base_reqs(visa_exempt_nationalities=["US", "GB"])

        doc = _build_document("DE", reqs)

        assert doc["visa_exempt_nationalities"] == ["US", "GB"]


class TestBuildDocumentOptionalFields:
    """_build_document includes optional fields only when present."""

    def test_includes_embassy_url_when_present(self):
        reqs = _base_reqs(embassy_url="https://embassy.example.com")

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"

    def test_includes_evisa_portal_url_when_present(self):
        reqs = _base_reqs(evisa_portal_url="https://evisa.example.com")

        doc = _build_document("DE", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example.com"

    def test_includes_estimated_processing_days_when_present(self):
        reqs = _base_reqs(estimated_processing_days=14)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 14

    def test_includes_entry_form_url_when_present(self):
        reqs = _base_reqs(entry_form_url="https://entry.example.com")

        doc = _build_document("DE", reqs)

        assert doc["entry_form_url"] == "https://entry.example.com"

    def test_omits_embassy_url_when_not_set(self):
        reqs = _base_reqs()

        doc = _build_document("DE", reqs)

        assert "embassy_url" not in doc

    def test_omits_evisa_portal_url_when_not_set(self):
        reqs = _base_reqs()

        doc = _build_document("DE", reqs)

        assert "evisa_portal_url" not in doc

    def test_omits_estimated_processing_days_when_not_set(self):
        reqs = _base_reqs()

        doc = _build_document("DE", reqs)

        assert "estimated_processing_days" not in doc

    def test_omits_entry_form_url_when_not_set(self):
        reqs = _base_reqs()

        doc = _build_document("DE", reqs)

        assert "entry_form_url" not in doc


class TestCountryNameLookup:
    """_build_document uses _COUNTRY_NAMES for country name, falling back to code."""

    def test_known_country_uses_name(self):
        reqs = _base_reqs(country_code="JP")

        doc = _build_document("JP", reqs)

        assert doc["country_name"] == "Japan"

    def test_unknown_country_falls_back_to_code(self):
        reqs = _base_reqs(country_code="XX")

        doc = _build_document("XX", reqs)

        assert doc["country_name"] == "XX"
