"""Tests for the Bedrock Knowledge Base S3 sync handler.

Covers the handler's upload loop (keys, bucket, body, content type,
document count), the empty-requirements case, re-sync overwrites, and
every optional-field and country-name branch of _build_document.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

TEST_BUCKET = "test-kb-bucket"


# ------------------------------------------------------------------
# Fixtures and helpers
# ------------------------------------------------------------------

def _make_requirements(
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
            ["US", "GB", "CA"] if visa_exempt_nationalities is None
            else visa_exempt_nationalities
        ),
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


def _default_requirements() -> dict[str, TravelDocRequirements]:
    """Small requirements set: Schengen 3-month, transit-visa 6-month, visa-free."""
    return {
        "DE": _make_requirements(),
        "CN": _make_requirements(
            country_code="CN",
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            embassy_url="https://cn.example/embassy",
            evisa_portal_url="https://cn.example/evisa",
            estimated_processing_days=10,
        ),
        "GB": _make_requirements(
            country_code="GB",
            requires_visa=False,
            passport_validity_months=0,
            entry_form_url="https://gb.example/eta",
        ),
    }


@pytest.fixture(autouse=True)
def s3():
    """Activate moto, create the KB bucket, and swap module-level state."""
    with moto.mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=TEST_BUCKET)

        orig_client = sync_mod.s3_client
        orig_bucket = sync_mod.KNOWLEDGE_BASE_BUCKET
        orig_reqs = sync_mod.COUNTRY_REQUIREMENTS

        sync_mod.s3_client = client
        sync_mod.KNOWLEDGE_BASE_BUCKET = TEST_BUCKET
        sync_mod.COUNTRY_REQUIREMENTS = _default_requirements()

        try:
            yield client
        finally:
            sync_mod.s3_client = orig_client
            sync_mod.KNOWLEDGE_BASE_BUCKET = orig_bucket
            sync_mod.COUNTRY_REQUIREMENTS = orig_reqs


def _list_keys(client) -> list[str]:
    resp = client.list_objects_v2(Bucket=TEST_BUCKET)
    return sorted(obj["Key"] for obj in resp.get("Contents", []))


def _get_doc(client, country_code: str) -> dict:
    obj = client.get_object(
        Bucket=TEST_BUCKET, Key=f"country-requirements/{country_code}.json"
    )
    return json.loads(obj["Body"].read())


# ------------------------------------------------------------------
# Handler: upload loop
# ------------------------------------------------------------------

class TestHandlerSyncsAllCountries:
    """Every country in COUNTRY_REQUIREMENTS is uploaded once and counted."""

    def test_returns_200_with_document_count(self, s3):
        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 3}}

    def test_uploads_one_object_per_country_under_prefix(self, s3):
        handler({}, None)

        assert _list_keys(s3) == [
            "country-requirements/CN.json",
            "country-requirements/DE.json",
            "country-requirements/GB.json",
        ]

    def test_object_content_type_is_json(self, s3):
        handler({}, None)

        head = s3.head_object(Bucket=TEST_BUCKET, Key="country-requirements/DE.json")
        assert head["ContentType"] == "application/json"

    def test_uploaded_body_matches_built_document(self, s3):
        handler({}, None)

        assert _get_doc(s3, "CN") == _build_document(
            "CN", sync_mod.COUNTRY_REQUIREMENTS["CN"]
        )

    def test_body_is_pretty_printed_json(self, s3):
        handler({}, None)

        body = s3.get_object(
            Bucket=TEST_BUCKET, Key="country-requirements/DE.json"
        )["Body"].read().decode()
        assert body == json.dumps(
            _build_document("DE", sync_mod.COUNTRY_REQUIREMENTS["DE"]), indent=2
        )

    def test_event_and_context_are_ignored(self, s3):
        result = handler({"detail-type": "Scheduled Event", "foo": 1}, object())

        assert result["body"]["documents_synced"] == 3


class TestHandlerEmptyRequirements:
    """No requirements means nothing is uploaded and a zero count is returned."""

    def test_empty_requirements_syncs_nothing(self, s3):
        sync_mod.COUNTRY_REQUIREMENTS = {}

        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 0}}
        assert _list_keys(s3) == []


class TestHandlerBucketConfiguration:
    """Uploads go to the configured KNOWLEDGE_BASE_BUCKET."""

    def test_uses_configured_bucket(self, s3):
        s3.create_bucket(Bucket="other-kb-bucket")
        sync_mod.KNOWLEDGE_BASE_BUCKET = "other-kb-bucket"

        handler({}, None)

        resp = s3.list_objects_v2(Bucket="other-kb-bucket")
        assert len(resp["Contents"]) == 3
        assert _list_keys(s3) == []

    def test_missing_bucket_raises(self, s3):
        sync_mod.KNOWLEDGE_BASE_BUCKET = "does-not-exist"

        with pytest.raises(s3.exceptions.NoSuchBucket):
            handler({}, None)


class TestHandlerResyncAfterRequirementsChange:
    """Re-running after a rule change overwrites the stale document."""

    def test_resync_overwrites_changed_passport_validity(self, s3):
        handler({}, None)
        sync_mod.COUNTRY_REQUIREMENTS["DE"] = _make_requirements(
            passport_validity_months=6
        )

        handler({}, None)

        assert _get_doc(s3, "DE")["passport_validity_months"] == 6
        assert len(_list_keys(s3)) == 3


# ------------------------------------------------------------------
# _build_document: required fields
# ------------------------------------------------------------------

class TestBuildDocumentRequiredFields:
    """Core requirement fields are always present and copied verbatim."""

    def test_minimal_document_has_only_required_keys(self):
        doc = _build_document("DE", _make_requirements())

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB", "CA"],
        }

    def test_visa_not_required(self):
        doc = _build_document("GB", _make_requirements(country_code="GB", requires_visa=False))

        assert doc["visa_required"] is False

    def test_transit_visa_required_for_layovers(self):
        doc = _build_document("CN", _make_requirements(country_code="CN", transit_visa_required=True))

        assert doc["transit_visa_required"] is True

    def test_six_month_passport_validity(self):
        doc = _build_document("JP", _make_requirements(country_code="JP", passport_validity_months=6))

        assert doc["passport_validity_months"] == 6

    def test_no_passport_validity_requirement(self):
        doc = _build_document("MX", _make_requirements(country_code="MX", passport_validity_months=0))

        assert doc["passport_validity_months"] == 0

    def test_empty_visa_exempt_nationalities(self):
        doc = _build_document("CN", _make_requirements(country_code="CN", visa_exempt_nationalities=[]))

        assert doc["visa_exempt_nationalities"] == []

    def test_country_code_comes_from_requirements(self):
        doc = _build_document("XX", _make_requirements(country_code="DE"))

        assert doc["country_code"] == "DE"


class TestBuildDocumentCountryName:
    """Country name is looked up by key, falling back to the code itself."""

    def test_known_country_name(self):
        doc = _build_document("AE", _make_requirements(country_code="AE"))

        assert doc["country_name"] == "United Arab Emirates"

    def test_unknown_country_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_requirements(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"


# ------------------------------------------------------------------
# _build_document: optional fields
# ------------------------------------------------------------------

class TestBuildDocumentOptionalFields:
    """Optional fields are included only when set (truthy / not None)."""

    def test_embassy_url_included(self):
        doc = _build_document("DE", _make_requirements(embassy_url="https://de.example"))

        assert doc["embassy_url"] == "https://de.example"

    def test_empty_embassy_url_omitted(self):
        doc = _build_document("DE", _make_requirements(embassy_url=""))

        assert "embassy_url" not in doc

    def test_evisa_portal_url_included(self):
        doc = _build_document("DE", _make_requirements(evisa_portal_url="https://evisa.example"))

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_empty_evisa_portal_url_omitted(self):
        doc = _build_document("DE", _make_requirements(evisa_portal_url=""))

        assert "evisa_portal_url" not in doc

    def test_processing_days_included(self):
        doc = _build_document("DE", _make_requirements(estimated_processing_days=15))

        assert doc["estimated_processing_days"] == 15

    def test_zero_processing_days_included(self):
        doc = _build_document("DE", _make_requirements(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    def test_none_processing_days_omitted(self):
        doc = _build_document("DE", _make_requirements(estimated_processing_days=None))

        assert "estimated_processing_days" not in doc

    def test_entry_form_url_included(self):
        doc = _build_document("DE", _make_requirements(entry_form_url="https://form.example"))

        assert doc["entry_form_url"] == "https://form.example"

    def test_empty_entry_form_url_omitted(self):
        doc = _build_document("DE", _make_requirements(entry_form_url=""))

        assert "entry_form_url" not in doc

    def test_all_optional_fields_included(self):
        doc = _build_document(
            "CN",
            _make_requirements(
                country_code="CN",
                embassy_url="https://e",
                evisa_portal_url="https://v",
                estimated_processing_days=7,
                entry_form_url="https://f",
            ),
        )

        assert {
            "embassy_url", "evisa_portal_url",
            "estimated_processing_days", "entry_form_url",
        } <= doc.keys()
