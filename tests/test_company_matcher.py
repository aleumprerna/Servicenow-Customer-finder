from services.company_matcher import customer_search_name, company_match_score, normalize_company_name


def test_customer_search_name_removes_legal_suffix_without_changing_core_name() -> None:
    assert customer_search_name("Harbour Energy plc") == "Harbour Energy"
    assert customer_search_name("Example Holdings Limited") == "Example Holdings"
    assert customer_search_name("Acme, Inc.") == "Acme"
    assert customer_search_name("Harbour Energy") == "Harbour Energy"


def test_legal_suffixes_are_removed() -> None:
    assert normalize_company_name("Microsoft Corporation") == "microsoft"
    assert normalize_company_name("Accenture PLC") == "accenture"


def test_ampersand_is_normalized() -> None:
    assert normalize_company_name("Marks & Spencer Ltd") == "marks and spencer"


def test_long_subsidiary_name_is_not_automatic_yes() -> None:
    assert company_match_score("ABC", "ABC Consulting Services India Private Limited") < 85


def test_expected_legal_name_matches() -> None:
    assert company_match_score("Microsoft", "Microsoft Corporation") == 100
