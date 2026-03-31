"""Country-specific travel document requirements data."""

from src.models.types import TravelDocRequirements


COUNTRY_REQUIREMENTS: dict[str, TravelDocRequirements] = {
    # Schengen Area countries — 3-month passport validity requirement
    "DE": TravelDocRequirements(
        country_code="DE",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "FR": TravelDocRequirements(
        country_code="FR",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "IT": TravelDocRequirements(
        country_code="IT",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "ES": TravelDocRequirements(
        country_code="ES",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "NL": TravelDocRequirements(
        country_code="NL",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "AT": TravelDocRequirements(
        country_code="AT",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "BE": TravelDocRequirements(
        country_code="BE",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "CH": TravelDocRequirements(
        country_code="CH",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "PT": TravelDocRequirements(
        country_code="PT",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    "GR": TravelDocRequirements(
        country_code="GR",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=3,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "SG", "BR"],
    ),
    # Non-Schengen countries — 6-month passport validity requirement
    "US": TravelDocRequirements(
        country_code="US",
        requires_visa=True,
        transit_visa_required=True,
        passport_validity_months=6,
        visa_exempt_nationalities=["GB", "CA", "AU", "JP", "KR", "NZ", "SG", "DE", "FR", "IT", "ES", "NL"],
    ),
    "GB": TravelDocRequirements(
        country_code="GB",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=["US", "CA", "AU", "JP", "KR", "NZ", "SG", "DE", "FR", "IT", "ES", "NL"],
    ),
    "JP": TravelDocRequirements(
        country_code="JP",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "KR", "NZ", "SG", "DE", "FR", "IT", "ES", "NL"],
    ),
    "AU": TravelDocRequirements(
        country_code="AU",
        requires_visa=True,
        transit_visa_required=True,
        passport_validity_months=6,
        visa_exempt_nationalities=["NZ"],
    ),
    "CN": TravelDocRequirements(
        country_code="CN",
        requires_visa=True,
        transit_visa_required=True,
        passport_validity_months=6,
        visa_exempt_nationalities=[],
    ),
    "IN": TravelDocRequirements(
        country_code="IN",
        requires_visa=True,
        transit_visa_required=True,
        passport_validity_months=6,
        visa_exempt_nationalities=["NP", "BT"],
    ),
    "BR": TravelDocRequirements(
        country_code="BR",
        requires_visa=True,
        transit_visa_required=False,
        passport_validity_months=6,
        visa_exempt_nationalities=["US", "GB", "CA", "AU", "JP", "KR", "NZ", "DE", "FR", "IT", "ES", "NL"],
    ),
}

# Default requirements for countries not explicitly listed
_DEFAULT_REQUIREMENTS = TravelDocRequirements(
    country_code="DEFAULT",
    requires_visa=True,
    transit_visa_required=False,
    passport_validity_months=6,
    visa_exempt_nationalities=[],
)


def get_requirements(country_code: str) -> TravelDocRequirements:
    """Look up travel document requirements for a country.

    Falls back to a default (6-month passport validity, visa required,
    transit visa not required, empty exempt list) if the country is not found.

    Args:
        country_code: ISO 3166-1 alpha-2 country code.

    Returns:
        TravelDocRequirements for the specified country.
    """
    return COUNTRY_REQUIREMENTS.get(country_code, _DEFAULT_REQUIREMENTS)
