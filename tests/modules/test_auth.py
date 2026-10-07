import uuid
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import settings
from app.core.security import create_refresh_token
from app.main import app
from app.modules.auth import service as auth_service
from app.modules.users.models import UserStatus


async def test_login_returns_token_pair(client, seeded_db, user_factory):
    await user_factory(email="patron@fiil.com", password="dogru-parola", role_key="patron")

    response = await client.post(
        "/auth/login", json={"email": "patron@fiil.com", "password": "dogru-parola"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["refresh_token"]


async def test_login_with_wrong_password_is_rejected(client, seeded_db, user_factory):
    await user_factory(email="patron@fiil.com", password="dogru-parola", role_key="patron")

    response = await client.post(
        "/auth/login", json={"email": "patron@fiil.com", "password": "yanlis-parola"}
    )

    assert response.status_code == 401


async def test_login_with_unknown_email_is_rejected(client, seeded_db):
    response = await client.post(
        "/auth/login", json={"email": "yok@fiil.com", "password": "herhangi"}
    )
    assert response.status_code == 401


async def test_login_error_does_not_reveal_whether_email_exists(client, seeded_db, user_factory):
    """Kullanıcı sayımını engeller: iki hata da birebir aynı mesajı döndürmeli."""
    await user_factory(email="var@fiil.com", password="dogru-parola", role_key="patron")

    wrong_password = await client.post(
        "/auth/login", json={"email": "var@fiil.com", "password": "yanlis"}
    )
    unknown_email = await client.post(
        "/auth/login", json={"email": "yok@fiil.com", "password": "yanlis"}
    )

    assert wrong_password.status_code == unknown_email.status_code
    assert wrong_password.json() == unknown_email.json()


async def test_passive_user_cannot_log_in(client, seeded_db, user_factory):
    await user_factory(
        email="pasif@fiil.com", password="parola", role_key="patron", status="passive"
    )
    response = await client.post(
        "/auth/login", json={"email": "pasif@fiil.com", "password": "parola"}
    )
    assert response.status_code == 401


async def test_unknown_email_still_runs_password_verification(
    client, seeded_db, user_factory, monkeypatch
):
    """Zamanlama sizintisi korumasi: argon2 dogrulamasi hem bilinmeyen e-posta hem de
    bilinen-e-posta/yanlis-parola durumunda calismali — aksi halde suresi cok kisa olan
    yol (kullanici yok) saldirgana e-postanin var olup olmadigini sizdirir. Duvar saati
    zamanlamasi yerine `verify_password` cagrilarini sayiyoruz (flaky olmasin diye)."""
    await user_factory(email="var@fiil.com", password="dogru-parola", role_key="patron")

    calls: list[str] = []
    original_verify = auth_service.verify_password

    def spy_verify_password(plain: str, hashed: str) -> bool:
        calls.append(hashed)
        return original_verify(plain, hashed)

    monkeypatch.setattr(auth_service, "verify_password", spy_verify_password)

    await client.post("/auth/login", json={"email": "yok@fiil.com", "password": "herhangi"})
    assert len(calls) == 1, "Bilinmeyen e-posta icin argon2 dogrulamasi calismadi"

    await client.post("/auth/login", json={"email": "var@fiil.com", "password": "yanlis"})
    assert len(calls) == 2, "Bilinen e-posta/yanlis parola icin argon2 dogrulamasi calismadi"


async def test_me_returns_current_user(client, seeded_db, user_factory):
    await user_factory(email="patron@fiil.com", password="parola", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "patron@fiil.com", "password": "parola"}
    )
    token = login.json()["access_token"]

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 200
    assert response.json()["email"] == "patron@fiil.com"
    assert response.json()["role_key"] == "patron"


async def test_me_without_token_is_rejected(client, seeded_db):
    response = await client.get("/auth/me")
    assert response.status_code == 401


async def test_me_with_garbage_token_is_rejected(client, seeded_db):
    response = await client.get("/auth/me", headers={"Authorization": "Bearer cop-token"})
    assert response.status_code == 401


async def test_refresh_token_cannot_be_used_as_access_token(client, seeded_db, user_factory):
    """Token tipi karıştırılamaz."""
    await user_factory(email="patron@fiil.com", password="parola", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "patron@fiil.com", "password": "parola"}
    )
    refresh = login.json()["refresh_token"]

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {refresh}"})

    assert response.status_code == 401


async def test_refresh_issues_new_access_token(client, seeded_db, user_factory):
    await user_factory(email="patron@fiil.com", password="parola", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "patron@fiil.com", "password": "parola"}
    )

    response = await client.post(
        "/auth/refresh", json={"refresh_token": login.json()["refresh_token"]}
    )

    assert response.status_code == 200
    assert response.json()["access_token"]


async def test_refresh_for_passive_user_is_rejected(client, seeded_db, user_factory):
    """Pasife alinan bir kullanicinin eski refresh token'i yeni access token basmamali."""
    user = await user_factory(email="pasif-refresh@fiil.com", password="parola", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "pasif-refresh@fiil.com", "password": "parola"}
    )
    refresh_token = login.json()["refresh_token"]

    user.status = UserStatus.passive
    await seeded_db.flush()

    response = await client.post("/auth/refresh", json={"refresh_token": refresh_token})

    assert response.status_code == 401


async def test_refresh_for_nonexistent_user_is_rejected(client, seeded_db):
    """Silinmis/hic var olmamis bir kullanici icin gecerli imzali refresh token bile
    yeni token basmamali."""
    fake_refresh_token = create_refresh_token(uuid.uuid4(), 0)

    response = await client.post("/auth/refresh", json={"refresh_token": fake_refresh_token})

    assert response.status_code == 401


async def test_login_stamps_last_login_at(client, seeded_db, user_factory):
    user = await user_factory(email="patron@fiil.com", password="parola", role_key="patron")
    assert user.last_login_at is None

    await client.post("/auth/login", json={"email": "patron@fiil.com", "password": "parola"})

    await seeded_db.refresh(user)
    assert user.last_login_at is not None


async def test_me_status_wire_format_is_plain_string(client, seeded_db, user_factory):
    """MeResponse.status daha sıkı tiplense de kablo formatı (frontend'in beklediği)
    düz "active" string'i olmaya devam etmeli, "UserStatus.active" gibi bir şey değil."""
    await user_factory(email="patron2@fiil.com", password="parola", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "patron2@fiil.com", "password": "parola"}
    )
    token = login.json()["access_token"]

    response = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert response.json()["status"] == "active"


def test_me_response_status_schema_references_user_status_enum() -> None:
    schema = app.openapi()
    status_property = schema["components"]["schemas"]["MeResponse"]["properties"]["status"]

    ref_target = status_property.get("$ref") or status_property.get("allOf", [{}])[0].get("$ref")
    assert ref_target is not None
    assert ref_target.endswith("UserStatus")

    enum_schema = schema["components"]["schemas"]["UserStatus"]
    assert set(enum_schema["enum"]) == {"active", "on_leave", "passive"}


async def test_logout_revokes_existing_tokens(client, user_factory) -> None:
    await user_factory(email="logout@fiil.com", password="parola1234", role_key="patron")
    login = await client.post(
        "/auth/login", json={"email": "logout@fiil.com", "password": "parola1234"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await client.get("/auth/me", headers=headers)).status_code == 200
    assert (await client.post("/auth/logout", headers=headers)).status_code == 204
    # logout token_version'ı artırdı — o token'la basılan her şey artık geçersiz.
    assert (await client.get("/auth/me", headers=headers)).status_code == 401


# --- /auth/me sayfa hucreleri ---


async def _me(client, user_factory, role_key: str, email: str) -> dict:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    token = login.json()["access_token"]
    resp = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    return resp.json()


async def test_me_permissions_alani_sokuldu_sayfa_hucreleri_vardir(client, seeded_db, user_factory):
    """IZN-B6b: modül düzeyi harita (`permissions`) kalktı; yetki `pages` + `hidden_fields`tir."""
    body = await _me(client, user_factory, "site_chief", "sc@me-perms.co")

    assert "permissions" not in body
    assert body["pages"]["genel.projeler"]["level"] in {"view", "edit"}
    assert isinstance(body["hidden_fields"], list)


async def test_readonly_role_cannot_read_role_matrix_endpoint(client, seeded_db, user_factory):
    """BE-A'nin gerekcesi: mevcut matris ucu salt-okunur role kapali."""
    await user_factory(email="sc2@me-perms.co", password="parola1234", role_key="site_chief")
    login = await client.post(
        "/auth/login", json={"email": "sc2@me-perms.co", "password": "parola1234"}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    assert (await client.get("/roles", headers=headers)).status_code == 403


async def test_me_keeps_existing_fields(client, seeded_db, user_factory):
    """Alan EKLENIR; hicbir mevcut alan kaldirilmaz/yeniden adlandirilmaz."""
    body = await _me(client, user_factory, "patron", "patron@me-perms.co")

    assert set(body) >= {"id", "email", "full_name", "title", "role_key", "status"}
    assert body["email"] == "patron@me-perms.co"
    assert body["role_key"] == "patron"


def test_me_response_semasinda_permissions_alani_yok() -> None:
    schema = app.openapi()
    props = schema["components"]["schemas"]["MeResponse"]["properties"]
    assert "permissions" not in props
    assert {"pages", "hidden_fields"} <= set(props)


async def test_refresh_does_not_extend_absolute_session_lifetime(client, seeded_db, user_factory):
    """Refresh ucu, sunulan refresh token'in MUTLAK omrunu UZATMAMALI.

    Kusur (auth-refresh-rotasyon, kayit 25): `/auth/refresh` her cagrida
    `create_refresh_token` ile O ANDAN itibaren 30 gunluk YENI bir refresh token
    basiyordu (app/core/security.py:96-99) ve eskisini gecersizlestirmiyordu.
    Rotasyon/jti/kara liste olmadigi icin bu, calinmis bir refresh token'a SINIRSIZ
    omur veriyor: saldirgan her 29 gunde bir refresh cagirdikca saat sifirlaniyor,
    30 gunluk tavan hicbir zaman dolmuyordu.

    Burada 25 gun once basilmis (5 gun omru kalan) gercek bir refresh token sunuluyor;
    donen refresh token'in son kullanma zamani SUNULANDAN BUYUK OLMAMALI.
    """
    user = await user_factory(email="rotasyon@fiil.com", password="parola", role_key="patron")
    simdi = datetime.now(UTC)
    yirmi_bes_gun_once_basilan = jwt.encode(
        {
            "sub": str(user.id),
            "ver": user.token_version,
            "type": "refresh",
            "iat": simdi - timedelta(days=25),
            "exp": simdi + timedelta(days=5),
        },
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )

    response = await client.post(
        "/auth/refresh", json={"refresh_token": yirmi_bes_gun_once_basilan}
    )

    assert response.status_code == 200
    assert response.json()["access_token"], "Refresh yine de yeni access token basmali"

    def _exp(token: str) -> int:
        return jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
            options={"verify_exp": False},
        )["exp"]

    sunulan_exp = _exp(yirmi_bes_gun_once_basilan)
    donen_exp = _exp(response.json()["refresh_token"])
    assert donen_exp <= sunulan_exp, (
        "Refresh, oturumun mutlak omrunu uzatti: sunulan token "
        f"{sunulan_exp} icin geciyordu, donen token {donen_exp} icin geciyor "
        f"({donen_exp - sunulan_exp} saniye uzama). Calinan bir refresh token "
        "bu sayede sinirsiz yasar."
    )

    # Oturum surekliligi kirilmamali: donen refresh token hala kullanilabilir olmali.
    ikinci = await client.post(
        "/auth/refresh", json={"refresh_token": response.json()["refresh_token"]}
    )
    assert ikinci.status_code == 200
