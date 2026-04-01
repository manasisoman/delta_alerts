"""Integration tests for critical passport expiry alerts.

Verifies that expiring passports are flagged as CRITICAL across multiple
countries with different validity requirements, including layover scenarios
where the transit country has a different expiration policy than the final
destination.
"""

from datetime import date

from src.evaluators.passport_evaluator import evaluate_passport_expiry
from src.models.types import AlertSeverity

from tests.conftest import make_itinerary, make_profile, make_segment


class TestCriticalSeverityAcrossCountries:
    """Passport below a country's validity window must be CRITICAL regardless of country."""

    def test_schengen_3_month_requirement_germany(self, base_requirements):
        """DE requires 3 months validity -- passport with only 2 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2026, 10, 20))
        segment = make_segment(destination="DE", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet DE's 3-month" in r for r in result.reasons)

    def test_6_month_requirement_china(self, base_requirements):
        """CN requires 6 months validity -- passport with only 4 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2027, 1, 1))
        segment = make_segment(destination="CN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)

    def test_6_month_requirement_japan(self, base_requirements):
        """JP requires 6 months validity -- passport with only 5 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2027, 1, 25))
        segment = make_segment(destination="JP", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet JP's 6-month" in r for r in result.reasons)

    def test_6_month_requirement_india(self, base_requirements):
        """IN requires 6 months validity -- passport with only 3 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2026, 12, 1))
        segment = make_segment(destination="IN", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet IN's 6-month" in r for r in result.reasons)

    def test_6_month_requirement_thailand(self, base_requirements):
        """TH requires 6 months validity -- passport with only 5 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2027, 1, 15))
        segment = make_segment(destination="TH", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet TH's 6-month" in r for r in result.reasons)

    def test_3_month_requirement_colombia(self, base_requirements):
        """CO requires 3 months validity -- passport with only 2 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2026, 10, 20))
        segment = make_segment(destination="CO", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CO's 3-month" in r for r in result.reasons)

    def test_3_month_requirement_new_zealand(self, base_requirements):
        """NZ requires 3 months validity -- passport with only 2 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2026, 10, 20))
        segment = make_segment(destination="NZ", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet NZ's 3-month" in r for r in result.reasons)

    def test_3_month_requirement_panama(self, base_requirements):
        """PA requires 3 months validity -- passport with only 2 months is CRITICAL."""
        profile = make_profile(passport_expiry=date(2026, 10, 20))
        segment = make_segment(destination="PA", departure=date(2026, 9, 1))
        itinerary = make_itinerary(segment)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet PA's 3-month" in r for r in result.reasons)


class TestCriticalSeverityMultiSegmentItinerary:
    """Multiple destinations with mixed requirements -- each failing segment is CRITICAL."""

    def test_multi_country_all_failing(self, base_requirements):
        """Passport under both 3-month and 6-month thresholds across segments."""
        # Passport expires Oct 20, 2026 -- only ~2 months from Sep departure
        # DE needs 3 months (fails), CN needs 6 months (fails)
        profile = make_profile(passport_expiry=date(2026, 10, 20))
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_cn = make_segment(destination="CN", departure=date(2026, 9, 10), flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_cn)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
        assert any("DE" in r for r in result.reasons)
        assert any("CN" in r for r in result.reasons)

    def test_mixed_pass_and_fail(self, base_requirements):
        """Passport meets 3-month requirement (DE) but fails 6-month requirement (JP)."""
        # Passport expires Mar 15, 2027 -- ~6.5 months from Sep 1 departure
        # DE needs 3 months (passes), JP needs 6 months (fails -- only ~5 months from Oct 15)
        profile = make_profile(passport_expiry=date(2027, 3, 15))
        seg_de = make_segment(destination="DE", departure=date(2026, 9, 1), flight_number="DL100")
        seg_jp = make_segment(destination="JP", departure=date(2026, 10, 15), flight_number="DL200")
        itinerary = make_itinerary(seg_de, seg_jp)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 1
        assert any("JP" in r for r in result.reasons)


class TestLayoverDifferentExpirationPolicy:
    """Layover country has a different expiration policy than the final destination."""

    def test_layover_3_month_destination_6_month_both_fail(self, base_requirements):
        """Layover in DE (3-month), final destination CN (6-month) -- both fail."""
        # Passport expires Oct 20, 2026 -- ~2 months from Sep departure
        profile = make_profile(passport_expiry=date(2026, 10, 20))
        seg_layover = make_segment(
            destination="DE",
            departure=date(2026, 9, 1),
            arrival=date(2026, 9, 1),
            is_layover=True,
            flight_number="DL100",
            origin="ATL",
        )
        seg_final = make_segment(
            destination="CN",
            departure=date(2026, 9, 1),
            arrival=date(2026, 9, 2),
            is_layover=False,
            flight_number="DL200",
            origin="DE",
        )
        itinerary = make_itinerary(seg_layover, seg_final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert len(result.reasons) == 2
        assert any("DE" in r for r in result.reasons)
        assert any("CN" in r for r in result.reasons)

    def test_layover_6_month_destination_3_month_layover_fails(self, base_requirements):
        """Layover in JP (6-month), final destination CO (3-month) -- layover fails."""
        # Passport expires Jan 15, 2027 -- ~4.5 months from Sep departure
        # JP needs 6 months (fails -> CRITICAL), CO needs 3 months (passes but
        # passport is within 6 months of travel -> INFO)
        profile = make_profile(passport_expiry=date(2027, 1, 15))
        seg_layover = make_segment(
            destination="JP",
            departure=date(2026, 9, 1),
            arrival=date(2026, 9, 1),
            is_layover=True,
            flight_number="DL300",
            origin="ATL",
        )
        seg_final = make_segment(
            destination="CO",
            departure=date(2026, 9, 2),
            arrival=date(2026, 9, 3),
            is_layover=False,
            flight_number="DL400",
            origin="JP",
        )
        itinerary = make_itinerary(seg_layover, seg_final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet JP's 6-month" in r for r in result.reasons)

    def test_layover_3_month_destination_6_month_only_destination_fails(self, base_requirements):
        """Layover in PA (3-month), final destination IN (6-month) -- only destination fails."""
        # Passport expires Feb 25, 2027 -- ~6 months from Sep departure
        # PA needs 3 months (passes but within 6 months -> INFO),
        # IN needs 6 months (fails -> CRITICAL)
        profile = make_profile(passport_expiry=date(2027, 2, 25))
        seg_layover = make_segment(
            destination="PA",
            departure=date(2026, 9, 1),
            arrival=date(2026, 9, 1),
            is_layover=True,
            flight_number="DL500",
            origin="ATL",
        )
        seg_final = make_segment(
            destination="IN",
            departure=date(2026, 9, 2),
            arrival=date(2026, 9, 3),
            is_layover=False,
            flight_number="DL600",
            origin="PA",
        )
        itinerary = make_itinerary(seg_layover, seg_final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet IN's 6-month" in r for r in result.reasons)

    def test_layover_6_month_destination_3_month_both_pass(self, base_requirements):
        """Layover in TH (6-month), final destination NZ (3-month) -- both pass."""
        # Passport expires Jun 1, 2030 -- well above all thresholds
        profile = make_profile(passport_expiry=date(2030, 6, 1))
        seg_layover = make_segment(
            destination="TH",
            departure=date(2026, 9, 1),
            arrival=date(2026, 9, 1),
            is_layover=True,
            flight_number="DL700",
            origin="ATL",
        )
        seg_final = make_segment(
            destination="NZ",
            departure=date(2026, 9, 2),
            arrival=date(2026, 9, 3),
            is_layover=False,
            flight_number="DL800",
            origin="TH",
        )
        itinerary = make_itinerary(seg_layover, seg_final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is False
        assert result.severity is None
        assert result.reasons == []

    def test_layover_stricter_than_destination(self, base_requirements):
        """Layover in CN (6-month) is stricter than destination DE (3-month).

        Passport has ~4 months validity: passes DE's 3-month requirement but
        fails CN's 6-month layover requirement.  The overall severity should
        still be CRITICAL because the layover requirement applies.
        """
        profile = make_profile(passport_expiry=date(2027, 1, 1))
        seg_layover = make_segment(
            destination="CN",
            departure=date(2026, 9, 1),
            arrival=date(2026, 9, 1),
            is_layover=True,
            flight_number="DL900",
            origin="ATL",
        )
        seg_final = make_segment(
            destination="DE",
            departure=date(2026, 9, 2),
            arrival=date(2026, 9, 3),
            is_layover=False,
            flight_number="DL1000",
            origin="CN",
        )
        itinerary = make_itinerary(seg_layover, seg_final)

        result = evaluate_passport_expiry(profile, itinerary, base_requirements)

        assert result.is_alert_required is True
        assert result.severity == AlertSeverity.CRITICAL
        assert any("does not meet CN's 6-month" in r for r in result.reasons)
