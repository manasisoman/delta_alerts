"""Data models for the Delta Concierge Alert system."""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class AlertSeverity(Enum):
    """Severity levels for travel document alerts."""

    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    INFO = "INFO"


class AlertType(Enum):
    """Types of travel document alerts."""

    PASSPORT = "PASSPORT"
    VISA = "VISA"


@dataclass
class ValidationError:
    """Represents a validation error for a travel document field."""

    field: str
    message: str
    code: str


@dataclass
class VisaRecord:
    """Represents a visa record on file for a traveler."""

    country_code: str
    visa_type: str
    issue_date: date
    expiry_date: date
    visa_number: str


@dataclass
class SkyMilesProfile:
    """Delta SkyMiles member profile with travel document information."""

    skymiles_number: str
    first_name: str
    last_name: str
    nationality: str
    passport_number: Optional[str]
    passport_expiry: Optional[date]
    visa_records: list[VisaRecord]
    endpoint_arn: str


@dataclass
class FlightSegment:
    """A single flight segment within an itinerary."""

    flight_number: str
    origin: str
    destination: str
    departure_date: date
    arrival_date: date
    is_layover: bool


@dataclass
class Itinerary:
    """A travel itinerary consisting of one or more flight segments."""

    confirmation_number: str
    segments: list[FlightSegment]


@dataclass
class TravelDocRequirements:
    """Travel document requirements for a specific country."""

    country_code: str
    requires_visa: bool
    transit_visa_required: bool
    passport_validity_months: int
    visa_exempt_nationalities: list[str]


@dataclass
class PassportEvaluation:
    """Result of evaluating passport validity against an itinerary."""

    profile: SkyMilesProfile
    segments_evaluated: list[FlightSegment]
    is_alert_required: bool
    severity: Optional[AlertSeverity]
    reasons: list[str]
    validation_errors: list[ValidationError]


@dataclass
class VisaEvaluation:
    """Result of evaluating visa requirements against an itinerary."""

    profile: SkyMilesProfile
    segments_evaluated: list[FlightSegment]
    is_alert_required: bool
    severity: Optional[AlertSeverity]
    reasons: list[str]
    validation_errors: list[ValidationError]


@dataclass
class AlertRecord:
    """A persisted alert record for a SkyMiles member."""

    alert_id: str
    skymiles_number: str
    alert_type: AlertType
    severity: AlertSeverity
    reasons: list[str]
    created_at: str
    itinerary_ref: str


@dataclass
class NotificationPayload:
    """Payload for sending a push notification via SNS."""

    endpoint_arn: str
    alert_record: AlertRecord
    title: str
    body: str
    push_data: dict
