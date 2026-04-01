"""Tests for the sync_knowledge_base Lambda handler."""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


def _base_reqs(**overrides) -> TravelDocRequirements:
    """Build a minimal TravelDocRequirements with sensible defaults."""
    defaults = dict(
        country_code="DE",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB"],
    )
    defaults.update(overrides)
    return TravelDocRequirements(**defaults)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_s3():
    """Activate moto S3 mock and monkey-patch the module-level s3_client."""
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
def s3_client(_mock_s3):
    """Expose the moto S3 client for direct assertions."""
    return _mock_s3


# ---------------------------------------------------------------------------
# _build_document — base fields
# ---------------------------------------------------------------------------

class TestBuildDocumentBaseFields:
    """_build_document always includes the core requirement fields."""

    def test_base_fields_present(self):
        """All mandatory fields appear in the output."""
        reqs = _base_reqs()

        doc = _build_document("DE", reqs)

        assert doc["country_code"] == "DE"
        assert doc["country_name"] == "Germany"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is False
        assert doc["passport_validity_months"] == 3
        assert doc["visa_exempt_nationalities"] == ["US", "GB"]

    def test_unknown_country_code_falls_back_to_code(self):
        """Country code not in _COUNTRY_NAMES uses the code itself as name."""
        reqs = _base_reqs(country_code="ZZ")

        doc = _build_document("ZZ", reqs)

        assert doc["country_name"] == "ZZ"


# ---------------------------------------------------------------------------
# _build_document — optional fields
# ---------------------------------------------------------------------------

class TestBuildDocumentOptionalFields:
    """Optional fields are included only when the requirement provides them."""

    def test_no_optional_fields_when_absent(self):
        """Bare-minimum reqs produce no optional keys."""
        reqs = _base_reqs()

        doc = _build_document("DE", reqs)

        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_embassy_url_included(self):
        """embassy_url appears when set on the requirements."""
        reqs = _base_reqs(embassy_url="https://embassy.example.com")

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"

    def test_evisa_portal_url_included(self):
        """evisa_portal_url appears when set on the requirements."""
        reqs = _base_reqs(evisa_portal_url="https://evisa.example.com")

        doc = _build_document("DE", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example.com"

    def test_estimated_processing_days_included(self):
        """estimated_processing_days appears when not None."""
        reqs = _base_reqs(estimated_processing_days=14)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 14

    def test_estimated_processing_days_zero_included(self):
        """estimated_processing_days=0 is a valid value and should be included."""
        reqs = _base_reqs(estimated_processing_days=0)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included(self):
        """entry_form_url appears when set on the requirements."""
        reqs = _base_reqs(entry_form_url="https://entry.example.com")

        doc = _build_document("DE", reqs)

        assert doc["entry_form_url"] == "https://entry.example.com"

    def test_all_optional_fields_included(self):
        """When all optional fields are set, all appear in the document."""
        reqs = _base_reqs(
            embassy_url="https://embassy.example.com",
            evisa_portal_url="https://evisa.example.com",
            estimated_processing_days=7,
            entry_form_url="https://entry.example.com",
        )

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example.com"
        assert doc["evisa_portal_url"] == "https://evisa.example.com"
        assert doc["estimated_processing_days"] == 7
        assert doc["entry_form_url"] == "https://entry.example.com"


# ---------------------------------------------------------------------------
# _build_document — country name mapping
# ---------------------------------------------------------------------------

class TestBuildDocumentCountryNames:
    """_build_document resolves country codes to human-readable names."""

    def test_known_country_resolves_to_name(self):
        """A code present in _COUNTRY_NAMES maps to its full name."""
        reqs = _base_reqs(country_code="JP")

        doc = _build_document("JP", reqs)

        assert doc["country_name"] == "Japan"

    def test_another_known_country(self):
        """Verify a second entry in the name mapping."""
        reqs = _base_reqs(country_code="BR")

        doc = _build_document("BR", reqs)

        assert doc["country_name"] == "Brazil"


# ---------------------------------------------------------------------------
# handler — end-to-end S3 upload
# ---------------------------------------------------------------------------

class TestHandlerUpload:
    """handler() iterates COUNTRY_REQUIREMENTS and uploads each document to S3."""

    def test_uploads_all_countries_to_s3(self, s3_client, monkeypatch):
        """Every country in COUNTRY_REQUIREMENTS gets an S3 object."""
        small_reqs = {
            "DE": _base_reqs(country_code="DE"),
            "JP": _base_reqs(country_code="JP"),
        }
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", small_reqs)

        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == 2

        # Verify objects in S3
        objs = s3_client.list_objects_v2(
            Bucket="delta-concierge-kb", Prefix="country-requirements/"
        )
        keys = sorted(o["Key"] for o in objs["Contents"])
        assert keys == [
            "country-requirements/DE.json",
            "country-requirements/JP.json",
        ]

    def test_uploaded_document_content_is_valid_json(self, s3_client, monkeypatch):
        """Each uploaded object is parseable JSON matching _build_document output."""
        small_reqs = {"DE": _base_reqs(country_code="DE")}
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", small_reqs)

        handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/DE.json",
        )
        body = json.loads(obj["Body"].read())

        assert body["country_code"] == "DE"
        assert body["country_name"] == "Germany"
        assert body["visa_required"] is True

    def test_empty_requirements_syncs_zero(self, s3_client, monkeypatch):
        """An empty COUNTRY_REQUIREMENTS dict uploads nothing."""
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {})

        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == 0

    def test_handler_with_full_country_requirements(self, s3_client):
        """handler() works with the real COUNTRY_REQUIREMENTS data."""
        from src.data.country_requirements import COUNTRY_REQUIREMENTS

        result = handler({}, None)

        assert result["statusCode"] == 200
        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)

    def test_s3_object_content_type(self, s3_client, monkeypatch):
        """Uploaded S3 objects have application/json content type."""
        small_reqs = {"DE": _base_reqs(country_code="DE")}
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", small_reqs)

        handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/DE.json",
        )
        assert obj["ContentType"] == "application/json"

    def test_optional_fields_survive_s3_roundtrip(self, s3_client, monkeypatch):
        """Optional fields set on reqs appear in the uploaded JSON."""
        reqs_with_extras = _base_reqs(
            country_code="IL",
            embassy_url="https://embassy.example.com",
            entry_form_url="https://entry.example.com",
            estimated_processing_days=14,
        )
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"IL": reqs_with_extras})

        handler({}, None)

        obj = s3_client.get_object(
            Bucket="delta-concierge-kb",
            Key="country-requirements/IL.json",
        )
        body = json.loads(obj["Body"].read())
        assert body["embassy_url"] == "https://embassy.example.com"
        assert body["entry_form_url"] == "https://entry.example.com"
        assert body["estimated_processing_days"] == 14
