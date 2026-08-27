import pytest

from services.country_normalizer import CountryNormalizationError, normalize_country


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("United States", "US"),
        ("USA", "US"),
        ("United States of America", "US"),
        ("US", "US"),
        ("US - United States", "US"),
        ("string:IN", "IN"),
        ("United Kingdom", "GB"),
        ("Germany", "DE"),
        ("Singapore", "SG"),
        ("Australia", "AU"),
    ],
)
def test_normalize_country(value: str, expected: str) -> None:
    assert normalize_country(value) == expected


def test_invalid_country_raises() -> None:
    with pytest.raises(CountryNormalizationError):
        normalize_country("Not a real country")
