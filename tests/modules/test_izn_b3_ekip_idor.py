"""IZN-B3 — "Kule ekibi Köprü'yü görmez" MATRİSİ (IDOR, KARARLAR §1.7).

İki proje, iki ekip. Kule ekibinden biri Köprü'nün HİÇBİR kaynağını göremez: tekil uçlarda 404,
liste uçlarında satır YOK. İki katman:

1. ROTA TABANLI TARAMA: proje bağlamlı GET uçlarının (yol parametreleri ⊆ {project_id, site_id,
   section_id}) TÜMÜ — proje, şantiye, bölüm, blok, ünite, BOQ, sözleşme, satış, planlama, stok,
   puantaj, günlük, EV, belge klasörü, maliyet… — Köprü kimlikleriyle 404 verir. Sessiz-boş tarama
   (yol sayısı tabanı) KIRMIZI olur; yeni bir proje bağlamlı GET ucu eklenince kendiliğinden
   taranır. Her uç için KONTROL: aynı uç Kule kimlikleriyle 404 DEĞİLDİR (sınama anlamlı).
2. VARLIK KİMLİKLİ UÇLAR (kimlik parametreli): hakediş, günlük kaydı, BOQ kalemi — Köprü kaydı
   404, Kule kaydı 200; liste uçları (`/projects`, `/sites`, `/progress-payments`) yalnız Kule'yi
   taşır.

Ayrıca `all_projects` kişi her ikisini görür, Sistem Yöneticisi her ikisini görür, ekibi OLMAYAN
kişi hiçbirini görmez.
"""

from __future__ import annotations

import re
import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from httpx import AsyncClient

from app.main import app
from app.modules.boq.models import BoqItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.site_diary.models import SiteDiaryEntry
from app.modules.sites.models import Section, Site
from app.modules.units.models import Block, Unit
from tests._proje_ekibi import ekibe_ekle, tum_projeler
from tests.modules._boq import _group, _item, _site
from tests.modules.units._units_api import _block, _unit

PASSWORD = "parola1234"
PARAM = re.compile(r"{(\w+)}")
PROJE_BAGLAMLI_PARAMLAR = {"project_id", "site_id", "section_id"}

#: Sessiz-boş taramayı yakalayan taban (ölçüldü 2026-10-05: 46 uç). Düşerse sınıflandırıcı
#: bozulmuştur.
TARAMA_TABANI = 40

#: Kule kimlikleriyle de 404/409 vermesi MEŞRU olan uçlar (bu dünyada o kaynak yok). Köprü yanıtının
#: gövde mesajı Kule'ninkinden FARKLI olmalıdır (görünürlük kapısı kaynak kontrolünden ÖNCE çalışır:
#: "bulunamadı" ≠ "baseline yok").
KONTROL_MESRU = {
    "/projects/{project_id}/land-share/summary": "kat karşılığı olmayan proje",
    "/projects/{project_id}/land-share/units": "kat karşılığı olmayan proje",
    "/sites/{site_id}/earned-value/budget": "baseline yok",
    "/sites/{site_id}/earned-value/budget/schedule": "baseline yok",
    "/sites/{site_id}/earned-value/code-tree": "baseline yok",
    "/sites/{site_id}/earned-value/panel": "baseline yok",
    "/sites/{site_id}/earned-value/reports/daily": "baseline yok",
    "/sites/{site_id}/earned-value/reports/weekly": "baseline yok",
    "/sites/{site_id}/earned-value/reports/weekly.xlsx": "baseline yok",
    "/sites/{site_id}/earned-value/settings/preview": "baseline yok",
    "/sites/{site_id}/earned-value/settings/preview/composite": "baseline yok",
}

_GUN = {"date": "2026-05-04"}


def _kalem_sorgusu(proje) -> dict:  # noqa: ANN001
    return {
        "measure": "spent",
        "numerator_item_id": [str(proje.item.id)],
        "denominator_item_id": str(proje.item.id),
    }


#: Zorunlu sorgu parametreleri (olmadan 422 gelir ve sınama anlamsızlaşır). Değer sözlük ya da
#: `(proje) -> sözlük`.
SORGU = {
    "/sites/{site_id}/diary/skeleton": {"entry_date": "2026-05-04"},
    "/sites/{site_id}/diary/summary": _GUN,
    "/sites/{site_id}/plan": {"week_start": "2026-05-04"},
    "/sites/{site_id}/plan/day-summary": {"start": "2026-05-04"},
    "/sites/{site_id}/timesheet": {"year": "2026", "month": "5"},
    "/sites/{site_id}/timesheet/export.xlsx": {"year": "2026", "month": "5"},
    "/sites/{site_id}/timesheet/week": {"iso_year": "2026", "iso_week": "19"},
    "/sites/{site_id}/earned-value/reports/daily": _GUN,
    "/sites/{site_id}/earned-value/reports/weekly": {"week": "1"},
    "/sites/{site_id}/earned-value/reports/weekly.xlsx": {"week": "1"},
    "/sites/{site_id}/earned-value/settings/preview": _GUN,
    "/sites/{site_id}/earned-value/settings/preview/composite": _kalem_sorgusu,
    "/sites/{site_id}/earned-value/panel": {"date": "2026-05-04", "range": "4w"},
    "/projects/{project_id}/progress-payments/diary-suggestion": {"period": "2026-05"},
}


class Proje:
    """Bir projenin kaynakları + Kule/Köprü karşılaştırması için kimlikler."""

    def __init__(self, project: Project, site: Site, section: Section, item: BoqItem) -> None:
        self.project, self.site, self.section, self.item = project, site, section, item
        self.block: Block | None = None
        self.unit: Unit | None = None
        self.payment: ProgressPayment | None = None
        self.entry: SiteDiaryEntry | None = None

    def params(self) -> dict[str, str]:
        return {
            "project_id": str(self.project.id),
            "site_id": str(self.site.id),
            "section_id": str(self.section.id),
        }


async def _proje_kur(session, project_factory, kod: str, ad: str, creator_id: uuid.UUID) -> Proje:
    project = await project_factory(kod, name=ad)
    site = await _site(session, project, code=f"{kod}-S")
    section = Section(
        site_id=site.id,
        name=f"{ad} Bölümü",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 29),
        planned_worker_count=3,
        sort_order=1,
    )
    session.add(section)
    await session.flush()
    grup = await _group(session, site, name=f"{ad} Grubu")
    kalem = await _item(session, site, grup, code=f"{kod}.001")
    dunya = Proje(project, site, section, kalem)
    dunya.block = await _block(session, project, site, name=f"{ad} Blok")
    dunya.unit = await _unit(session, project, dunya.block)
    session.add(
        ProjectContract(
            project_id=project.id,
            contract_no=f"SZL-{kod}",
            amount=Decimal("1000000"),
            advance_pct=Decimal("10"),
            retainage_pct=Decimal("5"),
            vat_pct=Decimal("20"),
        )
    )
    await session.flush()
    dunya.payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.draft,
        vat_pct=Decimal("20"),
        advance_pct=Decimal("0"),
        retainage_pct=Decimal("0"),
        created_by=creator_id,
    )
    session.add(dunya.payment)
    dunya.entry = SiteDiaryEntry(
        site_id=site.id, project_id=project.id, entry_date=date(2026, 5, 4), created_by=creator_id
    )
    session.add(dunya.entry)
    await session.flush()
    return dunya


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


class Dunya:
    def __init__(self) -> None:
        self.kule: Proje
        self.kopru: Proje
        self.kule_ekibi: dict[str, str]
        self.kopru_ekibi: dict[str, str]
        self.admin: dict[str, str]
        self.herkes: dict[str, str]
        self.ekipsiz: dict[str, str]


@pytest.fixture
async def dunya(client: AsyncClient, seeded_db, user_factory, project_factory) -> Dunya:
    d = Dunya()
    kuleci = await user_factory(email="kule@izn.co", password=PASSWORD, role_key="patron")
    koprucu = await user_factory(email="kopru@izn.co", password=PASSWORD, role_key="patron")
    await user_factory(email="admin@izn.co", password=PASSWORD, role_key="system_admin")
    gezgin = await user_factory(email="herkes@izn.co", password=PASSWORD, role_key="patron")
    await user_factory(email="ekipsiz@izn.co", password=PASSWORD, role_key="patron")
    d.kule = await _proje_kur(seeded_db, project_factory, "KULE", "Kule", kuleci.id)
    d.kopru = await _proje_kur(seeded_db, project_factory, "KOPRU", "Köprü", kuleci.id)
    await ekibe_ekle(seeded_db, kuleci, d.kule.project.id)
    await ekibe_ekle(seeded_db, koprucu, d.kopru.project.id)
    await tum_projeler(seeded_db, gezgin)
    d.kule_ekibi = await _giris(client, "kule@izn.co")
    d.kopru_ekibi = await _giris(client, "kopru@izn.co")
    d.admin = await _giris(client, "admin@izn.co")
    d.herkes = await _giris(client, "herkes@izn.co")
    d.ekipsiz = await _giris(client, "ekipsiz@izn.co")
    seeded_db.expunge_all()  # ortak test oturumu: gerçek istekte oturum ayrıdır
    return d


def _tarama_ucları() -> list[str]:
    yollar: set[str] = set()
    for ctx in iter_route_contexts(app.routes):
        if not isinstance(ctx.original_route, APIRoute) or "GET" not in ctx.methods:
            continue
        paramlar = set(PARAM.findall(ctx.path))
        if paramlar and paramlar <= PROJE_BAGLAMLI_PARAMLAR:
            yollar.add(ctx.path)
    return sorted(yollar)


def _doldur(yol: str, proje: Proje) -> str:
    return PARAM.sub(lambda m: proje.params()[m.group(1)], yol)


def test_tarama_sessiz_bos_degil_ve_sorgu_tablosu_bayat_degil() -> None:
    uclar = _tarama_ucları()
    assert len(uclar) >= TARAMA_TABANI, len(uclar)
    assert set(SORGU) <= set(uclar), set(SORGU) - set(uclar)
    assert set(KONTROL_MESRU) <= set(uclar), set(KONTROL_MESRU) - set(uclar)


def _sorgu(yol: str, proje: Proje) -> dict | None:
    deger = SORGU.get(yol)
    return deger(proje) if callable(deger) else deger


@pytest.mark.parametrize("yol", _tarama_ucları())
async def test_kule_ekibi_kopruyu_goremez_proje_baglamli_GET_taramasi(
    client: AsyncClient, dunya: Dunya, yol: str
) -> None:
    kopru = await client.get(
        _doldur(yol, dunya.kopru), params=_sorgu(yol, dunya.kopru), headers=dunya.kule_ekibi
    )
    assert kopru.status_code == 404, (yol, kopru.status_code, kopru.text[:200])
    # KONTROL: aynı uç Kule kimlikleriyle ulaşılabilirdir (sınama anlamlı).
    kendi = await client.get(
        _doldur(yol, dunya.kule), params=_sorgu(yol, dunya.kule), headers=dunya.kule_ekibi
    )
    if yol in KONTROL_MESRU:
        assert kendi.status_code in (200, 404, 409), (yol, kendi.status_code)
        if kendi.status_code != 200:
            # Görünürlük kapısı kaynak kontrolünden ÖNCE: Köprü'nün mesajı Kule'ninkinden farklı.
            assert kopru.json()["detail"] != kendi.json()["detail"], (yol, kopru.text)
    else:
        assert kendi.status_code == 200, (yol, kendi.status_code, kendi.text[:200])
    # Köprü ekibi AYNI uçtan Kule'yi görmez (aynası).
    ayna = await client.get(
        _doldur(yol, dunya.kule), params=_sorgu(yol, dunya.kule), headers=dunya.kopru_ekibi
    )
    assert ayna.status_code == 404, (yol, ayna.status_code)
    # Sistem Yöneticisi ve "Tüm projeler" kişisi ikisini de görür: görünürlük 404'ü DEĞİL
    # (200 ya da kaynağa özgü 404/409 — mesaj "bulunamadı" görünürlük mesajından farklı).
    for baslik in (dunya.admin, dunya.herkes):
        for proje in (dunya.kule, dunya.kopru):
            yanit = await client.get(_doldur(yol, proje), params=_sorgu(yol, proje), headers=baslik)
            if yol in KONTROL_MESRU:
                assert yanit.status_code in (200, 404, 409), (yol, yanit.status_code)
                if yanit.status_code != 200:
                    assert yanit.json()["detail"] == kendi.json()["detail"], (yol, yanit.text)
            else:
                assert yanit.status_code == 200, (yol, yanit.status_code, yanit.text[:200])
    # Ekibi olmayan kişi ikisini de göremez.
    for proje in (dunya.kule, dunya.kopru):
        yanit = await client.get(
            _doldur(yol, proje), params=_sorgu(yol, proje), headers=dunya.ekipsiz
        )
        assert yanit.status_code == 404, (yol, yanit.status_code)


# --- varlık kimlikli uçlar ----------------------------------------------------------------

VARLIK_UCLARI = [
    ("hakedis", lambda p: f"/progress-payments/{p.payment.id}"),
    ("gunluk_kaydi", lambda p: f"/diary/{p.entry.id}"),
    ("boq_kalemi_dagitim", lambda p: f"/boq/items/{p.item.id}/allocations"),
    ("bolum", lambda p: f"/sections/{p.section.id}"),
    ("santiye", lambda p: f"/sites/{p.site.id}"),
    ("proje", lambda p: f"/projects/{p.project.id}"),
]


@pytest.mark.parametrize(("ad", "yol"), VARLIK_UCLARI, ids=[v[0] for v in VARLIK_UCLARI])
async def test_varlik_kimlikli_uclarda_kopru_kaydi_404_kule_kaydi_200(
    client: AsyncClient, dunya: Dunya, ad: str, yol
) -> None:
    assert (await client.get(yol(dunya.kule), headers=dunya.kule_ekibi)).status_code == 200, ad
    kopru = await client.get(yol(dunya.kopru), headers=dunya.kule_ekibi)
    assert kopru.status_code == 404, (ad, kopru.status_code, kopru.text[:200])
    assert (await client.get(yol(dunya.kopru), headers=dunya.kopru_ekibi)).status_code == 200, ad
    assert (await client.get(yol(dunya.kule), headers=dunya.kopru_ekibi)).status_code == 404, ad
    for proje in (dunya.kule, dunya.kopru):
        assert (await client.get(yol(proje), headers=dunya.herkes)).status_code == 200, ad
        assert (await client.get(yol(proje), headers=dunya.admin)).status_code == 200, ad


@pytest.mark.parametrize(
    ("yol", "kimlik"),
    [
        ("/projects", lambda p: str(p.project.id)),
        ("/sites", lambda p: str(p.site.id)),
        ("/progress-payments", lambda p: str(p.payment.id)),
    ],
    ids=["projeler", "santiyeler", "hakedisler"],
)
async def test_liste_uclarinda_kopru_hic_gorunmez(
    client: AsyncClient, dunya: Dunya, yol: str, kimlik
) -> None:
    def kimlikler(govde: dict | list) -> set[str]:
        satirlar = govde["items"] if isinstance(govde, dict) else govde
        return {s["id"] for s in satirlar}

    kule = await client.get(yol, headers=dunya.kule_ekibi)
    assert kule.status_code == 200, kule.text[:200]
    assert kimlik(dunya.kule) in kimlikler(kule.json())
    assert kimlik(dunya.kopru) not in kimlikler(kule.json())
    herkes = kimlikler((await client.get(yol, headers=dunya.herkes)).json())
    assert {kimlik(dunya.kule), kimlik(dunya.kopru)} <= herkes
    ekipsiz = kimlikler((await client.get(yol, headers=dunya.ekipsiz)).json())
    assert not ({kimlik(dunya.kule), kimlik(dunya.kopru)} & ekipsiz)
