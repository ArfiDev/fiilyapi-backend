"""DSC-B4 dünyası: B2'nin `kur(yazanlar=True)` dünyası + `genislet_b4()` (B3'ten BAĞIMSIZ).

`kur` DEĞİŞMEZ (mevcut 86 golden bozulmasın); bu modül ayrı bir katman ekler:

```
Şantiye "B-Blok" (DSC-B, aynı proje)   kendi BOQ'u:
  GB1 Betonarme-B → KAB   IB1 10.001 "Beton B" m3 100 ₺10   gerçekleşen 20 (gönderilmiş)
  GB2 Elektrik-B  → DUV   IB2 20.001 "Kablo B" m  50 ₺20    gerçekleşen 10
  GB3 Eşlemesiz           IB3 30.001 "Boya B"  m2 40 ₺30    gerçekleşen  5   (disiplinsiz)
  EV: AKTİF revizyon (dondurulmuş) + eşleme; 2 puantaj kişisi (Mayıs 2026)
Proje 2 "DSC-P02" (REVİZYONSUZ)  şantiye C (DSC-C): GC1 → IC1 90.001 "Sıva C" m2 10 ₺100,
  gerçekleşen 4; EV revizyonu YOK → kısıtlıda fiziksel % None (görünür kalem yok)
Aktör pm_atamasiz (project_manager, atama YOK) — civil/elek ile AYNI izinler (F1)
Hakediş: P1'e ONAYLI işveren hakedişi (SZL-01.001 × 12) → financial_progress = %5,00
```
Bu dünyada Civil/Elektrik/pm_atamasiz aktörlerinin P2 erişimi de vardır.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.contracts.models import EmployerContractItem
from app.modules.earned_value.models import EvBaselineLeaf, EvRevision
from app.modules.personnel.models import Personnel
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine, WorkerSource
from app.modules.sites.models import Section, Site
from app.modules.timesheet.models import TimesheetEntry
from app.modules.users.models import UserProjectAccess
from tests._disiplin_dunyasi import GUN1, SIFRE, Dunya, _giris, _kimlik, kur
from tests.discipline_scope._b2_golden_araclari import rastgele_kimlikleri_etiketle

D = Decimal
FIXED_TODAY = date(2026, 5, 10)
_PUANTAJ_GUNU = date(2026, 5, 8)

#: `today` isimle içe aktarılmış modüller (B4 uçlarının yolu). `app.core.timezone.today` da
#: sabitlenir (`timeline.py` modül üzerinden okur). Sabitlenmezse işçi sayısı (Mayıs 2026
#: puantajı) 0 çıkar ve Ü3 kontrolü sahte-yeşil olur.
BUGUN_MODULLERI = (
    "app.core.timezone",
    "app.modules.sites.service.presenters",
    "app.modules.sites.service.codes",
    "app.modules.timesheet.counts",
    "app.modules.dashboard.risks",
    "app.modules.projects.service",
    "app.modules.earned_value.actuals",
)


@dataclass
class DunyaB4:
    d: Dunya
    santiye_b: Site
    proje2: object
    santiye_c: Site
    bolum_b: Section
    gb: dict[str, BoqGroup]
    ib: dict[str, BoqItem]
    ic1: BoqItem


async def _pm_atamasiz(session, client, user_factory, d: Dunya) -> None:
    user = await user_factory(
        email="pm_atamasiz@dsc-b4.co", password=SIFRE, role_key="project_manager"
    )
    session.add(UserProjectAccess(user_id=user.id, project_id=d.proje.id, all_projects=False))
    await session.flush()
    d.kullanici["pm_atamasiz"] = user
    d.etiketler[user.id] = "<user:pm_atamasiz>"
    d.baslik["pm_atamasiz"] = await _giris(client, "pm_atamasiz@dsc-b4.co")


async def pm_atamasiz_ekle(session, client, user_factory, d: Dunya) -> None:
    """AI kalıtım testi için: yalnız atamasız PM (civil/elek ile aynı rol ve proje erişimi)."""
    await _pm_atamasiz(session, client, user_factory, d)


async def _grup_ve_kalemler(session, santiye: Site, tanim, grup_no: int, kalem_no: int):
    gruplar = {
        ad: BoqGroup(id=_kimlik(15, grup_no + n), site_id=santiye.id, name=ad_tr, sort_order=n)
        for n, (ad, ad_tr) in enumerate(tanim["gruplar"], start=1)
    }
    session.add_all(gruplar.values())
    await session.flush()
    kalemler = {
        ad: BoqItem(
            id=_kimlik(16, kalem_no + n),
            site_id=santiye.id,
            group_id=gruplar[grup].id,
            code=kod,
            description=aciklama,
            unit=birim,
            quantity=D(miktar),
            unit_price=D(fiyat),
            sort_order=1,
        )
        for n, (ad, grup, kod, aciklama, birim, miktar, fiyat) in enumerate(
            tanim["kalemler"], start=1
        )
    }
    session.add_all(kalemler.values())
    await session.flush()
    return gruplar, kalemler


async def _gunluk(session, santiye: Site, no: int, kalemler, miktarlar, olusturan) -> None:
    kayit = SiteDiaryEntry(
        id=_kimlik(33, no),
        site_id=santiye.id,
        project_id=santiye.project_id,
        entry_date=GUN1,
        status=DiaryStatus.submitted,
        work_done=f"B4 günlük {no}",
        submitted_at=None,
        created_by=olusturan.id,
    )
    from datetime import UTC, datetime

    kayit.submitted_at = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)
    kayit.submitted_by_user_id = olusturan.id
    session.add(kayit)
    await session.flush()
    for n, (ad, miktar) in enumerate(miktarlar.items(), start=1):
        kalem = kalemler[ad]
        session.add(
            SiteDiaryLine(
                id=_kimlik(34, no * 10 + n),
                entry_id=kayit.id,
                boq_item_id=kalem.id,
                section_id=None,
                code=kalem.code,
                description=kalem.description,
                unit=kalem.unit,
                unit_price=kalem.unit_price,
                quantity=D(miktar),
            )
        )
    await session.flush()


async def _ev_b(client, santiye, admin, gb, ib, bolum, d: Dunya) -> None:
    taban = f"/sites/{santiye.id}/earned-value/budget"
    esleme = await client.put(
        f"{taban}/group-disciplines",
        headers=admin,
        json={
            "items": [
                {"boq_group_id": str(gb["gb1"].id), "discipline_id": str(d.kab.id)},
                {"boq_group_id": str(gb["gb2"].id), "discipline_id": str(d.duv.id)},
            ]
        },
    )
    assert esleme.status_code == 200, esleme.text
    oranlar = await client.patch(
        f"{taban}/leaves",
        headers=admin,
        json={
            "leaves": [
                {"boq_item_id": str(ib["ib1"].id), "section_id": str(bolum.id), "unit_mhr": "2"},
                {"boq_item_id": str(ib["ib2"].id), "section_id": str(bolum.id), "unit_mhr": "0.5"},
            ]
        },
    )
    assert oranlar.status_code == 200, oranlar.text
    dondur = await client.post(f"{taban}/freeze", headers=admin, json={})
    assert dondur.status_code == 200, dondur.text


async def _puantaj_b(session, santiye: Site, olusturan) -> None:
    for n, ad in enumerate(("Can Usta", "Ece Usta"), start=1):
        kisi = Personnel(
            id=_kimlik(44, n),
            full_name=ad,
            trade="Elektrikçi",
            source=WorkerSource.company,
            is_active=True,
            is_draft=False,
        )
        session.add(kisi)
        await session.flush()
        session.add(
            TimesheetEntry(
                id=_kimlik(45, n),
                personnel_id=kisi.id,
                site_id=santiye.id,
                project_id=santiye.project_id,
                work_date=_PUANTAJ_GUNU,
                hours=D(8),
                created_by=olusturan.id,
            )
        )
    await session.flush()


async def _hakedis(session, d: Dunya, olusturan) -> None:
    """P1'e ONAYLI işveren hakedişi: SZL-01.001 × 12 → financial = 132/2640 = %5,00."""
    sozlesme_kalemi = (
        await session.execute(
            select(EmployerContractItem).where(EmployerContractItem.code == "SZL-01.001")
        )
    ).scalar_one()
    odeme = ProgressPayment(
        id=_kimlik(70, 1),
        project_id=d.proje.id,
        sequence_no=1,
        status=ProgressPaymentStatus.approved,
        vat_pct=D(20),
        advance_pct=D(20),
        retainage_pct=D(5),
        created_by=olusturan.id,
    )
    odeme.lines = [
        ProgressPaymentLine(
            id=_kimlik(71, 1),
            contract_item_id=sozlesme_kalemi.id,
            site_id=d.santiye.id,
            code=sozlesme_kalemi.code,
            description=sozlesme_kalemi.description,
            unit=sozlesme_kalemi.unit,
            contract_unit_price=sozlesme_kalemi.unit_price,
            coefficient=D("1.000"),
            quantity=D(12),
            group_name="A — Betonarme",
        )
    ]
    session.add(odeme)
    await session.flush()


async def _etiketle(session, x: DunyaB4) -> None:
    e = x.d.etiketler
    e[x.santiye_b.id] = "<siteB>"
    e[x.santiye_c.id] = "<siteC>"
    e[x.proje2.id] = "<proje2>"
    e[x.bolum_b.id] = "<SB1>"
    e[_kimlik(14, 5)] = "<tahsis:5>"
    e[_kimlik(14, 6)] = "<tahsis:6>"
    for ad, g in x.gb.items():
        e[g.id] = f"<{ad.upper()}>"
    for ad, k in {**x.ib, "ic1": x.ic1}.items():
        e[k.id] = f"<{ad.upper()}>"
    e[_kimlik(15, 10)] = "<GC1>"
    for n in (1, 2, 3):
        e[_kimlik(33, n)] = f"<entry:b4_{n}>"
    for n in (1, 2):
        e[_kimlik(44, n)] = f"<kisi:b{n}>"
        e[_kimlik(45, n)] = f"<puantaj:b{n}>"
    for no in (1, 2, 3):
        for n in (1, 2, 3):
            e[_kimlik(34, no * 10 + n)] = f"<line:b4_{no}_{n}>"
    e[_kimlik(70, 1)] = "<hakedis:1>"
    e[_kimlik(71, 1)] = "<hakedis_satir:1>"
    await rastgele_kimlikleri_etiketle(session, x.d)
    for rev in (
        await session.execute(select(EvRevision).where(EvRevision.site_id == x.santiye_b.id))
    ).scalars():
        e[rev.id] = f"<revB:{rev.number}>"
        for lf in (
            await session.execute(
                select(EvBaselineLeaf).where(EvBaselineLeaf.revision_id == rev.id)
            )
        ).scalars():
            e[lf.id] = f"<bleafB:r{rev.number}:{lf.boq_item_id}>"


async def genislet_b4(
    session: AsyncSession, client: AsyncClient, user_factory, project_factory, d: Dunya
) -> DunyaB4:
    admin = d.baslik["atamasiz"]
    olusturan = d.kullanici["atamasiz"]
    santiye_b = Site(id=_kimlik(1, 2), project_id=d.proje.id, code="DSC-B", name="B-Blok Şantiyesi")
    proje2 = await project_factory(code="DSC-P02", name="Revizyonsuz Proje")
    santiye_c = Site(id=_kimlik(1, 3), project_id=proje2.id, code="DSC-C", name="C-Blok Şantiyesi")
    session.add_all([santiye_b, santiye_c])
    await session.flush()
    gb, ib = await _grup_ve_kalemler(
        session,
        santiye_b,
        {
            "gruplar": (("gb1", "Betonarme-B"), ("gb2", "Elektrik-B"), ("gb3", "Boya-B")),
            "kalemler": (
                ("ib1", "gb1", "10.001", "Beton B", "m3", 100, 10),
                ("ib2", "gb2", "20.001", "Kablo B", "m", 50, 20),
                ("ib3", "gb3", "30.001", "Boya B", "m2", 40, 30),
            ),
        },
        grup_no=0,
        kalem_no=0,
    )
    _, ic = await _grup_ve_kalemler(
        session,
        santiye_c,
        {
            "gruplar": (("gc1", "Sıva-C"),),
            "kalemler": (("ic1", "gc1", "90.001", "Sıva C", "m2", 10, 100),),
        },
        grup_no=9,
        kalem_no=9,
    )
    bolum_b = Section(
        id=_kimlik(2, 3),
        site_id=santiye_b.id,
        name="B1 Blok",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 15),
        planned_worker_count=6,
        sort_order=1,
    )
    session.add(bolum_b)
    await session.flush()
    session.add_all(
        [
            BoqItemSectionAllocation(
                id=_kimlik(14, 5), boq_item_id=ib["ib1"].id, section_id=bolum_b.id, quantity=D(60)
            ),
            BoqItemSectionAllocation(
                id=_kimlik(14, 6), boq_item_id=ib["ib2"].id, section_id=bolum_b.id, quantity=D(50)
            ),
        ]
    )
    await session.flush()
    await _gunluk(session, santiye_b, 1, ib, {"ib1": 20, "ib2": 10, "ib3": 5}, olusturan)
    await _gunluk(session, santiye_c, 2, ic, {"ic1": 4}, olusturan)
    await _puantaj_b(session, santiye_b, olusturan)
    await _ev_b(client, santiye_b, admin, gb, ib, bolum_b, d)
    await _hakedis(session, d, olusturan)
    await _pm_atamasiz(session, client, user_factory, d)
    for ad in ("civil", "elek", "pm_atamasiz"):
        session.add(
            UserProjectAccess(user_id=d.kullanici[ad].id, project_id=proje2.id, all_projects=False)
        )
    await session.flush()
    x = DunyaB4(d, santiye_b, proje2, santiye_c, bolum_b, gb, ib, ic["ic1"])
    await _etiketle(session, x)
    return x


@pytest.fixture
def sabit_bugun_b4(monkeypatch: pytest.MonkeyPatch):
    """`today()` her yerde `FIXED_TODAY` (işçi sayısı Mayıs 2026 puantajına bağlıdır)."""
    import importlib

    for ad in BUGUN_MODULLERI:
        monkeypatch.setattr(importlib.import_module(ad), "today", lambda: FIXED_TODAY)
    return FIXED_TODAY


@pytest.fixture
async def dunya_b4(
    sabit_bugun_b4, seeded_db: AsyncSession, client: AsyncClient, user_factory, project_factory
) -> DunyaB4:
    d = await kur(seeded_db, client, user_factory, project_factory, yazanlar=True)
    return await genislet_b4(seeded_db, client, user_factory, project_factory, d)


@pytest.fixture
def pm_atamasiz_b4(dunya_b4: DunyaB4) -> dict[str, str]:
    return dunya_b4.d.baslik["pm_atamasiz"]


__all__ = [
    "FIXED_TODAY",
    "DunyaB4",
    "dunya_b4",
    "genislet_b4",
    "pm_atamasiz_b4",
    "pm_atamasiz_ekle",
    "sabit_bugun_b4",
]
