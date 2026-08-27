from __future__ import annotations

import re
import unicodedata

import pycountry


COUNTRY_ALIASES = {
    "america": "US",
    "brunei": "BN",
    "czech republic": "CZ",
    "england": "GB",
    "hong kong": "HK",
    "ivory coast": "CI",
    "korea": "KR",
    "macedonia": "MK",
    "russia": "RU",
    "scotland": "GB",
    "south korea": "KR",
    "taiwan": "TW",
    "uae": "AE",
    "u k": "GB",
    "uk": "GB",
    "united kingdom": "GB",
    "united states": "US",
    "united states of america": "US",
    "u s": "US",
    "u s a": "US",
    "usa": "US",
    "vietnam": "VN",
    "wales": "GB",
}


class CountryNormalizationError(ValueError):
    pass


def _key(value: str) -> str:
    ascii_value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", ascii_value.lower()).strip()


def normalize_country(value: str | None) -> str:
    """Return an ISO 3166-1 alpha-2 code from common country representations."""

    if not value or not value.strip():
        raise CountryNormalizationError("Country is empty")

    raw = value.strip()
    if raw.lower().startswith("string:"):
        raw = raw.split(":", 1)[1].strip()

    # ServiceNow labels commonly use "US - United States".
    label_match = re.match(r"^([A-Za-z]{2})\s*-\s*.+$", raw)
    if label_match:
        return label_match.group(1).upper()

    if len(raw) == 2 and raw.isalpha():
        code = raw.upper()
        if pycountry.countries.get(alpha_2=code):
            return code

    normalized = _key(raw)
    if normalized in COUNTRY_ALIASES:
        return COUNTRY_ALIASES[normalized]

    try:
        country = pycountry.countries.lookup(raw)
    except LookupError as exc:
        raise CountryNormalizationError(f"Unrecognized country: {value}") from exc
    return country.alpha_2


def country_name(country_code: str) -> str:
    code = normalize_country(country_code)
    country = pycountry.countries.get(alpha_2=code)
    if country is None:
        raise CountryNormalizationError(f"Unrecognized country code: {country_code}")
    return country.name


def servicenow_country_value(country_code: str) -> str:
    return f"string:{normalize_country(country_code)}"
