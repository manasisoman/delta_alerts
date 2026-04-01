"""Critical-path tests for the visa evaluator."""

from datetime import date

from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


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
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 7, 1),
            visa_number="V002",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expires exactly on departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 1),
            visa_number="V003",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExemptNationality:
    """Visa-exempt nationality should produce no alert."""

    def test_us_national_to_germany(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestLayoverSkip:
    """Layover at a country without transit visa requirement → skipped."""

    def test_layover_no_transit_visa_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False

    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestMultipleVisaRecords:
    """When multiple visas exist for a country, the latest-expiring one is used."""

    def test_prefers_valid_visa_over_expired(self, base_requirements):
        expired_visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2020, 1, 1), expiry_date=date(2022, 1, 1),
            visa_number="V-OLD",
        )
        valid_visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2026, 1, 1), expiry_date=date(2028, 1, 1),
            visa_number="V-NEW",
        )
        profile = make_profile(nationality="IN", visa_records=[expired_visa, valid_visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None


class TestNoVisaRequired:
    """Country that does not require a visa → segment skipped, no alert."""

    def test_no_visa_required_skips_evaluation(self):
        from src.models.types import TravelDocRequirements

        reqs = {
            "MX": TravelDocRequirements(
                country_code="MX",
                requires_visa=False,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            ),
        }
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="MX", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, reqs)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestVisaExpiresWithinWarningWindow:
    """Visa expires within VISA_EXPIRY_WARNING_DAYS of travel → INFO."""

    def test_visa_expires_within_30_days_of_travel(self, base_requirements):
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 20),
            visa_number="V004",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert "Visa for CN expires within 30 days of travel" in result.reasons

    def test_visa_expires_exactly_on_warning_boundary(self, base_requirements):
        """Visa expiry == departure + 30 days → still INFO (boundary inclusive)."""
        visa = VisaRecord(
            country_code="CN", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 10, 1),
            visa_number="V005",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO


class TestSeverityConsolidation:
    """Consolidated severity keeps the highest across multiple segments."""

    def test_warning_upgraded_to_critical(self):
        """First segment WARNING (expires on travel date), second segment CRITICAL (no visa).

        Exercises _max_severity upgrading from a non-None current to a higher
        severity (line 132-133: new > current → return new).
        """
        from src.models.types import TravelDocRequirements

        reqs = {
            "JP": TravelDocRequirements(
                country_code="JP",
                requires_visa=True,
                transit_visa_required=False,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            ),
            "CN": TravelDocRequirements(
                country_code="CN",
                requires_visa=True,
                transit_visa_required=True,
                passport_validity_months=6,
                visa_exempt_nationalities=[],
            ),
        }
        jp_visa = VisaRecord(
            country_code="JP", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 5),
            visa_number="V006",
        )
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        seg_jp = make_segment(destination="JP", departure=date(2026, 9, 5), flight_number="DL200")
        seg_cn = make_segment(destination="CN", departure=date(2026, 9, 10), flight_number="DL201")
        itinerary = make_itinerary(seg_jp, seg_cn)

        result = evaluate_visa_requirements(profile, itinerary, reqs)

        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for JP expires on date of travel" in result.reasons
        assert "No visa on file for CN" in result.reasons

    def test_critical_not_downgraded_by_info(self):
        """First segment CRITICAL (no visa), second segment INFO (expiring soon).

        Exercises _max_severity keeping current when new < current
        (line 134: return current).
        """
        from src.models.types import TravelDocRequirements

        reqs = {
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
                visa_exempt_nationalities=[],
            ),
        }
        jp_visa = VisaRecord(
            country_code="JP", visa_type="TOURIST",
            issue_date=date(2025, 1, 1), expiry_date=date(2026, 9, 20),
            visa_number="V007",
        )
        profile = make_profile(nationality="IN", visa_records=[jp_visa])
        seg_cn = make_segment(destination="CN", departure=date(2026, 9, 1), flight_number="DL200")
        seg_jp = make_segment(destination="JP", departure=date(2026, 9, 5), flight_number="DL201")
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, reqs)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
