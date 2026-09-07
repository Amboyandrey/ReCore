"""End-to-end auth flow: signup, login, /me, logout, and the failure paths around each."""

from httpx import AsyncClient

CREDENTIALS = {"email": "ada@example.com", "password": "correct horse battery staple"}


async def test_signup_creates_an_account_without_signing_in(client: AsyncClient) -> None:
    """Signup returns the new user but sets no session cookie."""
    response = await client.post("/api/v1/auth/signup", json=CREDENTIALS)

    assert response.status_code == 201
    assert response.json()["email"] == CREDENTIALS["email"]
    assert "recore_session" not in response.cookies


async def test_signup_rejects_a_duplicate_email_case_insensitively(client: AsyncClient) -> None:
    """A second signup with the same email in different case is rejected, not silently allowed."""
    await client.post("/api/v1/auth/signup", json=CREDENTIALS)

    response = await client.post(
        "/api/v1/auth/signup", json={**CREDENTIALS, "email": "Ada@Example.com"}
    )

    assert response.status_code == 409


async def test_signup_rejects_a_short_password(client: AsyncClient) -> None:
    """A password under the minimum length is rejected before it ever reaches the service."""
    response = await client.post(
        "/api/v1/auth/signup", json={"email": "short@example.com", "password": "tooshort"}
    )

    assert response.status_code == 422


async def test_login_with_wrong_password_gives_a_generic_error(client: AsyncClient) -> None:
    """A wrong password and an unknown email produce the identical response."""
    await client.post("/api/v1/auth/signup", json=CREDENTIALS)

    wrong_password = await client.post(
        "/api/v1/auth/login", json={**CREDENTIALS, "password": "not the right password"}
    )
    unknown_email = await client.post(
        "/api/v1/auth/login", json={**CREDENTIALS, "email": "nobody@example.com"}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json()["detail"] == unknown_email.json()["detail"]


async def test_login_sets_a_session_cookie_and_me_reflects_it(client: AsyncClient) -> None:
    """A successful login sets the session cookie, and /me then returns that same user."""
    await client.post("/api/v1/auth/signup", json=CREDENTIALS)

    login = await client.post("/api/v1/auth/login", json=CREDENTIALS)
    assert login.status_code == 200
    assert "recore_session" in login.cookies
    csrf_token = login.json()["csrf_token"]

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == CREDENTIALS["email"]
    assert csrf_token  # issued and non-empty; logout test below proves it's actually checked


async def test_me_without_a_session_is_rejected(client: AsyncClient) -> None:
    """Calling /me with no session cookie at all is a 401, not a crash."""
    response = await client.get("/api/v1/auth/me")

    assert response.status_code == 401


async def test_logout_requires_a_matching_csrf_header(client: AsyncClient) -> None:
    """Logout without the CSRF header — or with the wrong one — is refused, not silently run."""
    await client.post("/api/v1/auth/signup", json=CREDENTIALS)
    await client.post("/api/v1/auth/login", json=CREDENTIALS)

    no_header = await client.post("/api/v1/auth/logout")
    wrong_header = await client.post(
        "/api/v1/auth/logout", headers={"X-CSRF-Token": "not-the-real-token"}
    )

    assert no_header.status_code == 403
    assert wrong_header.status_code == 403


async def test_logout_ends_the_session_for_good(client: AsyncClient) -> None:
    """After logout, the same cookie no longer authenticates — the session is actually gone."""
    await client.post("/api/v1/auth/signup", json=CREDENTIALS)
    login = await client.post("/api/v1/auth/login", json=CREDENTIALS)
    csrf_token = login.json()["csrf_token"]

    logout = await client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf_token})
    assert logout.status_code == 204

    me = await client.get("/api/v1/auth/me")
    assert me.status_code == 401


async def test_login_is_rate_limited_after_repeated_failures(client: AsyncClient) -> None:
    """Enough failed attempts in a row eventually get a 429 instead of another 401."""
    await client.post("/api/v1/auth/signup", json=CREDENTIALS)

    statuses = []
    for _ in range(10):
        response = await client.post(
            "/api/v1/auth/login", json={**CREDENTIALS, "password": "wrong every time"}
        )
        statuses.append(response.status_code)

    assert 429 in statuses
