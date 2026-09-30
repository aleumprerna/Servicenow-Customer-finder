from microsoft_auth import MicrosoftAuthSettings, _login_page


def test_login_page_has_microsoft_action_and_permission_copy() -> None:
    page = _login_page(configured=True)

    assert 'href="/auth/microsoft"' in page
    assert "Sign in with Microsoft" in page
    assert "permission to view your basic profile" in page


def test_login_page_explains_missing_configuration() -> None:
    page = _login_page(configured=False)

    assert 'aria-disabled="true"' in page
    assert ".env.example" in page


def test_signed_in_page_escapes_claims() -> None:
    page = _login_page(
        user={"name": "<Admin>", "preferred_username": "person@example.com"}
    )

    assert "Welcome, &lt;Admin&gt;" in page
    assert "<Admin>" not in page
    assert "person@example.com" in page


def test_authority_uses_configured_tenant() -> None:
    settings = MicrosoftAuthSettings("client", "secret", "tenant-id", "http://localhost/callback")

    assert settings.configured is True
    assert settings.authority == "https://login.microsoftonline.com/tenant-id"
