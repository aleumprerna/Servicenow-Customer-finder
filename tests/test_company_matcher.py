from services.company_matcher import company_match_score, normalize_company_name


def test_legal_suffixes_are_removed() -> None:
    assert normalize_company_name("Microsoft Corporation") == "microsoft"
    assert normalize_company_name("Accenture PLC") == "accenture"


def test_ampersand_is_normalized() -> None:
    assert normalize_company_name("Marks & Spencer Ltd") == "marks and spencer"


def test_long_subsidiary_name_is_not_automatic_yes() -> None:
    assert company_match_score("ABC", "ABC Consulting Services India Private Limited") < 85


def test_expected_legal_name_matches() -> None:
    assert company_match_score("Microsoft", "Microsoft Corporation") == 100
