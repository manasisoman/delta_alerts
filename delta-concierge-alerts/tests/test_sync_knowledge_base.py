"""Tests for the Bedrock Knowledge Base sync handler.

Covers the S3 upload loop in ``handler`` (keys, bodies, content type,
document count, re-sync overwrite) and every optional-field branch in
``_build_document``, including layover/transit-visa flags, per-country
passport validity windows, and visa-exempt nationalities.
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements

TEST_BUCKET = "test-kb-bucket"


# ------------------------------------------------------------------
# Helpers & fixtures
# ------------------------------------------------------------------

def _make_reqs(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with sensible defaults and overrides."""
    fields = {
        "country_code": "DE",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB", "CA"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _fixture_requirements() -> dict[str, TravelDocRequirements]:
    """Small, isolated requirements set used in place of production data."""
    return {
        "DE": _make_reqs(),
        "CN": _make_reqs(
            country_code="CN",
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
            embassy_url="https://cn.example/embassy",
            evisa_portal_url="https://cn.example/evisa",
            estimated_processing_days=10,
            entry_form_url="https://cn.example/entry",
        ),
        "ZZ": _make_reqs(
            country_code="ZZ",
            requires_visa=False,
            passport_validity_months=0,
            visa_exempt_nationalities=[],
        ),
    }


@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the KB bucket, and swap module-level state."""
    with moto.mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket=TEST_BUCKET)

        orig_s3_client = sync_mod.s3_client
        orig_bucket = sync_mod.KNOWLEDGE_BASE_BUCKET
        orig_requirements = sync_mod.COUNTRY_REQUIREMENTS

        sync_mod.s3_client = s3
        sync_mod.KNOWLEDGE_BASE_BUCKET = TEST_BUCKET
        sync_mod.COUNTRY_REQUIREMENTS = _fixture_requirements()

        try:
            yield s3
        finally:
            sync_mod.s3_client = orig_s3_client
            sync_mod.KNOWLEDGE_BASE_BUCKET = orig_bucket
            sync_mod.COUNTRY_REQUIREMENTS = orig_requirements


@pytest.fixture
def s3(_mock_aws):
    """Expose the moto S3 client created by _mock_aws."""
    return _mock_aws


def _get_doc(s3, country_code: str) -> dict:
    obj = s3.get_object(
        Bucket=TEST_BUCKET, Key=f"country-requirements/{country_code}.json"
    )
    return json.loads(obj["Body"].read())


def _list_keys(s3) -> list[str]:
    resp = s3.list_objects_v2(Bucket=TEST_BUCKET)
    return sorted(o["Key"] for o in resp.get("Contents", []))


# ------------------------------------------------------------------
# handler — S3 sync
# ------------------------------------------------------------------

class TestHandlerSync:
    """handler uploads one JSON document per country and reports the count."""

    def test_happy_path_returns_200_with_count(self, s3):
        result = handler({}, None)

        assert result == {"statusCode": 200, "body": {"documents_synced": 3}}

    def test_uploads_one_object_per_country_under_prefix(self, s3):
        handler({}, None)

        assert _list_keys(s3) == [
            "country-requirements/CN.json",
            "country-requirements/DE.json",
            "country-requirements/ZZ.json",
        ]

    def test_object_content_type_is_json(self, s3):
        handler({}, None)

        head = s3.head_object(
            Bucket=TEST_BUCKET, Key="country-requirements/DE.json"
        )
        assert head["ContentType"] == "application/json"

    def test_object_body_matches_built_document(self, s3):
        handler({}, None)

        expected = _build_document("CN", sync_mod.COUNTRY_REQUIREMENTS["CN"])
        assert _get_doc(s3, "CN") == expected

    def test_body_is_pretty_printed_json(self, s3):
        handler({}, None)

        body = s3.get_object(
            Bucket=TEST_BUCKET, Key="country-requirements/DE.json"
        )["Body"].read().decode()
        assert body.startswith("{\n  ")

    def test_empty_requirements_syncs_nothing(self, s3):
        sync_mod.COUNTRY_REQUIREMENTS = {}

        result = handler({}, None)

        assert result["body"]["documents_synced"] == 0
        assert _list_keys(s3) == []

    def test_uses_configured_bucket(self, s3):
        s3.create_bucket(Bucket="other-kb-bucket")
        sync_mod.KNOWLEDGE_BASE_BUCKET = "other-kb-bucket"

        handler({}, None)

        resp = s3.list_objects_v2(Bucket="other-kb-bucket")
        assert resp["KeyCount"] == 3
        assert _list_keys(s3) == []

    def test_event_and_context_are_ignored(self, s3):
        result = handler({"detail": {"anything": 1}}, object())

        assert result["body"]["documents_synced"] == 3

    def test_missing_bucket_raises(self, s3):
        sync_mod.KNOWLEDGE_BASE_BUCKET = "does-not-exist"

        with pytest.raises(s3.exceptions.NoSuchBucket):
            handler({}, None)

    def test_syncs_full_production_dataset(self, s3):
        sync_mod.COUNTRY_REQUIREMENTS = COUNTRY_REQUIREMENTS

        result = handler({}, None)

        assert result["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)
        assert len(_list_keys(s3)) == len(COUNTRY_REQUIREMENTS)


class TestRequirementsChangeResync:
    """Re-running the sync after requirements change overwrites stale docs."""

    def test_resync_overwrites_changed_country(self, s3):
        handler({}, None)
        sync_mod.COUNTRY_REQUIREMENTS["DE"] = _make_reqs(
            passport_validity_months=6, transit_visa_required=True
        )

        handler({}, None)

        doc = _get_doc(s3, "DE")
        assert doc["passport_validity_months"] == 6
        assert doc["transit_visa_required"] is True

    def test_resync_drops_removed_optional_field(self, s3):
        handler({}, None)
        sync_mod.COUNTRY_REQUIREMENTS["CN"] = _make_reqs(country_code="CN")

        handler({}, None)

        assert "embassy_url" not in _get_doc(s3, "CN")

    def test_resync_adds_new_country(self, s3):
        handler({}, None)
        sync_mod.COUNTRY_REQUIREMENTS["JP"] = _make_reqs(country_code="JP")

        result = handler({}, None)

        assert result["body"]["documents_synced"] == 4
        assert "country-requirements/JP.json" in _list_keys(s3)


# ------------------------------------------------------------------
# _build_document — required fields
# ------------------------------------------------------------------

class TestBuildDocumentBaseFields:
    """Required requirement fields are always present in the document."""

    def test_happy_path_minimal_document(self):
        doc = _build_document("DE", _make_reqs())

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB", "CA"],
        }

    def test_country_code_comes_from_requirements(self):
        doc = _build_document("DE", _make_reqs(country_code="XX"))

        assert doc["country_code"] == "XX"

    def test_visa_not_required(self):
        doc = _build_document("DE", _make_reqs(requires_visa=False))

        assert doc["visa_required"] is False

    def test_document_is_json_serializable(self):
        doc = _build_document("CN", _fixture_requirements()["CN"])

        assert json.loads(json.dumps(doc)) == doc


class TestBuildDocumentCountryName:
    """country_name resolves from the lookup table, else falls back to code."""

    @pytest.mark.parametrize(
        "code, name",
        [("US", "United States"), ("GB", "United Kingdom"), ("AE", "United Arab Emirates")],
    )
    def test_known_country_name(self, code, name):
        doc = _build_document(code, _make_reqs(country_code=code))

        assert doc["country_name"] == name

    def test_unknown_country_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_reqs(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_every_production_country_has_a_name(self):
        missing = [c for c in COUNTRY_REQUIREMENTS if c not in sync_mod._COUNTRY_NAMES]

        assert missing == []


class TestBuildDocumentLayoverTransitVisa:
    """transit_visa_required (layover rule) is passed through unchanged."""

    def test_transit_visa_required_for_layover(self):
        doc = _build_document("CN", _make_reqs(country_code="CN", transit_visa_required=True))

        assert doc["transit_visa_required"] is True

    def test_transit_visa_not_required_for_layover(self):
        doc = _build_document("DE", _make_reqs(transit_visa_required=False))

        assert doc["transit_visa_required"] is False

    def test_production_china_requires_transit_visa(self):
        doc = _build_document("CN", COUNTRY_REQUIREMENTS["CN"])

        assert doc["transit_visa_required"] == COUNTRY_REQUIREMENTS["CN"].transit_visa_required


class TestBuildDocumentPassportValidity:
    """passport_validity_months reflects each country's validity window."""

    def test_schengen_three_month_window(self):
        doc = _build_document("FR", _make_reqs(country_code="FR", passport_validity_months=3))

        assert doc["passport_validity_months"] == 3

    def test_six_month_window(self):
        doc = _build_document("JP", _make_reqs(country_code="JP", passport_validity_months=6))

        assert doc["passport_validity_months"] == 6

    def test_zero_month_window_preserved(self):
        doc = _build_document("ZZ", _make_reqs(country_code="ZZ", passport_validity_months=0))

        assert doc["passport_validity_months"] == 0

    @pytest.mark.parametrize("code", ["DE", "FR", "IT", "ES", "NL"])
    def test_production_schengen_countries_are_three_months(self, code):
        doc = _build_document(code, COUNTRY_REQUIREMENTS[code])

        assert doc["passport_validity_months"] == 3


class TestBuildDocumentVisaExemptions:
    """visa_exempt_nationalities is passed through unchanged."""

    def test_exempt_nationalities_listed(self):
        doc = _build_document("DE", _make_reqs(visa_exempt_nationalities=["US", "JP"]))

        assert doc["visa_exempt_nationalities"] == ["US", "JP"]

    def test_no_exempt_nationalities(self):
        doc = _build_document("CN", _make_reqs(country_code="CN", visa_exempt_nationalities=[]))

        assert doc["visa_exempt_nationalities"] == []


# ------------------------------------------------------------------
# _build_document — optional fields
# ------------------------------------------------------------------

class TestBuildDocumentOptionalFields:
    """Optional fields appear only when set (truthy / not None)."""

    @pytest.mark.parametrize(
        "field",
        ["embassy_url", "evisa_portal_url", "estimated_processing_days", "entry_form_url"],
    )
    def test_optional_field_omitted_by_default(self, field):
        doc = _build_document("DE", _make_reqs())

        assert field not in doc

    def test_embassy_url_included(self):
        doc = _build_document("DE", _make_reqs(embassy_url="https://de.example"))

        assert doc["embassy_url"] == "https://de.example"

    def test_evisa_portal_url_included(self):
        doc = _build_document("DE", _make_reqs(evisa_portal_url="https://evisa.example"))

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_entry_form_url_included(self):
        doc = _build_document("DE", _make_reqs(entry_form_url="https://entry.example"))

        assert doc["entry_form_url"] == "https://entry.example"

    def test_estimated_processing_days_included(self):
        doc = _build_document("DE", _make_reqs(estimated_processing_days=7))

        assert doc["estimated_processing_days"] == 7

    def test_estimated_processing_days_zero_is_included(self):
        doc = _build_document("DE", _make_reqs(estimated_processing_days=0))

        assert doc["estimated_processing_days"] == 0

    @pytest.mark.parametrize("field", ["embassy_url", "evisa_portal_url", "entry_form_url"])
    def test_empty_string_url_omitted(self, field):
        doc = _build_document("DE", _make_reqs(**{field: ""}))

        assert field not in doc

    def test_all_optional_fields_included(self):
        doc = _build_document("CN", _fixture_requirements()["CN"])

        assert doc["embassy_url"] == "https://cn.example/embassy"
        assert doc["evisa_portal_url"] == "https://cn.example/evisa"
        assert doc["estimated_processing_days"] == 10
        assert doc["entry_form_url"] == "https://cn.example/entry"
