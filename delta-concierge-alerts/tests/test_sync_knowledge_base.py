"""Tests for the Knowledge Base sync handler.

Covers the happy path (a JSON document written to S3 for every country in
COUNTRY_REQUIREMENTS with the expected key/content/count), the optional-field
branches of ``_build_document``, the country-name fallback, and the
empty-requirements edge case.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import handler
from src.models.types import TravelDocRequirements


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _make_reqs(
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


def _read_document(country_code: str) -> dict:
    """Fetch and parse a synced document from the mocked S3 bucket."""
    key = f"{sync_mod.S3_PREFIX}/{country_code}.json"
    body = sync_mod.s3_client.get_object(
        Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key=key
    )["Body"].read()
    return json.loads(body)


def _list_keys() -> list[str]:
    """List all object keys currently in the mocked S3 bucket."""
    resp = sync_mod.s3_client.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)
    return [obj["Key"] for obj in resp.get("Contents", [])]


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the KB bucket, and monkey-patch the S3 client."""
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
def patch_requirements(monkeypatch):
    """Return a helper that swaps the module-level COUNTRY_REQUIREMENTS dict."""
    def _patch(requirements: dict[str, TravelDocRequirements]) -> None:
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", requirements)

    return _patch


# ------------------------------------------------------------------
# Happy path
# ------------------------------------------------------------------

class TestHappyPath:
    """One document is written to S3 for each country requirement."""

    def test_returns_200_with_synced_count(self, patch_requirements):
        patch_requirements({"DE": _make_reqs("DE"), "CN": _make_reqs("CN")})

        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == 2

    def test_writes_one_object_per_country_with_expected_keys(self, patch_requirements):
        patch_requirements({"DE": _make_reqs("DE"), "JP": _make_reqs("JP")})

        handler({}, None)

        assert sorted(_list_keys()) == [
            "country-requirements/DE.json",
            "country-requirements/JP.json",
        ]

    def test_document_content_matches_requirements(self, patch_requirements):
        reqs = _make_reqs(
            "CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        )
        patch_requirements({"CN": reqs})

        handler({}, None)

        doc = _read_document("CN")
        assert doc["country_code"] == "CN"
        assert doc["country_name"] == "China"
        assert doc["visa_required"] is True
        assert doc["transit_visa_required"] is True
        assert doc["passport_validity_months"] == 6
        assert doc["visa_exempt_nationalities"] == []

    def test_object_written_as_json_content_type(self, patch_requirements):
        patch_requirements({"DE": _make_reqs("DE")})

        handler({}, None)

        obj = sync_mod.s3_client.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key="country-requirements/DE.json",
        )
        assert obj["ContentType"] == "application/json"


# ------------------------------------------------------------------
# Optional fields
# ------------------------------------------------------------------

class TestOptionalFields:
    """Optional document keys appear only when their source value is set."""

    def test_optional_fields_omitted_when_unset(self, patch_requirements):
        patch_requirements({"DE": _make_reqs("DE")})

        handler({}, None)

        doc = _read_document("DE")
        assert "embassy_url" not in doc
        assert "evisa_portal_url" not in doc
        assert "estimated_processing_days" not in doc
        assert "entry_form_url" not in doc

    def test_optional_fields_included_when_set(self, patch_requirements):
        patch_requirements(
            {
                "DE": _make_reqs(
                    "DE",
                    embassy_url="https://embassy.example/de",
                    evisa_portal_url="https://evisa.example/de",
                    estimated_processing_days=10,
                    entry_form_url="https://entry.example/de",
                )
            }
        )

        handler({}, None)

        doc = _read_document("DE")
        assert doc["embassy_url"] == "https://embassy.example/de"
        assert doc["evisa_portal_url"] == "https://evisa.example/de"
        assert doc["estimated_processing_days"] == 10
        assert doc["entry_form_url"] == "https://entry.example/de"

    def test_zero_processing_days_is_included(self, patch_requirements):
        patch_requirements({"DE": _make_reqs("DE", estimated_processing_days=0)})

        handler({}, None)

        doc = _read_document("DE")
        assert doc["estimated_processing_days"] == 0


# ------------------------------------------------------------------
# Country name resolution
# ------------------------------------------------------------------

class TestCountryName:
    """country_name resolves from the lookup table, falling back to the code."""

    def test_known_code_maps_to_name(self, patch_requirements):
        patch_requirements({"FR": _make_reqs("FR")})

        handler({}, None)

        assert _read_document("FR")["country_name"] == "France"

    def test_unknown_code_falls_back_to_code(self, patch_requirements):
        patch_requirements({"ZZ": _make_reqs("ZZ")})

        handler({}, None)

        assert _read_document("ZZ")["country_name"] == "ZZ"


# ------------------------------------------------------------------
# Empty requirements
# ------------------------------------------------------------------

class TestEmptyRequirements:
    """No requirements means nothing is written and the count is zero."""

    def test_no_objects_written(self, patch_requirements):
        patch_requirements({})

        response = handler({}, None)

        assert response["body"]["documents_synced"] == 0
        assert _list_keys() == []
