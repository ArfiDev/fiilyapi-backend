"""P-YT3 — `inventory/` yer tutucularinin DENETIM bekcileri.

ŞS'nin iki sutunu (`monthly_need` "Aylik Ihtiyac" · `section` "Bolum") YERINDE
KALDI. Davranis bekcisi zaten vardi
(`test_stock_summary_api.py::test_santiye_stogu_pending_sutunlari`); bu dosya
onun altindaki **GEREKCEYI** cakar, cunku gerekce 2026-08-23'te DEGISTI:

* eski gerekce: *"planlama/BOQ turevi, modul gelince dolar"*
* olculen olgu: `site_planning` modulu **CANLI** — ve iki bagimsiz engel var.

**ENGEL 1 — KAYNAK YOK, VE OLMAYACAK.** `PlanResourceKind` yalnizca `crew` ve
`equipment` tasir; `SitePlanRow`da ne `stock_item_id` ne de bir MALZEME
miktari vardir (tek sayisal kolon `planned_worker_count`). Modelin kendi
docstring'i *"Plan-gerceklesen kiyas kolonu YOKTUR (spec §5)"* der. Yani
bekleyen sey MODUL degil, o modulun hic tasimadigi bir KAVRAMdir.

**ENGEL 2 — IZIN KAPISI (K4).** `site_planning` bir izin modulu DEGILDIR;
router'i `PERMISSION_MODULE = "site_diary"` ile kapilanir. `procurement` rolu
`inventory=full` ama `site_diary=none`dur — plan verisini stok ekranina basmak
o kapiyi ATLARDI.

🔴 `section` icin AYRICA bir TUZAK olculdu: `procurement`ta gorunuste isleyen
bir kaynak VAR (`purchase_requests.section_id` + `purchase_request_lines.
stock_item_id`) ve K4 onu engellemez. Engel ANLAMDIR: o bag "bu malzemeyi HANGI
BOLUM TALEP ETTI"dir — stogun bulundugu bolum degil. Basilsaydi ekran makul
gorunen ama yanlis bir "Bolum" gosterirdi (K1 sinif C'nin tanimi).
"""

from datetime import date
from decimal import Decimal

from app.core.access import AccessLevel
from app.modules.inventory import service as inventory_service
from app.modules.procurement.models import (
    PurchasePriority,
    PurchaseRequest,
    PurchaseRequestLine,
)
from app.modules.site_planning import service as site_planning_service
from app.modules.site_planning.models import PlanResourceKind, SitePlanRow
from app.modules.sites.models import Section

# --------------------------------------------------------------------------- #
# ENGEL 1 — kaynak yok (yapisal, DB'siz)
# --------------------------------------------------------------------------- #


def test_site_planning_MALZEME_satiri_TASIMAZ():
    """`monthly_need` (B) GECERLI'nin dayanagi: plan izgarasinda malzeme YOK."""
    assert {uye.value for uye in PlanResourceKind} == {"crew", "equipment"}, (
        "plan kaynak turleri degisti — `monthly_need` gerekcesi yeniden olculmelidir"
    )
    kolonlar = set(SitePlanRow.__table__.columns.keys())
    assert "stock_item_id" not in kolonlar, "plan satiri artik stok kartina baglaniyor"
    assert not (kolonlar & {"quantity", "planned_quantity", "monthly_need"}), (
        f"plan satirina MIKTAR kolonu eklenmis: {sorted(kolonlar)}"
    )


def test_site_planning_ANAHTARI_bir_IZIN_MODULU_DEGILDIR():
    """🔴 P-YT2'nin "anahtar uzayi ayrismis" kanonunun BESINCI ornegi.

    `pending_module="site_planning"` hicbir izin modulunu adlandirmaz — modulun
    kendisi `site_diary` kapisini kullanir. Deger yanit govdesindedir ve
    degistirmek sozlesme kirar; bu yuzden DUZELTILMEZ, cakillir.
    """
    from app.core.sayfalar import MODUL_ANAHTARLARI

    modul_anahtarlari = set(MODUL_ANAHTARLARI)

    assert inventory_service.PENDING_SITE_PLANNING == "site_planning"
    assert inventory_service.PENDING_SITE_PLANNING not in modul_anahtarlari, (
        "`site_planning` bir izin modulu OLDU — iki zarfin gerekcesi yeniden yazilmali"
    )
    assert site_planning_service.PERMISSION_MODULE == "site_diary", (
        "planlama modulunun izin kapisi degisti — K4 karsilastirmasi bayat"
    )


def test_K4_inventory_okuyup_site_diary_okuyamayan_rol_HÂLÂ_VAR():
    """ENGEL 2 — plan verisini stok ekranina basmak `site_diary` kapisini atlardi."""
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER

    sizdiran = {
        rol
        for sira, rol in enumerate(ROLE_ORDER)
        if MATRIX["inventory"][sira] is not AccessLevel.none
        and MATRIX["site_diary"][sira] is AccessLevel.none
    }

    assert sizdiran == {"procurement"}, (
        f"inventory/site_diary izin ayrismasi DEGISTI: {sorted(sizdiran)} (K4)"
    )


def test_K4_procurement_KAYNAGI_izin_kapisiyla_engellenmez():
    """🔴 Karsit olcum — `section` icin engel K4 DEGIL, ANLAMdir.

    Bu bekci bilincli olarak BOS bir kume iddia eder: `inventory`yi okuyup
    `procurement`ta `none` olan HICBIR rol yoktur. Yani talep-bolum bagini
    basmak bir yetki genislemesi OLMAZDI; alani yer tutucu birakan sey
    verinin YANLIS ANLAMIDIR. Bu ayrimi yazmadan "K4 hepsini kapatti" demek
    denetimi yaniltirdi.
    """
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER

    sizdiran = {
        rol
        for sira, rol in enumerate(ROLE_ORDER)
        if MATRIX["inventory"][sira] is not AccessLevel.none
        and MATRIX["procurement"][sira] is AccessLevel.none
    }

    assert sizdiran == set(), (
        f"inventory/procurement ayrismasi DOGDU: {sorted(sizdiran)} — "
        "`section` gerekcesine artik K4 de eklenmelidir"
    )


# --------------------------------------------------------------------------- #
# TUZAK — "makul gorunen kaynak" kurulur, zarf yine de BOS kalir
# --------------------------------------------------------------------------- #


async def test_PLAN_ve_TALEP_VARKEN_DE_zarflar_BOS_KALIR(
    client,
    admin_headers,
    seeded_db,
    gorunen_proje,
    gorunen_santiye,
    depo_fabrikasi,
    kart_fabrikasi,
    user_factory,
):
    """🔑 Denetimin en guclu bekcisi.

    Santiyeye (a) bolumu olan bir PLAN SATIRI ve (b) ayni stok kartini ayni
    bolume baglayan bir SATINALMA TALEBI kurulur. Ikisi de "Bolum" sutununu
    doldurmak icin makul gorunur. Zarflar yine de BOS doner.
    """
    bolum = Section(site_id=gorunen_santiye.id, code="B-01", name="Kaba Yapı")
    seeded_db.add(bolum)
    depo = await depo_fabrikasi("D-1 Ambar", site=gorunen_santiye)
    kart = await kart_fabrikasi("SNK-0421")
    await seeded_db.flush()

    seeded_db.add(
        SitePlanRow(
            site_id=gorunen_santiye.id,
            project_id=gorunen_proje.id,
            kind=PlanResourceKind.crew,
            section_id=bolum.id,
            label="Kalıpçı",
            planned_worker_count=14,
        )
    )
    talep_sahibi = await user_factory(
        email="yt3-talep@t.co", password="parola1234", role_key="procurement"
    )
    talep = PurchaseRequest(
        request_no="SAT-2026-9001",
        request_date=date(2026, 8, 23),
        priority=PurchasePriority.normal,
        project_id=gorunen_proje.id,
        site_id=gorunen_santiye.id,
        section_id=bolum.id,
        created_by_user_id=talep_sahibi.id,
    )
    seeded_db.add(talep)
    await seeded_db.flush()
    seeded_db.add(
        PurchaseRequestLine(
            request_id=talep.id,
            stock_item_id=kart.id,
            quantity=Decimal("120.000"),
            sort_order=0,
        )
    )
    await seeded_db.flush()

    # 🔴 POZITIF KONTROL — bekci once GIRDISININ BOS OLMADIGINI kanitlar.
    assert (
        await seeded_db.scalar(
            SitePlanRow.__table__.select()
            .where(SitePlanRow.site_id == gorunen_santiye.id)
            .with_only_columns(SitePlanRow.id)
        )
    ) is not None, "plan satiri yazilmamis — asagidaki iddia BOSA koser"
    assert (
        await seeded_db.scalar(
            PurchaseRequestLine.__table__.select()
            .where(PurchaseRequestLine.stock_item_id == kart.id)
            .with_only_columns(PurchaseRequestLine.id)
        )
    ) is not None, "talep satiri yazilmamis — asagidaki iddia BOSA koser"

    giris = await client.post(
        "/stock/entries",
        json={
            "entry_type": "purchase",
            "entry_date": "2026-08-23",
            "warehouse_id": str(depo.id),
            "lines": [{"item_id": str(kart.id), "quantity": "5.000"}],
        },
        headers=admin_headers,
    )
    assert giris.status_code == 201, giris.text

    satir = (await client.get(f"/sites/{gorunen_santiye.id}/stock", headers=admin_headers)).json()[
        "items"
    ][0]

    # 🔴 TAM KUME: iki sutun ayri `assert`lerde olsaydi ilkinin kirilmasi
    # ikincisini HIC KOSTURMAZDI ve tablo yarim gorunurdu.
    assert {
        "monthly_need": satir["monthly_need"],
        "section": satir["section"],
    } == {
        # (B) — plan satiri VAR ama malzeme ihtiyaci TASIMAZ; uydurma sayi basilmaz
        "monthly_need": {"available": False, "value": None, "pending_module": "site_planning"},
        # (C) — talep bolumu "stogun bolumu" DEGILDIR; makul gorunen yanlis basilmaz
        "section": {"available": False, "items": [], "pending_module": "site_planning"},
    }, "ŞS yer tutucu tablosu KAYDI — schemas.SiteStockRow docstring'i bayatladi"


async def test_bekleyen_siparis_KPI_si_HÂLÂ_BAGLI(
    client, admin_headers, gorunen_santiye, depo_fabrikasi, kart_fabrikasi
):
    """Regresyon: `StockSummaryKpis.pending_orders` SA T4'te GERCEGE dondu.

    Denetim onu YENIDEN yer tutucuya cevirmedi. Siparis yokken deger `0`dir ve
    zarf DOLUDUR — "gercek 0" ile "bilinmiyor" ayri seylerdir (K2).
    """
    depo = await depo_fabrikasi("D-1 Ambar", site=gorunen_santiye)
    await kart_fabrikasi("SNK-0421")
    assert depo is not None

    kpi = (await client.get("/stock/summary", headers=admin_headers)).json()["kpis"]

    assert kpi["pending_orders"] == {
        "available": True,
        "value": "0",
        "pending_module": None,
    }, "bekleyen siparis KPI'si yer tutucuya GERILEDI"
