"""IZN-B2 — rol uçlarının SÖZLEŞME davranışı: sayfa izinleri okuma/yazma, gizli alanlar, kopyalama,
rol silme kuralı, eski hücre yazma ucunun 410'u, `RoleResponse.user_count/is_locked`.

Kapı köprüsü ve parite testi AYRI dosyalardadır (bu dosya yalnız `roles` yüzeyini çakar).
"""

import uuid

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel
from app.core.sayfalar import ONAY_VAR_ANAHTARLARI, SAYFA_ANAHTARLARI, SAYFA_BY_KEY
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.roles import seed_data
from app.modules.roles.models import Role, RoleHiddenField, RolePagePermission
from app.modules.roles.schemas import RoleCreate
from app.modules.users.models import User

SIFRE = "parola1234"
ONAYSIZ_SAYFA = "genel.gosterge_paneli"  # has_approval=false
ONAYLI_SAYFA = "mali.hakedis_isveren"  # has_approval=true
VIEWER_GIZLI = sorted(c.value for c in seed_data.IZN_HIDDEN_FIELDS["viewer"])


@pytest.fixture
async def izn_db(seeded_db):
    await seed_data.seed_izn_reference_data(seeded_db)
    return seeded_db


async def _giris(client, user_factory, role_key: str) -> dict:
    email = f"{role_key}@izn-b2.co"
    await user_factory(email=email, password=SIFRE, role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _rol(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _denetim(session, action: AuditAction) -> list[str]:
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.occurred_at)
    return [row.detail for row in (await session.execute(stmt)).scalars().all()]


# ---------------------------------------------------------------------------
# GET /roles/{id}/pages
# ---------------------------------------------------------------------------


async def test_get_pages_sistem_yoneticisi_kilitli_ve_her_sayfa_duzenler(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "system_admin")
    cevap = await client.get(f"/roles/{rol.id}/pages", headers=admin)
    assert cevap.status_code == 200, cevap.text
    govde = cevap.json()
    assert govde["is_locked"] is True
    assert govde["hidden_fields"] == []
    assert list(govde["pages"]) == list(SAYFA_ANAHTARLARI) or set(govde["pages"]) == set(
        SAYFA_ANAHTARLARI
    )
    for key, hucre in govde["pages"].items():
        assert hucre == {"level": "edit", "approve": key in ONAY_VAR_ANAHTARLARI}, key


async def test_get_pages_sade_rol_100_sayfa_ve_gizli_alanlar_doner(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = (await client.get(f"/roles/{rol.id}/pages", headers=admin)).json()
    assert govde["is_locked"] is False
    assert len(govde["pages"]) == 100
    beklenen = {
        key: {"level": level.value, "approve": approve}
        for key, (level, approve) in seed_data.PAGE_MATRIX["viewer"].items()
    }
    assert govde["pages"] == beklenen
    assert govde["hidden_fields"] == VIEWER_GIZLI


async def test_get_pages_rol_yoksa_404_ve_yetkisiz_403(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    yok = await client.get("/roles/00000000-0000-0000-0000-000000000001/pages", headers=admin)
    assert yok.status_code == 404
    sef = await _giris(client, user_factory, "site_chief")
    rol = await _rol(izn_db, "viewer")
    assert (await client.get(f"/roles/{rol.id}/pages", headers=sef)).status_code == 403


# ---------------------------------------------------------------------------
# PUT /roles/{id}/pages  (TAM matris + hidden_fields, atomik)
# ---------------------------------------------------------------------------


async def _put(client, headers, rol_id, govde):
    return await client.put(f"/roles/{rol_id}/pages", json=govde, headers=headers)


async def _tam_govde(client, admin, rol_id, ovr=None, hidden=None) -> dict:
    """Rolün mevcut tam matrisini alır, `ovr` ile üstüne yazar (UI "Kaydet"i gibi 100 anahtar)."""
    mevcut = (await client.get(f"/roles/{rol_id}/pages", headers=admin)).json()
    return {
        "pages": {**mevcut["pages"], **(ovr or {})},
        "hidden_fields": mevcut["hidden_fields"] if hidden is None else hidden,
    }


async def test_put_pages_tam_matris_yazar_ve_denetler(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(
        client,
        admin,
        rol.id,
        {
            ONAYLI_SAYFA: {"level": "edit", "approve": True},
            ONAYSIZ_SAYFA: {"level": "none", "approve": False},
        },
    )
    cevap = await _put(client, admin, rol.id, govde)
    assert cevap.status_code == 200, cevap.text
    assert cevap.json()["pages"] == govde["pages"]
    assert cevap.json()["hidden_fields"] == VIEWER_GIZLI

    satirlar = await _denetim(izn_db, AuditAction.update)
    assert len(satirlar) == 1  # gizli alan değişmedi → ayrı satır yok
    assert satirlar[0].startswith("Sayfa izinleri değişti: Görüntüleyici (2 sayfa)")
    assert "Hakedişler › İşveren: Düzenler + Onaylar" in satirlar[0]


async def test_put_pages_ayni_matris_denetim_satiri_uretmez(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(client, admin, rol.id)
    assert (await _put(client, admin, rol.id, govde)).status_code == 200
    assert await _denetim(izn_db, AuditAction.update) == []


async def test_put_pages_hidden_fields_tam_degistirme_tekillestirir_ve_ayri_satir_yazar(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")  # başlangıç: tum_tutarlar
    govde = await _tam_govde(
        client, admin, rol.id, hidden=["maas_kisisel", "banka_kasa", "maas_kisisel"]
    )
    cevap = await _put(client, admin, rol.id, govde)
    assert cevap.status_code == 200, cevap.text
    assert cevap.json()["hidden_fields"] == ["banka_kasa", "maas_kisisel"]
    kalan = (
        await izn_db.execute(
            select(RoleHiddenField.category).where(RoleHiddenField.role_id == rol.id)
        )
    ).scalars()
    assert sorted(c.value for c in kalan) == ["banka_kasa", "maas_kisisel"]
    assert await _denetim(izn_db, AuditAction.update) == [
        "Gizli alanlar değişti: Görüntüleyici · gizli: Banka/kasa bakiyeleri, "
        "Maaş ve kişisel bilgiler"
    ]


async def test_put_pages_bos_hidden_fields_hepsini_acar(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(client, admin, rol.id, hidden=[])
    cevap = await _put(client, admin, rol.id, govde)
    assert cevap.json()["hidden_fields"] == []
    assert (await _denetim(izn_db, AuditAction.update))[0].endswith("gizli: hiçbiri")


async def test_put_pages_onay_eylemi_olmayan_sayfada_approve_422(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(
        client, admin, rol.id, {ONAYSIZ_SAYFA: {"level": "view", "approve": True}}
    )
    cevap = await _put(client, admin, rol.id, govde)
    assert cevap.status_code == 422
    assert cevap.json()["detail"] == f"{SAYFA_BY_KEY[ONAYSIZ_SAYFA].ad}: bu sayfada onay eylemi yok"
    assert await _denetim(izn_db, AuditAction.update) == []


async def test_put_pages_gormez_iken_approve_422(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(
        client, admin, rol.id, {ONAYLI_SAYFA: {"level": "none", "approve": True}}
    )
    cevap = await _put(client, admin, rol.id, govde)
    assert cevap.status_code == 422
    assert "Görmez düzeyindeki sayfada onay verilemez" in cevap.json()["detail"]


async def test_put_pages_atomik_ihlal_varsa_gecerli_hucre_ve_gizli_alan_da_yazilmaz(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    once = (await client.get(f"/roles/{rol.id}/pages", headers=admin)).json()
    govde = await _tam_govde(
        client,
        admin,
        rol.id,
        {
            ONAYLI_SAYFA: {"level": "edit", "approve": True},  # geçerli
            ONAYSIZ_SAYFA: {"level": "view", "approve": True},  # ihlal
        },
        hidden=[],
    )
    assert (await _put(client, admin, rol.id, govde)).status_code == 422
    assert (await client.get(f"/roles/{rol.id}/pages", headers=admin)).json() == once


async def test_put_pages_eksik_sayfa_422_bilinmeyen_anahtar_422_bos_govde_422(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(client, admin, rol.id)
    govde["pages"].pop(ONAYLI_SAYFA)
    eksik = await _put(client, admin, rol.id, govde)
    assert eksik.status_code == 422
    assert eksik.json()["detail"].startswith("Tüm sayfalar gönderilmeli: 1 sayfa eksik")
    assert SAYFA_BY_KEY[ONAYLI_SAYFA].ad in eksik.json()["detail"]

    fazla = await _tam_govde(client, admin, rol.id)
    fazla["pages"]["yok.boyle_sayfa"] = {"level": "view", "approve": False}
    assert (await _put(client, admin, rol.id, fazla)).status_code == 422

    kategori = await _tam_govde(client, admin, rol.id, hidden=["uydurma"])
    assert (await _put(client, admin, rol.id, kategori)).status_code == 422
    bos = await client.put(
        f"/roles/{rol.id}/pages", json={"pages": {}, "hidden_fields": []}, headers=admin
    )
    assert bos.status_code == 422


async def test_put_pages_sistem_yoneticisi_rolu_kilitli_403(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "system_admin")
    govde = await _tam_govde(client, admin, rol.id)
    cevap = await _put(client, admin, rol.id, govde)
    assert cevap.status_code == 403
    assert cevap.json()["detail"] == "Sistem Yöneticisi rolünün izinleri değiştirilemez"


async def test_put_pages_rol_yok_404_ve_yetkisiz_403(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    viewer = await _rol(izn_db, "viewer")
    govde = await _tam_govde(client, admin, viewer.id)
    yok = await client.put(
        "/roles/00000000-0000-0000-0000-000000000001/pages", json=govde, headers=admin
    )
    assert yok.status_code == 404
    pm = await _giris(client, user_factory, "project_manager")
    assert (await _put(client, pm, viewer.id, govde)).status_code == 403


# ---------------------------------------------------------------------------
# POST /roles/{id}/copy
# ---------------------------------------------------------------------------


async def test_copy_kaynagin_sayfa_hucrelerini_ve_gizli_alanlarini_kopyalar(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    kaynak = await _rol(izn_db, "viewer")
    cevap = await client.post(
        f"/roles/{kaynak.id}/copy",
        json={"name": "Görüntüleyici Kopya", "emoji": "👀", "description": "kopya"},
        headers=admin,
    )
    assert cevap.status_code == 201, cevap.text
    yeni = cevap.json()
    assert yeni["key"] == "goruntuleyici_kopya"
    assert yeni["is_system"] is False
    assert yeni["is_locked"] is False
    assert yeni["user_count"] == 0
    assert (yeni["name"], yeni["emoji"], yeni["description"]) == (
        "Görüntüleyici Kopya",
        "👀",
        "kopya",
    )
    a = (await client.get(f"/roles/{kaynak.id}/pages", headers=admin)).json()
    b = (await client.get(f"/roles/{yeni['id']}/pages", headers=admin)).json()
    assert a["pages"] == b["pages"]
    assert a["hidden_fields"] == b["hidden_fields"] == VIEWER_GIZLI
    assert await _denetim(izn_db, AuditAction.create) == [
        "Rol kopyalandı: Görüntüleyici → Görüntüleyici Kopya"
    ]


async def test_copy_sistem_yoneticisi_kopyasi_kilitsiz_her_sayfa_duzenler_gizli_alan_yok(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    kaynak = await _rol(izn_db, "system_admin")
    cevap = await client.post(
        f"/roles/{kaynak.id}/copy", json={"name": "Süper Kopya"}, headers=admin
    )
    assert cevap.status_code == 201, cevap.text
    yeni = cevap.json()
    assert yeni["is_locked"] is False
    assert yeni["is_system"] is False
    govde = (await client.get(f"/roles/{yeni['id']}/pages", headers=admin)).json()
    assert govde["hidden_fields"] == []
    for key, hucre in govde["pages"].items():
        assert hucre == {"level": "edit", "approve": key in ONAY_VAR_ANAHTARLARI}, key
    # Kopya SİLİNEBİLİR (kilit kopyalanmaz).
    assert (await client.delete(f"/roles/{yeni['id']}", headers=admin)).status_code == 204


async def test_copy_ayni_adla_iki_kez_anahtar_cakismasi_ek_alir(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    kaynak = await _rol(izn_db, "viewer")
    anahtarlar = []
    for _ in range(3):
        cevap = await client.post(
            f"/roles/{kaynak.id}/copy", json={"name": "Ikiz Rol"}, headers=admin
        )
        assert cevap.status_code == 201, cevap.text
        anahtarlar.append(cevap.json()["key"])
    assert anahtarlar == ["ikiz_rol", "ikiz_rol_2", "ikiz_rol_3"]


async def test_copy_rakamla_baslayan_ad_gecerli_anahtar_uretir(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    kaynak = await _rol(izn_db, "viewer")
    cevap = await client.post(
        f"/roles/{kaynak.id}/copy", json={"name": "2. Vardiya"}, headers=admin
    )
    assert cevap.status_code == 201
    assert cevap.json()["key"] == "rol_2_vardiya"


async def test_copy_kaynak_yok_404_bos_ad_422_yetkisiz_403(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    kaynak = await _rol(izn_db, "viewer")
    assert (
        await client.post(
            "/roles/00000000-0000-0000-0000-000000000001/copy", json={"name": "X"}, headers=admin
        )
    ).status_code == 404
    assert (
        await client.post(f"/roles/{kaynak.id}/copy", json={"name": ""}, headers=admin)
    ).status_code == 422
    pm = await _giris(client, user_factory, "project_manager")
    assert (
        await client.post(f"/roles/{kaynak.id}/copy", json={"name": "Y"}, headers=pm)
    ).status_code == 403


# ---------------------------------------------------------------------------
# DELETE /roles/{id} kuralı + RoleResponse.user_count/is_locked
# ---------------------------------------------------------------------------


async def test_rol_silme_sistem_yoneticisi_403_kullanicili_409_kullanicisiz_204(
    client, izn_db, user_factory
):
    admin = await _giris(client, user_factory, "system_admin")
    sysadmin = await _rol(izn_db, "system_admin")
    r403 = await client.delete(f"/roles/{sysadmin.id}", headers=admin)
    assert r403.status_code == 403
    assert r403.json()["detail"] == "Sistem Yöneticisi rolü silinemez"

    kullanicili = await _rol(izn_db, "system_admin")
    assert kullanicili.id == sysadmin.id  # admin kullanıcısı bu rolde: user_count >= 1
    sef = await _rol(izn_db, "site_chief")
    await user_factory(email="sef@izn-b2.co", password=SIFRE, role_key="site_chief")
    r409 = await client.delete(f"/roles/{sef.id}", headers=admin)
    assert r409.status_code == 409
    assert "atanmış kullanıcılar var" in r409.json()["detail"]

    ozel = await client.post("/roles", json={"key": "gecici", "name": "Geçici"}, headers=admin)
    assert (await client.delete(f"/roles/{ozel.json()['id']}", headers=admin)).status_code == 204


async def test_rol_silme_is_system_artik_kilit_degil_kullanicisiz_patron_silinir(
    client, izn_db, user_factory
):
    """KARARLAR 8e9a684: silinemeyen TEK rol Sistem Yöneticisi (Patron `is_system` olsa da)."""
    admin = await _giris(client, user_factory, "system_admin")
    patron = await _rol(izn_db, "patron")
    patron.is_system = True  # `is_system` kilit DEĞİL: True olsa da kullanıcısız Patron silinir
    await izn_db.flush()
    assert (await client.delete(f"/roles/{patron.id}", headers=admin)).status_code == 204


async def test_rol_listesi_user_count_ve_is_locked(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    await user_factory(email="p1@izn-b2.co", password=SIFRE, role_key="procurement")
    await user_factory(email="p2@izn-b2.co", password=SIFRE, role_key="procurement")
    roller = {r["key"]: r for r in (await client.get("/roles", headers=admin)).json()}
    assert roller["procurement"]["user_count"] == 2
    assert roller["system_admin"]["user_count"] == 1
    assert roller["viewer"]["user_count"] == 0
    assert roller["system_admin"]["is_locked"] is True
    assert all(r["is_locked"] is False for k, r in roller.items() if k != "system_admin")


# ---------------------------------------------------------------------------
# Eski hücre yazma ucu: 410
# ---------------------------------------------------------------------------


async def test_eski_hucre_yazma_ucu_410_ve_hicbir_sey_yazmaz(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "accounting")
    sayfalar_once = (await client.get(f"/roles/{rol.id}/pages", headers=admin)).json()
    cevap = await client.put(
        f"/roles/{rol.id}/permissions/inventory",
        json={"access_level": "view", "scope": "all"},
        headers=admin,
    )
    assert cevap.status_code == 410
    assert "Sayfa İzinleri" in cevap.json()["detail"]
    assert (await client.get(f"/roles/{rol.id}/pages", headers=admin)).json() == sayfalar_once
    assert await _denetim(izn_db, AuditAction.update) == []


async def test_eski_hucre_yazma_ucu_yetkisiz_403_oturumsuz_401(client, izn_db, user_factory):
    rol = await _rol(izn_db, "accounting")
    govde = {"access_level": "view", "scope": "all"}
    assert (
        await client.put(f"/roles/{rol.id}/permissions/inventory", json=govde)
    ).status_code == 401
    pm = await _giris(client, user_factory, "project_manager")
    assert (
        await client.put(f"/roles/{rol.id}/permissions/inventory", json=govde, headers=pm)
    ).status_code == 403


async def test_put_pages_tekrar_yazimi_yinelenen_satir_acmaz(client, izn_db, user_factory):
    """Her rolün 100 sayfa satırı vardır; PUT upsert yinelenen satır açmaz."""
    admin = await _giris(client, user_factory, "system_admin")
    rol = await _rol(izn_db, "viewer")
    govde = await _tam_govde(
        client, admin, rol.id, {ONAYLI_SAYFA: {"level": "edit", "approve": True}}
    )
    for _ in range(2):
        assert (await _put(client, admin, rol.id, govde)).status_code == 200
    stmt = select(RolePagePermission).where(RolePagePermission.role_id == rol.id)
    assert len((await izn_db.execute(stmt)).scalars().all()) == 100


# ---------------------------------------------------------------------------
# hidden_fields_effective (IZN-B4: yeni maske her rolde geçerli -> HER ZAMAN true)
# ---------------------------------------------------------------------------


async def test_hidden_fields_effective_her_rolde_true(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    muhasebe = await _rol(izn_db, "accounting")  # eski satırlı seed rol (eskiden false)
    viewer = await _rol(izn_db, "viewer")
    eski = (await client.get(f"/roles/{muhasebe.id}/pages", headers=admin)).json()
    yeni = (await client.get(f"/roles/{viewer.id}/pages", headers=admin)).json()
    assert eski["hidden_fields_effective"] is True
    assert yeni["hidden_fields_effective"] is True
    # PUT yanıtında da true.
    govde = {"pages": eski["pages"], "hidden_fields": ["maas_kisisel"]}
    put = await client.put(f"/roles/{muhasebe.id}/pages", json=govde, headers=admin)
    assert put.json()["hidden_fields_effective"] is True
    # Kopya rol da true.
    kopya = await client.post(
        f"/roles/{muhasebe.id}/copy", json={"name": "Muh Kopya"}, headers=admin
    )
    govde = (await client.get(f"/roles/{kopya.json()['id']}/pages", headers=admin)).json()
    assert govde["hidden_fields_effective"] is True


async def test_copy_eski_role_permissions_satirlarini_kopyalar_muhasebe_finance_maskesi_kalir(
    client, izn_db, user_factory
):
    """CEO onarımı: kopya donmuş eski `scope`u korur (Muhasebe `finance` maskesini kaybetmez)."""
    from app.core.access import Scope
    from app.modules.roles.models import Role
    from app.modules.roles.repository import derived_role_matrix

    async def actor_scope(session, user, modul):
        # IZN-B6a: `core.permissions.actor_scope` söküldü; aynı kuralı görüntü matrisi taşır.
        rol = await session.get(Role, user.role_id)
        matris = await derived_role_matrix(session, rol.id, rol.key)
        return next(scope for m, _lvl, scope in matris if m.key == modul)

    from app.modules.roles.models import RolePermission

    admin = await _giris(client, user_factory, "system_admin")
    muhasebe = await _rol(izn_db, "accounting")
    kopya = await client.post(
        f"/roles/{muhasebe.id}/copy", json={"name": "Muhasebe Kopya"}, headers=admin
    )
    assert kopya.status_code == 201, kopya.text
    kopya_id = kopya.json()["id"]
    kaynak_satirlar = {
        (r.module_id, r.access_level, r.scope)
        for r in (
            await izn_db.execute(
                select(RolePermission).where(RolePermission.role_id == muhasebe.id)
            )
        ).scalars()
    }
    kopya_satirlar = {
        (r.module_id, r.access_level, r.scope)
        for r in (
            await izn_db.execute(
                select(RolePermission).where(RolePermission.role_id == uuid.UUID(kopya_id))
            )
        ).scalars()
    }
    assert kopya_satirlar == kaynak_satirlar != set()
    # Kopya rolün kullanıcısı için kapsam: `finance` (donmuş satırdan), `all` DEĞİL.
    await user_factory(email="kopya@izn-b2.co", password=SIFRE, role_key="muhasebe_kopya")
    kullanici = (
        await izn_db.execute(select(User).where(User.email == "kopya@izn-b2.co"))
    ).scalar_one()
    assert await actor_scope(izn_db, kullanici, "boq") is Scope.finance


async def test_copy_sistem_yoneticisi_kopyasinda_legacy_admin_full_olur(
    client, izn_db, user_factory
):
    from app.modules.roles.models import RolePermission

    admin = await _giris(client, user_factory, "system_admin")
    kaynak = await _rol(izn_db, "system_admin")
    kopya = await client.post(
        f"/roles/{kaynak.id}/copy", json={"name": "Süper Kopya 2"}, headers=admin
    )
    seviyeler = {
        r.access_level
        for r in (
            await izn_db.execute(
                select(RolePermission).where(
                    RolePermission.role_id == uuid.UUID(kopya.json()["id"])
                )
            )
        ).scalars()
    }
    assert seviyeler == {AccessLevel.full}  # silme (admin) kopyalanmaz


async def test_sistem_yoneticisi_rolunu_yalniz_sistem_yoneticisi_atar_rol_yonetimi_sahibi_dahil(
    client, izn_db, user_factory
):
    from app.core.access import Scope
    from app.modules.roles.service import create_custom_role
    from tests._legacy_permission_yardimcisi import update_role_permission

    aktor_rol = await create_custom_role(
        izn_db, RoleCreate(key="rol_yoneticisi", name="Rol Yön", emoji="", description="")
    )
    # Rol Yönetimi Düzenler (eski user_management admin) + Kullanıcılar Düzenler.
    await update_role_permission(
        izn_db, aktor_rol.id, "user_management", AccessLevel.admin, Scope.all
    )
    aktor = await _giris(client, user_factory, "rol_yoneticisi")
    sysadmin_rol = await _rol(izn_db, "system_admin")
    cevap = await client.post(
        "/users",
        json={
            "email": "yeni-sa@izn-b2.co",
            "password": SIFRE,
            "full_name": "SA",
            "role_id": str(sysadmin_rol.id),
        },
        headers=aktor,
    )
    assert cevap.status_code == 403, cevap.text
    assert "Sistem Yöneticisi" in cevap.json()["detail"]
    # Sistem Yöneticisi atar.
    admin = await _giris(client, user_factory, "system_admin")
    ok = await client.post(
        "/users",
        json={
            "email": "yeni-sa2@izn-b2.co",
            "password": SIFRE,
            "full_name": "SA2",
            "role_id": str(sysadmin_rol.id),
        },
        headers=admin,
    )
    assert ok.status_code == 201, ok.text
