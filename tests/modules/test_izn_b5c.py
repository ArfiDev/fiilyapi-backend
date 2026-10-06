"""IZN-B5c — sites yazma kapıları SAYFA BAŞINA, bölüm belgesi bağlama, madde 16, dar görme.

Tablo UYGULAMADAN BAĞIMSIZ elle yazıldı (IZN-B5c-ESLEME.md + CEO kararları). Her sayfa için TEK
hücreli (yalnız o sayfada Düzenler/Görür) özel rol kurulur ve tablodaki HER ucun çağrılır: kapı
reddederse 403, geçerse (gövde/kayıt hatası dahil) 403 DIŞI. Beklenen: 403 ⇔ sayfa kümede DEĞİL.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, update

from app.core.page_gate import pages_ok
from app.core.sayfalar import PageLevel
from app.modules.roles import seed_data
from app.modules.roles.models import Role, RolePagePermission
from app.modules.sites.models import Section
from tests._ekip_dunyasi import proje_kur, rol_kur

pytestmark = pytest.mark.asyncio

SIFRE = "parola1234"
AYNI = uuid.uuid4()

# --- elle yazılmış sayfa kümeleri -----------------------------------------------------------
PV = (  # projects:view sayfaları (eski GET /projects/{id} kümesi)
    "genel.projeler",
    "genel.proje_takvimi",
    "mali.satis_blok",
    "mali.satis_unite",
    "mali.satis_excel",
    "mali.satis_paylasim",
    "proje.ozet",
    "proje.paylasim_tablosu",
)
SANTIYE_TUM = (  # 14 santiye.* sayfası
    "santiye.bolumler",
    "santiye.is_kalemleri",
    "santiye.puantaj",
    "santiye.stok",
    "santiye.hakedisler",
    "santiye.gunluk_kayit",
    "santiye.belgeler",
    "santiye.bolum_dagilimi",
    "santiye.gunluk_ozet",
    "santiye.gunluk_planlama",
    "santiye.adam_saat_butcesi",
    "santiye.planlama_paneli",
    "santiye.gunluk_ilerleme_raporu",
    "santiye.haftalik_qurr",
)
BOLUM_TUM = (  # 7 bolum.* sayfası
    "bolum.detay",
    "bolum.is_kalemleri",
    "bolum.puantaj",
    "bolum.malzeme",
    "bolum.hakedis",
    "bolum.gunluk_kayit",
    "bolum.gunluk_kayit_detay",
)
SITE_GORUR = ("proje.santiyeler", *SANTIYE_TUM, *BOLUM_TUM)  # 22
SECTION_GORUR = (
    "santiye.bolumler",
    "santiye.gunluk_kayit",
    *BOLUM_TUM,
)  # 9 (şirket sayfası saha.gunluk_kayit YOK)
SECTIONS_LIST_GORUR = (
    "santiye.bolumler",
    "bolum.detay",
    "santiye.stok",
    "santiye.puantaj",
    "santiye.gunluk_planlama",
)  # 5 (şirket sayfaları stok.* YOK)
SITES_ESKI = ("santiye.bolumler", "bolum.detay")  # eski sites:view/full sayfaları

#: (yöntem, yol) → ucu açan sayfalar (herhangi biri yeter).
YAZMA: dict[tuple[str, str], tuple[str, ...]] = {
    ("POST", "/projects/{id}/sites"): ("proje.santiyeler",),
    ("PATCH", "/sites/{id}"): ("proje.santiyeler",),
    ("POST", "/sites/{id}/sections"): ("santiye.bolumler",),
    ("PATCH", "/sections/{id}"): ("bolum.detay",),
    ("POST", "/section-types"): ("santiye.bolumler", "bolum.detay"),
    ("POST", "/sections/{id}/documents"): ("bolum.detay",),
    ("PATCH", "/sections/documents/{id}"): ("bolum.detay",),
}
GORME: dict[tuple[str, str], tuple[str, ...]] = {
    ("GET", "/projects/{id}/sites"): ("proje.santiyeler", "santiye.bolumler", "bolum.detay"),
    ("GET", "/projects/{id}"): (*PV, "proje.santiyeler"),
    ("GET", "/sites/{id}"): SITE_GORUR,
    ("GET", "/sections/{id}"): SECTION_GORUR,
    ("GET", "/sites/{id}/sections"): SECTIONS_LIST_GORUR,
    # DEĞİŞMEYENLER (eski sites:view / projects:view)
    ("GET", "/section-types"): SITES_ESKI,
    ("GET", "/sites"): SITES_ESKI,
    ("GET", "/sections/{id}/documents"): SITES_ESKI,
    ("GET", "/projects"): PV,  # madde 16: listeye 61 EKLENMEZ
}
TUM = {**YAZMA, **GORME}

HAVUZ = sorted(
    {
        "proje.santiyeler",
        *SANTIYE_TUM,
        *BOLUM_TUM,
        *PV,
        "saha.gunluk_kayit",
        "stok.satinalma_talepleri",
        "stok.stok_depo",
        "mali.hesap_plani",
    }
)


def _url(yol: str) -> str:
    return yol.format(id=AYNI)


async def _oturum(client, session, user_factory, key: str, sayfa: str, level: PageLevel):
    rol = await rol_kur(session, key, PageLevel.none)
    await session.execute(
        update(RolePagePermission)
        .where(RolePagePermission.role_id == rol.id, RolePagePermission.page_key == sayfa)
        .values(level=level)
    )
    await session.flush()
    email = f"{key}@b5c.co"
    await user_factory(email=email, password=SIFRE, role_key=key)
    yanit = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    assert yanit.status_code == 200, yanit.text
    return {"Authorization": f"Bearer {yanit.json()['access_token']}"}


async def _durumlar(client, basliklar, uclar) -> dict[tuple[str, str], int]:
    out = {}
    for yontem, yol in uclar:
        yanit = await client.request(yontem, _url(yol), json={}, headers=basliklar)
        out[(yontem, yol)] = yanit.status_code
    return out


async def test_tablo_sayilari() -> None:
    assert len(YAZMA) == 7
    assert len(SITE_GORUR) == 22
    assert len(SECTION_GORUR) == 9
    assert len(SECTIONS_LIST_GORUR) == 5


@pytest.mark.parametrize("sayfa", HAVUZ)
async def test_tek_sayfanin_duzenleri_yalniz_kendi_uclarini_acar(
    client, seeded_db, user_factory, sayfa
) -> None:
    """(i) komşu sayfanın Düzenler'i ucu AÇMAZ (403) · (ii) doğru sayfanınki açar (403 DIŞI)."""
    basliklar = await _oturum(
        client, seeded_db, user_factory, "e_" + sayfa.replace(".", "_"), sayfa, PageLevel.edit
    )
    durumlar = await _durumlar(client, basliklar, TUM)
    yanlis = {
        uc: (kod, sayfa in TUM[uc])
        for uc, kod in durumlar.items()
        if (kod != 403) != (sayfa in TUM[uc])
    }
    assert yanlis == {}, f"{sayfa}: (durum, kümede mi) beklenmeyen: {yanlis}"


@pytest.mark.parametrize("sayfa", HAVUZ)
async def test_tek_sayfanin_gorur_yalniz_okuma_uclarini_acar(
    client, seeded_db, user_factory, sayfa
) -> None:
    """Görür: yazma uçlarının HİÇBİRİ açılmaz; okuma uçları kümeyle birebir."""
    basliklar = await _oturum(
        client, seeded_db, user_factory, "g_" + sayfa.replace(".", "_"), sayfa, PageLevel.view
    )
    durumlar = await _durumlar(client, basliklar, TUM)
    yanlis = {
        uc: kod
        for uc, kod in durumlar.items()
        if (kod != 403) != (uc in GORME and sayfa in GORME[uc])
    }
    assert yanlis == {}, f"{sayfa}: beklenmeyen: {yanlis}"


async def test_61_tek_basina_proje_sayfasini_acar_listeyi_acmaz(
    client, seeded_db, user_factory
) -> None:
    """Madde 16: yalnız `proje.santiyeler` Görür → /projects/{id} ve /sites açık; liste kapalı."""
    basliklar = await _oturum(
        client, seeded_db, user_factory, "m16", "proje.santiyeler", PageLevel.view
    )
    for yol in ("/projects/{id}", "/projects/{id}/sites"):
        yanit = await client.get(_url(yol), headers=basliklar)
        assert yanit.status_code != 403, f"{yol}: {yanit.status_code}"
    assert (await client.get("/projects", headers=basliklar)).status_code == 403


async def _izn_rolleri_kur(session) -> dict[str, Role]:
    """`seed_reference_data` yalnız eski 7 rolü kurar; 6 IZN rolü üretimde migration'dan gelir.
    Burada `IZN_ROLES` + `PAGE_MATRIX` + `HIDDEN_FIELDS` (seed kaynağı) aynen kurulur."""
    for satir in seed_data.IZN_ROLES:
        session.add(Role(**satir))
    await session.flush()
    roller = {r.key: r for r in (await session.execute(select(Role))).scalars()}
    await seed_data._seed_page_cells(session, roller, tuple(seed_data.IZN_ROLE_ORDER))
    await session.flush()
    return roller


# --- seed ölçümü: dar genişlemede YALNIZ warehouse_keeper kazanır ---------------------------
_GENISLEYEN_GET = {
    "/sites/{id}": SITE_GORUR,
    "/sections/{id}": SECTION_GORUR,
    "/sites/{id}/sections": SECTIONS_LIST_GORUR,
}


async def test_seed_rollerinde_yalniz_warehouse_keeper_kazanir(
    client, seeded_db, user_factory
) -> None:
    from app.modules.users.models import User

    await _izn_rolleri_kur(seeded_db)
    roller = (
        (await seeded_db.execute(select(Role).where(Role.key != "system_admin"))).scalars().all()
    )
    assert len(roller) == 13
    kazanan: dict[str, set[str]] = {yol: set() for yol in _GENISLEYEN_GET}
    kaybeden: dict[str, set[str]] = {yol: set() for yol in _GENISLEYEN_GET}
    for rol in roller:
        email = f"seed_{rol.key}@b5c.co"
        await user_factory(email=email, password=SIFRE, role_key=rol.key)
        yanit = await client.post("/auth/login", json={"email": email, "password": SIFRE})
        if yanit.status_code != 200:
            continue
        basliklar = {"Authorization": f"Bearer {yanit.json()['access_token']}"}
        user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
        eski = await pages_ok(seeded_db, user, SITES_ESKI, "view")
        for yol in _GENISLEYEN_GET:
            yeni = (await client.get(_url(yol), headers=basliklar)).status_code != 403
            if yeni and not eski:
                kazanan[yol].add(rol.key)
            if eski and not yeni:
                kaybeden[yol].add(rol.key)
    assert kaybeden == {yol: set() for yol in _GENISLEYEN_GET}
    assert kazanan == {yol: {"warehouse_keeper"} for yol in _GENISLEYEN_GET}


# --- maske: warehouse_keeper'ın tutar alanları üç GET'te maskeli ----------------------------
_TUTAR_ALANLARI = ("budget", "contract_amount", "total_progress_payment")


def _tutar_alanlari(govde, yol="") -> list[tuple[str, object]]:
    """Yanıt ağacındaki TÜM tutar alanları (budget · contract_amount · total_progress_payment)."""
    bulunan: list[tuple[str, object]] = []
    if isinstance(govde, dict):
        for anahtar, deger in govde.items():
            if anahtar in _TUTAR_ALANLARI:
                bulunan.append((f"{yol}.{anahtar}", deger))
            else:
                bulunan += _tutar_alanlari(deger, f"{yol}.{anahtar}")
    elif isinstance(govde, list):
        for sira, oge in enumerate(govde):
            bulunan += _tutar_alanlari(oge, f"{yol}[{sira}]")
    return bulunan


def _maskeli_mi(deger) -> bool:
    if isinstance(deger, dict):
        return deger.get("available") is False and deger.get("value") is None
    return deger is None


async def _maske_dunyasi(client, seeded_db, user_factory, project_factory, gizli_kume_bos: bool):
    from sqlalchemy import delete

    from app.modules.roles.models import RoleHiddenField
    from app.modules.users.models import User
    from tests._proje_ekibi import ekibe_ekle

    roller = await _izn_rolleri_kur(seeded_db)
    if gizli_kume_bos:
        await seeded_db.execute(
            delete(RoleHiddenField).where(RoleHiddenField.role_id == roller["warehouse_keeper"].id)
        )
        await seeded_db.flush()
    admin = await user_factory(email="b5c_adm@izn.co", password=SIFRE, role_key="patron")
    dunya = await proje_kur(seeded_db, project_factory, "B5CM", "B5c", admin.id)
    dunya.site.budget = Decimal("5000.00")
    seeded_db.add(Section(site_id=dunya.site.id, name="İkinci Bölüm", sort_order=2))
    await seeded_db.flush()

    async def _baslik(email: str, role_key: str) -> dict[str, str]:
        await user_factory(email=email, password=SIFRE, role_key=role_key)
        user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
        await ekibe_ekle(seeded_db, user, dunya.project.id)
        girdi = await client.post("/auth/login", json={"email": email, "password": SIFRE})
        return {"Authorization": f"Bearer {girdi.json()['access_token']}"}

    yollar = (
        f"/sites/{dunya.site.id}",
        f"/sections/{dunya.section.id}",
        f"/sites/{dunya.site.id}/sections",
    )
    return (
        await _baslik("b5c_p@izn.co", "patron"),
        await _baslik("b5c_w@izn.co", "warehouse_keeper"),
        yollar,
    )


async def _depo_tutarlari(client, depo, yollar):
    sonuc: dict[str, list[tuple[str, object]]] = {}
    for yol in yollar:
        yanit = await client.get(yol, headers=depo)
        assert yanit.status_code == 200, (yol, yanit.text)
        sonuc[yol] = _tutar_alanlari(yanit.json())
    return sonuc


async def test_warehouse_keeper_uc_get_tutar_maskeli(
    client, seeded_db, user_factory, project_factory
) -> None:
    """Dar genişlemeyle kazanılan üç GET'te warehouse_keeper'ın (seed gizli kümesi
    {tum_tutarlar, maas_kisisel}) TÜM tutar alanları maskeli; patron aynı kayıtta değeri görür."""
    patron, depo, yollar = await _maske_dunyasi(
        client, seeded_db, user_factory, project_factory, gizli_kume_bos=False
    )
    site_yol, sec_yol, liste_yol = yollar
    tutarlar = await _depo_tutarlari(client, depo, yollar)
    # kapsam: site ucu = budget + contract_amount + total_progress_payment + 2 bölüm budget'ı
    assert len(tutarlar[site_yol]) >= 5, tutarlar[site_yol]
    assert len(tutarlar[sec_yol]) >= 1
    assert len(tutarlar[liste_yol]) >= 2, "liste ucunda en az 2 satır beklenir"
    for yol, alanlar in tutarlar.items():
        acik = [(ad, d) for ad, d in alanlar if not _maskeli_mi(d)]
        assert acik == [], f"{yol}: maskesiz tutar alanı: {acik}"
    # düz tutar (Decimal) alanı None; yer tutucu alanlar {available: false, value: null}
    site = (await client.get(site_yol, headers=depo)).json()
    assert site["budget"] is None
    assert isinstance(site["contract_amount"], dict)
    # kontrol: patron aynı ağaçta değeri görür
    p = (await client.get(site_yol, headers=patron)).json()
    assert Decimal(str(p["budget"])) == Decimal("5000.00")


async def test_maske_kalkarsa_tutar_testi_kirmizi_olur(
    client, seeded_db, user_factory, project_factory
) -> None:
    """MUTASYON KANITI: gizli küme boşken aynı tarama maskesiz alan BULUR (test duyarlı)."""
    _, depo, yollar = await _maske_dunyasi(
        client, seeded_db, user_factory, project_factory, gizli_kume_bos=True
    )
    tutarlar = await _depo_tutarlari(client, depo, yollar)
    acik = [ad for alanlar in tutarlar.values() for ad, d in alanlar if not _maskeli_mi(d)]
    assert acik, "maske kalkınca açık alan bulunmalıydı"


async def test_tum_projeler_kisisi_yalniz_stok_depo_goruru_bolum_listesini_acmaz(
    client, seeded_db, user_factory
) -> None:
    """Şirket sayfası stok.stok_depo Görür'ü (tüm projeler) iç ekran ucunu AÇMAZ."""
    from app.modules.users.models import User

    basliklar = await _oturum(
        client, seeded_db, user_factory, "tp_stok", "stok.stok_depo", PageLevel.view
    )
    user = (
        await seeded_db.execute(select(User).where(User.email == "tp_stok@b5c.co"))
    ).scalar_one()
    user.all_projects = True
    await seeded_db.flush()
    yanit = await client.get(f"/sites/{AYNI}/sections", headers=basliklar)
    assert yanit.status_code == 403, yanit.text
