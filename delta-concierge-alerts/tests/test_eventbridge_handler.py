"""Tests for the EventBridge ``ItineraryChanged`` handler.

The handler resolves stale alerts for a changed itinerary and then
re-evaluates it. Its two collaborators — ``resolve_alerts_for_itinerary``
(DynamoDB) and ``evaluate_itinerary`` — are monkeypatched so these tests
stay focused on the handler's own extraction, delegation, and response
shaping logic.
"""

import pytest

import src.handlers.eventbridge_handler as eb_mod
from src.handlers.eventbridge_handler import handler


@pytest.fixture
def calls():
    """Record collaborator invocations so tests can assert on them."""
    return {"resolve": [], "evaluate": []}


@pytest.fixture(autouse=True)
def _patch_collaborators(monkeypatch, calls):
    """Replace the module-level collaborators with recording stubs.

    ``resolve_alerts_for_itinerary`` returns a fixed count and
    ``evaluate_itinerary`` returns a sentinel result; both capture the
    arguments they receive for later assertions.
    """
    def fake_resolve(*, skymiles_number, confirmation_number, resolution):
        calls["resolve"].append(
            {
                "skymiles_number": skymiles_number,
                "confirmation_number": confirmation_number,
                "resolution": resolution,
            }
        )
        return 3

    def fake_evaluate(detail):
        calls["evaluate"].append(detail)
        return {"passport_status": "OK", "visa_status": "OK", "alerts_sent": 0}

    monkeypatch.setattr(eb_mod, "resolve_alerts_for_itinerary", fake_resolve)
    monkeypatch.setattr(eb_mod, "evaluate_itinerary", fake_evaluate)


def _base_event(**detail_overrides) -> dict:
    """Build a valid EventBridge event with optional ``detail`` overrides."""
    detail = {
        "profile": {
            "skymiles_number": "1234567890",
            "first_name": "Test",
            "last_name": "User",
        },
        "itinerary": {
            "confirmation_number": "DL-TEST",
            "segments": [],
        },
        "change_type": "DATE_CHANGE",
    }
    detail.update(detail_overrides)
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


class TestHappyPath:
    """Full round-trip: resolve stale alerts, re-evaluate, return 200."""

    def test_processes_event_and_returns_200(self, calls):
        event = _base_event()

        response = handler(event, None)

        assert response["statusCode"] == 200
        body = response["body"]
        assert body["change_type"] == "DATE_CHANGE"
        assert body["resolved_alerts"] == 3
        assert body["evaluation_result"] == {
            "passport_status": "OK",
            "visa_status": "OK",
            "alerts_sent": 0,
        }

    def test_resolve_called_with_extracted_identifiers(self, calls):
        event = _base_event()

        handler(event, None)

        assert len(calls["resolve"]) == 1
        resolve_call = calls["resolve"][0]
        assert resolve_call["skymiles_number"] == "1234567890"
        assert resolve_call["confirmation_number"] == "DL-TEST"
        assert "itinerary changed" in resolve_call["resolution"]

    def test_evaluate_called_with_full_detail(self, calls):
        event = _base_event()

        handler(event, None)

        assert len(calls["evaluate"]) == 1
        assert calls["evaluate"][0] is event["detail"]


class TestChangeTypeDefault:
    """``change_type`` defaults to ``UNKNOWN`` when absent from detail."""

    def test_missing_change_type_defaults_to_unknown(self, calls):
        event = _base_event()
        del event["detail"]["change_type"]

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"


class TestChangeTypeVariants:
    """Every supported change_type is echoed back unchanged."""

    @pytest.mark.parametrize(
        "change_type",
        ["DATE_CHANGE", "SEGMENT_ADDED", "SEGMENT_REMOVED", "ROUTE_CHANGE"],
    )
    def test_change_type_echoed_in_response(self, calls, change_type):
        event = _base_event(change_type=change_type)

        response = handler(event, None)

        assert response["body"]["change_type"] == change_type


class TestResolvedCount:
    """The resolved-alert count from the store flows into the response."""

    def test_zero_resolved_alerts(self, monkeypatch, calls):
        monkeypatch.setattr(
            eb_mod,
            "resolve_alerts_for_itinerary",
            lambda **kwargs: 0,
        )
        event = _base_event()

        response = handler(event, None)

        assert response["body"]["resolved_alerts"] == 0


class TestMissingRequiredFields:
    """Absent required identifiers surface as KeyErrors."""

    def test_missing_detail_raises_key_error(self, calls):
        with pytest.raises(KeyError):
            handler({}, None)

    def test_missing_profile_raises_key_error(self, calls):
        event = _base_event()
        del event["detail"]["profile"]

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_itinerary_raises_key_error(self, calls):
        event = _base_event()
        del event["detail"]["itinerary"]

        with pytest.raises(KeyError):
            handler(event, None)
