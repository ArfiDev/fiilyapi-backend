"""IZN-B5a — çözücüsüz PROJEYE BAĞLI uçlar (18 sızıntı) + `root_path` sağlamlaştırması (23c).

Dünya: kişinin ANA rolü hiçbir sayfayı AÇMAZ; A projesinde her sayfası düzenlenebilir bir EKİP
rolü var, B projesinde yalnız ana rol. Çözücüsüz çekirdekte yol bağlamı yoktu: kapı yalnız ANA
rolle karar verirdi (A'da da 403) — ya da gövdeden bağlam alan uçta ekip rolü atlatılırdı.

POZİTİF KONTROL: A'daki kayda erişim (200/204) — çözücü çalışıyor. NEGATİF: B'deki kayıt 403.
Şirket geneli kayıtlar (stok kartı, EV disiplin/katalog, merkez depo) projeden BAĞIMSIZDIR → `None`.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from fastapi import Request
from sqlalchemy import select

from app.core.sayfalar import PageLevel
from app.modules.catalog.models import EvCatalogItem, EvDiscipline
from app.modules.contracts.models import SubcontractorContract
from app.modules.customers.models import Customer, CustomerType
from app.modules.documents.models import EntityDocumentScope, EntityDocumentType
from app.modules.documents.models.links import (
    SectionDocument,
    SubcontractorContractDocument,
    UnitDocument,
    UnitSaleDocument,
)
from app.modules.inventory.models import StockCategory, StockItem, Warehouse
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceStatus,
)
from app.modules.projects.context import RESOLVERS, request_project, resolve_project
from app.modules.sales.models import SaleType, UnitSale, UnitSaleStatus
from app.modules.sites.models import Section, Site
from app.modules.treasury.models import BankAccount, BankAccountType, Payment, PaymentMethodKind
from app.modules.units.models import Block, Unit, UnitKind
from app.modules.users.models import ProjectMember
from tests._ekip_dunyasi import rol_kur
from tests._iban import tr_iban

PAROLA = "parola1234"

YENI_ANAHTARLAR = {
    ("/sections", "link_id"),
    ("/units", "link_id"),
    ("/sales", "link_id"),
    ("/sales", "owner_id"),
    ("/subcontractor-contracts", "link_id"),
    ("/warehouses", "warehouse_id"),
    ("/payments", "payment_id"),
    ("/stock", "item_id"),
    ("/earned-value", "discipline_id"),
    ("/earned-value", "item_id"),
}


class Dunya:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.proje: dict[str, uuid.UUID] = {}
        self.bag: dict[tuple[str, str], uuid.UUID] = {}  # (sahip, A|B) → bağ kimliği
        self.sahip: dict[tuple[str, str], uuid.UUID] = {}  # (sahip, A|B) → sahip kaydı kimliği
        self.depo: dict[str, uuid.UUID] = {}
        self.odeme: dict[str, uuid.UUID] = {}


@pytest.fixture
async def dunya(seeded_db, client, user_factory, project_factory) -> Dunya:
    d = Dunya()
    zayif = await rol_kur(seeded_db, "zayif_b5a", PageLevel.none)
    guclu = await rol_kur(seeded_db, "guclu_b5a", PageLevel.edit)
    user = await user_factory(email="b5a@izn.co", password=PAROLA, role_key="zayif_b5a")
    user.role_id = zayif.id
    musteri = Customer(customer_type=CustomerType.person, name="Müşteri B5a")
    seeded_db.add(musteri)
    hesap = BankAccount(
        bank_name="Ziraat", account_type=BankAccountType.checking, iban=tr_iban(7),
        opening_balance=Decimal("0.00"), is_active=True,
    )  # fmt: skip
    seeded_db.add(hesap)
    await seeded_db.flush()
    tipler = {}
    for kapsam in (
        EntityDocumentScope.section,
        EntityDocumentScope.unit,
        EntityDocumentScope.unit_sale,
        EntityDocumentScope.subcontractor_contract,
    ):
        tip = EntityDocumentType(scope=kapsam, code="b5a", name="B5a", sort_order=1)
        seeded_db.add(tip)
        tipler[kapsam] = tip
    await seeded_db.flush()
    for ad, rol in (("A", guclu), ("B", zayif)):
        proje = await project_factory(code=f"B5-{ad}", name=f"B5a {ad}")
        d.proje[ad] = proje.id
        seeded_db.add(ProjectMember(user_id=user.id, project_id=proje.id, role_id=rol.id))
        site = Site(project_id=proje.id, code=f"B5-{ad}", name=f"Şantiye {ad}")
        seeded_db.add(site)
        await seeded_db.flush()
        bolum = Section(site_id=site.id, name=f"Bölüm {ad}")
        blok = Block(project_id=proje.id, site_id=site.id, name=f"Blok {ad}")
        seeded_db.add_all([bolum, blok])
        await seeded_db.flush()
        unite = Unit(
            project_id=proje.id, block_id=blok.id, unit_no="1", unit_kind=UnitKind.apartment
        )
        seeded_db.add(unite)
        await seeded_db.flush()
        satis = UnitSale(
            unit_id=unite.id, project_id=proje.id, customer_id=musteri.id,
            sale_type=SaleType.sale, status=UnitSaleStatus.active,
            sale_price=Decimal("1000.00"), created_by=user.id,
        )  # fmt: skip
        sozlesme = SubcontractorContract(project_id=proje.id, created_by=user.id)
        depo = Warehouse(name=f"Depo {ad}", site_id=site.id)
        fatura = Invoice(
            direction=InvoiceDirection.outgoing, invoice_no=f"B5A-{ad}",
            document_type=InvoiceDocumentType.einvoice, status=InvoiceStatus.sent,
            issue_date=date(2026, 8, 1), party_name="Karşı", subtotal=Decimal("100.00"),
            advance_amount=Decimal("0.00"), retention_amount=Decimal("0.00"),
            tax_base=Decimal("100.00"), vat_amount=Decimal("0.00"),
            withholding_amount=Decimal("0.00"), total=Decimal("100.00"),
            created_by_id=user.id, project_id=proje.id,
        )  # fmt: skip
        seeded_db.add_all([satis, sozlesme, depo, fatura])
        await seeded_db.flush()
        odeme = Payment(
            invoice_id=fatura.id, bank_account_id=hesap.id, method=PaymentMethodKind.transfer,
            amount=Decimal("10.00"), paid_on=date(2026, 8, 14), created_by_id=user.id,
        )  # fmt: skip
        seeded_db.add(odeme)
        await seeded_db.flush()
        d.depo[ad], d.odeme[ad] = depo.id, odeme.id
        sahipler = {
            "sections": (SectionDocument, "section_id", bolum.id, EntityDocumentScope.section),
            "units": (UnitDocument, "unit_id", unite.id, EntityDocumentScope.unit),
            "sales": (UnitSaleDocument, "unit_sale_id", satis.id, EntityDocumentScope.unit_sale),
            "subcontractor-contracts": (
                SubcontractorContractDocument,
                "subcontractor_contract_id",
                sozlesme.id,
                EntityDocumentScope.subcontractor_contract,
            ),
        }
        for kok, (model, kolon, sahip_id, kapsam) in sahipler.items():
            bag = model(type_id=tipler[kapsam].id, **{kolon: sahip_id})
            seeded_db.add(bag)
            await seeded_db.flush()
            d.bag[(kok, ad)], d.sahip[(kok, ad)] = bag.id, sahip_id
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": "b5a@izn.co", "password": PAROLA})
    assert resp.status_code == 200, resp.text
    d.headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    return d


SAHIPLER = ["sections", "units", "sales", "subcontractor-contracts"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kok", SAHIPLER)
async def test_belge_bagi_patch_a_gecer_b_403(client, dunya, kok):
    govde = {"note": "b5a"}
    a = await client.patch(
        f"/{kok}/documents/{dunya.bag[(kok, 'A')]}", json=govde, headers=dunya.headers
    )
    assert a.status_code == 200, f"{kok}: A'da PATCH {a.status_code} {a.text}"
    b = await client.patch(
        f"/{kok}/documents/{dunya.bag[(kok, 'B')]}", json=govde, headers=dunya.headers
    )
    assert b.status_code == 403, f"{kok}: B'de PATCH {b.status_code} {b.text}"


@pytest.mark.asyncio
@pytest.mark.parametrize("ad", ["A", "B"])
@pytest.mark.parametrize("kok", SAHIPLER)
async def test_belge_bagi_delete_cozucusu_sahibin_projesini_verir(seeded_db, dunya, kok, ad):
    """DELETE ucu Sistem Yöneticisi kapılıdır (rol farkı gözlenemez); bağlam yine maske/kapsam
    tüketicileri için çözülmelidir: bağ → sahibin projesi."""
    bag = {"link_id": str(dunya.bag[(kok, ad)])}
    assert await resolve_project(seeded_db, f"/{kok}/documents/x", bag) == dunya.proje[ad]


@pytest.mark.asyncio
async def test_satis_belge_listesi_ve_baglama_satis_projesinde(client, dunya):
    a = await client.get(f"/sales/{dunya.sahip[('sales', 'A')]}/documents", headers=dunya.headers)
    assert a.status_code == 200, a.text
    b = await client.get(f"/sales/{dunya.sahip[('sales', 'B')]}/documents", headers=dunya.headers)
    assert b.status_code == 403, b.text
    p = await client.post(
        f"/sales/{dunya.sahip[('sales', 'B')]}/documents", json={}, headers=dunya.headers
    )
    assert p.status_code == 403, p.text


@pytest.mark.asyncio
async def test_depo_yeniden_adlandirma_deponun_santiye_projesinde(client, dunya):
    a = await client.patch(
        f"/warehouses/{dunya.depo['A']}", json={"name": "Yeni A"}, headers=dunya.headers
    )
    assert a.status_code == 200, a.text
    b = await client.patch(
        f"/warehouses/{dunya.depo['B']}", json={"name": "Yeni B"}, headers=dunya.headers
    )
    assert b.status_code == 403, b.text


@pytest.mark.asyncio
@pytest.mark.parametrize("ad", ["A", "B"])
async def test_cozucu_depo_odeme_projeyi_verir(seeded_db, dunya, ad):
    assert (
        await resolve_project(seeded_db, "/warehouses/x", {"warehouse_id": str(dunya.depo[ad])})
        == dunya.proje[ad]
    )
    assert (
        await resolve_project(seeded_db, "/payments/x", {"payment_id": str(dunya.odeme[ad])})
        == dunya.proje[ad]
    )


@pytest.mark.asyncio
async def test_merkez_depo_projesiz_none(seeded_db):
    merkez = Warehouse(name="Merkez B5a", site_id=None)
    seeded_db.add(merkez)
    await seeded_db.flush()
    assert (
        await resolve_project(seeded_db, "/warehouses/x", {"warehouse_id": str(merkez.id)}) is None
    )


@pytest.mark.asyncio
async def test_bilinmeyen_kimlikler_none(seeded_db):
    bos = str(uuid.uuid4())
    for yol, param in (
        ("/warehouses/x", "warehouse_id"),
        ("/payments/x", "payment_id"),
        ("/sections/documents/x", "link_id"),
        ("/units/documents/x", "link_id"),
        ("/sales/documents/x", "link_id"),
        ("/subcontractor-contracts/documents/x", "link_id"),
    ):
        assert await resolve_project(seeded_db, yol, {param: bos}) is None, yol


@pytest.mark.asyncio
async def test_sirket_geneli_kayitlar_projesiz(seeded_db):
    """Stok kartı, EV disiplini ve EV katalog kalemi projeden BAĞIMSIZ (kolon yok) → `None`."""
    kart = StockItem(code="B5A-1", name="Çimento", category=list(StockCategory)[0], unit="Ton")
    disiplin = (await seeded_db.execute(select(EvDiscipline).limit(1))).scalars().first()
    kalem = (await seeded_db.execute(select(EvCatalogItem).limit(1))).scalars().first()
    seeded_db.add(kart)
    await seeded_db.flush()
    vakalar = [("/stock/items/x", "item_id", kart.id)]
    if disiplin is not None:
        vakalar.append(("/earned-value/disciplines/x", "discipline_id", disiplin.id))
    if kalem is not None:
        vakalar.append(("/earned-value/catalog/x", "item_id", kalem.id))
    for yol, param, kimlik in vakalar:
        assert await resolve_project(seeded_db, yol, {param: str(kimlik)}) is None, yol


def test_resolvers_b5a_anahtarlari_var():
    assert YENI_ANAHTARLAR <= set(RESOLVERS)


# --- 23c: root_path ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_root_path_onekten_bagimsiz_cozucu_calisir(seeded_db, dunya):
    """Uygulama `root_path` ile yayınlanırsa `request.url.path` öneki içerir ('/api/units/...');
    eşleşen rotanın `path_format`ı ise köksüzdür. Bağlam rotadan okunmalı."""
    from fastapi.routing import iter_route_contexts

    from app.main import app

    yol = f"/units/{dunya.sahip[('units', 'A')]}"  # MEVCUT çözücü: yalnız önek sınanır
    rota = next(
        c.original_route
        for c in iter_route_contexts(app.routes)
        if c.path == "/units/{unit_id}" and "PATCH" in c.methods
    )
    scope = {
        "type": "http",
        "method": "PATCH",
        "path": f"/api{yol}",
        "root_path": "/api",
        "raw_path": f"/api{yol}".encode(),
        "query_string": b"",
        "headers": [],
        "route": rota,
        "path_params": {"unit_id": str(dunya.sahip[("units", "A")])},
    }
    assert await request_project(seeded_db, Request(scope)) == dunya.proje["A"]


# --- 23c bekçisi: iç içe önekli include `_route_path`'i bozmamalı ---------------------------


def _istek_yolu_kovani(app, url: str) -> str:
    from fastapi.testclient import TestClient

    return TestClient(app).get(url).json()["segment"]


def test_ic_ice_onekli_include_baglam_onegini_bozmaz():
    """Sentetik: `include_router(sub, prefix="/units")` altındaki `/{unit_id}` rotasının
    `path_format`ı '/{unit_id}'dir; bağlam öneki yine '/units' olmalı (aksi hâlde çözücü
    eşleşmez, bağlam `None`a düşer → ekip rolü atlatılır/ana rolle karar)."""
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from app.modules.projects.context import _route_path, _segment

    alt = APIRouter()

    @alt.get("/{unit_id}")
    async def _u(unit_id: str, request: Request):
        return {
            "segment": _segment(_route_path(request)),
            "format": request.scope["route"].path_format,
        }

    ust = APIRouter()
    ust.include_router(alt, prefix="/units")
    uygulama = FastAPI()
    uygulama.include_router(ust)
    yanit = TestClient(uygulama).get("/units/123").json()
    assert yanit["segment"] == "/units", yanit  # path_format tabanlı eski kod "/{unit_id}" verirdi
    # root_path'li dilim de aynı önek
    kok = FastAPI(root_path="/api")
    kok.include_router(ust)
    assert TestClient(kok).get("/units/123").json()["segment"] == "/units"


def test_gercek_rotalarda_baglam_oneki_etkin_yolun_segmenti():
    """Uygulamanın TÜM rotaları: scope'a düşen orijinal rotanın `path_format` segmenti, etkin
    (önekli) yolun segmentine eşit. Önekli iç içe include eklenirse KIRMIZI olur."""
    from fastapi.routing import APIRoute, iter_route_contexts

    from app.main import app
    from app.modules.projects.context import _segment

    fark = [
        (c.original_route.path_format, c.path)
        for c in iter_route_contexts(app.routes)
        if isinstance(c.original_route, APIRoute)
        and _segment(c.original_route.path_format) != _segment(c.path)
    ]
    assert not fark, fark
