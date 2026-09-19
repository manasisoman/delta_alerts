"""Tests for the Knowledge Base sync Lambda handler.

Covers the S3 upload loop in ``handler`` (one object per country, key
layout, JSON body/content type, returned count) and every optional-field
branch in ``_build_document`` (embassy URL, e-visa portal URL, estimated
processing days, entry form URL, and known vs. unknown country names).
"""

import json

import boto3
import moto
import pytest

import src.handlers.sync_knowledge_base as sync_mod
from src.data.country_requirements import COUNTRY_REQUIREMENTS
from src.handlers.sync_knowledge_base import _build_document, handler
from src.models.types import TravelDocRequirements


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto, create the KB bucket, and patch the module S3 client."""
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
    """Build a TravelDocRequirements with all optional fields unset by default."""
    return TravelDocRequirements(
        country_code=country_code,
        requires_visa=requires_visa,
        transit_visa_required=transit_visa_required,
        passport_validity_months=passport_validity_months,
        visa_exempt_nationalities=(
            visa_exempt_nationalities if visa_exempt_nationalities is not None else ["US", "GB"]
        ),
        embassy_url=embassy_url,
        evisa_portal_url=evisa_portal_url,
        estimated_processing_days=estimated_processing_days,
        entry_form_url=entry_form_url,
    )


def _list_keys(s3) -> list[str]:
    """Return every object key currently in the KB bucket."""
    keys: list[str] = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET):
        keys.extend(obj["Key"] for obj in page.get("Contents", []))
    return keys


# ------------------------------------------------------------------
# handler: happy path
# ------------------------------------------------------------------

class TestHandlerSyncsAllCountries:
    """handler uploads exactly one JSON document per COUNTRY_REQUIREMENTS entry."""

    def test_returns_200_with_synced_count(self):
        response = handler({}, None)

        assert response["statusCode"] == 200
        assert response["body"] == {"documents_synced": len(COUNTRY_REQUIREMENTS)}

    def test_uploads_one_object_per_country_under_prefix(self, s3):
        handler({}, None)

        keys = _list_keys(s3)
        expected = {
            f"{sync_mod.S3_PREFIX}/{code}.json" for code in COUNTRY_REQUIREMENTS
        }
        assert set(keys) == expected

    def test_uploaded_body_is_built_document_json(self, s3):
        handler({}, None)

        obj = s3.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key=f"{sync_mod.S3_PREFIX}/DE.json",
        )
        body = json.loads(obj["Body"].read())
        assert body == _build_document("DE", COUNTRY_REQUIREMENTS["DE"])
        assert body["country_code"] == "DE"
        assert body["country_name"] == "Germany"

    def test_uploaded_object_has_json_content_type(self, s3):
        handler({}, None)

        obj = s3.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
            Key=f"{sync_mod.S3_PREFIX}/US.json",
        )
        assert obj["ContentType"] == "application/json"

    def test_event_and_context_are_ignored(self):
        response = handler({"detail-type": "Scheduled Event", "foo": "bar"}, object())

        assert response["statusCode"] == 200
        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)


class TestHandlerWithPatchedRequirements:
    """handler iterates whatever COUNTRY_REQUIREMENTS contains at call time."""

    def test_empty_requirements_syncs_nothing(self, s3, monkeypatch):
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {})

        response = handler({}, None)

        assert response["body"] == {"documents_synced": 0}
        assert _list_keys(s3) == []

    def test_single_requirement_syncs_one_document(self, s3, monkeypatch):
        reqs = _make_requirements(country_code="XX", embassy_url="https://embassy.xx")
        monkeypatch.setattr(sync_mod, "COUNTRY_REQUIREMENTS", {"XX": reqs})

        response = handler({}, None)

        assert response["body"] == {"documents_synced": 1}
        assert _list_keys(s3) == [f"{sync_mod.S3_PREFIX}/XX.json"]
        obj = s3.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key=f"{sync_mod.S3_PREFIX}/XX.json"
        )
        body = json.loads(obj["Body"].read())
        assert body["country_name"] == "XX"
        assert body["embassy_url"] == "https://embassy.xx"

    def test_rerun_overwrites_existing_documents(self, s3, monkeypatch):
        monkeypatch.setattr(
            sync_mod, "COUNTRY_REQUIREMENTS", {"XX": _make_requirements(country_code="XX")}
        )
        handler({}, None)

        monkeypatch.setattr(
            sync_mod,
            "COUNTRY_REQUIREMENTS",
            {"XX": _make_requirements(country_code="XX", passport_validity_months=9)},
        )
        handler({}, None)

        assert _list_keys(s3) == [f"{sync_mod.S3_PREFIX}/XX.json"]
        obj = s3.get_object(
            Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET, Key=f"{sync_mod.S3_PREFIX}/XX.json"
        )
        assert json.loads(obj["Body"].read())["passport_validity_months"] == 9


class TestHandlerS3Failure:
    """handler propagates S3 errors rather than swallowing them."""

    def test_missing_bucket_raises(self, s3):
        s3.delete_bucket(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)

        with pytest.raises(s3.exceptions.NoSuchBucket):
            handler({}, None)


# ------------------------------------------------------------------
# _build_document: required fields
# ------------------------------------------------------------------

class TestBuildDocumentRequiredFields:
    """_build_document always emits the six core fields and nothing else by default."""

    def test_base_fields_only_when_no_optionals(self):
        reqs = _make_requirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB", "CA"],
        )

        doc = _build_document("DE", reqs)

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB", "CA"],
        }

    def test_visa_required_false_is_preserved(self):
        reqs = _make_requirements(requires_visa=False)

        doc = _build_document("DE", reqs)

        assert doc["visa_required"] is False

    def test_transit_visa_required_true_is_preserved(self):
        reqs = _make_requirements(transit_visa_required=True)

        doc = _build_document("DE", reqs)

        assert doc["transit_visa_required"] is True

    def test_country_code_comes_from_requirements_not_key(self):
        reqs = _make_requirements(country_code="FR")

        doc = _build_document("DE", reqs)

        assert doc["country_code"] == "FR"
        assert doc["country_name"] == "Germany"


class TestBuildDocumentCountryName:
    """country_name resolves via _COUNTRY_NAMES and falls back to the code."""

    @pytest.mark.parametrize(
        ("code", "name"),
        [("DE", "Germany"), ("US", "United States"), ("GB", "United Kingdom"), ("TZ", "Tanzania")],
    )
    def test_known_code_maps_to_name(self, code, name):
        doc = _build_document(code, _make_requirements(country_code=code))

        assert doc["country_name"] == name

    def test_unknown_code_falls_back_to_code(self):
        doc = _build_document("ZZ", _make_requirements(country_code="ZZ"))

        assert doc["country_name"] == "ZZ"

    def test_every_country_in_requirements_has_a_name(self):
        for code in COUNTRY_REQUIREMENTS:
            assert code in sync_mod._COUNTRY_NAMES, f"missing display name for {code}"


# ------------------------------------------------------------------
# _build_document: optional field branches
# ------------------------------------------------------------------

class TestBuildDocumentOptionalFields:
    """Each optional field is included only when set."""

    def test_embassy_url_included_when_set(self):
        reqs = _make_requirements(embassy_url="https://embassy.example")

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example"

    def test_embassy_url_omitted_when_none(self):
        doc = _build_document("DE", _make_requirements(embassy_url=None))

        assert "embassy_url" not in doc

    def test_embassy_url_omitted_when_empty_string(self):
        doc = _build_document("DE", _make_requirements(embassy_url=""))

        assert "embassy_url" not in doc

    def test_evisa_portal_url_included_when_set(self):
        reqs = _make_requirements(evisa_portal_url="https://evisa.example")

        doc = _build_document("DE", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example"

    def test_evisa_portal_url_omitted_when_none(self):
        doc = _build_document("DE", _make_requirements(evisa_portal_url=None))

        assert "evisa_portal_url" not in doc

    def test_estimated_processing_days_included_when_set(self):
        reqs = _make_requirements(estimated_processing_days=14)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 14

    def test_estimated_processing_days_zero_is_included(self):
        reqs = _make_requirements(estimated_processing_days=0)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_estimated_processing_days_omitted_when_none(self):
        doc = _build_document("DE", _make_requirements(estimated_processing_days=None))

        assert "estimated_processing_days" not in doc

    def test_entry_form_url_included_when_set(self):
        reqs = _make_requirements(entry_form_url="https://entry.example")

        doc = _build_document("DE", reqs)

        assert doc["entry_form_url"] == "https://entry.example"

    def test_entry_form_url_omitted_when_none(self):
        doc = _build_document("DE", _make_requirements(entry_form_url=None))

        assert "entry_form_url" not in doc

    def test_all_optionals_included_together(self):
        reqs = _make_requirements(
            country_code="CA",
            embassy_url="https://embassy.ca",
            evisa_portal_url="https://eta.ca",
            estimated_processing_days=14,
            entry_form_url="https://arrivecan.ca",
        )

        doc = _build_document("CA", reqs)

        assert doc["country_name"] == "Canada"
        assert doc["embassy_url"] == "https://embassy.ca"
        assert doc["evisa_portal_url"] == "https://eta.ca"
        assert doc["estimated_processing_days"] == 14
        assert doc["entry_form_url"] == "https://arrivecan.ca"

    def test_document_is_json_serializable(self):
        reqs = _make_requirements(
            embassy_url="https://embassy.example",
            evisa_portal_url="https://evisa.example",
            estimated_processing_days=7,
            entry_form_url="https://entry.example",
        )

        doc = _build_document("DE", reqs)

        assert json.loads(json.dumps(doc, indent=2)) == doc
