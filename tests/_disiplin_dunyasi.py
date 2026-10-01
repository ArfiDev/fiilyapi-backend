"""DSC-B1 ORTAK "disiplin dünyası" kurucusu (`tests/_yaris.py` emsali: düz async kurucu).

Bir şantiye, iki bölüm, üç grup/kalem ve iki disiplinli bir dünya kurar; hem atamasız
GOLDEN'ı (`tests/discipline_scope/test_b1_golden_atamasiz.py`) hem iki aktörlü görünürlük
testlerini besler. Determinizm için VARLIK KİMLİKLERİ SABİTTİR (`_kimlik`): sıralama
ölçütleri `created_at` (aynı transaction'da hep eşit) sonra `id` olduğundan rastgele
UUID golden'ı çalıştırmadan çalıştırmaya değiştirirdi.

```
Şantiye "A-Blok"   bölümler: S1 04–15.05.2026 · S2 18–29.05.2026
Disiplinler        KAB "Civil" · DUV "Elektrik"
G1 Betonarme → KAB  I1 01.001 "Beton"  m3 100 ₺10  → S1 60 · S2 30 (10 bölümsüz)
G2 Duvar     → DUV  I2 02.001 "Tuğla"  m2  50 ₺20  → S1 50
G3 Eşlemesiz        I3 03.001 "Boya"   m2  40 ₺30  → S2 20     (Ü1: disiplinsiz)
AKTİF baseline: G1→KAB, G2→DUV. TASLAK: G1→DUV (R(site)=aktif ?? taslak ayrışması:
görünürlük AKTİF'e bakar, bütçe ekranı TASLAĞA).
Günlük  E1 05.05 (gönderilmiş, başlık S1): I1·S1 5 · I1·S2 3 · I2·S1 4 · I3·S2 2 · NULL·S1 1
        E2 06.05 (gönderilmiş, başlık S2): I1·bölümsüz 2 · I2·S1 1
        E3 07.05 (taslak, başlık yok)    : I2·S1 6 · I3 bölümsüz 1
Stok    alım (I1 · I2 · NULL) · transfer (NULL) · sarf/düzeltme (I1·S1 · I2·S1 · NULL·S1 · I1·S2)
EV      05.05 dağılımı: Ali → l:I1:S1 5 sa (KAB) · Veli → l:I2:S1 4 sa (DUV)
Aktörler atamasiz (system_admin) · civil / elek (project_manager, aynı proje erişimi,
        UserDiscipline sırasıyla KAB / DUV)
B2 YAZAN aktörler (F1: PM'in günlük yetkisi `view` → yazma testi 403'ü İZİN kapısından alır,
        disiplin kapısı MASKELENİR; bu yüzden yazabilen roller):
        civil_yazar / elek_yazar (patron `_F`, KAB / DUV) · yazar_atamasiz (patron, ATAMASIZ eş:
        pozitif kontrol) · admin_kisitli (system_admin, KAB; boq DELETE / reopen gibi admin
        kapılı uçlar için; atamasız eşi `atamasiz`)
```
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.earned_value.engine import ContractorType
from app.modules.earned_value.models import UserDiscipline
from app.modules.inventory.models import (
    StockCategory,
    StockEntry,
    StockEntryLine,
    StockEntryType,
    StockItem,
    Warehouse,
)
from app.modules.personnel.models import Personnel
from app.modules.projects.models import Project, ProjectContract
from app.modules.site_diary.models import (
    DiaryStatus,
    SiteDiaryEntry,
    SiteDiaryLine,
    SiteDiaryWorkerCount,
    WorkerSource,
)
from app.modules.sites.models import Section, Site
from app.modules.timesheet.models import TimesheetEntry
from app.modules.users.models import User, UserProjectAccess

D = Decimal
SIFRE = "parola1234"
GUN1 = date(2026, 5, 5)
GUN2 = date(2026, 5, 6)
GUN3 = date(2026, 5, 7)
_GONDERIM = datetime(2026, 5, 8, 9, 0, tzinfo=UTC)


def _kimlik(tur: int, sira: int) -> uuid.UUID:
    """Sabit, okunur UUID (golden determinizmi)."""
    return uuid.UUID(f"{tur:08x}-0000-4000-8000-{sira:012x}")


@dataclass
class Dunya:
    """Kurulan dünyanın tutamakları. `etiketler`: UUID → golden etiketi."""

    proje: Project
    santiye: Site
    s1: Section
    s2: Section
    g: dict[str, BoqGroup]
    i: dict[str, BoqItem]
    kab: EvDiscipline
    duv: EvDiscipline
    gunluk: dict[str, SiteDiaryEntry]
    satir: dict[str, SiteDiaryLine]
    stok_hareketi: dict[str, StockEntry]
    kullanici: dict[str, User]
    baslik: dict[str, dict[str, str]]
    yabanci_kalem_kimligi: uuid.UUID = field(default_factory=lambda: _kimlik(99, 1))
    etiketler: dict[uuid.UUID, str] = field(default_factory=dict)


def _ev_url(santiye: Site, kuyruk: str = "") -> str:
    return f"/sites/{santiye.id}/earned-value{kuyruk}"


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": SIFRE})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _kullanicilar(
    session: AsyncSession, client: AsyncClient, user_factory, proje: Project
) -> tuple[dict[str, User], dict[str, dict[str, str]]]:
    """atamasiz (system_admin) · civil · elek (aynı sıradan rol + aynı proje erişimi)."""
    kullanici: dict[str, User] = {}
    for ad, rol in (
        ("atamasiz", "system_admin"),
        ("civil", "project_manager"),
        ("elek", "project_manager"),
    ):
        user = await user_factory(email=f"{ad}@dsc-b1.co", password=SIFRE, role_key=rol)
        if ad != "atamasiz":
            session.add(UserProjectAccess(user_id=user.id, project_id=proje.id, all_projects=False))
        kullanici[ad] = user
    await session.flush()
    baslik = {ad: await _giris(client, f"{ad}@dsc-b1.co") for ad in kullanici}
    return kullanici, baslik


#: F1: (ad, rol, disiplin kodu | None). Sıra SABİT, mevcut aktörlerden SONRA eklenir.
YAZAN_AKTORLER = (
    ("civil_yazar", "patron", "KAB"),
    ("elek_yazar", "patron", "DUV"),
    ("yazar_atamasiz", "patron", None),
    ("admin_kisitli", "system_admin", "KAB"),
)


async def _yazan_aktorler(
    session: AsyncSession, client: AsyncClient, user_factory, proje: Project, kab, duv
) -> tuple[dict[str, User], dict[str, dict[str, str]]]:
    """Yazabilen kısıtlı aktörler + aynı roldeki atamasız eş (F1). Atamalar burada yapılır."""
    disiplin = {"KAB": kab, "DUV": duv}
    kullanici: dict[str, User] = {}
    for ad, rol, kod in YAZAN_AKTORLER:
        user = await user_factory(email=f"{ad}@dsc-b2.co", password=SIFRE, role_key=rol)
        if rol != "system_admin":
            session.add(UserProjectAccess(user_id=user.id, project_id=proje.id, all_projects=False))
        if kod is not None:
            session.add(UserDiscipline(user_id=user.id, discipline_id=disiplin[kod].id))
        kullanici[ad] = user
    await session.flush()
    return kullanici, {ad: await _giris(client, f"{ad}@dsc-b2.co") for ad in kullanici}


async def _boq(session: AsyncSession, santiye: Site, s1: Section, s2: Section):
    gruplar = {
        ad: BoqGroup(id=_kimlik(10, n), site_id=santiye.id, name=ad_tr, sort_order=n)
        for n, (ad, ad_tr) in enumerate(
            (("g1", "Betonarme"), ("g2", "Duvar"), ("g3", "Boya")), start=1
        )
    }
    session.add_all(gruplar.values())
    await session.flush()
    tanim = (
        ("i1", "g1", "01.001", "Beton", "m3", 100, 10),
        ("i2", "g2", "02.001", "Tuğla", "m2", 50, 20),
        ("i3", "g3", "03.001", "Boya", "m2", 40, 30),
    )
    kalemler = {
        ad: BoqItem(
            id=_kimlik(11, n),
            site_id=santiye.id,
            group_id=gruplar[grup].id,
            code=kod,
            description=aciklama,
            unit=birim,
            quantity=D(miktar),
            unit_price=D(fiyat),
            sort_order=1,
        )
        for n, (ad, grup, kod, aciklama, birim, miktar, fiyat) in enumerate(tanim, start=1)
    }
    session.add_all(kalemler.values())
    await session.flush()
    session.add_all(
        [
            BoqItemSectionAllocation(
                id=_kimlik(14, 1), boq_item_id=kalemler["i1"].id, section_id=s1.id, quantity=D(60)
            ),
            BoqItemSectionAllocation(
                id=_kimlik(14, 2), boq_item_id=kalemler["i1"].id, section_id=s2.id, quantity=D(30)
            ),
            BoqItemSectionAllocation(
                id=_kimlik(14, 3), boq_item_id=kalemler["i2"].id, section_id=s1.id, quantity=D(50)
            ),
            BoqItemSectionAllocation(
                id=_kimlik(14, 4), boq_item_id=kalemler["i3"].id, section_id=s2.id, quantity=D(20)
            ),
        ]
    )
    await session.flush()
    await _sozlesme_koprusu(session, santiye, gruplar["g1"], kalemler)
    return gruplar, kalemler


async def _sozlesme_koprusu(session: AsyncSession, santiye: Site, grup, kalemler) -> None:
    """I1 ve I2 işveren sözleşmesi kalemine köprülü (günlük özeti `contract_item_*` alanları,
    K7 ölçümü); I3 köprüsüz."""
    session.add(
        ProjectContract(project_id=santiye.project_id, contract_no="DSC-SZL", amount=D("1000000"))
    )
    await session.flush()
    sozlesme_grubu = EmployerContractGroup(
        id=_kimlik(12, 1), project_id=santiye.project_id, name="A — Betonarme", sort_order=0
    )
    session.add(sozlesme_grubu)
    await session.flush()
    for n, (ad, miktar, fiyat) in enumerate((("i1", 120, 11), ("i2", 60, 22)), start=1):
        kalem = kalemler[ad]
        sozlesme_kalemi = EmployerContractItem(
            id=_kimlik(13, n),
            project_id=santiye.project_id,
            group_id=sozlesme_grubu.id,
            code=f"SZL-{kalem.code}",
            description=f"{kalem.code} sözleşme kalemi",
            unit=kalem.unit,
            quantity=D(miktar),
            unit_price=D(fiyat),
        )
        session.add(sozlesme_kalemi)
        await session.flush()
        kalem.contract_item_id = sozlesme_kalemi.id
    await session.flush()


async def _disiplinler(session: AsyncSession) -> tuple[EvDiscipline, EvDiscipline]:
    kab = EvDiscipline(
        id=_kimlik(20, 1),
        code="KAB",
        name="Civil",
        color="#2563eb",
        default_contractor_type=ContractorType.OWN,
        sort_order=1,
    )
    duv = EvDiscipline(
        id=_kimlik(20, 2),
        code="DUV",
        name="Elektrik",
        color="#16a34a",
        default_contractor_type=ContractorType.SUBCON,
        sort_order=2,
    )
    session.add_all([kab, duv])
    await session.flush()
    session.add_all(
        [
            EvCatalogItem(
                id=_kimlik(21, 1),
                discipline_id=kab.id,
                name="Beton",
                uom="m³",
                standard_unit_mhr=D("1.80"),
                default_contractor_type=ContractorType.OWN,
            ),
            EvCatalogItem(
                id=_kimlik(21, 2),
                discipline_id=duv.id,
                name="Tuğla",
                uom="m2",
                standard_unit_mhr=D("0.55"),
                default_contractor_type=ContractorType.SUBCON,
            ),
        ]
    )
    await session.flush()
    return kab, duv


async def _ev_baseline(
    client: AsyncClient, santiye: Site, admin: dict[str, str], g, i, s1, s2, kab, duv
) -> None:
    """AKTİF baseline (G1→KAB, G2→DUV) + TASLAK (G1→DUV yeniden eşleme)."""
    taban = _ev_url(santiye, "/budget")
    esleme = await client.put(
        f"{taban}/group-disciplines",
        headers=admin,
        json={
            "items": [
                {"boq_group_id": str(g["g1"].id), "discipline_id": str(kab.id)},
                {"boq_group_id": str(g["g2"].id), "discipline_id": str(duv.id)},
            ]
        },
    )
    assert esleme.status_code == 200, esleme.text
    oranlar = await client.patch(
        f"{taban}/leaves",
        headers=admin,
        json={
            "leaves": [
                {"boq_item_id": str(i["i1"].id), "section_id": str(s1.id), "unit_mhr": "2"},
                {"boq_item_id": str(i["i1"].id), "section_id": str(s2.id), "unit_mhr": "2"},
                {"boq_item_id": str(i["i1"].id), "section_id": None, "unit_mhr": "2"},
                {"boq_item_id": str(i["i2"].id), "section_id": str(s1.id), "unit_mhr": "0.5"},
            ]
        },
    )
    assert oranlar.status_code == 200, oranlar.text
    dondur = await client.post(f"{taban}/freeze", headers=admin, json={})
    assert dondur.status_code == 200, dondur.text
    taslak = await client.post(f"{taban}/revisions", headers=admin)
    assert taslak.status_code == 201, taslak.text
    yeniden = await client.put(
        f"{taban}/group-disciplines",
        headers=admin,
        json={"items": [{"boq_group_id": str(g["g1"].id), "discipline_id": str(duv.id)}]},
    )
    assert yeniden.status_code == 200, yeniden.text
    # taslakta bir yaprak oranı değişir: fark ucu (diff) boş kalmasın
    oran = await client.patch(
        f"{taban}/leaves",
        headers=admin,
        json={
            "leaves": [
                {"boq_item_id": str(i["i2"].id), "section_id": str(s1.id), "unit_mhr": "0.6"}
            ]
        },
    )
    assert oran.status_code == 200, oran.text


async def _gunluk(session: AsyncSession, santiye: Site, s1: Section, s2: Section, i, olusturan):
    """Üç günlük kaydı; satır kimlikleri sabit. `None` kalem = bağı kopmuş (NULL) satır."""

    def kayit(n: int, gun: date, bolum, durum: DiaryStatus) -> SiteDiaryEntry:
        gonderildi = durum is DiaryStatus.submitted
        return SiteDiaryEntry(
            id=_kimlik(30, n),
            site_id=santiye.id,
            project_id=santiye.project_id,
            entry_date=gun,
            section_id=None if bolum is None else bolum.id,
            status=durum,
            work_done=f"Gün {n} işleri",
            submitted_at=_GONDERIM if gonderildi else None,
            submitted_by_user_id=olusturan.id if gonderildi else None,
            created_by=olusturan.id,
        )

    kayitlar = {
        "e1": kayit(1, GUN1, s1, DiaryStatus.submitted),
        "e2": kayit(2, GUN2, s2, DiaryStatus.submitted),
        "e3": kayit(3, GUN3, None, DiaryStatus.draft),
    }
    session.add_all(kayitlar.values())
    await session.flush()
    satirlar_tanim = (
        ("e1_i1_s1", "e1", "i1", s1, 5),
        ("e1_i1_s2", "e1", "i1", s2, 3),
        ("e1_i2_s1", "e1", "i2", s1, 4),
        ("e1_i3_s2", "e1", "i3", s2, 2),
        ("e1_null_s1", "e1", None, s1, 1),
        ("e2_i1_yok", "e2", "i1", None, 2),
        ("e2_i2_s1", "e2", "i2", s1, 1),
        ("e3_i2_s1", "e3", "i2", s1, 6),
        ("e3_i3_yok", "e3", "i3", None, 1),
    )
    satirlar: dict[str, SiteDiaryLine] = {}
    for n, (ad, kayit_adi, kalem, bolum, miktar) in enumerate(satirlar_tanim, start=1):
        kalem_nesnesi = None if kalem is None else i[kalem]
        satirlar[ad] = SiteDiaryLine(
            id=_kimlik(31, n),
            entry_id=kayitlar[kayit_adi].id,
            boq_item_id=None if kalem_nesnesi is None else kalem_nesnesi.id,
            section_id=None if bolum is None else bolum.id,
            code="00.000" if kalem_nesnesi is None else kalem_nesnesi.code,
            description="Bağı kopmuş satır" if kalem_nesnesi is None else kalem_nesnesi.description,
            unit="m2" if kalem_nesnesi is None else kalem_nesnesi.unit,
            unit_price=D(5) if kalem_nesnesi is None else kalem_nesnesi.unit_price,
            quantity=D(miktar),
        )
    session.add_all(satirlar.values())
    session.add_all(
        [
            SiteDiaryWorkerCount(
                id=_kimlik(32, 1),
                entry_id=kayitlar["e1"].id,
                trade="Kalıpçı",
                source=WorkerSource.company,
                count=4,
            ),
            SiteDiaryWorkerCount(
                id=_kimlik(32, 2),
                entry_id=kayitlar["e2"].id,
                trade="Betoncu",
                source=WorkerSource.company,
                count=2,
            ),
        ]
    )
    await session.flush()
    return kayitlar, satirlar


async def _puantaj(session: AsyncSession, santiye: Site, olusturan: User) -> dict[str, Personnel]:
    kisiler = {
        ad: Personnel(
            id=_kimlik(40, n),
            full_name=ad,
            trade=meslek,
            source=WorkerSource.company,
            is_active=True,
            is_draft=False,
        )
        for n, (ad, meslek) in enumerate((("Ali Usta", "Kalıpçı"), ("Veli Usta", "Betoncu")), 1)
    }
    session.add_all(kisiler.values())
    await session.flush()
    for n, (ad, saat) in enumerate((("Ali Usta", "5"), ("Veli Usta", "4")), start=1):
        for gun_no, gun in enumerate((GUN1, GUN2), start=1):
            session.add(
                TimesheetEntry(
                    id=_kimlik(41, n * 10 + gun_no),
                    personnel_id=kisiler[ad].id,
                    site_id=santiye.id,
                    project_id=santiye.project_id,
                    work_date=gun,
                    hours=D(saat),
                    created_by=olusturan.id,
                )
            )
    await session.flush()
    return kisiler


async def _ev_gun_dagilimi(client, santiye, admin, i, s1, kisiler) -> None:
    """05.05 dağılımı: Ali → I1·S1 (KAB) · Veli → I2·S1 (DUV). Günlük gönderilmiş."""
    yaprak1 = f"l:{i['i1'].id}:{s1.id}"
    yaprak2 = f"l:{i['i2'].id}:{s1.id}"
    resp = await client.put(
        _ev_url(santiye, f"/days/{GUN1.isoformat()}/allocation"),
        headers=admin,
        json={
            "codes": [
                {"node_id": yaprak1, "rule": "direct"},
                {"node_id": yaprak2, "rule": "direct"},
            ],
            "cells": [
                {
                    "row": {"kind": "personnel", "ref_id": str(kisiler["Ali Usta"].id)},
                    "node_id": yaprak1,
                    "hours": "5",
                },
                {
                    "row": {"kind": "personnel", "ref_id": str(kisiler["Veli Usta"].id)},
                    "node_id": yaprak2,
                    "hours": "4",
                },
            ],
        },
    )
    assert resp.status_code == 200, resp.text


async def _stok(session: AsyncSession, santiye: Site, s1: Section, s2: Section, i):
    santiye_depo = Warehouse(id=_kimlik(50, 1), name="A-Blok Depo", site_id=santiye.id)
    merkez_depo = Warehouse(id=_kimlik(50, 2), name="Merkez Depo", site_id=None)
    kartlar = {
        ad: StockItem(
            id=_kimlik(51, n),
            code=kod,
            name=ad_tr,
            category=StockCategory.structural,
            unit="Adet",
        )
        for n, (ad, kod, ad_tr) in enumerate(
            (("k1", "STK-1", "Çimento"), ("k2", "STK-2", "Tuğla kartı")), start=1
        )
    }
    session.add_all([santiye_depo, merkez_depo, *kartlar.values()])
    await session.flush()

    def hareket(n, tip, gun, depo, kaynak=None) -> StockEntry:
        return StockEntry(
            id=_kimlik(52, n),
            entry_type=tip,
            entry_date=gun,
            warehouse_id=depo.id,
            source_warehouse_id=None if kaynak is None else kaynak.id,
            note=f"Hareket {n}",
        )

    hareketler = {
        "alim": hareket(1, StockEntryType.purchase, date(2026, 5, 2), santiye_depo),
        "transfer": hareket(
            2, StockEntryType.transfer, date(2026, 5, 3), santiye_depo, merkez_depo
        ),
        "sarf": hareket(3, StockEntryType.adjustment, date(2026, 5, 4), santiye_depo),
        "sarf_yabanci": hareket(4, StockEntryType.adjustment, date(2026, 5, 4), santiye_depo),
    }
    session.add_all(hareketler.values())
    await session.flush()
    satirlar = (
        ("alim", "k1", "i1", None, "100", "12.50"),
        ("alim", "k2", "i2", None, "40", "8.00"),
        ("alim", "k1", None, None, "10", "12.50"),
        ("transfer", "k1", None, None, "25", None),
        ("sarf", "k1", "i1", s1, "-30", "12.50"),
        ("sarf", "k2", "i2", s1, "-12", "8.00"),
        ("sarf", "k1", None, s1, "-4", "12.50"),
        ("sarf_yabanci", "k1", "i1", s2, "-6", "12.50"),
        ("sarf_yabanci", "k2", None, s2, "-2", None),
    )
    session.add_all(
        [
            StockEntryLine(
                id=_kimlik(53, n),
                entry_id=hareketler[hareket_adi].id,
                item_id=kartlar[kart].id,
                boq_item_id=None if kalem is None else i[kalem].id,
                section_id=None if bolum is None else bolum.id,
                quantity=D(miktar),
                unit_price=None if fiyat is None else D(fiyat),
            )
            for n, (hareket_adi, kart, kalem, bolum, miktar, fiyat) in enumerate(satirlar, start=1)
        ]
    )
    await session.flush()
    return hareketler


def _etiketle(d: Dunya) -> None:
    """UUID → golden etiketi. Sabit kimlikli her varlık okunur bir ad alır."""
    e = d.etiketler
    e[d.proje.id] = "<proje>"
    e[d.santiye.id] = "<site>"
    e[d.s1.id] = "<S1>"
    e[d.s2.id] = "<S2>"
    for ad, g in d.g.items():
        e[g.id] = f"<{ad.upper()}>"
    for ad, item in d.i.items():
        e[item.id] = f"<{ad.upper()}>"
    e[d.kab.id] = "<KAB>"
    e[d.duv.id] = "<DUV>"
    for ad, kayit in d.gunluk.items():
        e[kayit.id] = f"<entry:{ad}>"
    for ad, satir in d.satir.items():
        e[satir.id] = f"<line:{ad}>"
    for ad, hareket in d.stok_hareketi.items():
        e[hareket.id] = f"<stok:{ad}>"
    for n in range(1, 5):
        e[_kimlik(14, n)] = f"<tahsis:{n}>"
    for n in (1, 2):
        e[_kimlik(21, n)] = f"<katalog:{n}>"
        e[_kimlik(32, n)] = f"<isci_sayisi:{n}>"
        e[_kimlik(40, n)] = f"<kisi:{n}>"
    for n in range(1, 10):
        e[_kimlik(53, n)] = f"<stok_satir:{n}>"
    for n in (1, 2):
        e[_kimlik(51, n)] = f"<kart:k{n}>"
        e[_kimlik(50, n)] = f"<depo:{n}>"
        e[_kimlik(13, n)] = f"<sozlesme_kalemi:{n}>"
    for ad, user in d.kullanici.items():
        e[user.id] = f"<user:{ad}>"
    e[d.yabanci_kalem_kimligi] = "<yok>"


async def kur(
    session: AsyncSession,
    client: AsyncClient,
    user_factory,
    project_factory,
    *,
    yazanlar: bool = False,
) -> Dunya:
    """Dünyayı kurar; `UserDiscipline` atamaları EN SONDA (baseline kurulumu atamasız
    yönetici ile yapılır). `yazanlar=True` (B2 modülleri) F1 yazan aktörlerini de ekler; varsayılan
    KAPALI: `ev_disiplinler.user_count` B1 golden'ında sabittir, ek atama onu değiştirirdi."""
    proje = await project_factory(code="DSC-P01", name="Disiplin Projesi")
    santiye = Site(id=_kimlik(1, 1), project_id=proje.id, code="DSC-A", name="A-Blok Şantiyesi")
    session.add(santiye)
    await session.flush()
    s1 = Section(
        id=_kimlik(2, 1),
        site_id=santiye.id,
        name="A Blok",
        start_date=date(2026, 5, 4),
        end_date=date(2026, 5, 15),
        planned_worker_count=12,
        sort_order=1,
    )
    s2 = Section(
        id=_kimlik(2, 2),
        site_id=santiye.id,
        name="B Blok",
        start_date=date(2026, 5, 18),
        end_date=date(2026, 5, 29),
        planned_worker_count=8,
        sort_order=2,
    )
    session.add_all([s1, s2])
    await session.flush()
    kullanici, baslik = await _kullanicilar(session, client, user_factory, proje)
    g, i = await _boq(session, santiye, s1, s2)
    kab, duv = await _disiplinler(session)
    kisiler = await _puantaj(session, santiye, kullanici["atamasiz"])
    gunluk, satir = await _gunluk(session, santiye, s1, s2, i, kullanici["atamasiz"])
    await _ev_baseline(client, santiye, baslik["atamasiz"], g, i, s1, s2, kab, duv)
    await _ev_gun_dagilimi(client, santiye, baslik["atamasiz"], i, s1, kisiler)
    stok = await _stok(session, santiye, s1, s2, i)
    session.add_all(
        [
            UserDiscipline(user_id=kullanici["civil"].id, discipline_id=kab.id),
            UserDiscipline(user_id=kullanici["elek"].id, discipline_id=duv.id),
        ]
    )
    await session.flush()
    if yazanlar:
        yazan, yazan_baslik = await _yazan_aktorler(session, client, user_factory, proje, kab, duv)
        kullanici = {**kullanici, **yazan}
        baslik = {**baslik, **yazan_baslik}
    dunya = Dunya(
        proje=proje,
        santiye=santiye,
        s1=s1,
        s2=s2,
        g=g,
        i=i,
        kab=kab,
        duv=duv,
        gunluk=gunluk,
        satir=satir,
        stok_hareketi=stok,
        kullanici=kullanici,
        baslik=baslik,
    )
    _etiketle(dunya)
    return dunya


async def kullaniciyi_bul(session: AsyncSession, email: str) -> User:
    return (await session.execute(select(User).where(User.email == email))).scalar_one()
