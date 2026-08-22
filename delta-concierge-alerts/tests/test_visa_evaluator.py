"""Critical-path tests for the visa evaluator."""

from datetime import date, timedelta

from src.config import VISA_EXPIRY_WARNING_DAYS
from src.evaluators.visa_evaluator import evaluate_visa_requirements
from src.models.types import AlertSeverity, TravelDocRequirements, VisaRecord

from tests.conftest import make_itinerary, make_profile, make_segment


def make_visa(
    country_code: str = "CN",
    expiry: date = date(2030, 1, 1),
    issue: date = date(2020, 1, 1),
    visa_type: str = "TOURIST",
    visa_number: str = "V001",
) -> VisaRecord:
    """Helper to build a VisaRecord with sensible defaults."""
    return VisaRecord(
        country_code=country_code,
        visa_type=visa_type,
        issue_date=issue,
        expiry_date=expiry,
        visa_number=visa_number,
    )


def make_requirements(
    country_code: str = "CN",
    requires_visa: bool = True,
    transit_visa_required: bool = True,
    passport_validity_months: int = 6,
    visa_exempt_nationalities: list[str] | None = None,
) -> dict[str, TravelDocRequirements]:
    """Helper to build a single-country requirements mapping."""
    return {
        country_code: TravelDocRequirements(
            country_code=country_code,
            requires_visa=requires_visa,
            transit_visa_required=transit_visa_required,
            passport_validity_months=passport_validity_months,
            visa_exempt_nationalities=visa_exempt_nationalities or [],
        )
    }


class TestNoVisaRequired:
    """Destinations that do not require a visa are skipped entirely."""

    def test_requires_visa_false_is_skipped(self):
        requirements = make_requirements("MX", requires_visa=False)
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="MX", departure=date(2030, 9, 1)))

        result = evaluate_visa_requirements(profile, itinerary, requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverHandling:
    """Layovers only matter when the country requires a transit visa."""

    def test_layover_without_transit_visa_requirement_is_skipped(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []

    def test_non_layover_still_evaluated_when_transit_visa_not_required(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="DE", departure=date(2030, 9, 1), is_layover=False)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for DE" in result.reasons


class TestVisaExemptNationality:
    """Travelers of a visa-exempt nationality need no visa."""

    def test_exempt_nationality_is_skipped(self, base_requirements):
        profile = make_profile(nationality="US", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="DE", departure=date(2030, 9, 1)))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestMissingVisa:
    """No visa on file for a country that requires one → CRITICAL."""

    def test_no_visa_for_required_country(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons

    def test_visa_for_different_country_does_not_match(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[make_visa(country_code="JP")])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for CN" in result.reasons


class TestExpiredVisa:
    """Visa on file but already expired → CRITICAL."""

    def test_visa_expired_before_today(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=date(2020, 1, 1), visa_number="V001")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN has expired" in result.reasons


class TestVisaExpiresBeforeTravel:
    """Visa expires before departure (but after today) → CRITICAL."""

    def test_visa_expires_before_departure(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=date(2030, 7, 1), visa_number="V002")
        profile = make_profile(nationality="IN", visa_records=[visa])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "Visa for CN expires before date of travel" in result.reasons


class TestVisaExpiresOnTravelDate:
    """Visa expiring exactly on the departure date → WARNING."""

    def test_visa_expires_on_departure(self, base_requirements):
        departure = date(2030, 9, 1)
        visa = make_visa(country_code="CN", expiry=departure, visa_number="V003")
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=departure))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.WARNING
        assert "Visa for CN expires on date of travel" in result.reasons


class TestVisaExpiresWithinWarningWindow:
    """Visa expiring shortly after travel → INFO advisory."""

    def test_visa_expires_within_warning_window(self, base_requirements):
        departure = date(2030, 9, 1)
        visa = make_visa(
            country_code="CN",
            expiry=departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1),
            visa_number="V004",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=departure))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.INFO
        assert (
            f"Visa for CN expires within {VISA_EXPIRY_WARNING_DAYS} days of travel"
            in result.reasons
        )

    def test_visa_expiring_on_warning_boundary_is_info(self, base_requirements):
        departure = date(2030, 9, 1)
        visa = make_visa(
            country_code="CN",
            expiry=departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS),
            visa_number="V005",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=departure))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.INFO

    def test_visa_expiring_past_warning_window_is_clean(self, base_requirements):
        departure = date(2030, 9, 1)
        visa = make_visa(
            country_code="CN",
            expiry=departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS + 1),
            visa_number="V006",
        )
        profile = make_profile(nationality="IN", visa_records=[visa])
        itinerary = make_itinerary(make_segment(destination="CN", departure=departure))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []


class TestLayoverWithTransitVisaRequired:
    def test_layover_with_transit_visa_required(self, base_requirements):
        """China requires transit visa → layover without visa should alert."""
        profile = make_profile(nationality="IN", visa_records=[])
        segment = make_segment(destination="CN", departure=date(2030, 9, 1), is_layover=True)
        itinerary = make_itinerary(segment)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL


class TestUnknownDestinationFallsBackToDefaults:
    """Destinations absent from the requirements mapping use static defaults."""

    def test_unmapped_destination_uses_country_requirements(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        itinerary = make_itinerary(make_segment(destination="US", departure=date(2030, 9, 1)))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert "No visa on file for US" in result.reasons


class TestVisaRecordSelection:
    """When several records match a country, the latest expiry wins."""

    def test_latest_expiry_record_is_selected(self, base_requirements):
        departure = date(2030, 9, 1)
        expired = make_visa(country_code="CN", expiry=date(2020, 1, 1), visa_number="V-OLD")
        valid = make_visa(country_code="CN", expiry=date(2035, 1, 1), visa_number="V-NEW")
        profile = make_profile(nationality="IN", visa_records=[expired, valid])
        itinerary = make_itinerary(make_segment(destination="CN", departure=departure))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.reasons == []

    def test_latest_expiry_still_insufficient_reports_once(self, base_requirements):
        departure = date(2030, 9, 1)
        older = make_visa(country_code="CN", expiry=date(2029, 1, 1), visa_number="V-OLD")
        newer = make_visa(country_code="CN", expiry=date(2030, 8, 1), visa_number="V-NEW")
        profile = make_profile(nationality="IN", visa_records=[older, newer])
        itinerary = make_itinerary(make_segment(destination="CN", departure=departure))

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert result.reasons == ["Visa for CN expires before date of travel"]


class TestSeverityConsolidation:
    """Consolidated severity is the highest across all segments."""

    def test_critical_wins_over_info(self, base_requirements):
        departure = date(2030, 9, 1)
        cn_visa = make_visa(
            country_code="CN",
            expiry=departure + timedelta(days=VISA_EXPIRY_WARNING_DAYS - 1),
            visa_number="V-CN",
        )
        profile = make_profile(nationality="IN", visa_records=[cn_visa])
        seg_cn = make_segment(destination="CN", departure=departure, flight_number="DL100")
        seg_jp = make_segment(
            destination="JP", departure=date(2030, 10, 1), flight_number="DL200"
        )
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2

    def test_warning_wins_over_info(self, base_requirements):
        cn_visa = make_visa(country_code="CN", expiry=date(2030, 9, 1), visa_number="V-CN")
        jp_visa = make_visa(
            country_code="JP",
            expiry=date(2030, 10, 15),
            visa_number="V-JP",
        )
        profile = make_profile(nationality="IN", visa_records=[cn_visa, jp_visa])
        seg_cn = make_segment(destination="CN", departure=date(2030, 9, 1), flight_number="DL100")
        seg_jp = make_segment(destination="JP", departure=date(2030, 10, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.WARNING
        assert len(result.reasons) == 2

    def test_later_lower_severity_does_not_downgrade(self, base_requirements):
        cn_visa = make_visa(country_code="CN", expiry=date(2020, 1, 1), visa_number="V-CN")
        jp_visa = make_visa(country_code="JP", expiry=date(2030, 10, 1), visa_number="V-JP")
        profile = make_profile(nationality="IN", visa_records=[cn_visa, jp_visa])
        seg_cn = make_segment(destination="CN", departure=date(2030, 9, 1), flight_number="DL100")
        seg_jp = make_segment(destination="JP", departure=date(2030, 10, 1), flight_number="DL200")
        itinerary = make_itinerary(seg_cn, seg_jp)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.severity == AlertSeverity.CRITICAL

    def test_itinerary_change_re_evaluation_clears_alert(self, base_requirements):
        visa = make_visa(country_code="CN", expiry=date(2030, 8, 1), visa_number="V-CN")
        profile = make_profile(nationality="IN", visa_records=[visa])
        original = make_itinerary(make_segment(destination="CN", departure=date(2030, 9, 1)))
        rebooked = make_itinerary(make_segment(destination="CN", departure=date(2030, 6, 1)))

        before = evaluate_visa_requirements(profile, original, base_requirements)
        after = evaluate_visa_requirements(profile, rebooked, base_requirements)

        assert before.severity == AlertSeverity.CRITICAL
        assert after.is_alert_required is False
        assert after.severity is None


class TestEvaluationMetadata:
    """The evaluation echoes back the profile and segments it examined."""

    def test_metadata_is_populated(self, base_requirements):
        profile = make_profile(nationality="IN", visa_records=[])
        seg_cn = make_segment(destination="CN", departure=date(2030, 9, 1), flight_number="DL100")
        seg_de = make_segment(destination="DE", departure=date(2030, 9, 5), flight_number="DL200")
        itinerary = make_itinerary(seg_cn, seg_de)

        result = evaluate_visa_requirements(profile, itinerary, base_requirements)

        assert result.profile is profile
        assert result.segments_evaluated == [seg_cn, seg_de]
        assert result.validation_errors == []
