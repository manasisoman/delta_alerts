"""Shared fixtures for Delta Concierge Alert tests."""

import os

# Set AWS region before any boto3 imports (needed by module-level clients)
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID", "testing")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "testing")
os.environ.setdefault("AWS_SECURITY_TOKEN", "testing")
os.environ.setdefault("AWS_SESSION_TOKEN", "testing")

from datetime import date

import pytest

from src.models.types import (
    FlightSegment,
    Itinerary,
    SkyMilesProfile,
    TravelDocRequirements,
    VisaRecord,
)


@pytest.fixture
def base_requirements() -> dict[str, TravelDocRequirements]:
    """Minimal requirements dict for testing."""
    return {
        "DE": TravelDocRequirements(
            country_code="DE",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=3,
            visa_exempt_nationalities=["US", "GB", "CA"],
        ),
        "CN": TravelDocRequirements(
            country_code="CN",
            requires_visa=True,
            transit_visa_required=True,
            passport_validity_months=6,
            visa_exempt_nationalities=[],
        ),
        "JP": TravelDocRequirements(
            country_code="JP",
            requires_visa=True,
            transit_visa_required=False,
            passport_validity_months=6,
            visa_exempt_nationalities=["US", "GB"],
        ),
    }


def make_profile(
    nationality: str = "IN",
    passport_number: str | None = "P123456",
    passport_expiry: date | None = date(2030, 1, 1),
    visa_records: list[VisaRecord] | None = None,
) -> SkyMilesProfile:
    """Helper to build a SkyMilesProfile with sensible defaults."""
    return SkyMilesProfile(
        skymiles_number="9999999",
        first_name="Test",
        last_name="User",
        nationality=nationality,
        passport_number=passport_number,
        passport_expiry=passport_expiry,
        visa_records=visa_records or [],
        endpoint_arn="arn:aws:sns:us-east-1:000:endpoint/test",
    )


def make_itinerary(
    *segments: FlightSegment,
    confirmation: str = "DL-TEST",
) -> Itinerary:
    """Helper to build an Itinerary from segments."""
    return Itinerary(
        confirmation_number=confirmation,
        segments=list(segments),
    )


def make_segment(
    destination: str = "DE",
    departure: date = date(2026, 9, 1),
    arrival: date = date(2026, 9, 2),
    is_layover: bool = False,
    flight_number: str = "DL100",
    origin: str = "ATL",
) -> FlightSegment:
    """Helper to build a FlightSegment with sensible defaults."""
    return FlightSegment(
        flight_number=flight_number,
        origin=origin,
        destination=destination,
        departure_date=departure,
        arrival_date=arrival,
        is_layover=is_layover,
    )
