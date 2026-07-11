"""Tests for the visa evaluator."""

from datetime import date, timedelta

from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


def make_visa(
    country_code: str = "CN",
    expiry_date: date = date(2101, 1, 1),
    visa_number: str = "V-TEST",
) -> VisaRecord:
    """Build a visa record with valid defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type="TOURIST",
        issue_date=date(2099, 1, 1),
        expiry_date=expiry_date,
        visa_number=visa_number,
    )


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V001",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2099, 1, 1), expiry_date=date(2100, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2100, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestLayoverWithTransitVisaRequired:
    """Layovers are evaluated when the destination requires a transit visa."""

    def test_layover_with_transit_visa_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="CN",
            departure=date(2100, 9, 1),
            is_layover=True,
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestVisaNotRequired:
    """Destinations without a visa requirement are skipped."""

    def test_no_alert_when_destination_does_not_require_visa(self):
        requirements = {
            "CL": TravelDocRequirements(
                country_code="CL",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            ),
        }
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CL", departure=date(2100, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverWithoutTransitVisaRequired:
    """Layovers are skipped when the destination requires no transit visa."""

    def test_no_alert_for_layover_without_transit_requirement(
        self,
        base_requirements,
    ):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(
            destination="DE",
            departure=date(2100, 9, 1),
            is_layover=True,
        )
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaExemptNationality:
    """Visa-exempt travelers do not need a matching visa record."""

    def test_no_alert_for_exempt_nationality(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2100, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestMatchingVisaSelection:
    """The latest-expiring matching visa determines the result."""

    def test_latest_expiry_wins_across_multiple_matching_visas(
        self,
        base_requirements,
    ):
        expired_visa = make_visa(
            expiry_date=date(2020, 1, 1),
            visa_number="V-EXPIRED",
        )
        valid_visa = make_visa(
            expiry_date=date(2101, 1, 1),
            visa_number="V-VALID",
        )
        profile = make_profile(
            nationality="IN",
            visa_records=[valid_visa, expired_visa],
        )
        segment = make_segment(destination="CN", departure=date(2100, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []

    def test_visas_for_other_destinations_are_ignored(self, base_requirements):
        profile = make_profile(
            nationality="IN",
            visa_records=[make_visa(country_code="JP")],
        )
        segment = make_segment(destination="CN", departure=date(2100, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for CN"]


class TestTravelDateBoundaries:
    """Travel-date comparisons map to the documented severities."""

    def test_visa_expiring_on_departure_is_warning(self, base_requirements):
        departure = date(2100, 9, 1)
        profile = make_profile(
            nationality="IN",
            visa_records=[make_visa(expiry_date=departure)],
        )
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert result.reasons == ["Visa for CN expires on date of travel"]


class TestExpiryWarningBoundaries:
    """The configured 30-day post-travel window produces INFO alerts."""

    def test_expiry_exactly_30_days_after_departure_is_info(
        self,
        base_requirements,
    ):
        departure = date(2100, 9, 1)
        profile = make_profile(
            nationality="IN",
            visa_records=[
                make_visa(expiry_date=departure + timedelta(days=30)),
            ],
        )
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert result.reasons == [
            "Visa for CN expires within 30 days of travel",
        ]

    def test_expiry_31_days_after_departure_has_no_alert(
        self,
        base_requirements,
    ):
        departure = date(2100, 9, 1)
        profile = make_profile(
            nationality="IN",
            visa_records=[
                make_visa(expiry_date=departure + timedelta(days=31)),
            ],
        )
        segment = make_segment(destination="CN", departure=departure)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestSeverityConsolidation:
    """The highest severity across all segments is retained."""

    def test_later_critical_alert_replaces_info(self, base_requirements):
        departure = date(2100, 9, 1)
        cn_visa = make_visa(expiry_date=departure + timedelta(days=30))
        profile = make_profile(nationality="IN", visa_records=[cn_visa])
        info_segment = make_segment(
            destination="CN",
            departure=departure,
            flight_number="DL100",
        )
        critical_segment = make_segment(
            destination="JP",
            departure=departure,
            flight_number="DL200",
        )
        itinerary = make_itinerary(info_segment, critical_segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_later_info_alert_does_not_replace_critical(
        self,
        base_requirements,
    ):
        departure = date(2100, 9, 1)
        cn_visa = make_visa(expiry_date=departure + timedelta(days=30))
        profile = make_profile(nationality="IN", visa_records=[cn_visa])
        critical_segment = make_segment(
            destination="JP",
            departure=departure,
            flight_number="DL100",
        )
        info_segment = make_segment(
            destination="CN",
            departure=departure,
            flight_number="DL200",
        )
        itinerary = make_itinerary(critical_segment, info_segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2


class TestDefaultRequirementsFallback:
    """Unknown destinations use the default visa requirements."""

    def test_missing_destination_requirement_uses_default(
        self,
        base_requirements,
    ):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="XX", departure=date(2100, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["No visa on file for XX"]
