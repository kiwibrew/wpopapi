import hashlib
import re
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import SESSION_COOKIE_NAME
from app.dependencies import get_password_reset_mailer, get_worldpop_service
from app.main import app
from app.models.models import User
from app.repositories.users import UserRepository
from app.services.worldpop import CoordinatesOutsideCountryError


UserFactory = Callable[..., Awaitable[User]]


async def sign_in(
    client: AsyncClient,
    email: str,
    password: str = "correct-horse",
) -> None:
    response = await client.post(
        "/login", data={"username": email, "password": password}
    )
    assert response.status_code == 303


async def csrf_from(client: AsyncClient, path: str = "/") -> str:
    response = await client.get(path)
    match = re.search(r'name="csrf_token" value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


@pytest.mark.asyncio
async def test_valid_sign_in_sets_cookie_and_invalid_sign_in_does_not(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    await user_factory("admin@example.com", is_admin=True)
    invalid = await client.post(
        "/login",
        data={"username": "admin@example.com", "password": "wrong-pass"},
    )
    assert invalid.status_code == 303
    assert SESSION_COOKIE_NAME not in invalid.cookies

    valid = await client.post(
        "/login",
        data={"username": "ADMIN@example.com", "password": "correct-horse"},
    )
    assert valid.status_code == 303
    assert valid.cookies[SESSION_COOKIE_NAME]
    set_cookie = valid.headers["set-cookie"].lower()
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "path=/" in set_cookie
    assert "max-age=" in set_cookie


@pytest.mark.asyncio
async def test_inactive_user_cannot_sign_in_or_use_api(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory(
        "off@example.com", is_active=False, bearer_token="off-token"
    )
    login = await client.post(
        "/login", data={"username": user.email, "password": "correct-horse"}
    )
    assert SESSION_COOKIE_NAME not in login.cookies
    api = await client.get(
        "/api/pop",
        params={"iso3": "NZL", "lat": 0, "lon": 0},
        headers={"Authorization": "Bearer off-token"},
    )
    assert api.status_code == 403


@pytest.mark.asyncio
async def test_persistent_token_and_access_jwt_authenticate_api(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("api@example.com", bearer_token="persistent-token")

    class FakeWorldPopService:
        async def get_pop(self, iso3: str, lat: float, lon: float) -> int:
            return 42

    app.dependency_overrides[get_worldpop_service] = lambda: FakeWorldPopService()
    persistent = await client.get(
        "/api/pop",
        params={"iso3": "NZL", "lat": 0, "lon": 0},
        headers={"Authorization": "Bearer persistent-token"},
    )
    token = await client.post(
        "/token", data={"username": user.email, "password": "correct-horse"}
    )
    jwt_access = await client.get(
        "/api/pop",
        params={"iso3": "NZL", "lat": 0, "lon": 0},
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
    )
    assert persistent.json() == {"pop": 42}
    assert jwt_access.json() == {"pop": 42}


@pytest.mark.asyncio
async def test_api_rejects_a_point_outside_the_worldpop_tile(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    await user_factory("api@example.com", bearer_token="persistent-token")

    class FakeWorldPopService:
        async def get_pop(self, iso3: str, lat: float, lon: float) -> int:
            raise CoordinatesOutsideCountryError(iso3.strip().upper())

    app.dependency_overrides[get_worldpop_service] = lambda: FakeWorldPopService()
    response = await client.get(
        "/api/pop",
        params={"iso3": "nzl", "lat": -80, "lon": 170},
        headers={"Authorization": "Bearer persistent-token"},
    )

    assert response.status_code == 422
    assert response.json() == {
        "detail": (
            "the submitted point is not within the bounds of the WorldPop tile "
            "for country code NZL"
        )
    }


@pytest.mark.asyncio
async def test_anonymous_user_cannot_access_protected_ui_or_openapi(
    client: AsyncClient,
) -> None:
    for path in ("/docs", "/app-docs", "/manage-users"):
        response = await client.get(path)
        assert response.status_code == 303
        assert response.headers["location"] == "/"
    openapi = await client.get("/openapi.json")
    assert openapi.status_code == 401
    assert openapi.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_non_admin_sees_only_own_account_and_cannot_manage_users(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("user@example.com")
    await user_factory("other@example.com")
    await sign_in(client, user.email)
    page = await client.get("/manage-users")
    assert user.email in page.text
    assert "other@example.com" not in page.text
    assert "password_hash" not in page.text
    token = await client.post(
        "/token", data={"username": user.email, "password": "correct-horse"}
    )
    forbidden = await client.post(
        "/users/create",
        data={"email": "new@example.com", "password": "new-password"},
        headers={"Authorization": f"Bearer {token.json()['access_token']}"},
    )
    assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_admin_lifecycle_changes_and_self_protection(
    client: AsyncClient,
    db_session: AsyncSession,
    user_factory: UserFactory,
) -> None:
    admin = await user_factory("admin@example.com", is_admin=True)
    second_admin = await user_factory("second@example.com", is_admin=True)
    api_user = await user_factory("api@example.com", bearer_token="old-token")
    await sign_in(client, admin.email)
    csrf = await csrf_from(client, "/manage-users")

    self_change = await client.post(
        f"/users/{admin.id}/toggle-active", data={"csrf_token": csrf}
    )
    assert self_change.status_code == 400
    self_role = await client.post(
        f"/users/{admin.id}/toggle-admin", data={"csrf_token": csrf}
    )
    self_delete = await client.post(
        f"/users/{admin.id}/delete", data={"csrf_token": csrf}
    )
    assert self_role.status_code == 400
    assert self_delete.status_code == 400

    replacement = await client.post(
        f"/users/{api_user.id}/regen-token", data={"csrf_token": csrf}
    )
    assert replacement.status_code == 303
    assert api_user.bearer_token != "old-token"
    old_access = await client.get(
        "/openapi.json", headers={"Authorization": "Bearer old-token"}
    )
    new_access = await client.get(
        "/openapi.json",
        headers={"Authorization": f"Bearer {api_user.bearer_token}"},
    )
    assert old_access.status_code == 401
    assert new_access.status_code == 200

    promoted = await client.post(
        f"/users/{api_user.id}/toggle-admin", data={"csrf_token": csrf}
    )
    assert promoted.status_code == 303
    assert api_user.is_admin and api_user.bearer_token is None
    demoted = await client.post(
        f"/users/{api_user.id}/toggle-admin", data={"csrf_token": csrf}
    )
    assert demoted.status_code == 303
    assert not api_user.is_admin and api_user.bearer_token

    deactivated = await client.post(
        f"/users/{second_admin.id}/toggle-active", data={"csrf_token": csrf}
    )
    assert deactivated.status_code == 303
    last_admin = await client.post(
        f"/users/{admin.id}/toggle-active", data={"csrf_token": csrf}
    )
    assert last_admin.status_code == 400

    created = await client.post(
        "/users/create",
        data={
            "email": "created@example.com",
            "password": "new-password",
            "csrf_token": csrf,
        },
    )
    assert created.status_code == 303
    created_user = await UserRepository(db_session).get_by_email("created@example.com")
    assert created_user is not None and created_user.bearer_token
    deleted = await client.post(
        f"/users/{created_user.id}/delete", data={"csrf_token": csrf}
    )
    assert deleted.status_code == 303


@pytest.mark.asyncio
async def test_ui_navigation_and_swagger_match_the_required_layout(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    admin = await user_factory("admin@example.com", is_admin=True)
    await sign_in(client, admin.email)
    for path in ("/", "/manage-users", "/app-docs", "/tile-cache", "/docs"):
        page = await client.get(path)
        assert page.status_code == 200
        assert "navbar navbar-expand-lg bg-dark navbar-dark" in page.text
        assert 'class="container"' in page.text
        assert (
            page.text.index("<nav") < page.text.index("<main")
            if "<main" in page.text
            else True
        )
        assert page.text.index('href="/docs"') < page.text.index('href="/app-docs"')
        assert page.text.index('href="/app-docs"') < page.text.index(
            'href="/manage-users"'
        )
        assert "btn btn-link nav-link" in page.text


@pytest.mark.asyncio
async def test_cookie_authenticated_mutation_requires_csrf(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    admin = await user_factory("admin@example.com", is_admin=True)
    user = await user_factory("user@example.com")
    await sign_in(client, admin.email)
    rejected = await client.post(f"/users/{user.id}/regen-token")
    assert rejected.status_code == 403
    csrf = await csrf_from(client, "/manage-users")
    accepted = await client.post(
        f"/users/{user.id}/regen-token", data={"csrf_token": csrf}
    )
    assert accepted.status_code == 303


@pytest.mark.asyncio
async def test_password_reset_request_does_not_disclose_account_eligibility(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    active = await user_factory("active@example.com")
    await user_factory("inactive@example.com", is_active=False)
    sent_codes: list[tuple[str, str]] = []

    class FakeMailer:
        async def send(self, recipient: str, code: str) -> None:
            sent_codes.append((recipient, code))

    app.dependency_overrides[get_password_reset_mailer] = lambda: FakeMailer()
    responses = [
        await client.post("/forgot-password", data={"email": email})
        for email in ("unknown@example.com", "inactive@example.com", active.email)
    ]
    assert [response.status_code for response in responses] == [303, 303, 303]
    assert len({response.text for response in responses}) == 1
    assert sent_codes and sent_codes[0][0] == active.email
    assert sent_codes[0][1] not in responses[-1].text
    assert active.reset_token_hash is not None
    assert active.reset_token_hash != sent_codes[0][1]
    assert active.reset_token_expires_at is not None
    expires_at = active.reset_token_expires_at.replace(tzinfo=UTC)
    assert (
        timedelta(minutes=29) < expires_at - datetime.now(UTC) <= timedelta(minutes=30)
    )


@pytest.mark.asyncio
async def test_password_reset_changes_password_once_and_redirects(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("reset@example.com")
    sent_codes: list[str] = []

    class FakeMailer:
        async def send(self, _recipient: str, code: str) -> None:
            sent_codes.append(code)

    app.dependency_overrides[get_password_reset_mailer] = lambda: FakeMailer()
    await client.post("/forgot-password", data={"email": user.email})
    reset = await client.post(
        "/reset-password", data={"code": sent_codes[0], "password": "new-password"}
    )
    assert reset.status_code == 303
    assert reset.headers["location"] == "/?status=password_reset"
    assert user.reset_token_hash is None
    assert user.reset_token_expires_at is None
    signed_in = await client.post(
        "/login", data={"username": user.email, "password": "new-password"}
    )
    assert signed_in.status_code == 303
    reused = await client.post(
        "/reset-password", data={"code": sent_codes[0], "password": "another-password"}
    )
    assert reused.status_code == 400


@pytest.mark.asyncio
async def test_invalid_expired_and_inactive_reset_codes_do_not_change_password(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("expired@example.com")
    user.reset_token_hash = hashlib.sha256(b"expired").hexdigest()
    user.reset_token_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    inactive = await user_factory("inactive-reset@example.com", is_active=False)
    inactive.reset_token_hash = hashlib.sha256(b"inactive").hexdigest()
    inactive.reset_token_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    for code in ("unknown", "expired", "inactive"):
        response = await client.post(
            "/reset-password", data={"code": code, "password": "new-password"}
        )
        assert response.status_code == 400
    assert (
        await client.post(
            "/login", data={"username": user.email, "password": "correct-horse"}
        )
    ).status_code == 303


@pytest.mark.asyncio
async def test_second_password_reset_request_invalidates_the_first_code(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("twice@example.com")
    sent_codes: list[str] = []

    class FakeMailer:
        async def send(self, _recipient: str, code: str) -> None:
            sent_codes.append(code)

    app.dependency_overrides[get_password_reset_mailer] = lambda: FakeMailer()
    await client.post("/forgot-password", data={"email": user.email})
    await client.post("/forgot-password", data={"email": user.email})
    first = await client.post(
        "/reset-password", data={"code": sent_codes[0], "password": "new-password"}
    )
    second = await client.post(
        "/reset-password", data={"code": sent_codes[1], "password": "new-password"}
    )
    assert first.status_code == 400
    assert second.status_code == 303


@pytest.mark.asyncio
async def test_password_reset_delivery_failure_clears_code_and_acknowledges_request(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("delivery@example.com")

    class FailingMailer:
        async def send(self, _recipient: str, _code: str) -> None:
            raise OSError("unavailable")

    app.dependency_overrides[get_password_reset_mailer] = lambda: FailingMailer()
    response = await client.post("/forgot-password", data={"email": user.email})
    assert response.status_code == 303
    assert user.reset_token_hash is None
    assert user.reset_token_expires_at is None


@pytest.mark.asyncio
async def test_non_administrator_cannot_view_tile_cache(
    client: AsyncClient, user_factory: UserFactory
) -> None:
    user = await user_factory("user@example.com")
    await sign_in(client, user.email)
    response = await client.get("/tile-cache")
    assert response.status_code == 403
