"""Tests for the Bedrock Knowledge Base sync Lambda handler.

Covers the S3 upload loop (keys, content type, body shape, document
count), the optional-field branches in _build_document, country-name
fallback, transit/layover and visa-exemption flags, per-country passport
validity windows, and re-syncing after requirements change.
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
# Fixtures and helpers
# ------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _mock_aws():
    """Activate moto mocks, create the KB bucket, and patch the module-level S3 client."""
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
    """Expose the moto S3 client created by _mock_aws."""
    return _mock_aws


@pytest.fixture
def use_requirements():
    """Swap the module-level COUNTRY_REQUIREMENTS for test data, restoring afterwards."""
    orig = sync_mod.COUNTRY_REQUIREMENTS

    def _apply(requirements: dict[str, TravelDocRequirements]) -> None:
        sync_mod.COUNTRY_REQUIREMENTS = requirements

    try:
        yield _apply
    finally:
        sync_mod.COUNTRY_REQUIREMENTS = orig


def _make_reqs(**overrides) -> TravelDocRequirements:
    """Build a TravelDocRequirements with all optional fields unset by default."""
    fields = {
        "country_code": "DE",
        "requires_visa": True,
        "transit_visa_required": False,
        "passport_validity_months": 3,
        "visa_exempt_nationalities": ["US", "GB", "CA"],
    }
    fields.update(overrides)
    return TravelDocRequirements(**fields)


def _list_keys(s3) -> list[str]:
    resp = s3.list_objects_v2(Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET)
    return sorted(obj["Key"] for obj in resp.get("Contents", []))


def _get_object(s3, country_code: str) -> dict:
    return s3.get_object(
        Bucket=sync_mod.KNOWLEDGE_BASE_BUCKET,
        Key=f"{sync_mod.S3_PREFIX}/{country_code}.json",
    )


def _get_document(s3, country_code: str) -> dict:
    return json.loads(_get_object(s3, country_code)["Body"].read())


# ------------------------------------------------------------------
# Handler: S3 upload behaviour
# ------------------------------------------------------------------

class TestHandlerSyncsDocuments:
    """Every country in the requirements map is uploaded as one JSON object under the prefix."""

    def test_response_reports_synced_count(self, use_requirements, base_requirements):
        use_requirements(base_requirements)

        response = handler({}, None)

        assert response == {"statusCode": 200, "body": {"documents_synced": 3}}

    def test_one_object_per_country_under_prefix(self, s3, use_requirements, base_requirements):
        use_requirements(base_requirements)

        handler({}, None)

        assert _list_keys(s3) == [
            "country-requirements/CN.json",
            "country-requirements/DE.json",
            "country-requirements/JP.json",
        ]

    def test_objects_have_json_content_type(self, s3, use_requirements, base_requirements):
        use_requirements(base_requirements)

        handler({}, None)

        assert _get_object(s3, "DE")["ContentType"] == "application/json"

    def test_body_is_indented_json_matching_built_document(
        self, s3, use_requirements, base_requirements
    ):
        use_requirements(base_requirements)

        handler({}, None)

        raw = _get_object(s3, "CN")["Body"].read().decode()
        expected = _build_document("CN", base_requirements["CN"])
        assert raw == json.dumps(expected, indent=2)

    def test_empty_requirements_syncs_nothing(self, s3, use_requirements):
        use_requirements({})

        response = handler({}, None)

        assert response["body"]["documents_synced"] == 0
        assert _list_keys(s3) == []

    def test_event_and_context_are_ignored(self, use_requirements, base_requirements):
        use_requirements(base_requirements)

        response = handler({"unexpected": "payload"}, object())

        assert response["body"]["documents_synced"] == 3

    def test_syncs_all_production_countries(self, s3):
        response = handler({}, None)

        assert response["body"]["documents_synced"] == len(COUNTRY_REQUIREMENTS)
        assert len(_list_keys(s3)) == len(COUNTRY_REQUIREMENTS)


class TestItineraryRequirementChanges:
    """Re-running the sync after requirements change overwrites the stored documents."""

    def test_resync_overwrites_changed_requirements(self, s3, use_requirements):
        use_requirements({"DE": _make_reqs(passport_validity_months=3)})
        handler({}, None)
        use_requirements({"DE": _make_reqs(passport_validity_months=6)})

        handler({}, None)

        assert _get_document(s3, "DE")["passport_validity_months"] == 6

    def test_resync_picks_up_newly_added_destination(self, s3, use_requirements, base_requirements):
        use_requirements({"DE": base_requirements["DE"]})
        handler({}, None)
        use_requirements(base_requirements)

        response = handler({}, None)

        assert response["body"]["documents_synced"] == 3
        assert "country-requirements/JP.json" in _list_keys(s3)

    def test_resync_reflects_new_visa_exemption(self, s3, use_requirements):
        use_requirements({"DE": _make_reqs(visa_exempt_nationalities=["US"])})
        handler({}, None)
        use_requirements({"DE": _make_reqs(visa_exempt_nationalities=["US", "IN"])})

        handler({}, None)

        assert _get_document(s3, "DE")["visa_exempt_nationalities"] == ["US", "IN"]


# ------------------------------------------------------------------
# _build_document: core fields
# ------------------------------------------------------------------

class TestDocumentCoreFields:
    """Core requirement fields are always present and copied verbatim."""

    def test_baseline_document_has_only_core_fields(self):
        reqs = _make_reqs()

        doc = _build_document("DE", reqs)

        assert doc == {
            "country_code": "DE",
            "country_name": "Germany",
            "visa_required": True,
            "transit_visa_required": False,
            "passport_validity_months": 3,
            "visa_exempt_nationalities": ["US", "GB", "CA"],
        }

    def test_visa_not_required(self):
        reqs = _make_reqs(requires_visa=False)

        doc = _build_document("DE", reqs)

        assert doc["visa_required"] is False

    def test_empty_exempt_nationalities_preserved(self):
        reqs = _make_reqs(visa_exempt_nationalities=[])

        doc = _build_document("DE", reqs)

        assert doc["visa_exempt_nationalities"] == []


class TestCountryNameLookup:
    """country_name comes from the lookup table, falling back to the code itself."""

    def test_known_country_uses_display_name(self):
        reqs = _make_reqs(country_code="AE")

        doc = _build_document("AE", reqs)

        assert doc["country_name"] == "United Arab Emirates"

    def test_unknown_country_falls_back_to_code(self):
        reqs = _make_reqs(country_code="XX")

        doc = _build_document("XX", reqs)

        assert doc["country_name"] == "XX"
        assert doc["country_code"] == "XX"

    def test_country_code_field_comes_from_requirements(self):
        reqs = _make_reqs(country_code="FR")

        doc = _build_document("DE", reqs)

        assert doc["country_code"] == "FR"
        assert doc["country_name"] == "Germany"


class TestTransitAndLayoverRequirements:
    """Transit-visa flag (used for layover segments) is propagated to the document."""

    def test_transit_visa_required_for_layover_country(self, base_requirements):
        doc = _build_document("CN", base_requirements["CN"])

        assert doc["transit_visa_required"] is True

    def test_transit_visa_not_required_for_layover_country(self, base_requirements):
        doc = _build_document("DE", base_requirements["DE"])

        assert doc["transit_visa_required"] is False


class TestPassportValidityAndVisaLimits:
    """Per-country passport validity windows and visa exemptions are synced as-is."""

    def test_schengen_three_month_validity(self, base_requirements):
        doc = _build_document("DE", base_requirements["DE"])

        assert doc["passport_validity_months"] == 3

    def test_six_month_validity_country(self, base_requirements):
        doc = _build_document("JP", base_requirements["JP"])

        assert doc["passport_validity_months"] == 6

    def test_no_exemptions_country(self, base_requirements):
        doc = _build_document("CN", base_requirements["CN"])

        assert doc["visa_exempt_nationalities"] == []

    def test_exempt_nationalities_listed(self, base_requirements):
        doc = _build_document("JP", base_requirements["JP"])

        assert doc["visa_exempt_nationalities"] == ["US", "GB"]

    def test_production_schengen_and_six_month_countries(self, s3):
        handler({}, None)

        assert _get_document(s3, "FR")["passport_validity_months"] == 3
        assert _get_document(s3, "CN")["passport_validity_months"] == 6


# ------------------------------------------------------------------
# _build_document: optional fields
# ------------------------------------------------------------------

class TestOptionalFields:
    """Optional URLs are included only when truthy; processing days only when not None."""

    def test_embassy_url_included_when_set(self):
        reqs = _make_reqs(embassy_url="https://embassy.example/de")

        doc = _build_document("DE", reqs)

        assert doc["embassy_url"] == "https://embassy.example/de"

    def test_embassy_url_omitted_when_empty(self):
        reqs = _make_reqs(embassy_url="")

        doc = _build_document("DE", reqs)

        assert "embassy_url" not in doc

    def test_evisa_portal_url_included_when_set(self):
        reqs = _make_reqs(evisa_portal_url="https://evisa.example/de")

        doc = _build_document("DE", reqs)

        assert doc["evisa_portal_url"] == "https://evisa.example/de"

    def test_evisa_portal_url_omitted_when_empty(self):
        reqs = _make_reqs(evisa_portal_url="")

        doc = _build_document("DE", reqs)

        assert "evisa_portal_url" not in doc

    def test_processing_days_included_when_set(self):
        reqs = _make_reqs(estimated_processing_days=10)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 10

    def test_processing_days_zero_still_included(self):
        reqs = _make_reqs(estimated_processing_days=0)

        doc = _build_document("DE", reqs)

        assert doc["estimated_processing_days"] == 0

    def test_entry_form_url_included_when_set(self):
        reqs = _make_reqs(entry_form_url="https://entry.example/de")

        doc = _build_document("DE", reqs)

        assert doc["entry_form_url"] == "https://entry.example/de"

    def test_entry_form_url_omitted_when_empty(self):
        reqs = _make_reqs(entry_form_url="")

        doc = _build_document("DE", reqs)

        assert "entry_form_url" not in doc

    def test_optional_fields_round_trip_through_s3(self, s3, use_requirements):
        use_requirements({
            "IN": _make_reqs(
                country_code="IN",
                evisa_portal_url="https://evisa.example/in",
                estimated_processing_days=4,
            ),
        })

        handler({}, None)

        doc = _get_document(s3, "IN")
        assert doc["evisa_portal_url"] == "https://evisa.example/in"
        assert doc["estimated_processing_days"] == 4
        assert "embassy_url" not in doc
        assert "entry_form_url" not in doc
