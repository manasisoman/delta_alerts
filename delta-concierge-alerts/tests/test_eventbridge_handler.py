"""Unit tests for the EventBridge itinerary-change handler.

The handler is a thin orchestrator: it parses an EventBridge event envelope,
resolves stale alerts for the affected itinerary, delegates re-evaluation to
``evaluate_itinerary``, and returns a response summarizing both steps.

These tests monkeypatch the two module-level collaborators
(``resolve_alerts_for_itinerary`` and ``evaluate_itinerary``) so each test can
assert on the arguments the handler forwards and the response it builds,
without touching DynamoDB or the full evaluation pipeline.
"""

import pytest

import src.handlers.eventbridge_handler as eb
from src.handlers.eventbridge_handler import handler


def _base_event(**detail_overrides) -> dict:
    """Build a valid EventBridge event envelope with optional detail overrides."""
    detail = {
        "profile": {"skymiles_number": "1234567890"},
        "itinerary": {"confirmation_number": "DL-TEST"},
        "change_type": "DATE_CHANGE",
    }
    for key, val in detail_overrides.items():
        if val is _OMIT:
            detail.pop(key, None)
        else:
            detail[key] = val
    return {
        "source": "delta.booking-system",
        "detail-type": "ItineraryChanged",
        "detail": detail,
    }


_OMIT = object()


@pytest.fixture
def patched(monkeypatch):
    """Patch collaborators with recording fakes; expose their captured calls."""
    calls = {"resolve": [], "evaluate": []}

    def fake_resolve(**kwargs):
        calls["resolve"].append(kwargs)
        return 3

    def fake_evaluate(detail):
        calls["evaluate"].append(detail)
        return {"statusCode": 200, "body": {"alerts_sent": 1}}

    monkeypatch.setattr(eb, "resolve_alerts_for_itinerary", fake_resolve)
    monkeypatch.setattr(eb, "evaluate_itinerary", fake_evaluate)
    return calls


class TestEventParsing:
    """The handler reads skymiles and confirmation from ``event['detail']``."""

    def test_extracts_identifiers_from_detail(self, patched):
        event = _base_event(
            profile={"skymiles_number": "5550001"},
            itinerary={"confirmation_number": "DL-XYZ789"},
        )

        handler(event, None)

        resolve_call = patched["resolve"][0]
        assert resolve_call["skymiles_number"] == "5550001"
        assert resolve_call["confirmation_number"] == "DL-XYZ789"


class TestChangeType:
    """``change_type`` defaults to ``UNKNOWN`` and is echoed in the response."""

    def test_defaults_to_unknown_when_absent(self, patched):
        event = _base_event(change_type=_OMIT)

        response = handler(event, None)

        assert response["body"]["change_type"] == "UNKNOWN"

    def test_explicit_change_type_is_preserved(self, patched):
        event = _base_event(change_type="ROUTE_CHANGE")

        response = handler(event, None)

        assert response["body"]["change_type"] == "ROUTE_CHANGE"


class TestStaleAlertResolution:
    """Stale alerts are resolved before re-evaluation with the right arguments."""

    def test_resolve_called_with_expected_arguments(self, patched):
        event = _base_event()

        handler(event, None)

        assert len(patched["resolve"]) == 1
        call = patched["resolve"][0]
        assert call["skymiles_number"] == "1234567890"
        assert call["confirmation_number"] == "DL-TEST"
        assert call["resolution"] == (
            "Auto-resolved: itinerary changed, re-evaluating"
        )


class TestReEvaluationDelegation:
    """Re-evaluation is delegated to ``evaluate_itinerary`` with the detail."""

    def test_evaluate_called_with_detail(self, patched):
        event = _base_event()

        handler(event, None)

        assert len(patched["evaluate"]) == 1
        assert patched["evaluate"][0] is event["detail"]


class TestResponse:
    """The response combines the resolved count and the evaluation result."""

    def test_response_shape(self, patched):
        event = _base_event(change_type="SEGMENT_ADDED")

        response = handler(event, None)

        assert response["statusCode"] == 200
        assert response["body"] == {
            "change_type": "SEGMENT_ADDED",
            "resolved_alerts": 3,
            "evaluation_result": {"statusCode": 200, "body": {"alerts_sent": 1}},
        }


class TestExceptionBehavior:
    """Missing required envelope fields surface as ``KeyError``."""

    def test_missing_detail_raises(self, patched):
        with pytest.raises(KeyError):
            handler({"source": "delta.booking-system"}, None)

    def test_missing_profile_raises(self, patched):
        event = _base_event(profile=_OMIT)

        with pytest.raises(KeyError):
            handler(event, None)

    def test_missing_itinerary_raises(self, patched):
        event = _base_event(itinerary=_OMIT)

        with pytest.raises(KeyError):
            handler(event, None)
