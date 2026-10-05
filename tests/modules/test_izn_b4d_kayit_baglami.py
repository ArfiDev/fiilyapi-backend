"""IZN-B4d onarımı — saha KAYIT bağlamı: `/equipment/...`, `/purchase-requests/...` yol
parametresi kaydın KENDİ projesini çözer; maske ve yazma kapısı o projedeki rolle karar verir.

Dünya: kişinin ANA rolü (site_chief) `maliyet_kar` GİZLİ; A projesinde gizlisiz bir EKİP rolü var,
B projesinde yalnız ana rol. Çözücüsüz çekirdekte yol bağlamı yoktu: maske iki rolün birleşimi
(her yerde gizli), kapı yalnız ana rolle karar verirdi.

POZİTİF KONTROL: A'daki kayıt görülür/yazılır (yol çözücüsü çalışıyor). NEGATİF: B'deki kayıt
gizli/403. Oluşturma uçları (POST) gövdedeki `site_id`/`project_id`den bağlam çözmeye devam eder.
(Gövde ile kaydı başka projeye TAŞIMA saldırısı IZN-HF1'in matrisindedir, burada YOK.)
"""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.sayfalar import HiddenCategory
from app.modules.equipment.models import (
    Equipment,
    EquipmentCategory,
    EquipmentFuelLog,
    EquipmentOwnership,
    EquipmentRatePeriod,
    EquipmentRentalInvoice,
    EquipmentRentalInvoiceLine,
    EquipmentStatus,
    RentalInvoiceStatus,
    RentalLineKind,
)
from app.modules.procurement.models import (
    PaymentTerms,
    PurchasePriority,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
    Supplier,
)
from app.modules.sites.models import Site
from app.modules.users.models import ProjectMember
from tests._hassas_alan import rol_gizle, rol_gizli


class Dunya:
    """A (ekip rolü gizlisiz) ve B (yalnız gizli ana rol) projelerindeki saha kayıtları."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.site: dict[str, Site] = {}
        self.makine: dict[str, Equipment] = {}
        self.yakit: dict[str, EquipmentFuelLog] = {}
        self.fatura: dict[str, EquipmentRentalInvoice] = {}
        self.kalem: dict[str, EquipmentRentalInvoiceLine] = {}
        self.talep: dict[str, PurchaseRequest] = {}
        self.talep_satiri: dict[str, PurchaseRequestLine] = {}


@pytest.fixture
async def dunya(seeded_db, client, user_factory, project_factory) -> Dunya:
    d = Dunya()
    projeler = {
        "A": await project_factory(code="KB-A", name="Kayıt Bağlamı A"),
        "B": await project_factory(code="KB-B", name="Kayıt Bağlamı B"),
    }
    email = "kayit@izn.co"
    user = await user_factory(email=email, password="parola1234", role_key="site_chief")
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    acik = await rol_gizli(seeded_db, "acik_kayit_rol", [])
    seeded_db.add(ProjectMember(user_id=user.id, project_id=projeler["A"].id, role_id=acik.id))
    seeded_db.add(ProjectMember(user_id=user.id, project_id=projeler["B"].id, role_id=user.role_id))
    tedarikci = Supplier(name="Kayıt Tedarikçi", payment_terms=PaymentTerms.days_30)
    seeded_db.add(tedarikci)
    await seeded_db.flush()
    for ad, proje in projeler.items():
        site = Site(project_id=proje.id, code=f"KB-{ad}", name=f"Şantiye {ad}")
        seeded_db.add(site)
        await seeded_db.flush()
        d.site[ad] = site
        makine = Equipment(
            name=f"Makine {ad}",
            category=EquipmentCategory.machinery,
            ownership=EquipmentOwnership.owned,
            status=EquipmentStatus.working,
            purchase_amount=Decimal("4250000.00"),
            site_id=site.id,
        )
        seeded_db.add(makine)
        await seeded_db.flush()
        d.makine[ad] = makine
        yakit = EquipmentFuelLog(
            equipment_id=makine.id,
            fuel_date=date(2026, 7, 5),
            site_id=site.id,
            liters=Decimal("100.000"),
            unit_price=Decimal("40.0000"),
        )
        fatura = EquipmentRentalInvoice(
            supplier_id=tedarikci.id,
            invoice_no=f"KB-{ad}",
            invoice_amount=Decimal("100000.00"),
            period_year=2026,
            period_month=7,
            site_id=site.id,
            rate_period=EquipmentRatePeriod.hourly,
            vat_rate=Decimal("0.20"),
            status=RentalInvoiceStatus.draft,
        )
        talep = PurchaseRequest(
            request_no=f"SAT-KB-{ad}",
            request_date=date(2026, 8, 12),
            priority=PurchasePriority.normal,
            project_id=proje.id,
            status=PurchaseRequestStatus.draft,
            created_by_user_id=user.id,
        )
        seeded_db.add_all([yakit, fatura, talep])
        await seeded_db.flush()
        d.yakit[ad], d.fatura[ad], d.talep[ad] = yakit, fatura, talep
        kalem = EquipmentRentalInvoiceLine(
            invoice_id=fatura.id,
            equipment_id=makine.id,
            line_kind=list(RentalLineKind)[0],
            site_id=site.id,
            worked_hours=Decimal("10.00"),
            breakdown_hours=Decimal("0.00"),
            rate_amount=Decimal("320.00"),
        )
        satir = PurchaseRequestLine(
            request_id=talep.id,
            free_text_name="Çimento",
            free_text_unit="Ton",
            quantity=Decimal("4.000"),
            estimated_unit_price=Decimal("1000.00"),
            sort_order=0,
        )
        seeded_db.add_all([kalem, satir])
        await seeded_db.flush()
        d.kalem[ad], d.talep_satiri[ad] = kalem, satir
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    d.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    return d


# (kayıt, GET yolu, okunan alan, PATCH yolu, PATCH gövdesi) — kayıt başına bir okuma + yazma.
def _durumlar(d: Dunya, ad: str) -> list[tuple[str, str, str, str, dict]]:
    return [
        (
            "ekipman",
            f"/equipment/{d.makine[ad].id}",
            "purchase_amount",
            f"/equipment/{d.makine[ad].id}",
            {"purchase_amount": "5.00"},
        ),
        (
            "yakit",
            f"/equipment/fuel-logs/{d.yakit[ad].id}",
            "amount",
            f"/equipment/fuel-logs/{d.yakit[ad].id}",
            {"unit_price": "50.0000"},
        ),
        (
            "kira_faturasi",
            f"/equipment/rental-invoices/{d.fatura[ad].id}",
            "invoice_amount",
            f"/equipment/rental-invoices/{d.fatura[ad].id}",
            {"invoice_amount": "1.00"},
        ),
        (
            "kira_kalemi",
            f"/equipment/rental-invoices/{d.fatura[ad].id}",
            "invoice_amount",
            f"/equipment/rental-invoice-lines/{d.kalem[ad].id}",
            {"rate_amount": "1.00"},
        ),
        (
            "talep",
            f"/purchase-requests/{d.talep[ad].id}",
            "estimated_total",
            f"/purchase-requests/{d.talep[ad].id}",
            {"lines": [{"id": "SATIR", "estimated_unit_price": "9.00"}]},
        ),
    ]


def _govde(d: Dunya, ad: str, govde: dict) -> dict:
    """Talep PATCH gövdesindeki `SATIR` yer tutucusunu gerçek satır kimliğiyle doldurur."""
    if "lines" not in govde:
        return govde
    return {"lines": [{**s, "id": str(d.talep_satiri[ad].id)} for s in govde["lines"]]}


@pytest.mark.asyncio
@pytest.mark.parametrize("kayit", ["ekipman", "yakit", "kira_faturasi", "kira_kalemi", "talep"])
async def test_a_projesindeki_kayit_gorunur_ve_yazilir_pozitif_kontrol(client, dunya, kayit):
    for ad_, get_yol, alan, patch_yol, govde in _durumlar(dunya, "A"):
        if ad_ != kayit:
            continue
        oku = await client.get(get_yol, headers=dunya.headers)
        assert oku.status_code == 200, oku.text
        assert oku.json()[alan] is not None, f"{kayit}: A'da {alan} görünmeli (çözücü çalışmıyor)"
        yaz = await client.patch(patch_yol, json=_govde(dunya, "A", govde), headers=dunya.headers)
        assert yaz.status_code == 200, f"{kayit}: A'da yazma {yaz.status_code} {yaz.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("kayit", ["ekipman", "yakit", "kira_faturasi", "kira_kalemi", "talep"])
async def test_b_projesindeki_kayit_maskeli_ve_yazma_403(client, dunya, kayit):
    for ad_, get_yol, alan, patch_yol, govde in _durumlar(dunya, "B"):
        if ad_ != kayit:
            continue
        oku = await client.get(get_yol, headers=dunya.headers)
        assert oku.status_code == 200, oku.text
        assert oku.json()[alan] is None, f"{kayit}: B'de {alan} gizli olmalı"
        yaz = await client.patch(patch_yol, json=_govde(dunya, "B", govde), headers=dunya.headers)
        assert yaz.status_code == 403, f"{kayit}: B'de yazma {yaz.status_code} {yaz.text}"


@pytest.mark.asyncio
async def test_teklif_alt_yolu_talebin_projesini_izler(client, dunya, seeded_db):
    """`/purchase-requests/{rid}/quotes` talebin projesinde: A'da tutar görünür, B'de gizli."""
    from app.modules.procurement.models import PurchaseQuote

    tedarikci = (await seeded_db.execute(select(Supplier))).scalars().first()
    for ad in ("A", "B"):
        seeded_db.add(
            PurchaseQuote(
                request_id=dunya.talep[ad].id,
                supplier_id=tedarikci.id,
                unit_price=Decimal("1250.00"),
                delivery_time="3 gün",
                payment_terms=PaymentTerms.days_30,
                shipping_included=True,
            )
        )
    await seeded_db.flush()
    a = await client.get(f"/purchase-requests/{dunya.talep['A'].id}/quotes", headers=dunya.headers)
    b = await client.get(f"/purchase-requests/{dunya.talep['B'].id}/quotes", headers=dunya.headers)
    assert a.status_code == b.status_code == 200
    assert a.json()["items"][0]["unit_price"] == "1250.00"
    assert b.json()["items"][0]["unit_price"] is None


@pytest.mark.asyncio
async def test_olusturma_uclari_govdeden_baglam_cozmeye_devam_eder(client, dunya):
    """POST: yol parametresi yok → gövdedeki `site_id` / `project_id` bağlamı verir.

    Oluşturma yazması serbesttir (IZN-B4d kararı: yalnız PATCH/PUT kapısı); yanıt maskesi bağlama
    göre değişir: A'da tutar görünür, B'de `null`.
    """
    for ad, gorunur in (("A", True), ("B", False)):
        makine = await client.post(
            "/equipment",
            json={
                "name": f"Yeni {ad}",
                "category": "machinery",
                "ownership": "owned",
                "purchase_amount": "10.00",
                "site_id": str(dunya.site[ad].id),
            },
            headers=dunya.headers,
        )
        assert makine.status_code == 201, makine.text
        assert (makine.json()["purchase_amount"] == "10.00") is gorunur, f"ekipman {ad}"
        talep = await client.post(
            "/purchase-requests",
            json={
                "project_id": str(dunya.site[ad].project_id),
                "lines": [
                    {
                        "free_text_name": "K",
                        "free_text_unit": "Adet",
                        "quantity": "1",
                        "estimated_unit_price": "5.00",
                    }
                ],
            },
            headers=dunya.headers,
        )
        assert talep.status_code == 201, talep.text
        toplam = talep.json()["estimated_total"]
        assert (toplam is not None and Decimal(toplam) == Decimal("5")) is gorunur, f"talep {ad}"


def test_resolvers_saha_anahtarlari_var():
    from app.modules.projects.context import RESOLVERS

    beklenen = {
        ("/equipment", "equipment_id"),
        ("/equipment", "log_id"),
        ("/equipment", "invoice_id"),
        ("/equipment", "line_id"),
        ("/purchase-requests", "request_id"),
        ("/purchase-orders", "order_id"),
    }
    assert beklenen <= set(RESOLVERS)
