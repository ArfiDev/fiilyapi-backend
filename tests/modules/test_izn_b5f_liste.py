"""IZN-B5f madde 23b — şirket türü liste/özet uçları, satırları kişinin O PROJEDEKİ rolüyle süzer.

Kök (IZN-B5e-OLCUM §23): şirket kapısı ANA rolle karar verir ve `record_gate` şirket çiftlerini
proje grubuna yazmaz → `visible_projects` ekip rolüne bakmadan üye olunan TÜM projeleri
döndürüyordu;
detay ucu ise yol çözücüsüyle Q'daki rolle karar verip 403 veriyordu (liste 200, detay 403).

CEO kararı (i): 17 uçta (9 liste + 8 özet) satırlar, kişinin o projedeki rolünün ilgili modül
sayfa izniyle süzülür (`visible_projects(..., sirket_ciftleri=...)`; global DEĞİL).

Dünya: ana rol her sayfayı GÖRÜR · A projesinde ekip rolü = ana rol (sayfalar Görür) · Q projesinde
ekip rolü HİÇBİR ŞEY. İki kontrol kişisi:
* `yalniz_a`: yalnız A'nın üyesi (beklenen görünüm) — `ekip` kişisinin yanıtı BİREBİR bununkine eşit
  olmalı;
* `hepsi` (`all_projects`): A + Q'nun hepsi → yanıt `yalniz_a`dan FARKLI olmalı (tohum her uçta Q
  verisi taşıdığı için bekçi sahte-yeşil olamaz).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.sayfalar import PageLevel
from app.core.timezone import today
from app.modules.equipment.models import (
    Equipment,
    EquipmentCategory,
    EquipmentDocument,
    EquipmentDocumentType,
    EquipmentFuelLog,
    EquipmentOwnership,
    EquipmentRatePeriod,
    EquipmentRentalInvoice,
    EquipmentStatus,
    EquipmentWorkLog,
    RentalInvoiceStatus,
    WorkLogType,
)
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceStatus,
)
from app.modules.procurement.models import (
    PaymentTerms,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchasePriority,
    PurchaseRequest,
    PurchaseRequestStatus,
    Supplier,
)
from app.modules.roles.models import Role
from app.modules.sites.models import Site
from app.modules.treasury.models import (
    FinancialInstrument,
    FinancialInstrumentDirection,
    FinancialInstrumentKind,
    FinancialInstrumentStatus,
)
from app.modules.users.models import User
from tests._ekip_dunyasi import rol_kur
from tests._proje_ekibi import ekibe_ekle, tum_projeler
from tests.modules.treasury._hz1_upcoming import _sorgu_sayaci

PASSWORD = "parola1234"


def _bugun():
    """Gün sınırı test GÖVDESİNDE okunur (modül düzeyinde `today()` yasak; TMP-FIX #167)."""
    return today()


class Dunya:
    """Test dünyasının kimlikleri."""

    a: object
    q: object
    site_a: Site
    site_q: Site
    basliklar: dict[str, dict[str, str]]


async def _giris(client, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _site(session, project, kod: str) -> Site:
    site = Site(project_id=project.id, code=kod, name=f"{kod} Şantiyesi")
    session.add(site)
    await session.flush()
    return site


async def _proje_verisi(session, proje, site, yaratan: User, tedarikci: Supplier, n: int) -> None:
    """Bir projenin her uç ailesine veri tohumu (`n`: tutarları projeye göre ayırır)."""
    carpan = Decimal(n)
    session.add(
        Invoice(
            direction=InvoiceDirection.incoming,
            invoice_no=f"B5F-G-{n}",
            document_type=InvoiceDocumentType.einvoice,
            status=InvoiceStatus.approved,
            issue_date=_bugun(),
            due_date=_bugun() + timedelta(days=5),
            party_name=f"Tedarikçi {n}",
            project_id=proje.id,
            site_id=site.id,
            subtotal=Decimal("100.00") * carpan,
            advance_amount=Decimal("0.00"),
            retention_amount=Decimal("0.00"),
            tax_base=Decimal("100.00") * carpan,
            vat_amount=Decimal("20.00") * carpan,
            withholding_amount=Decimal("0.00"),
            total=Decimal("120.00") * carpan,
            created_by_id=yaratan.id,
        )
    )
    session.add(
        Invoice(
            direction=InvoiceDirection.outgoing,
            invoice_no=f"B5F-C-{n}",
            document_type=InvoiceDocumentType.einvoice,
            status=InvoiceStatus.sent,
            issue_date=_bugun(),
            due_date=_bugun() + timedelta(days=9),
            party_name=f"Müşteri {n}",
            project_id=proje.id,
            site_id=site.id,
            subtotal=Decimal("50.00") * carpan,
            advance_amount=Decimal("0.00"),
            retention_amount=Decimal("0.00"),
            tax_base=Decimal("50.00") * carpan,
            vat_amount=Decimal("10.00") * carpan,
            withholding_amount=Decimal("0.00"),
            total=Decimal("60.00") * carpan,
            created_by_id=yaratan.id,
        )
    )
    session.add(
        FinancialInstrument(
            instrument_kind=FinancialInstrumentKind.cheque,
            direction=FinancialInstrumentDirection.received,
            serial_no=f"B5F-CEK-{n}",
            drawer_name=f"Keşideci {n}",
            issue_date=_bugun(),
            due_date=_bugun() + timedelta(days=3),
            amount=Decimal("1000.00") * carpan,
            status=FinancialInstrumentStatus.portfolio,
            project_id=proje.id,
        )
    )
    talep = PurchaseRequest(
        request_no=f"B5F-T-{n}",
        request_date=_bugun(),
        priority=PurchasePriority.normal,
        project_id=proje.id,
        site_id=site.id,
        status=PurchaseRequestStatus.draft,
        created_by_user_id=yaratan.id,
    )
    session.add(talep)
    session.add(
        PurchaseOrder(
            order_no=f"B5F-S-{n}",
            supplier_id=tedarikci.id,
            project_id=proje.id,
            total_amount=Decimal("5000.00") * carpan,
            status=PurchaseOrderStatus.approved,
            created_by_user_id=yaratan.id,
        )
    )
    makine = Equipment(
        name=f"Makine {n}",
        category=EquipmentCategory.machinery,
        status=EquipmentStatus.working,
        ownership=EquipmentOwnership.owned,
        site_id=site.id,
    )
    session.add(makine)
    await session.flush()
    session.add(
        EquipmentWorkLog(
            equipment_id=makine.id,
            work_date=_bugun(),
            site_id=site.id,
            record_type=WorkLogType.worked,
            hours=Decimal("1.00") * carpan,
        )
    )
    session.add(
        EquipmentFuelLog(
            equipment_id=makine.id,
            fuel_date=_bugun(),
            site_id=site.id,
            liters=Decimal("100.000") * carpan,
            unit_price=Decimal("40.0000"),
        )
    )
    session.add(
        EquipmentRentalInvoice(
            supplier_id=tedarikci.id,
            invoice_no=f"B5F-KF-{n}",
            slug=f"b5f-kf-{n}",
            invoice_amount=Decimal("1000.00") * carpan,
            period_year=_bugun().year,
            period_month=_bugun().month,
            site_id=site.id,
            rate_period=EquipmentRatePeriod.hourly,
            vat_rate=Decimal("0.20"),
            status=RentalInvoiceStatus.draft,
        )
    )
    tip = (
        await session.execute(
            select(EquipmentDocumentType).where(EquipmentDocumentType.code == "b5f-tip")
        )
    ).scalar_one_or_none()
    if tip is None:
        tip = EquipmentDocumentType(code="b5f-tip", name="B5f Belgesi", is_required=False)
        session.add(tip)
        await session.flush()
    session.add(
        EquipmentDocument(
            equipment_id=makine.id,
            type_id=tip.id,
            filename=f"belge-{n}.pdf",
            mime_type="application/pdf",
            size_bytes=3,
            content=b"abc",
            valid_until=_bugun() - timedelta(days=2),
        )
    )
    await session.flush()


@pytest.fixture
async def dunya(seeded_db, user_factory, project_factory, client) -> Dunya:
    ana = await rol_kur(seeded_db, "b5f_ana_gorur", PageLevel.view)
    hicbir = await rol_kur(seeded_db, "b5f_ekip_hicbir", PageLevel.none)
    a = await project_factory("B5F-A", name="Proje A")
    q = await project_factory("B5F-Q", name="Proje Q")
    site_a = await _site(seeded_db, a, "B5F-SA")
    site_q = await _site(seeded_db, q, "B5F-SQ")

    ekip = await user_factory(email="ekip@b5f.co", password=PASSWORD, role_key=ana.key)
    await ekibe_ekle(seeded_db, ekip, a.id, ana.id)
    await ekibe_ekle(seeded_db, ekip, q.id, hicbir.id)
    yalniz_a = await user_factory(email="yalniz-a@b5f.co", password=PASSWORD, role_key=ana.key)
    await ekibe_ekle(seeded_db, yalniz_a, a.id, ana.id)
    hepsi = await user_factory(email="hepsi@b5f.co", password=PASSWORD, role_key=ana.key)
    await tum_projeler(seeded_db, hepsi)
    await user_factory(email="sisyon@b5f.co", password=PASSWORD, role_key="system_admin")
    # ters yön: ana rol HİÇBİR ŞEY, A'da ekip rolü Görür → liste kapısı 403 (DEĞİŞMEZ)
    ters = await user_factory(email="ters@b5f.co", password=PASSWORD, role_key=hicbir.key)
    await ekibe_ekle(seeded_db, ters, a.id, ana.id)

    tedarikci = Supplier(name="B5f Tedarikçi", category="Genel", payment_terms=PaymentTerms.days_30)
    seeded_db.add(tedarikci)
    await seeded_db.flush()
    yaratan = await user_factory(email="yaratan@b5f.co", password=PASSWORD, role_key="system_admin")
    await _proje_verisi(seeded_db, a, site_a, yaratan, tedarikci, 1)
    await _proje_verisi(seeded_db, q, site_q, yaratan, tedarikci, 7)
    # projesiz (şirket geneli) fatura: bugünkü kural — ekibin ana rolü karar verir, süzülmez
    seeded_db.add(
        Invoice(
            direction=InvoiceDirection.outgoing,
            invoice_no="B5F-SIRKET",
            document_type=InvoiceDocumentType.einvoice,
            status=InvoiceStatus.sent,
            issue_date=_bugun(),
            party_name="Şirket Geneli",
            subtotal=Decimal("1.00"),
            advance_amount=Decimal("0.00"),
            retention_amount=Decimal("0.00"),
            tax_base=Decimal("1.00"),
            vat_amount=Decimal("0.00"),
            withholding_amount=Decimal("0.00"),
            total=Decimal("1.00"),
            created_by_id=yaratan.id,
        )
    )
    await seeded_db.flush()

    dunya = Dunya()
    dunya.a, dunya.q, dunya.site_a, dunya.site_q = a, q, site_a, site_q
    dunya.basliklar = {
        "ekip": await _giris(client, "ekip@b5f.co"),
        "yalniz_a": await _giris(client, "yalniz-a@b5f.co"),
        "hepsi": await _giris(client, "hepsi@b5f.co"),
        "sisyon": await _giris(client, "sisyon@b5f.co"),
        "ters": await _giris(client, "ters@b5f.co"),
    }
    return dunya


#: Özet uçlarının ay parametresi: parametrize anında değil, ÇAĞRI anında çözülür (`_cozumle`).
_AY: dict = {}


def _cozumle(params: dict) -> dict:
    if params is _AY:
        bugun = _bugun()
        return {"year": bugun.year, "month": bugun.month}
    return params


#: (ad, yol, parametre) — belgedeki 17 uç (§23.2: 9 liste + 8 özet/rapor; /suppliers kartı ayrıca).
UCLAR: list[tuple[str, str, dict]] = [
    ("fatura-liste", "/invoices", {}),
    ("cek-liste", "/financial-instruments", {}),
    ("talep-liste", "/purchase-requests", {}),
    ("siparis-liste", "/purchase-orders", {}),
    ("makine-liste", "/equipment", {}),
    ("calisma-liste", "/equipment/work-logs", {}),
    ("yakit-liste", "/equipment/fuel-logs", {}),
    ("kira-liste", "/equipment/rental-invoices", {}),
    ("yaklasan-odemeler", "/treasury/upcoming-payments", {}),
    ("fatura-ozet", "/invoices/summary", {}),
    ("cek-ozet", "/financial-instruments/summary", {}),
    ("satinalma-ozet", "/purchasing/summary", {}),
    ("tedarikci-liste", "/suppliers", {}),
    ("makine-ozet", "/equipment/summary", {}),
    ("yakit-ozet", "/equipment/fuel-summary", _AY),
    ("calisma-ozet", "/equipment/work-summary", _AY),
    ("belge-ozet", "/equipment/documents/summary", {}),
]
_IDLER = [u[0] for u in UCLAR]


def _sirala(govde):
    """Eş zamanlı kayıtların sırası belirsiz: karşılaştırma için dict listelerini sıralar."""
    if isinstance(govde, dict):
        # `can_*` alanları kişiye özgüdür (Sistem Yöneticisi silebilir); satır kümesi kıyaslanır
        return {k: _sirala(v) for k, v in govde.items() if not k.startswith("can_")}
    if isinstance(govde, list):
        return sorted((_sirala(v) for v in govde), key=lambda v: json.dumps(v, sort_keys=True))
    return govde


async def _al(client, baslik, yol, params):
    yanit = await client.get(yol, params=_cozumle(params), headers=baslik)
    assert yanit.status_code == 200, f"{yol}: {yanit.status_code} {yanit.text}"
    return _sirala(yanit.json())


@pytest.mark.parametrize(("ad", "yol", "params"), UCLAR, ids=_IDLER)
async def test_ekip_uyesi_Q_satirlarini_GORMEZ(client, dunya, ad, yol, params) -> None:
    """Q'da rolü Hiçbir şey olan ekip üyesinin yanıtı, yalnız A'nın üyesi olanınkiyle BİREBİR
    aynı; tüm-projeler kişisinin yanıtından FARKLI (tohum Q verisi taşıyor → sahte-yeşil değil)."""
    ekip = await _al(client, dunya.basliklar["ekip"], yol, params)
    yalniz_a = await _al(client, dunya.basliklar["yalniz_a"], yol, params)
    hepsi = await _al(client, dunya.basliklar["hepsi"], yol, params)
    assert hepsi != yalniz_a, f"{ad}: tohum Q verisi bu uçta fark üretmiyor"
    assert ekip == yalniz_a, f"{ad}: Q satırları ekip üyesine sızıyor"


@pytest.mark.parametrize(("ad", "yol", "params"), UCLAR, ids=_IDLER)
async def test_tum_projeler_ve_sisyon_degismez(client, dunya, ad, yol, params) -> None:
    """`all_projects` kişisi ve Sistem Yöneticisi Q'yu da GÖRÜR (süzgeç onlara uygulanmaz)."""
    hepsi = await _al(client, dunya.basliklar["hepsi"], yol, params)
    sisyon = await _al(client, dunya.basliklar["sisyon"], yol, params)
    yalniz_a = await _al(client, dunya.basliklar["yalniz_a"], yol, params)
    assert hepsi == sisyon
    assert hepsi != yalniz_a


@pytest.mark.parametrize(("ad", "yol", "params"), UCLAR, ids=_IDLER)
async def test_ters_yon_DEGISMEZ_ana_rol_gormuyorsa_liste_403(
    client, dunya, ad, yol, params
) -> None:
    """Ana rol sayfayı görmüyor, A'daki ekip rolü görüyor → liste 403 (multi_project YOK)."""
    yanit = await client.get(yol, params=_cozumle(params), headers=dunya.basliklar["ters"])
    assert yanit.status_code == 403, f"{ad}: {yanit.status_code} {yanit.text}"


def _suzgecli(d: Dunya) -> list[tuple[str, str, dict, Callable[[dict], bool]]]:
    """`?project_id=Q` / `?site_id=Q` ile süzgeçli çağrı: 200 + Q satırı YOK."""
    q = str(d.q.id)
    siteq = str(d.site_q.id)
    bos = lambda g: g["items"] == []  # noqa: E731
    return [
        ("/invoices", {"project_id": q}, bos),
        ("/financial-instruments", {"project_id": q}, bos),
        ("/purchase-requests", {"project_id": q}, bos),
        ("/purchase-orders", {"project_id": q}, bos),
        ("/equipment", {"site_id": siteq}, bos),
        ("/equipment/work-logs", {"site_id": siteq}, bos),
        ("/equipment/fuel-logs", {"site_id": siteq}, bos),
        ("/equipment/rental-invoices", {"site_id": siteq}, bos),
    ]


async def test_proje_suzgecli_cagri_bos_200_404_degil(client, dunya) -> None:
    for yol, params, kontrol in _suzgecli(dunya):
        yanit = await client.get(yol, params=params, headers=dunya.basliklar["ekip"])
        assert yanit.status_code == 200, f"{yol}: {yanit.status_code} {yanit.text}"
        assert kontrol(yanit.json()), f"{yol}: Q satırı süzgeçli çağrıda sızdı: {yanit.text[:200]}"
        # pozitif kontrol: aynı süzgeç tüm-projeler kişisine Q'nun satırını verir
        tam = await client.get(yol, params=params, headers=dunya.basliklar["hepsi"])
        assert tam.json()["items"], f"{yol}: tohum Q satırı üretmiyor (sahte-yeşil)"


async def test_satinalma_ozeti_project_id_suzgeci_Q_icin_bos(client, dunya) -> None:
    params = {"project_id": str(dunya.q.id)}
    ekip = await _al(client, dunya.basliklar["ekip"], "/purchasing/summary", params)
    yalniz_a = await _al(client, dunya.basliklar["yalniz_a"], "/purchasing/summary", params)
    hepsi = await _al(client, dunya.basliklar["hepsi"], "/purchasing/summary", params)
    assert ekip == yalniz_a
    assert hepsi != ekip


async def test_projesiz_sirket_faturasi_bugunku_kuralla_listede(client, dunya) -> None:
    """Projesiz kayıt: kural DEĞİŞMEDİ (ana rol karar verir) → ekip üyesi de görür."""
    govde = await _al(client, dunya.basliklar["ekip"], "/invoices", {})
    assert "B5F-SIRKET" in {f["invoice_no"] for f in govde["items"]}
    assert "B5F-G-7" not in {f["invoice_no"] for f in govde["items"]}
    assert "B5F-G-1" in {f["invoice_no"] for f in govde["items"]}


async def test_tedarikci_karti_Q_harcamasini_gostermez(client, dunya, seeded_db) -> None:
    tedarikci = (
        await seeded_db.execute(select(Supplier).where(Supplier.name == "B5f Tedarikçi"))
    ).scalar_one()
    yol = f"/suppliers/{tedarikci.id}"
    ekip = await _al(client, dunya.basliklar["ekip"], yol, {})
    yalniz_a = await _al(client, dunya.basliklar["yalniz_a"], yol, {})
    hepsi = await _al(client, dunya.basliklar["hepsi"], yol, {})
    assert hepsi != yalniz_a
    assert ekip == yalniz_a


async def test_rol_Q_icin_sayfayi_gorunce_satirlar_GERI_gelir(
    client, dunya, seeded_db, user_factory
) -> None:
    """POZİTİF KONTROL: Q'daki ekip rolü Görür olunca Q satırları listeye girer (süzgeç yalnız
    rolün hücresine bağlı; üyelik tek başına satır açmaz ve rol açınca kapanmaz)."""
    ekip = (await seeded_db.execute(select(User).where(User.email == "ekip@b5f.co"))).scalar_one()
    ana = (await seeded_db.execute(select(Role).where(Role.key == "b5f_ana_gorur"))).scalar_one()
    await ekibe_ekle(seeded_db, ekip, dunya.q.id, ana.id)
    govde = await _al(client, dunya.basliklar["ekip"], "/invoices", {})
    assert "B5F-G-7" in {f["invoice_no"] for f in govde["items"]}


async def test_sorgu_sayisi_proje_sayisiyla_ARTMAZ(
    client, dunya, seeded_db, user_factory, project_factory
) -> None:
    """Performans: süzgeç istek başına SABİT sorgu ekler; üye olunan proje sayısına bağlı DEĞİL
    (N+1 yok). Ekip 2 projedeyken ve 4 projedeyken `/invoices` aynı sayıda ifade yürütür."""
    ekip = (await seeded_db.execute(select(User).where(User.email == "ekip@b5f.co"))).scalar_one()
    hicbir = (
        await seeded_db.execute(select(Role).where(Role.key == "b5f_ekip_hicbir"))
    ).scalar_one()
    sayilar: list[int] = []
    for ek in (0, 2):
        for i in range(ek):
            proje = await project_factory(f"B5F-X{ek}{i}", name=f"Ek {i}")
            await ekibe_ekle(seeded_db, ekip, proje.id, hicbir.id)
        with _sorgu_sayaci() as ifadeler:
            await _al(client, dunya.basliklar["ekip"], "/invoices", {})
            await _al(client, dunya.basliklar["ekip"], "/purchase-orders", {})
        sayilar.append(len(ifadeler))
    assert sayilar[0] == sayilar[1], sayilar


# --- numara / slug ile detay: UUID ile AYNI kapı (çürütme onarımı) ----------------------------


async def _durum(client, baslik, yol) -> int:
    return (await client.get(yol, headers=baslik)).status_code


async def test_numara_ve_slug_ile_detay_Q_icin_403_UUID_ile_ayni(client, dunya, seeded_db) -> None:
    """Liste Q satırını düşürüyor; detay de numara/slug ile AÇILAMAZ (UUID'yle aynı 403)."""
    ekip = dunya.basliklar["ekip"]
    q_fatura = (
        await seeded_db.execute(select(Invoice).where(Invoice.invoice_no == "B5F-G-7"))
    ).scalar_one()
    q_kira = (
        await seeded_db.execute(
            select(EquipmentRentalInvoice).where(EquipmentRentalInvoice.slug == "b5f-kf-7")
        )
    ).scalar_one()
    for yol in (
        f"/invoices/{q_fatura.id}",
        "/invoices/B5F-G-7",
        f"/equipment/rental-invoices/{q_kira.id}",
        "/equipment/rental-invoices/b5f-kf-7",
    ):
        assert await _durum(client, ekip, yol) == 403, yol
    # pozitif kontrol: A'nın kayıtları numara/slug ile AÇILIR; tüm-projeler kişisi Q'yu da açar
    assert await _durum(client, ekip, "/invoices/B5F-G-1") == 200
    assert await _durum(client, ekip, "/equipment/rental-invoices/b5f-kf-1") == 200
    hepsi = dunya.basliklar["hepsi"]
    assert await _durum(client, hepsi, "/invoices/B5F-G-7") == 200
    assert await _durum(client, hepsi, "/equipment/rental-invoices/b5f-kf-7") == 200


async def test_Q_rolu_gorur_olunca_numara_ve_slug_ile_detay_200(client, dunya, seeded_db) -> None:
    ekip = (await seeded_db.execute(select(User).where(User.email == "ekip@b5f.co"))).scalar_one()
    ana = (await seeded_db.execute(select(Role).where(Role.key == "b5f_ana_gorur"))).scalar_one()
    await ekibe_ekle(seeded_db, ekip, dunya.q.id, ana.id)
    baslik = dunya.basliklar["ekip"]
    assert await _durum(client, baslik, "/invoices/B5F-G-7") == 200
    assert await _durum(client, baslik, "/equipment/rental-invoices/b5f-kf-7") == 200
