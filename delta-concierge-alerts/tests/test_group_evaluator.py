"""Critical-path tests for the group itinerary evaluator."""

from datetime import date

from src.evaluators.group_evaluator import evaluate_group_itinerary
from src.models.types import AlertSeverity, VisaRecord

from tests.conftest import make_group, make_profile, make_segment


DEPARTURE = date(2030, 9, 1)
ARRIVAL = date(2030, 9, 2)


def _visa(country: str, expiry: date, visa_number: str = "V001") -> VisaRecord:
    return VisaRecord(
        country_code=country,
        visa_type="TOURIST",
        issue_date=date(2028, 1, 1),
        expiry_date=expiry,
        visa_number=visa_number,
    )


def _clean_traveler(skymiles: str = "1000001", first: str = "Ann") -> object:
    """US national (visa-exempt for DE) with a long-valid passport."""
    return make_profile(
        nationality="US",
        passport_expiry=date(2040, 1, 1),
        skymiles_number=skymiles,
        first_name=first,
    )


class TestCleanGroup:
    """No traveler has a document issue → no alerts and no group severity."""

    def test_all_travelers_clean(self, base_requirements):
        group = make_group(
            travelers=[_clean_traveler("1000001", "Ann"), _clean_traveler("1000002", "Bob")],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert len(result.traveler_summaries) == 2
        assert all(not s.passport_result.is_alert_required for s in result.traveler_summaries)
        assert all(not s.visa_result.is_alert_required for s in result.traveler_summaries)

    def test_result_echoes_group_metadata(self, base_requirements):
        group = make_group(
            travelers=[_clean_traveler("1000001", "Ann")],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
            confirmation="DL-ABC123",
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.confirmation_number == "DL-ABC123"
        summary = result.traveler_summaries[0]
        assert summary.skymiles_number == "1000001"
        assert summary.first_name == "Ann"
        assert summary.last_name == "User"

    def test_empty_traveler_list(self, base_requirements):
        group = make_group(
            travelers=[],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.traveler_summaries == []
        assert result.group_severity is None


class TestGroupSeverityAggregation:
    """Group severity is the highest severity found across all travelers."""

    def test_one_critical_traveler_drives_group_critical(self, base_requirements):
        broken = make_profile(
            nationality="US",
            passport_number=None,
            passport_expiry=None,
            skymiles_number="1000002",
            first_name="Bob",
        )
        group = make_group(
            travelers=[_clean_traveler("1000001", "Ann"), broken],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.is_alert_required is False
        assert result.traveler_summaries[1].passport_result.severity == AlertSeverity.CRITICAL

    def test_info_only_group_stays_info(self, base_requirements):
        # Passport valid past DE's 3-month rule but expiring within 6 months of travel.
        expiring = make_profile(
            nationality="US",
            passport_expiry=date(2031, 1, 1),
            skymiles_number="1000003",
        )
        group = make_group(
            travelers=[expiring],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.INFO
        assert result.traveler_summaries[0].passport_result.severity == AlertSeverity.INFO

    def test_warning_outranks_info(self, base_requirements):
        expiring_passport = make_profile(
            nationality="US",
            passport_expiry=date(2031, 1, 1),
            skymiles_number="1000003",
        )
        # Visa expiring exactly on the departure date → WARNING.
        visa_on_travel_date = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[_visa("DE", DEPARTURE)],
            skymiles_number="1000004",
        )
        group = make_group(
            travelers=[expiring_passport, visa_on_travel_date],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.WARNING
        assert result.traveler_summaries[1].visa_result.severity == AlertSeverity.WARNING

    def test_critical_outranks_warning_regardless_of_traveler_order(self, base_requirements):
        critical_first = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[],
            skymiles_number="1000005",
        )
        warning_second = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[_visa("DE", DEPARTURE)],
            skymiles_number="1000006",
        )
        group = make_group(
            travelers=[critical_first, warning_second],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL


class TestPerTravelerIsolation:
    """Each traveler is evaluated independently against the shared segments."""

    def test_summaries_carry_the_matching_profile(self, base_requirements):
        ann = _clean_traveler("1000001", "Ann")
        bob = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[],
            skymiles_number="1000002",
            first_name="Bob",
        )
        group = make_group(
            travelers=[ann, bob],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        ann_summary, bob_summary = result.traveler_summaries
        assert ann_summary.passport_result.profile is ann
        assert ann_summary.visa_result.profile is ann
        assert bob_summary.visa_result.profile is bob
        assert bob_summary.visa_result.reasons == ["No visa on file for DE"]

    def test_all_shared_segments_are_evaluated(self, base_requirements):
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL1"),
            make_segment(
                destination="JP",
                departure=date(2030, 9, 10),
                arrival=date(2030, 9, 11),
                flight_number="DL2",
            ),
        ]
        group = make_group(travelers=[_clean_traveler()], segments=segments)

        result = evaluate_group_itinerary(group, base_requirements)

        summary = result.traveler_summaries[0]
        assert summary.passport_result.segments_evaluated == segments
        assert summary.visa_result.segments_evaluated == segments


class TestLayoverSegments:
    """Layovers only trigger visa alerts when a transit visa is required."""

    def test_layover_requiring_transit_visa_alerts(self, base_requirements):
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[],
            skymiles_number="1000007",
        )
        group = make_group(
            travelers=[traveler],
            segments=[
                make_segment(
                    destination="CN", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
                )
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]

    def test_layover_without_transit_visa_requirement_is_clean(self, base_requirements):
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[],
            skymiles_number="1000008",
        )
        group = make_group(
            travelers=[traveler],
            segments=[
                make_segment(
                    destination="DE", departure=DEPARTURE, arrival=ARRIVAL, is_layover=True
                )
            ],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.is_alert_required is False


class TestPassportValidityWindow:
    """Country-specific minimum passport validity applies to every traveler."""

    def test_passport_below_destination_minimum_is_critical(self, base_requirements):
        # CN requires 6 months validity; expiry is only ~2 months after departure.
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2030, 11, 1),
            visa_records=[_visa("CN", date(2035, 1, 1))],
            skymiles_number="1000009",
        )
        group = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert any(
            "6-month" in reason
            for reason in result.traveler_summaries[0].passport_result.reasons
        )

    def test_expired_passport_is_critical(self, base_requirements):
        traveler = make_profile(
            nationality="US",
            passport_expiry=date(2020, 1, 1),
            skymiles_number="1000010",
        )
        group = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].passport_result.reasons == ["Passport has expired"]


class TestDefaultRequirementsLookup:
    """When no requirements are passed, they are resolved from the default data."""

    def test_requirements_default_to_country_data(self):
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[],
            skymiles_number="1000011",
        )
        group = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="CN", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity == AlertSeverity.CRITICAL
        assert result.traveler_summaries[0].visa_result.reasons == ["No visa on file for CN"]

    def test_visa_exempt_nationality_from_default_data_is_clean(self):
        traveler = make_profile(
            nationality="US",
            passport_expiry=date(2040, 1, 1),
            visa_records=[],
            skymiles_number="1000012",
        )
        group = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None

    def test_repeated_destinations_are_looked_up_once(self):
        traveler = make_profile(
            nationality="US",
            passport_expiry=date(2040, 1, 1),
            skymiles_number="1000013",
        )
        segments = [
            make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL, flight_number="DL1"),
            make_segment(
                destination="DE",
                departure=date(2030, 9, 10),
                arrival=date(2030, 9, 11),
                flight_number="DL2",
            ),
        ]
        group = make_group(travelers=[traveler], segments=segments)

        result = evaluate_group_itinerary(group)

        assert result.group_severity is None
        assert len(result.traveler_summaries[0].passport_result.segments_evaluated) == 2


class TestItineraryChange:
    """Re-evaluating a group after its segments change yields updated severity."""

    def test_later_departure_makes_previously_clean_group_critical(self, base_requirements):
        traveler = make_profile(
            nationality="US",
            passport_expiry=date(2031, 1, 1),
            skymiles_number="1000014",
        )
        original = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=date(2030, 1, 1), arrival=date(2030, 1, 2))],
        )
        rebooked = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=date(2032, 1, 1), arrival=date(2032, 1, 2))],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(rebooked, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL

    def test_added_destination_introduces_visa_alert(self, base_requirements):
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[_visa("DE", date(2035, 1, 1))],
            skymiles_number="1000015",
        )
        original = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )
        extended = make_group(
            travelers=[traveler],
            segments=[
                make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL),
                make_segment(
                    destination="JP",
                    departure=date(2030, 9, 10),
                    arrival=date(2030, 9, 11),
                    flight_number="DL2",
                ),
            ],
        )

        before = evaluate_group_itinerary(original, base_requirements)
        after = evaluate_group_itinerary(extended, base_requirements)

        assert before.group_severity is None
        assert after.group_severity == AlertSeverity.CRITICAL
        assert "No visa on file for JP" in after.traveler_summaries[0].visa_result.reasons


class TestVisaRecordSelection:
    """The latest-expiring visa for a country wins for each traveler in the group."""

    def test_valid_visa_preferred_over_expired_duplicate(self, base_requirements):
        traveler = make_profile(
            nationality="IN",
            passport_expiry=date(2040, 1, 1),
            visa_records=[
                _visa("DE", date(2021, 1, 1), visa_number="OLD"),
                _visa("DE", date(2035, 1, 1), visa_number="NEW"),
            ],
            skymiles_number="1000016",
        )
        group = make_group(
            travelers=[traveler],
            segments=[make_segment(destination="DE", departure=DEPARTURE, arrival=ARRIVAL)],
        )

        result = evaluate_group_itinerary(group, base_requirements)

        assert result.group_severity is None
        assert result.traveler_summaries[0].visa_result.reasons == []
