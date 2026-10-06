"""IZN-B5f madde 22 (CEO kararı 22-iii) — projeler arası TAŞIMA yapan PATCH uçları.

Kural: (a) taşıma yanıtı kaydın ESKİ bağlamıyla maskelenir (satır başına maske hedef projenin
rolüne geçmez) · (b) taşıma = hedefte de Düzenler: hedefte kapının sayfalarında Düzenler yoksa
404 (hedef Görür/Hiçbir şey de 404 — varlık sızmaz) · SisYön her yerde geçer · taşımasız PATCH
davranışı değişmez.

7 uç (`IZN-B5e-OLCUM.md` §22.1): fatura, çek/senet, satınalma talebi, makine, çalışma kaydı,
yakıt kaydı, kira faturası.

BİLİNÇLİ KABUL (CEO): kayıt B'ye taşındıktan SONRA B'deki rol B'nin tutarını GET'te görür
(`test_seed_*` belgeler); yanıt eski bağlamla maskelidir ama kayıt artık B'nindir.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select

from app.core.sayfalar import HiddenCategory, PageLevel
from app.modules.equipment.models import (
    Equipment,
    EquipmentCategory,
    EquipmentFuelLog,
    EquipmentOwnership,
    EquipmentRatePeriod,
    EquipmentRentalInvoice,
    EquipmentWorkLog,
    RentalInvoiceStatus,
    WorkLogType,
)
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceLine,
    InvoiceStatus,
)
from app.modules.procurement.models import PaymentTerms, Supplier
from app.modules.roles import seed_data
from app.modules.roles.models import Role
from app.modules.sites.models import Site
from app.modules.treasury.models import (
    FinancialInstrument,
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
)
from tests._hassas_alan import rol_gizli
from tests._proje_ekibi import ekibe_ekle

PASSWORD = "parola1234"
_GIZLI = [HiddenCategory.tum_tutarlar, HiddenCategory.maliyet_kar, HiddenCategory.banka_kasa]
_BULUNAMADI = 404


async def _giris(client, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _santiye(session, proje, kod: str) -> Site:
    site = Site(project_id=proje.id, code=kod, name=f"{kod} Şantiyesi")
    session.add(site)
    await session.flush()
    return site


@dataclass
class Dunya:
    session: Any
    client: Any
    a: Any
    b: Any
    site_a: Site
    site_b: Site
    kisi_basligi: dict[str, str]
    admin_basligi: dict[str, str]
    kisi: Any
    sayac: int


@pytest.fixture
async def kur(seeded_db, user_factory, project_factory, client):
    """`kur(hedef_duzeyi)`: ana rol + A ekip rolü = Düzenler, tutarlar GİZLİ; B ekip rolü
    `hedef_duzeyi` (gizlisi YOK). Kişi A ve B ekibinde; SisYön ayrı."""
    await seed_data.seed_izn_reference_data(seeded_db)
    sayac = {"n": 0}

    async def _kur(hedef_duzeyi: PageLevel, kaynak_duzeyi: PageLevel = PageLevel.edit) -> Dunya:
        sayac["n"] += 1
        n = sayac["n"]
        a = await project_factory(f"B5F-A{n}", name=f"A{n}")
        b = await project_factory(f"B5F-B{n}", name=f"B{n}")
        site_a = await _santiye(seeded_db, a, f"FA{n}")
        site_b = await _santiye(seeded_db, b, f"FB{n}")
        ana = await rol_gizli(seeded_db, f"b5f_ana_{n}", _GIZLI, PageLevel.edit)
        ekip_a = await rol_gizli(seeded_db, f"b5f_ekip_a_{n}", _GIZLI, kaynak_duzeyi)
        ekip_b = await rol_gizli(seeded_db, f"b5f_ekip_b_{n}", [], hedef_duzeyi)
        kisi = await user_factory(email=f"kisi{n}@b5f.co", password=PASSWORD, role_key=ana.key)
        await user_factory(email=f"admin{n}@b5f.co", password=PASSWORD, role_key="system_admin")
        await ekibe_ekle(seeded_db, kisi, a.id, ekip_a.id)
        await ekibe_ekle(seeded_db, kisi, b.id, ekip_b.id)
        return Dunya(
            seeded_db,
            client,
            a,
            b,
            site_a,
            site_b,
            await _giris(client, f"kisi{n}@b5f.co"),
            await _giris(client, f"admin{n}@b5f.co"),
            kisi,
            n,
        )

    return _kur


# --------------------------------------------------------------------------- #
# Kayıt kurucuları (kayıt A projesinde / A şantiyesinde)
# --------------------------------------------------------------------------- #


async def _fatura(d: Dunya, proje_id=None) -> tuple[str, str]:
    fatura = Invoice(
        direction=InvoiceDirection.outgoing,
        invoice_no=f"B5F-{d.sayac}-{'P' if proje_id else 'S'}",
        document_type=InvoiceDocumentType.einvoice,
        status=InvoiceStatus.draft,
        issue_date=date(2026, 8, 1),
        party_name="Karşı",
        subtotal=Decimal("100.00"),
        advance_amount=Decimal("0.00"),
        retention_amount=Decimal("0.00"),
        tax_base=Decimal("100.00"),
        vat_amount=Decimal("20.00"),
        withholding_amount=Decimal("0.00"),
        total=Decimal("120.00"),
        created_by_id=d.kisi.id,
        project_id=proje_id,
    )
    d.session.add(fatura)
    await d.session.flush()
    d.session.add(
        InvoiceLine(
            invoice_id=fatura.id,
            sort_order=0,
            description="Kalem",
            quantity=Decimal("1"),
            unit_price=Decimal("100.00"),
            vat_rate=Decimal("20"),
            line_total=Decimal("100.00"),
        )
    )
    await d.session.flush()
    return f"/invoices/{fatura.id}", "total"


async def _fatura_a(d: Dunya) -> tuple[str, str]:
    return await _fatura(d, d.a.id)


async def _cek(d: Dunya) -> tuple[str, str]:
    evrak = FinancialInstrument(
        instrument_kind=FinancialInstrumentKind.cheque,
        direction=FinancialInstrumentDirection.received,
        serial_no=f"B5F{d.sayac:07d}",
        drawer_name="Keşideci",
        issue_date=date(2026, 7, 1),
        due_date=date(2026, 7, 25),
        amount=Decimal("1200.00"),
        status=FinancialInstrumentStatus.portfolio,
        project_id=d.a.id,
    )
    d.session.add(evrak)
    await d.session.flush()
    return f"/financial-instruments/{evrak.id}", "amount"


async def _talep(d: Dunya) -> tuple[str, str]:
    yanit = await d.client.post(
        "/purchase-requests",
        json={
            "project_id": str(d.a.id),
            "lines": [
                {
                    "free_text_name": "Çimento",
                    "free_text_unit": "ton",
                    "quantity": "2",
                    "estimated_unit_price": "50",
                }
            ],
        },
        headers=d.admin_basligi,
    )
    assert yanit.status_code == 201, yanit.text
    return f"/purchase-requests/{yanit.json()['id']}", "estimated_total"


async def _makine_kaydi(d: Dunya) -> Equipment:
    makine = Equipment(
        name="Ekskavatör",
        category=EquipmentCategory.machinery,
        ownership=EquipmentOwnership.owned,
        purchase_amount=Decimal("4250000.00"),
        site_id=d.site_a.id,
    )
    d.session.add(makine)
    await d.session.flush()
    return makine


async def _makine(d: Dunya) -> tuple[str, str]:
    makine = await _makine_kaydi(d)
    return f"/equipment/{makine.id}", "purchase_amount"


async def _calisma(d: Dunya) -> tuple[str, str | None]:
    makine = await _makine_kaydi(d)
    kayit = EquipmentWorkLog(
        equipment_id=makine.id,
        work_date=date(2026, 7, 6),
        site_id=d.site_a.id,
        record_type=WorkLogType.worked,
        hours=Decimal("8.00"),
    )
    d.session.add(kayit)
    await d.session.flush()
    return f"/equipment/work-logs/{kayit.id}", None


async def _yakit(d: Dunya) -> tuple[str, str]:
    makine = await _makine_kaydi(d)
    kayit = EquipmentFuelLog(
        equipment_id=makine.id,
        fuel_date=date(2026, 7, 5),
        site_id=d.site_a.id,
        liters=Decimal("100.000"),
        unit_price=Decimal("40.0000"),
    )
    d.session.add(kayit)
    await d.session.flush()
    return f"/equipment/fuel-logs/{kayit.id}", "amount"


async def _kira(d: Dunya) -> tuple[str, str]:
    tedarikci = Supplier(name="Kira Tedarikçi", payment_terms=PaymentTerms.days_30)
    d.session.add(tedarikci)
    await d.session.flush()
    fatura = EquipmentRentalInvoice(
        supplier_id=tedarikci.id,
        invoice_no=f"KF-B5F-{d.sayac}",
        invoice_amount=Decimal("100000.00"),
        period_year=2026,
        period_month=7,
        site_id=d.site_a.id,
        rate_period=EquipmentRatePeriod.hourly,
        vat_rate=Decimal("0.20"),
        status=RentalInvoiceStatus.draft,
    )
    d.session.add(fatura)
    await d.session.flush()
    return f"/equipment/rental-invoices/{fatura.id}", "invoice_amount"


@dataclass(frozen=True)
class Uc:
    ad: str
    kurucu: Callable[[Dunya], Awaitable[tuple[str, str | None]]]
    hedef_alani: str  # gövdedeki taşıma anahtarı ("project_id" | "site_id")
    tasimasiz_govde: dict[str, Any]

    def tasima_govdesi(self, d: Dunya) -> dict[str, str]:
        hedef = d.b.id if self.hedef_alani == "project_id" else d.site_b.id
        return {self.hedef_alani: str(hedef)}


UCLAR = [
    Uc("fatura", _fatura_a, "project_id", {"note": "düzeltme"}),
    Uc("cek_senet", _cek, "project_id", {"description": "düzeltme"}),
    Uc("satinalma_talebi", _talep, "project_id", {"justification": "düzeltme"}),
    Uc("makine", _makine, "site_id", {"name": "Ekskavatör 2"}),
    Uc("calisma_kaydi", _calisma, "site_id", {"hours": "7.00"}),
    Uc("yakit_kaydi", _yakit, "site_id", {"note": "düzeltme"}),
    Uc("kira_faturasi", _kira, "site_id", {"invoice_no": "KF-2"}),
]
_UC_KIMLIK = [u.ad for u in UCLAR]


def _alan(yanit, alan: str | None):
    return None if alan is None else yanit.json().get(alan)


# --------------------------------------------------------------------------- #
# (b) taşıma = hedefte de Düzenler
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hedef_duzeyi", [PageLevel.none, PageLevel.view])
@pytest.mark.parametrize("uc", UCLAR, ids=_UC_KIMLIK)
async def test_hedefte_duzenler_yoksa_tasima_404(kur, uc: Uc, hedef_duzeyi) -> None:
    d = await kur(hedef_duzeyi)
    yol, _ = await uc.kurucu(d)

    yanit = await d.client.patch(yol, json=uc.tasima_govdesi(d), headers=d.kisi_basligi)

    assert yanit.status_code == _BULUNAMADI, yanit.text
    # Taşıma OLMADI: SisYön kaydı hâlâ A'da görür (yanıt şemasında proje varsa).
    okuma = await d.client.get(yol, headers=d.admin_basligi)
    if okuma.status_code == 200 and "project_id" in okuma.json():
        assert okuma.json()["project_id"] == str(d.a.id)


@pytest.mark.parametrize("uc", UCLAR, ids=_UC_KIMLIK)
async def test_hedefte_duzenler_varsa_tasima_200_yanit_eski_baglamla_maskeli(kur, uc: Uc) -> None:
    d = await kur(PageLevel.edit)
    yol, hassas = await uc.kurucu(d)

    yanit = await d.client.patch(yol, json=uc.tasima_govdesi(d), headers=d.kisi_basligi)

    assert yanit.status_code == 200, yanit.text
    if hassas is not None:
        # (a): kayıt B'de ama yanıt A'nın (eski bağlam) gizli kümesiyle maskeli.
        assert _alan(yanit, hassas) is None, f"{hassas} SIZDI: {yanit.json()[hassas]}"


@pytest.mark.parametrize("uc", UCLAR, ids=_UC_KIMLIK)
async def test_sisyon_tasir_ve_tutari_gorur(kur, uc: Uc) -> None:
    d = await kur(PageLevel.none)
    yol, hassas = await uc.kurucu(d)

    yanit = await d.client.patch(yol, json=uc.tasima_govdesi(d), headers=d.admin_basligi)

    assert yanit.status_code == 200, yanit.text
    if hassas is not None:
        assert _alan(yanit, hassas) is not None


@pytest.mark.parametrize("uc", UCLAR, ids=_UC_KIMLIK)
async def test_tasimasiz_patch_davranisi_degismez(kur, uc: Uc) -> None:
    """Hedefi olmayan PATCH: hedef kapısı ve eski bağlam bayrağı DEVREYE GİRMEZ (kontrol)."""
    d = await kur(PageLevel.none)  # hedefte hiçbir şey olsa da taşıma yok → 200
    yol, hassas = await uc.kurucu(d)

    yanit = await d.client.patch(yol, json=uc.tasimasiz_govde, headers=d.kisi_basligi)

    assert yanit.status_code == 200, yanit.text
    if hassas is not None:
        assert _alan(yanit, hassas) is None


@pytest.mark.parametrize("uc", UCLAR[:3], ids=_UC_KIMLIK[:3])
async def test_ayni_projeye_tasima_hedef_kapisina_takilmaz(kur, uc: Uc) -> None:
    """P == Q taşıma DEĞİLDİR: aynı projeye 'taşıma' hedef kapısına takılmaz."""
    d = await kur(PageLevel.none)
    yol, _ = await uc.kurucu(d)

    yanit = await d.client.patch(yol, json={"project_id": str(d.a.id)}, headers=d.kisi_basligi)

    assert yanit.status_code == 200, yanit.text


# --------------------------------------------------------------------------- #
# Projesizden projeye (fatura senaryosu, ölçüm belgesi §22.2)
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("hedef_duzeyi", [PageLevel.none, PageLevel.view])
async def test_sirket_faturasi_hedefte_duzenler_yoksa_tasinamaz(kur, hedef_duzeyi) -> None:
    d = await kur(hedef_duzeyi)
    yol, _ = await _fatura(d, None)

    yanit = await d.client.patch(yol, json={"project_id": str(d.b.id)}, headers=d.kisi_basligi)

    assert yanit.status_code == _BULUNAMADI, yanit.text  # önce: 200 + total="120.00"


async def test_sirket_faturasi_hedefte_duzenler_varsa_tasinir_yanit_maskeli(kur) -> None:
    d = await kur(PageLevel.edit)
    yol, _ = await _fatura(d, None)

    yanit = await d.client.patch(yol, json={"project_id": str(d.b.id)}, headers=d.kisi_basligi)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["project_id"] == str(d.b.id)
    assert yanit.json()["total"] is None


async def test_projeye_bagli_kayit_projesize_tasima_hedef_kapisina_takilmaz(kur) -> None:
    """Hedef `null` (projesiz): kapı zaten ana rolle (HF1: bağlam None) karar verir; ek hedef
    kapısı YOK (mevcut davranış korunur)."""
    d = await kur(PageLevel.none)
    yol, _ = await _fatura(d, d.a.id)

    yanit = await d.client.patch(yol, json={"project_id": None}, headers=d.kisi_basligi)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] is None


# --------------------------------------------------------------------------- #
# SEED rolleriyle vaka (ölçüm §22.3): bilinçli kabul belgelenir
# --------------------------------------------------------------------------- #


async def test_seed_planning_engineer_to_project_manager_yanit_maskeli_sonra_b_gorur(
    client, seeded_db, user_factory, project_factory
) -> None:
    await seed_data.seed_izn_reference_data(seeded_db)
    a = await project_factory("B5F-SA", name="SA")
    b = await project_factory("B5F-SB", name="SB")
    await user_factory(email="sa@b5f.co", password=PASSWORD, role_key="system_admin")
    kisi = await user_factory(email="pe@b5f.co", password=PASSWORD, role_key="planning_engineer")
    rol = {
        k: (await seeded_db.execute(select(Role).where(Role.key == k))).scalar_one().id
        for k in ("planning_engineer", "project_manager")
    }
    await ekibe_ekle(seeded_db, kisi, a.id, rol["planning_engineer"])
    await ekibe_ekle(seeded_db, kisi, b.id, rol["project_manager"])
    admin = await _giris(client, "sa@b5f.co")
    olustur = await client.post(
        "/purchase-requests",
        json={
            "project_id": str(a.id),
            "lines": [
                {
                    "free_text_name": "Çimento",
                    "free_text_unit": "ton",
                    "quantity": "2",
                    "estimated_unit_price": "50",
                }
            ],
        },
        headers=admin,
    )
    assert olustur.status_code == 201, olustur.text
    talep = olustur.json()["id"]
    baslik = await _giris(client, "pe@b5f.co")

    once = await client.get(f"/purchase-requests/{talep}", headers=baslik)
    assert once.status_code == 200 and once.json()["estimated_total"] is None

    tasima = await client.patch(
        f"/purchase-requests/{talep}", json={"project_id": str(b.id)}, headers=baslik
    )

    assert tasima.status_code == 200, tasima.text  # hedefte (project_manager) Düzenler var
    assert tasima.json()["project_id"] == str(b.id)
    assert tasima.json()["estimated_total"] is None  # yanıt ESKİ bağlamla maskeli
    # BİLİNÇLİ KABUL (CEO 22): kayıt artık B'nin; B'deki rol B'nin tutarını görür.
    sonra = await client.get(f"/purchase-requests/{talep}", headers=baslik)
    assert sonra.status_code == 200
    assert sonra.json()["estimated_total"] is not None


# --------------------------------------------------------------------------- #
# (c) kaynakta da yazma yetkisi (HF1: taşımada bağlam None → kapı ana rolle geçer)
# --------------------------------------------------------------------------- #

_YETKISIZ = "Bu işlem için yetkiniz yok"


@pytest.mark.parametrize("kaynak_duzeyi", [PageLevel.none, PageLevel.view])
@pytest.mark.parametrize("uc", UCLAR, ids=_UC_KIMLIK)
async def test_kaynakta_duzenler_yoksa_tasima_403_kayit_yerinde(kur, uc: Uc, kaynak_duzeyi) -> None:
    d = await kur(PageLevel.edit, kaynak_duzeyi)  # hedefte Düzenler VAR, kaynakta yok
    yol, _ = await uc.kurucu(d)

    tasimasiz = await d.client.patch(yol, json=uc.tasimasiz_govde, headers=d.kisi_basligi)
    yanit = await d.client.patch(yol, json=uc.tasima_govdesi(d), headers=d.kisi_basligi)

    assert tasimasiz.status_code == 403, tasimasiz.text  # taşımasız PATCH zaten reddedilir
    assert yanit.status_code == 403, yanit.text
    assert yanit.json() == tasimasiz.json()
    assert yanit.json()["detail"] == _YETKISIZ
    okuma = await d.client.get(yol, headers=d.admin_basligi)
    if okuma.status_code == 200 and "project_id" in okuma.json():
        assert okuma.json()["project_id"] == str(d.a.id)


@pytest.mark.parametrize("kaynak_duzeyi", [PageLevel.none, PageLevel.view])
@pytest.mark.parametrize("uc", UCLAR[:3], ids=_UC_KIMLIK[:3])
async def test_kaynakta_duzenler_yoksa_projesize_tasima_403(kur, uc: Uc, kaynak_duzeyi) -> None:
    d = await kur(PageLevel.edit, kaynak_duzeyi)
    yol, _ = await uc.kurucu(d)

    yanit = await d.client.patch(yol, json={"project_id": None}, headers=d.kisi_basligi)

    assert yanit.status_code == 403, yanit.text


async def test_hedef_404_govdesi_modulun_kendi_metniyle_ayni(kur, project_factory) -> None:
    """Var olmayan, üye olunmayan ve Görür'lü hedef AYNI 404 gövdesini verir (proje + şantiye)."""
    import uuid

    for uc, bos in ((UCLAR[0], "project_id"), (UCLAR[3], "site_id")):
        gorur = await kur(PageLevel.view)
        yol, _ = await uc.kurucu(gorur)
        c = await project_factory(f"B5F-C{gorur.sayac}", name="C")
        site_c = await _santiye(gorur.session, c, f"FC{gorur.sayac}")
        hedefler = {
            "gorur": uc.tasima_govdesi(gorur),
            "uye_degil": {bos: str(c.id if bos == "project_id" else site_c.id)},
            "yok": {bos: str(uuid.uuid4())},
        }
        govdeler = {}
        for ad, govde in hedefler.items():
            yanit = await gorur.client.patch(yol, json=govde, headers=gorur.kisi_basligi)
            assert yanit.status_code == 404, f"{uc.ad}/{ad}: {yanit.text}"
            govdeler[ad] = yanit.json()
        assert govdeler["gorur"] == govdeler["uye_degil"] == govdeler["yok"], uc.ad
