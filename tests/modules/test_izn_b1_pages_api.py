"""IZN-B1 — `GET /pages` ve `/auth/me`nin EKLEYİCİ sayfa-izin alanları.

Kapsam: katalog ucu (yanıt biçimi, kapı), `/auth/me.pages|hidden_fields|is_system_admin`
(Sistem Yöneticisi, sıradan rol, yeni rol, özel rol, hücresiz rol, bayat anahtar), sorgu sayısı
bekçisi (N+1 yok), yeni rollerin atama kilidi, özel rol oluşturunca sayfa hücreleri ve
OpenAPI enum sözleşmesi.

Mevcut `permissions` haritası AYNEN kalır (frontend bugün onu okuyor) — burada da çakılır.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import delete, event, func, select

from app.core.access import AccessLevel, Scope
from app.core.errors import PermissionLockedError
from app.core.sayfalar import (
    GRUP_ADLARI,
    SAYFA_ANAHTARLARI,
    SAYFALAR,
    HiddenCategory,
    PageLevel,
)
from app.main import app
from app.modules.roles import seed_data
from app.modules.roles.models import (
    Module,
    Role,
    RoleHiddenField,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.schemas import RoleCreate
from app.modules.roles.service import create_custom_role
from tests._legacy_permission_yardimcisi import sync_page_cells, update_role_permission
from tests.conftest import test_engine

SIFRE = "parola1234"


@pytest.fixture
async def izn_db(seeded_db):
    """8 eski rol + 184 hücre (seed_reference_data) ÜSTÜNE 6 yeni rol + sayfa hücreleri."""
    await seed_data.seed_izn_reference_data(seeded_db)
    return seeded_db


async def _giris(client, user_factory, role_key: str, email: str | None = None) -> dict:
    email = email or f"{role_key}@izn-b1.co"
    await user_factory(email=email, password=SIFRE, role_key=role_key)
    login = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _me(client, headers) -> dict:
    cevap = await client.get("/auth/me", headers=headers)
    assert cevap.status_code == 200, cevap.text
    return cevap.json()


def _beklenen_pages(role_key: str) -> dict[str, dict]:
    return {
        key: {"level": level.value, "approve": approve}
        for key, (level, approve) in seed_data.PAGE_MATRIX[role_key].items()
    }


@contextmanager
def _sorgu_sayaci() -> Iterator[list[str]]:
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


# ---------------------------------------------------------------------------
# GET /pages
# ---------------------------------------------------------------------------


async def test_pages_oturumsuz_401(client, izn_db):
    assert (await client.get("/pages")).status_code == 401


async def test_pages_katalogu_menu_siralamasiyla_100_sayfa_doner(client, izn_db, user_factory):
    headers = await _giris(client, user_factory, "procurement")
    cevap = await client.get("/pages", headers=headers)
    assert cevap.status_code == 200
    govde = cevap.json()
    assert len(govde) == 100
    assert [p["key"] for p in govde] == list(SAYFA_ANAHTARLARI)
    ilk, son = govde[0], govde[-1]
    assert ilk == {
        "key": "genel.gosterge_paneli",
        "name": "Gösterge Paneli",
        "group": "genel",
        "group_name": "Genel",
        "subgroup": None,
        "route": "/",
        "kind": "sirket",
        "has_approval": False,
        "source": "dashboard",
        "twins": [],
    }
    assert son["key"] == "ayarlar.gelistirme"
    kok = next(p for p in govde if p["key"] == "mali.hakedis_isveren")
    assert kok["twins"] == [
        "proje.isveren_hakedis",
        "santiye.hakedisler",
        "bolum.hakedis",
    ]
    assert {p["group_name"] for p in govde} == set(GRUP_ADLARI.values())
    # `eski_modul` geçici köprü bilgisidir: API'ye ÇIKMAZ.
    assert all("eski_modul" not in p for p in govde)


async def test_pages_kapisi_yalniz_oturum_hicbir_modul_izni_istemez(client, izn_db, user_factory):
    """Hiçbir eski modül hücresi olmayan yeni rol de katalogu okur (menü anahtarları herkese)."""
    # Rol atama kilidi yüzünden kullanıcıyı doğrudan DB'de açıyoruz.
    headers = await _giris_yeni_rol(client, izn_db, "technical_office")
    cevap = await client.get("/pages", headers=headers)
    assert cevap.status_code == 200
    assert len(cevap.json()) == 100


async def _giris_yeni_rol(client, session, role_key: str) -> dict:
    from app.core.security import hash_password
    from app.modules.users.models import User, UserStatus

    role = (await session.execute(select(Role).where(Role.key == role_key))).scalar_one()
    email = f"{role_key}@izn-yeni.co"
    session.add(
        User(
            email=email,
            password_hash=hash_password(SIFRE),
            full_name="Yeni Rol",
            role_id=role.id,
            status=UserStatus.active,
        )
    )
    await session.flush()
    login = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


# ---------------------------------------------------------------------------
# /auth/me — yeni alanlar
# ---------------------------------------------------------------------------


async def test_me_sistem_yoneticisi_her_sayfada_duzenler_hucre_ve_bayrak_tasimaz(
    client, izn_db, user_factory
):
    me = await _me(client, await _giris(client, user_factory, "system_admin"))
    assert me["is_system_admin"] is True
    assert set(me["pages"]) == set(SAYFA_ANAHTARLARI)
    for sayfa in SAYFALAR:
        assert me["pages"][sayfa.key] == {"level": "edit", "approve": sayfa.onay_var}, sayfa.key
    assert me["hidden_fields"] == []
    # DB'de Sistem Yöneticisi için hücre YOKTUR (çözücü rol anahtarına bakar).
    sa_id = (await izn_db.execute(select(Role.id).where(Role.key == "system_admin"))).scalar_one()
    adet = await izn_db.scalar(
        select(func.count())
        .select_from(RolePagePermission)
        .where(RolePagePermission.role_id == sa_id)
    )
    assert adet == 0


async def test_me_sirada_rol_kendi_matrisinden_turetilmis_sayfalari_ve_bayragini_alir(
    client, izn_db, user_factory
):
    me = await _me(client, await _giris(client, user_factory, "site_chief"))
    assert me["is_system_admin"] is False
    assert me["pages"] == _beklenen_pages("site_chief")
    # Şef: madde 20 onaylı küme (B1'in `tum_tutarlar` türetmesi `izn_b4c` ile değişti); maaş AÇIK.
    assert me["hidden_fields"] == sorted(
        c.value for c in seed_data.ESKI_ROL_GIZLI_ALANLAR["site_chief"]
    )
    assert "maas_kisisel" not in me["hidden_fields"]
    # Şef günlüğe yazar ama muhasebeyi görmez.
    # "Yeniden aç" yalnız admin: şef günlüğü düzenler ama Onaylar'ı almaz (genişleme yok).
    assert me["pages"]["saha.gunluk_kayit"] == {"level": "edit", "approve": False}
    assert me["pages"]["mali.yevmiye"] == {"level": "none", "approve": False}
    # Eski harita AYNEN kalır.
    assert me["permissions"]["site_diary"] == "full"
    assert me["permissions"]["accounting"] == "none"


async def test_me_patron_ayarlar_sayfalarini_gormez_ve_gizli_alani_yok(
    client, izn_db, user_factory
):
    me = await _me(client, await _giris(client, user_factory, "patron"))
    assert me["pages"]["ayarlar.kullanicilar"]["level"] == "none"
    assert me["pages"]["mali.yevmiye"] == {"level": "edit", "approve": True}
    assert me["hidden_fields"] == []
    assert me["is_system_admin"] is False


@pytest.mark.parametrize("role_key", sorted(seed_data.IZN_ROLE_ORDER))
async def test_me_yeni_roller_kendi_baslangic_matrisini_alir(client, izn_db, role_key):
    headers = await _giris_yeni_rol(client, izn_db, role_key)
    me = await _me(client, headers)
    assert me["is_system_admin"] is False
    assert me["role_key"] == role_key
    assert me["pages"] == _beklenen_pages(role_key)
    assert me["hidden_fields"] == sorted(c.value for c in seed_data.HIDDEN_FIELDS[role_key])
    # IZN-B2: izin haritası SAYFA HÜCRELERİNDEN türetilir (kapı artık onları geçirir).
    assert set(me["permissions"].values()) - {"none"}
    if role_key == "viewer":
        assert set(me["permissions"].values()) <= {"none", "view"}


async def test_me_gorunteleyici_her_yeri_gorur_hicbir_yerde_duzenlemez(client, izn_db):
    me = await _me(client, await _giris_yeni_rol(client, izn_db, "viewer"))
    seviyeler = {g["level"] for g in me["pages"].values()}
    assert seviyeler <= {"none", "view"}
    assert not any(g["approve"] for g in me["pages"].values())
    assert me["pages"]["mali.yevmiye"]["level"] == "view"
    assert me["hidden_fields"] == ["maas_kisisel", "tum_tutarlar"]


async def test_me_ozel_rol_olusturulunca_100_gormez_hucre_alir(client, izn_db, user_factory):
    role = await create_custom_role(
        izn_db, RoleCreate(key="ozel_rol", name="Özel Rol", emoji="", description="")
    )
    adet = await izn_db.scalar(
        select(func.count())
        .select_from(RolePagePermission)
        .where(RolePagePermission.role_id == role.id)
    )
    assert adet == 100
    me = await _me(client, await _giris(client, user_factory, "ozel_rol"))
    assert set(me["pages"]) == set(SAYFA_ANAHTARLARI)
    assert {g["level"] for g in me["pages"].values()} == {"none"}
    assert me["hidden_fields"] == []


async def test_me_ozel_rolun_ekrandan_degistirilmis_hucresi_ve_bayragi_yanita_yansir(
    client, izn_db, user_factory
):
    role = await create_custom_role(
        izn_db, RoleCreate(key="ozel_rol2", name="Özel Rol 2", emoji="", description="")
    )
    hucre = await izn_db.get(RolePagePermission, (role.id, "mali.yevmiye"))
    hucre.level = PageLevel.edit
    hucre.can_approve = True
    izn_db.add(RoleHiddenField(role_id=role.id, category=HiddenCategory.banka_kasa))
    await izn_db.flush()

    me = await _me(client, await _giris(client, user_factory, "ozel_rol2"))
    assert me["pages"]["mali.yevmiye"] == {"level": "edit", "approve": True}
    assert me["pages"]["mali.mizan"] == {"level": "none", "approve": False}
    assert me["hidden_fields"] == ["banka_kasa"]


async def test_me_hucresi_olmayan_rol_bos_harita_alir_bilinmezlik_kurali(
    client, seeded_db, user_factory
):
    """Sayfa hücresi hiç yoksa `pages` boştur (FE: bilinmez = görünür; sınır backend'dedir)."""
    muhasebe = await _rol(seeded_db, "accounting")
    await seeded_db.execute(
        delete(RolePagePermission).where(RolePagePermission.role_id == muhasebe.id)
    )
    await seeded_db.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == muhasebe.id))
    await seeded_db.flush()
    me = await _me(client, await _giris(client, user_factory, "accounting"))
    assert me["pages"] == {}
    assert me["hidden_fields"] == []
    # IZN-B2: kapılar hücreden karar verir → hücresiz rol HER modülde `none` (fail-closed).
    assert me["permissions"]["accounting"] == "none"


async def test_me_katalogda_olmayan_bayat_anahtar_yanita_girmez(client, izn_db, user_factory):
    muhasebe = (await izn_db.execute(select(Role).where(Role.key == "accounting"))).scalar_one()
    izn_db.add(
        RolePagePermission(
            role_id=muhasebe.id,
            page_key="kaldirilmis.sayfa",
            level=PageLevel.view,
            can_approve=False,
        )
    )
    await izn_db.flush()
    me = await _me(client, await _giris(client, user_factory, "accounting"))
    assert "kaldirilmis.sayfa" not in me["pages"]
    assert set(me["pages"]) == set(SAYFA_ANAHTARLARI)


async def test_me_pages_ROLDEN_gelir_sabit_degerden_degil(client, izn_db, user_factory):
    """İki farklı rol AYNI sayfada FARKLI hücre döner (sabit değerle doldurma yakalanır)."""
    sef = await _me(client, await _giris(client, user_factory, "site_chief"))
    muhasebe = await _me(client, await _giris(client, user_factory, "accounting"))
    assert sef["pages"] != muhasebe["pages"]
    assert sef["pages"]["mali.yevmiye"] != muhasebe["pages"]["mali.yevmiye"]
    assert sef["pages"] == _beklenen_pages("site_chief")
    assert muhasebe["pages"] == _beklenen_pages("accounting")


async def test_me_permissions_haritasi_sayfa_hucrelerinden_turetilmis_eskiyle_ayni(
    client, izn_db, user_factory
):
    """IZN-B2: `permissions` salt-okur TÜRETİLMİŞ görünümdür. Eski harita ile tek fark: sayfa
    hücresinde ayrışmayan ara düzey (Proje Müdürü `dashboard`: eski `full`, görünen `view`)."""
    me = await _me(client, await _giris(client, user_factory, "project_manager"))
    beklenen = {
        module_key: cells[seed_data.ROLE_ORDER.index("project_manager")][0].value
        for module_key, cells in seed_data.MATRIX.items()
    }
    beklenen["dashboard"] = "view"
    assert me["permissions"] == beklenen


# ---------------------------------------------------------------------------
# Sorgu sayısı: N+1 YOK
# ---------------------------------------------------------------------------


async def test_me_sorgu_sayisi_hucre_sayisindan_BAGIMSIZ(client, izn_db, user_factory):
    """AYNI kullanıcı: 100 hücreyle de hücresiz de sayfa verisi için SABİT sayıda sorgu."""
    sef = await _giris(client, user_factory, "site_chief")
    admin = await _giris(client, user_factory, "system_admin")

    def _sayfa_sorgulari(ifadeler: list[str]) -> list[str]:
        return [s for s in ifadeler if "role_page_permissions" in s or "role_hidden_fields" in s]

    # Isınma: girişin ertelenmiş yazmaları (last_login_at/denetim) ilk isteğin sayacına karışmasın.
    assert (await client.get("/auth/me", headers=sef)).status_code == 200
    assert (await client.get("/auth/me", headers=admin)).status_code == 200

    with _sorgu_sayaci() as dolu:
        assert (await client.get("/auth/me", headers=sef)).status_code == 200

    sef_rol = (await izn_db.execute(select(Role).where(Role.key == "site_chief"))).scalar_one()
    await izn_db.execute(delete(RolePagePermission).where(RolePagePermission.role_id == sef_rol.id))
    await izn_db.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == sef_rol.id))
    await izn_db.flush()
    with _sorgu_sayaci() as bos:
        assert (await client.get("/auth/me", headers=sef)).status_code == 200

    with _sorgu_sayaci() as yonetici:
        assert (await client.get("/auth/me", headers=admin)).status_code == 200

    # Sayfa verisi: SABİT üç sorgu (hücreler + gizli alanlar + alan maskesi özeti), hücre
    # sayısından bağımsız (türetilmiş `permissions` hücreleri YENİDEN okumaz).
    assert len(_sayfa_sorgulari(dolu)) == 3
    assert len(_sayfa_sorgulari(bos)) == 3
    # Toplam sorgu sayısı hücre sayısına bağlı DEĞİL (satır başına sorgu yok).
    assert len(dolu) == len(bos)
    # Sistem Yöneticisi katalogdan türer: sayfa tablolarına HİÇ sormaz.
    assert _sayfa_sorgulari(yonetici) == []


# ---------------------------------------------------------------------------
# Yeni roller ARTIK atanabilir (B1 atama kilidi IZN-B2'de kalktı)
# ---------------------------------------------------------------------------


async def test_yeni_rol_kullaniciya_atanabilir_post_users(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    rol = (await izn_db.execute(select(Role).where(Role.key == "viewer"))).scalar_one()
    cevap = await client.post(
        "/users",
        json={
            "email": "yeni-viewer@izn.co",
            "password": SIFRE,
            "full_name": "V",
            "role_id": str(rol.id),
        },
        headers=admin,
    )
    assert cevap.status_code == 201, cevap.text


async def test_yeni_rol_kullaniciya_atanabilir_patch_users(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    hedef = await user_factory(email="hedef@izn.co", password=SIFRE, role_key="site_chief")
    rol = (await izn_db.execute(select(Role).where(Role.key == "finance_manager"))).scalar_one()
    cevap = await client.patch(f"/users/{hedef.id}", json={"role_id": str(rol.id)}, headers=admin)
    assert cevap.status_code == 200, cevap.text
    await izn_db.refresh(hedef)
    assert hedef.role_id == rol.id


# ---------------------------------------------------------------------------
# Seed: DB anahtarları = katalog; idempotent
# ---------------------------------------------------------------------------


async def test_seed_DB_anahtar_kumesi_katalogla_birebir_ve_idempotent(izn_db):
    adet = await izn_db.scalar(select(func.count()).select_from(RolePagePermission))
    assert adet == 13 * 100
    anahtarlar = set(
        (await izn_db.execute(select(RolePagePermission.page_key).distinct())).scalars()
    )
    assert anahtarlar == set(SAYFA_ANAHTARLARI)

    await seed_data.seed_izn_reference_data(izn_db)  # ikinci çağrı: değişmez
    assert await izn_db.scalar(select(func.count()).select_from(RolePagePermission)) == adet
    assert await izn_db.scalar(select(func.count()).select_from(Role)) == 14
    # Eski tablo DOKUNULMADI.
    assert await izn_db.scalar(select(func.count()).select_from(RolePermission)) == 184


# ---------------------------------------------------------------------------
# OpenAPI sözleşmesi
# ---------------------------------------------------------------------------


def test_openapi_page_key_bir_ENUM_ve_katalogla_ayni_100_deger():
    sema = app.openapi()["components"]["schemas"]
    assert set(sema["PageKey"]["enum"]) == set(SAYFA_ANAHTARLARI)
    assert set(sema["PageLevel"]["enum"]) == {"none", "view", "edit"}
    assert set(sema["HiddenCategory"]["enum"]) == {c.value for c in HiddenCategory}
    pages = sema["MeResponse"]["properties"]["pages"]
    assert pages["propertyNames"]["$ref"].endswith("PageKey")
    assert pages["additionalProperties"]["$ref"].endswith("PageGrant")
    assert sema["PageResponse"]["properties"]["key"]["$ref"].endswith("PageKey")
    assert "is_system_admin" in sema["MeResponse"]["required"]
    assert {"pages", "hidden_fields"} <= set(sema["MeResponse"]["required"])


# ---------------------------------------------------------------------------
# WRITE-THROUGH: eski hücre yazılınca sayfa hücreleri ve `tum_tutarlar` aynı transaction'da türer
# ---------------------------------------------------------------------------


async def _rol(session, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _sayfa(session, role: Role, page_key: str) -> tuple[PageLevel, bool]:
    await session.refresh(await session.get(RolePagePermission, (role.id, page_key)))
    hucre = await session.get(RolePagePermission, (role.id, page_key))
    return hucre.level, hucre.can_approve


async def _gizli(session, role: Role) -> set[HiddenCategory]:
    rows = await session.execute(
        select(RoleHiddenField.category).where(RoleHiddenField.role_id == role.id)
    )
    return set(rows.scalars())


async def test_write_through_hucreyi_DARALT_sayfa_hucresi_daralir(izn_db):
    muhasebe = await _rol(izn_db, "accounting")
    assert await _sayfa(izn_db, muhasebe, "mali.yevmiye") == (
        PageLevel.edit,
        True,
    )  # accounting full
    await update_role_permission(izn_db, muhasebe.id, "accounting", AccessLevel.view, Scope.all)
    assert await _sayfa(izn_db, muhasebe, "mali.yevmiye") == (PageLevel.view, False)
    assert await _sayfa(izn_db, muhasebe, "mali.mizan") == (PageLevel.view, False)
    assert await _sayfa(izn_db, muhasebe, "mali.hesap_plani") == (PageLevel.view, False)
    await update_role_permission(izn_db, muhasebe.id, "accounting", AccessLevel.none, Scope.all)
    assert await _sayfa(izn_db, muhasebe, "mali.mizan") == (PageLevel.none, False)
    # Aynı modülden türemeyen sayfalar DEĞİŞMEZ.
    assert await _sayfa(izn_db, muhasebe, "mali.fatura") == (PageLevel.edit, True)


async def test_write_through_hucreyi_GENISLET_sayfa_hucresi_genisler(izn_db):
    sef = await _rol(izn_db, "site_chief")
    assert await _sayfa(izn_db, sef, "mali.yevmiye") == (PageLevel.none, False)
    await update_role_permission(izn_db, sef.id, "accounting", AccessLevel.draft, Scope.all)
    assert await _sayfa(izn_db, sef, "mali.yevmiye") == (PageLevel.view, False)  # eşik full
    await update_role_permission(izn_db, sef.id, "accounting", AccessLevel.full, Scope.all)
    assert await _sayfa(izn_db, sef, "mali.yevmiye") == (PageLevel.edit, True)
    assert await _sayfa(izn_db, sef, "mali.mizan") == (PageLevel.view, False)


async def test_write_through_CAPRAZ_modul_esigi_projects_admin(izn_db):
    """Teklif "Dönüştür" Onaylar'ı = contracts full VE projects admin; iki modül de izlenir."""
    pm = await _rol(izn_db, "project_manager")
    assert await _sayfa(izn_db, pm, "teklif.teklif_hazirlama") == (PageLevel.edit, False)
    await update_role_permission(izn_db, pm.id, "projects", AccessLevel.admin, Scope.all)
    assert await _sayfa(izn_db, pm, "teklif.teklif_hazirlama") == (PageLevel.edit, True)
    assert await _sayfa(izn_db, pm, "genel.projeler") == (PageLevel.edit, False)  # proje oluştur
    await update_role_permission(izn_db, pm.id, "contracts", AccessLevel.view, Scope.all)
    assert await _sayfa(izn_db, pm, "teklif.teklif_hazirlama") == (PageLevel.view, False)


async def _eski_hucre(izn_db, rol, modul: str, level: AccessLevel, scope: Scope) -> None:
    """Eski matris hücresini DOĞRUDAN yazar + write-through türetimini koşar.

    IZN-B4: `update_role_permission` artık `limited`i reddeder (hiçbir modül eski köprüyü
    taşımıyor: `kablolu_moduller()` boş). Türetim (`sync_page_cells`) hâlâ eski satırlardan
    `tum_tutarlar`ı çıkarır; B6'da bu yol söküldüğünde test birlikte silinir.
    """
    izin = (
        await izn_db.execute(
            select(RolePermission)
            .join(Module, Module.id == RolePermission.module_id)
            .where(RolePermission.role_id == rol.id, Module.key == modul)
        )
    ).scalar_one()
    izin.access_level = level
    izin.scope = scope
    await izn_db.flush()
    await sync_page_cells(izn_db, rol.id)


async def test_write_through_limited_ac_kapa_tum_tutarlar_bayragi(izn_db):
    muhasebe = await _rol(izn_db, "accounting")
    assert await _gizli(izn_db, muhasebe) == set()
    await _eski_hucre(izn_db, muhasebe, "dashboard", AccessLevel.view, Scope.limited)
    assert await _gizli(izn_db, muhasebe) == {HiddenCategory.tum_tutarlar}
    await _eski_hucre(izn_db, muhasebe, "dashboard", AccessLevel.view, Scope.all)
    assert await _gizli(izn_db, muhasebe) == set()
    # Şef üç `limited` hücreye sahip: birini kapatmak bayrağı DÜŞÜRMEZ, sonuncusu düşürür.
    # Başlangıç kümesi madde 20'nin onaylı kümesidir (`tum_tutarlar` YOK); write-through yalnız
    # `tum_tutarlar` bayrağını açar/kapatır, onaylı kategorilere dokunmaz.
    sef = await _rol(izn_db, "site_chief")
    onayli = set(seed_data.ESKI_ROL_GIZLI_ALANLAR["site_chief"])
    assert await _gizli(izn_db, sef) == onayli
    await _eski_hucre(izn_db, sef, "dashboard", AccessLevel.view, Scope.limited)
    assert await _gizli(izn_db, sef) == onayli | {HiddenCategory.tum_tutarlar}
    for modul in ("dashboard", "projects", "sites"):
        await _eski_hucre(izn_db, sef, modul, AccessLevel.view, Scope.all)
    assert HiddenCategory.tum_tutarlar in await _gizli(izn_db, sef)  # boq hâlâ limited
    await _eski_hucre(izn_db, sef, "boq", AccessLevel.view, Scope.all)
    assert await _gizli(izn_db, sef) == onayli


async def test_write_through_me_yanitina_ve_endpointe_yansir(client, izn_db, user_factory):
    sef_headers = await _giris(client, user_factory, "site_chief")
    sef = await _rol(izn_db, "site_chief")
    await update_role_permission(izn_db, sef.id, "accounting", AccessLevel.full, Scope.all)
    me = await _me(client, sef_headers)
    assert me["pages"]["mali.yevmiye"] == {"level": "edit", "approve": True}
    assert me["permissions"]["accounting"] == "full"  # eski harita da aynı yönde


async def test_write_through_hucresi_olmayan_rolde_100_hucreyi_kurar(
    client, seeded_db, user_factory
):
    """Sayfa hücresi hiç yokken (migration öncesi/elle satırsız) ilk yazma 100 hücreyi türetir."""
    muhasebe = await _rol(seeded_db, "accounting")
    await seeded_db.execute(
        delete(RolePagePermission).where(RolePagePermission.role_id == muhasebe.id)
    )
    await seeded_db.flush()
    await update_role_permission(seeded_db, muhasebe.id, "treasury", AccessLevel.view, Scope.all)
    adet = await seeded_db.scalar(
        select(func.count())
        .select_from(RolePagePermission)
        .where(RolePagePermission.role_id == muhasebe.id)
    )
    assert adet == 100
    assert await _sayfa(seeded_db, muhasebe, "mali.hazine") == (PageLevel.view, False)


# ---------------------------------------------------------------------------
# Yeni rollerde eski hücre yazımı REDDEDİLİR (atama kilidini aşmasın)
# ---------------------------------------------------------------------------


async def test_yeni_rolde_servis_dogrudan_PermissionLockedError(izn_db):
    rol = await _rol(izn_db, "viewer")
    with pytest.raises(PermissionLockedError, match="yeni Sayfa İzinleri ekranından"):
        await update_role_permission(izn_db, rol.id, "inventory", AccessLevel.view, Scope.all)


# ---------------------------------------------------------------------------
# GET /roles.is_assignable
# ---------------------------------------------------------------------------


async def test_roles_is_assignable_HER_rol_true_atama_kilidi_kalkti(client, izn_db, user_factory):
    await create_custom_role(
        izn_db, RoleCreate(key="ozel_a", name="Özel A", emoji="", description="")
    )
    admin = await _giris(client, user_factory, "system_admin")
    cevap = await client.get("/roles", headers=admin)
    assert cevap.status_code == 200
    roller = {r["key"]: r for r in cevap.json()}
    assert len(roller) == 15  # 8 eski + 6 yeni + 1 özel
    assert all(r["is_assignable"] is True for r in roller.values())


async def test_roles_post_ve_patch_yanitinda_is_assignable_var(client, izn_db, user_factory):
    admin = await _giris(client, user_factory, "system_admin")
    olustur = await client.post(
        "/roles", json={"key": "yeni_ozel", "name": "Yeni Özel"}, headers=admin
    )
    assert olustur.status_code == 201
    assert olustur.json()["is_assignable"] is True
    yeniden = await client.patch(
        f"/roles/{olustur.json()['id']}", json={"name": "Başka Ad"}, headers=admin
    )
    assert yeniden.json()["is_assignable"] is True
