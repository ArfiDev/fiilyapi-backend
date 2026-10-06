"""IZN-B5d — gösterge paneli kartları SAYFA KÜMESİYLE kapılı (madde 17, CEO kararları).

Tablo UYGULAMADAN BAĞIMSIZ elle yazıldı (IZN-B5d-ESLEME.md §17 + CEO kararları). Kart başına TEK
hücreli (yalnız o sayfada Görür + paneli açan `genel.gosterge_paneli`) özel rol kurulur ve
`GET /dashboard/summary` okunur: kart açıksa dolu, kapalıysa `restricted`. Komşu sayfa (ör.
taşeron hakediş sayfası → Portföy) artık kartı AÇMAZ. `module`/`pending_module` dizeleri aynen.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select, update

from app.core.permissions import (
    can_read,
    can_read_pages,
    can_read_pages_projects,
    can_read_projects,
)
from app.core.sayfalar import PageLevel
from app.modules.dashboard import kart_sayfalari
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.seed_data import seed_izn_reference_data
from app.modules.users.models import ProjectMember
from tests._ekip_dunyasi import rol_kur
from tests.modules import _ilr

pytestmark = pytest.mark.asyncio

SIFRE = "parola1234"
PANEL = "genel.gosterge_paneli"

# --- elle yazılmış kart kümeleri ------------------------------------------------------------
K1_PROJE = ("genel.projeler", "proje.ozet")
HI = ("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler")
HT = ("mali.hakedis_taseron", "proje.taseron_hakedis")
K2_PORTFOY = HI
K3_STOK = ("stok.stok_depo", "santiye.stok", "bolum.malzeme")
K4_GECIKME = (*HT, "santiye.hakedisler")
K5_TAKVIM = ("santiye.bolumler", "bolum.detay")

# Eski modül kümeleri (komşu sayfalar dahil): hepsi tek hücreyle denenir.
ESKI_PV = (
    "genel.projeler",
    "genel.proje_takvimi",
    "mali.satis_blok",
    "mali.satis_unite",
    "mali.satis_excel",
    "mali.satis_paylasim",
    "proje.ozet",
    "proje.paylasim_tablosu",
)
ESKI_PPV = (*HI, *HT, "bolum.hakedis")
HAVUZ = sorted({*ESKI_PV, *ESKI_PPV, *K3_STOK, *K5_TAKVIM, "mali.hesap_plani"})


async def _oturum(client, session, user_factory, key: str, sayfalar: tuple[str, ...]):
    """Yalnız `sayfalar` (+ panel) Görür olan özel rol; kullanıcı TÜM projelere görünür."""
    rol = await rol_kur(session, key, PageLevel.none)
    await session.execute(
        update(RolePagePermission)
        .where(
            RolePagePermission.role_id == rol.id,
            RolePagePermission.page_key.in_((PANEL, *sayfalar)),
        )
        .values(level=PageLevel.view)
    )
    await session.flush()
    email = f"{key}@b5d.co"
    kullanici = await user_factory(email=email, password=SIFRE, role_key=key)
    kullanici.all_projects = True
    await session.flush()
    yanit = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    assert yanit.status_code == 200, yanit.text
    return kullanici, {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def _panel(client, basliklar) -> dict:
    yanit = await client.get("/dashboard/summary", headers=basliklar)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


def _kaynak(govde: dict, modul: str) -> str:
    return next(s["state"] for s in govde["risks"]["sources"] if s["module"] == modul)


def _portfoy_acik(govde: dict) -> bool:
    return govde["portfolio"]["available"] is True


async def test_sabit_kumeler_elle_yazilanla_birebir() -> None:
    assert set(kart_sayfalari.PROJE_KARTLARI) == set(K1_PROJE)
    assert set(kart_sayfalari.PORTFOY) == set(K2_PORTFOY)
    assert set(kart_sayfalari.RISK_STOK) == set(K3_STOK)
    assert set(kart_sayfalari.RISK_GECIKME) == set(K4_GECIKME)
    assert set(kart_sayfalari.RISK_TAKVIM) == set(K5_TAKVIM)


async def test_hakedis_aileleri_router_sabitleriyle_esit() -> None:
    from app.modules.progress_payments.router import _HAKEDIS_ISVEREN
    from app.modules.subcontractor_progress_payments.router_transitions import HAKEDIS_TASERON

    assert tuple(kart_sayfalari.HAKEDIS_ISVEREN) == tuple(_HAKEDIS_ISVEREN)
    assert tuple(kart_sayfalari.HAKEDIS_TASERON) == tuple(HAKEDIS_TASERON)


@pytest.mark.parametrize("sayfa", HAVUZ)
async def test_tek_hucreli_rolde_kart_yalniz_dogru_sayfayla_acilir(
    client, db_session, user_factory, project_factory, sayfa
):
    """Her kart için: tek Görür hücresi kartı açar ⇔ sayfa kartın kümesinde."""
    await project_factory(code=f"B5D-{abs(hash(sayfa)) % 10**6}", project_type="taahhut")
    _, basliklar = await _oturum(
        client, db_session, user_factory, "b5d_" + sayfa.replace(".", "_"), (sayfa,)
    )

    govde = await _panel(client, basliklar)

    assert bool(govde["projects"]) is (sayfa in K1_PROJE), "K1 proje kartları"
    assert _portfoy_acik(govde) is (sayfa in K2_PORTFOY), "K2 portföy"
    assert (_kaynak(govde, "inventory") == "ok") is (sayfa in K3_STOK), "K3 stok"
    assert (_kaynak(govde, "progress_payments") == "ok") is (sayfa in K4_GECIKME), "K4 gecikme"
    assert (_kaynak(govde, "sites") == "ok") is (sayfa in K5_TAKVIM), "K5 takvim"
    if sayfa not in K2_PORTFOY:
        assert govde["portfolio"]["pending_module"] is None  # restricted() zarfı


async def test_uc_panel_sayfasini_ister(client, db_session, user_factory) -> None:
    """K0: panel sayfası yok → 403 (kart sayfaları tek başına ucu açmaz); var → 200."""
    rol = await rol_kur(db_session, "b5d_k0", PageLevel.none)
    await db_session.execute(
        update(RolePagePermission)
        .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key.in_(HI))
        .values(level=PageLevel.view)
    )
    await db_session.flush()
    await user_factory(email="k0@b5d.co", password=SIFRE, role_key="b5d_k0")
    giris = await client.post("/auth/login", json={"email": "k0@b5d.co", "password": SIFRE})
    basliklar = {"Authorization": f"Bearer {giris.json()['access_token']}"}

    yanit = await client.get("/dashboard/summary", headers=basliklar)

    assert yanit.status_code == 403


async def test_proje_basina_karar_ekip_rolu_yalniz_kendi_projesinde_kart_acar(
    client, db_session, user_factory, project_factory
) -> None:
    """Proje başına davranış: A'da proje.ozet + işveren hakediş (proje içi) → A kartı ve A
    hasılatı; B'de yalnız proje.taseron_hakedis → B kartı ve B hasılatı YOK."""
    yazan = await _ilr.aktor(db_session, user_factory, "yazan@b5d.co")
    proje_a = await project_factory(code="B5D-A", project_type="taahhut")
    proje_b = await project_factory(code="B5D-B", project_type="taahhut")
    for proje, miktar, kod in ((proje_a, "300", "15.150.1002"), (proje_b, "900", "15.150.2003")):
        site = await _ilr.santiye(db_session, proje, code=f"S-{proje.code}")
        kalem = await _ilr.isveren_kalemi(
            db_session, proje, quantity="10000", unit_price="1000.00", code=kod
        )
        await _ilr.isveren_hakedisi(
            db_session,
            proje,
            yazan,
            kalem,
            site,
            quantity=miktar,
            status=ProgressPaymentStatus.approved,
        )
    ana = await rol_kur(db_session, "b5d_ana", PageLevel.none)
    await db_session.execute(
        update(RolePagePermission)
        .where(RolePagePermission.role_id == ana.id, RolePagePermission.page_key == PANEL)
        .values(level=PageLevel.view)
    )
    rol_a = await rol_kur(db_session, "b5d_rol_a", PageLevel.none)
    rol_b = await rol_kur(db_session, "b5d_rol_b", PageLevel.none)
    for rol, sayfalar in (
        (rol_a, ("proje.ozet", "proje.isveren_hakedis")),
        (rol_b, ("proje.taseron_hakedis", "mali.satis_blok")),
    ):
        await db_session.execute(
            update(RolePagePermission)
            .where(
                RolePagePermission.role_id == rol.id,
                RolePagePermission.page_key.in_(sayfalar),
            )
            .values(level=PageLevel.view)
        )
    kullanici = await user_factory(email="ekip@b5d.co", password=SIFRE, role_key="b5d_ana")
    for proje, rol in ((proje_a, rol_a), (proje_b, rol_b)):
        db_session.add(ProjectMember(user_id=kullanici.id, project_id=proje.id, role_id=rol.id))
    await db_session.flush()
    giris = await client.post("/auth/login", json={"email": "ekip@b5d.co", "password": SIFRE})
    basliklar = {"Authorization": f"Bearer {giris.json()['access_token']}"}

    govde = await _panel(client, basliklar)

    assert [k["id"] for k in govde["projects"]] == [str(proje_a.id)]
    # 300 x 1000.00: yalnız A'nın hasılatı (B'ninki 900 x 1000.00 toplama GİRMEZ)
    assert govde["portfolio"]["available"] is True
    assert govde["portfolio"]["value"] == "300000.00"


async def test_seed_13_rolde_kart_basina_eski_modul_karari_ile_yeni_sayfa_karari_ayni(
    seeded_db, user_factory, project_factory
) -> None:
    """GENİŞLEME = 0 / daralma = 0 (seed): 13 seed rolde, kart başına ana / ekip / projesiz dalda
    GERÇEK fonksiyonlarla eski modül okuması (`can_read_projects` / `can_read`) = yeni sayfa
    kümesi okuması (`can_read_pages_projects` / `can_read_pages`)."""
    await seed_izn_reference_data(seeded_db)
    kartlar = (
        ("projects", kart_sayfalari.PROJE_KARTLARI),
        ("progress_payments", kart_sayfalari.PORTFOY),
        ("inventory", kart_sayfalari.RISK_STOK),
        ("progress_payments", kart_sayfalari.RISK_GECIKME),
        ("sites", kart_sayfalari.RISK_TAKVIM),
    )
    roller = (
        (await seeded_db.execute(select(Role).where(Role.key != "system_admin"))).scalars().all()
    )
    assert len(roller) == 13
    proje = await project_factory(code="B5D-SEED", project_type="taahhut")
    for rol in roller:
        ana = await user_factory(email=f"{rol.key}-ana@b5d.co", password=SIFRE, role_key=rol.key)
        ekip = await user_factory(email=f"{rol.key}-ekip@b5d.co", password=SIFRE, role_key=rol.key)
        seeded_db.add(ProjectMember(user_id=ekip.id, project_id=proje.id, role_id=rol.id))
        await seeded_db.flush()
        for modul, sayfalar in kartlar:
            for kisi, dal in ((ana, "ana"), (ekip, "ekip")):
                eski = await can_read_projects(seeded_db, kisi, modul, [proje.id])
                yeni = await can_read_pages_projects(seeded_db, kisi, sayfalar, [proje.id])
                assert eski == yeni, (rol.key, modul, sayfalar, dal)
            eski_projesiz = await can_read(seeded_db, ana, modul)
            yeni_projesiz = await can_read_pages(seeded_db, ana, sayfalar)
            assert eski_projesiz == yeni_projesiz, (rol.key, modul, sayfalar, "projesiz")


async def test_projesiz_dal_ana_rolun_sayfa_kararini_yansitir(
    client, db_session, user_factory
) -> None:
    """Gorunur projesi olmayan aktör: kart ana rolün sayfa kararını yansıtır (portföy boş zarf,
    risk kaynağı ok / yoksa restricted)."""
    kullanici, basliklar = await _oturum(
        client, db_session, user_factory, "b5d_projesiz", ("mali.hakedis_taseron",)
    )
    govde = await _panel(client, basliklar)

    assert govde["projects"] == []
    assert govde["portfolio"]["available"] is False
    assert govde["portfolio"]["pending_module"] is None  # taşeron sayfası portföyü AÇMAZ
    assert _kaynak(govde, "progress_payments") == "ok"
    assert _kaynak(govde, "inventory") == "restricted"
    assert uuid.UUID(str(kullanici.id))
