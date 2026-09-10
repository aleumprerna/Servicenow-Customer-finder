from config import Settings


def test_openai_luna_is_the_default_llm_provider() -> None:
    settings = Settings(apollo_api_key="apollo-test", openai_api_key="openai-test")

    assert settings.llm_provider == "openai"
    assert settings.llm_api_key == "openai-test"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == "gpt-5.6-luna"
    assert settings.llm_reasoning_effort is None
    assert settings.llm_supports_hosted_web_search is True


def test_gemini_configuration_is_preserved_and_selectable() -> None:
    settings = Settings(
        apollo_api_key="apollo-test",
        llm_provider="gemini",
        gemini_api_key="gemini-test",
    )

    assert settings.llm_api_key == "gemini-test"
    assert settings.llm_base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert settings.llm_model == "gemini-3-flash-preview"
    assert settings.llm_reasoning_effort is None
    assert settings.llm_supports_hosted_web_search is True


def test_glm_configuration_is_preserved_and_selectable() -> None:
    settings = Settings(
        apollo_api_key="apollo-test",
        llm_provider="glm",
        glm_api_key="glm-test",
    )

    assert settings.llm_api_key == "glm-test"
    assert settings.llm_base_url == "https://api.tokenrouter.com/v1"
    assert settings.llm_model == "z-ai/glm-5.3"
    assert settings.llm_supports_hosted_web_search is False


def test_openai_configuration_is_preserved_and_selectable() -> None:
    settings = Settings(
        apollo_api_key="apollo-test",
        llm_provider="openai",
        glm_api_key="glm-test",
        openai_api_key="openai-test",
        openai_model="gpt-test",
    )

    assert settings.llm_api_key == "openai-test"
    assert settings.llm_base_url == "https://api.openai.com/v1"
    assert settings.llm_model == "gpt-test"
    assert settings.llm_supports_hosted_web_search is True
