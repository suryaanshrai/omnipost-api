"""Personal API tokens and the web app's httpOnly sign-in cookie."""

from datetime import timedelta

from django.utils import timezone
from rest_framework.test import APIClient

from omnipost_api.models import ApiToken, Post


def _user(django_user_model, name="tok"):
    return django_user_model.objects.create_user(username=name, email=f"{name}@example.com", password="pw-12345678")


def _signed_in(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client


def _make_token(user, **body):
    response = _signed_in(user).post("/account/tokens/", {"name": "script", **body}, format="json")
    assert response.status_code == 201, response.data
    return response.data


def test_token_key_is_shown_once_and_stored_hashed(db, django_user_model):
    user = _user(django_user_model)
    created = _make_token(user)
    assert created["key"].startswith("op_") and created["prefix"] in created["key"]
    row = ApiToken.objects.get()
    assert created["key"] not in (row.key_hash, row.prefix)
    listed = _signed_in(user).get("/account/tokens/").data
    assert "key" not in listed[0]


def test_bearer_token_works_and_records_use(db, django_user_model):
    user = _user(django_user_model)
    key = _make_token(user)["key"]
    workspace = user.memberships.get().workspace
    client = APIClient(HTTP_AUTHORIZATION=f"Bearer {key}")
    assert client.get("/workspaces/").status_code == 200
    created = client.post("/posts/", {"workspace": workspace.pk, "kind": "text", "base_text": "from a script"})
    assert created.status_code == 201
    assert ApiToken.objects.get().last_used_at is not None


def test_bad_revoked_and_expired_tokens_are_refused(db, django_user_model):
    user = _user(django_user_model)
    key = _make_token(user)["key"]
    assert APIClient(HTTP_AUTHORIZATION=f"Bearer {key}x").get("/workspaces/").status_code == 401
    assert APIClient(HTTP_AUTHORIZATION="Bearer op_nope_nope").get("/workspaces/").status_code == 401

    ApiToken.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert APIClient(HTTP_AUTHORIZATION=f"Bearer {key}").get("/workspaces/").status_code == 401

    second = _make_token(user, expires_in_days=30)
    assert second["expires_at"]
    _signed_in(user).delete(f"/account/tokens/{second['id']}/")
    assert APIClient(HTTP_AUTHORIZATION=f"Bearer {second['key']}").get("/workspaces/").status_code == 401


def test_read_only_token_cannot_write(db, django_user_model):
    user = _user(django_user_model)
    key = _make_token(user, read_only=True)["key"]
    workspace = user.memberships.get().workspace
    client = APIClient(HTTP_AUTHORIZATION=f"Bearer {key}")
    assert client.get(f"/posts/?workspace={workspace.pk}").status_code == 200
    assert client.post("/posts/", {"workspace": workspace.pk, "kind": "text", "base_text": "x"}).status_code == 403
    assert not Post.objects.exists()


def test_an_api_token_cannot_mint_tokens_or_take_over_the_account(db, django_user_model):
    user = _user(django_user_model)
    client = APIClient(HTTP_AUTHORIZATION=f"Bearer {_make_token(user)['key']}")
    assert client.post("/account/tokens/", {"name": "more"}).status_code == 403
    assert client.get("/account/tokens/").status_code == 403
    assert client.post("/account/delete/", {"password": "pw-12345678"}).status_code == 403
    pw = {"old_password": "pw-12345678", "new_password1": "Another-pw-987", "new_password2": "Another-pw-987"}
    assert client.post("/auth/password/change/", pw).status_code == 403
    assert client.post("/auth/logout/").status_code == 403


def test_tokens_are_private_to_their_owner(db, django_user_model):
    alice, bob = _user(django_user_model, "alice"), _user(django_user_model, "bob")
    token = _make_token(alice)
    assert _signed_in(bob).get("/account/tokens/").data == []
    assert _signed_in(bob).delete(f"/account/tokens/{token['id']}/").status_code == 404


# --- cookie sign-in -------------------------------------------------------------------


def test_web_app_login_sets_an_httponly_cookie_and_hides_the_key(db, django_user_model):
    _user(django_user_model, "web")
    client = APIClient()
    response = client.post(
        "/auth/login/", {"username": "web", "password": "pw-12345678"}, HTTP_X_OMNIPOST_CLIENT="web"
    )
    assert response.status_code == 200 and response.data["key"] == ""
    cookie = response.cookies["omnipost_auth"]
    assert cookie["httponly"] and cookie["samesite"] == "Lax" and cookie.value

    # The cookie only counts together with the header a foreign site can't send.
    assert client.get("/workspaces/").status_code == 401
    assert client.get("/workspaces/", HTTP_X_OMNIPOST_CLIENT="web").status_code == 200

    out = client.post("/auth/logout/", HTTP_X_OMNIPOST_CLIENT="web")
    assert out.status_code == 200 and out.cookies["omnipost_auth"].value == ""
    assert client.get("/workspaces/", HTTP_X_OMNIPOST_CLIENT="web").status_code == 401


def test_other_clients_still_get_the_key_in_the_body(db, django_user_model):
    _user(django_user_model, "cli")
    response = APIClient().post("/auth/login/", {"username": "cli", "password": "pw-12345678"})
    assert response.data["key"] and "omnipost_auth" not in response.cookies


def test_header_session_can_move_to_a_cookie(db, django_user_model):
    _user(django_user_model, "move")
    key = APIClient().post("/auth/login/", {"username": "move", "password": "pw-12345678"}).data["key"]
    client = APIClient()
    response = client.post("/auth/cookie/", HTTP_AUTHORIZATION=f"Token {key}", HTTP_X_OMNIPOST_CLIENT="web")
    assert response.status_code == 200 and response.cookies["omnipost_auth"].value == key
    assert client.get("/workspaces/", HTTP_X_OMNIPOST_CLIENT="web").status_code == 200


def test_cors_allows_the_client_header():
    from django.conf import settings

    assert "x-omnipost-client" in settings.CORS_ALLOW_HEADERS


def test_signup_leaves_no_django_session_behind(db, settings):
    settings.ACCOUNT_EMAIL_VERIFICATION = "none"
    client = APIClient()
    body = {"username": "new", "email": "new@example.com", "password1": "Strong-pw-9876", "password2": "Strong-pw-9876"}
    response = client.post("/auth/registration/", body, HTTP_X_OMNIPOST_CLIENT="web")
    assert response.status_code == 201 and response.cookies["omnipost_auth"].value
    assert "_auth_user_id" not in client.session
    # Signed out, an anonymous POST isn't held to session CSRF rules.
    client.post("/auth/logout/", HTTP_X_OMNIPOST_CLIENT="web")
    assert client.post("/auth/password/reset/", {"email": "new@example.com"}).status_code == 200
