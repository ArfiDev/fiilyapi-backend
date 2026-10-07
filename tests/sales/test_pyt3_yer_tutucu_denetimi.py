"""P-YT3 — `sales/` yer tutucu denetimi.

🔴 **BU DILIMIN EN ONEMLI OLCUMU BURADA: PREMISE CURUDU.**

P-YT1'in sayimi (`ROADMAP-BACKEND.md`) ve P-YT3 gorev emri `sales` icin **IKI
bos yer tutucu** sayiyordu (`UnitSaleResponse.unit_cost` · `sale_profit`).
Olculdu: **IKISI DE ZATEN BAGLI** ve **2026-08-09**'dan beri oyle (`e7b84cb`,
"P10 T3 — yer tutucular gerceğe"). `sales/service.py::_cost_metrics` her
yanitta `metric(...)` ile GERCEK degeri basar; kanit `test_sales_cost_binding.py`
dosyasinin tamamidir.

**Sayimi yanilten sey neydi:** `schemas.py`de P8 T3'ten (2026-08-02) kalan
`default_factory=lambda: MetricPlaceholder(pending_module=COST_MODULE)`
KALINTISI. Hicbir cagiran onu kullanmiyordu, ama bir *kelime-grep*i onu
"bos yer tutucu kurulumu" olarak sayiyordu.
🔑 *Bir sozcugu saymak, bir olguyu saymak degildir* — yonetimin `sites` icin
"10 dedim, 9'du" itirafinin ayni kok nedeni, ikinci kez ve baska bir modulde.

Bu dosya o kalintinin geri gelmesini yapisal olarak engeller.
"""

import ast
import pathlib
from decimal import Decimal

from app.modules.projects.schemas import MetricPlaceholder
from app.modules.sales import schemas as sales_schemas
from app.modules.sales import service as sales_service
from app.modules.units.models import Unit, UnitKind

_SALES_KOKU = pathlib.Path(sales_service.__file__).resolve().parent


# --------------------------------------------------------------------------- #
# K3 — KALINTI VARSAYILAN (yapisal, DB'siz)
# --------------------------------------------------------------------------- #


def test_maliyet_zarflari_ZORUNLUDUR__varsayilan_YOKTUR():
    """🔴 Bir varsayilan zarf, "bos durum" kararinin IKINCI KOPYASIDIR (K3).

    Gercek karar `service._cost_metrics`tedir ve orada "kaynak yok" hâli
    `metric(None, ...)` ile DOGAR. Semadaki varsayilan ise cagiran alani
    unuttugunda ayni zarfi SESSIZCE uretirdi — hem de `project_costs` modulu
    canli ve deger hesaplanabilirken. Alan ZORUNLU oldugunda o dal yapisal
    olarak imkansizdir: unutan cagiran `ValidationError` alir.
    """
    for ad in ("unit_cost", "sale_profit"):
        alan = sales_schemas.UnitSaleResponse.model_fields[ad]
        assert alan.annotation is MetricPlaceholder, f"{ad} zarf tipini kaybetti"
        assert alan.is_required(), (
            f"`UnitSaleResponse.{ad}` yeniden varsayilan kazandi — cagiran unuttugunda "
            "canli bir modul icin 'bekleniyor' basar (P-YT3 kalinti bulgusu)"
        )


def test_sales_icinde_ZARF_yalnizca_service_te_kurulur():
    """`MetricPlaceholder(...)` / `metric(...)` cagrisi `sales/` icinde TEK
    dosyada olmalidir. AST kullanilir, `grep` degil: bu dosyanin kendi
    docstring'i bile bir `grep`i yanıltirdi (P-YT2 dersi)."""
    bulgular: dict[str, list[int]] = {}
    for yol in sorted(_SALES_KOKU.rglob("*.py")):
        agac = ast.parse(yol.read_text(encoding="utf-8"))
        for dugum in ast.walk(agac):
            if not isinstance(dugum, ast.Call):
                continue
            hedef = dugum.func
            ad = hedef.id if isinstance(hedef, ast.Name) else getattr(hedef, "attr", None)
            if ad in {"MetricPlaceholder", "metric"}:
                bulgular.setdefault(yol.name, []).append(dugum.lineno)

    assert set(bulgular) == {"service.py"}, (
        f"zarf `sales/` icinde birden fazla yerde kuruluyor: {bulgular} (K3)"
    )


# --------------------------------------------------------------------------- #
# pending_modules — CANLI MODUL adlandiran etiketler
# --------------------------------------------------------------------------- #


def test_pending_modules_etiketleri__project_costs_ARTIK_YOK():
    """🔴 (A) BAYAT ETIKET — YANITIN KENDI ICINDE CELISKI.

    `pending_modules` ekrana *"bu bolumlerin kaynagi yok"* der. `project_costs`
    orada duruyordu — oysa onun besledigi IKI ALAN (`unit_cost`, `sale_profit`)
    AYNI YANITTA dolu geliyor. Bir ekran bu listeye bakarak maliyet sutununu
    gizleseydi, VERISI OLAN bir sutunu gizlemis olurdu (dashboard'da fiilen
    yasanan "rozet 3 derken govde bos" celiskisinin kardesi).

    Kalan ikisi OLCULDU ve GECERLI:
    * `documents` — `documents` tablosunda satisa bag YOKTUR (yalniz
      project_id/site_id/folder_id); "bu satisin belgeleri" listelenemez.
    * `invoicing` — `Invoice`ta `unit_sale_id` KOLONU YOKTUR (kaynak baglari
      progress_payments / purchase_orders / equipment_rental_invoices'tir);
      F206-207 pesinat faturasi bir satisa BAGLANAMAZ.
    """
    assert sales_schemas.PENDING_MODULES == ["documents", "invoicing"], (
        f"`PENDING_MODULES` degisti: {sales_schemas.PENDING_MODULES}"
    )
    assert sales_schemas.COST_MODULE not in sales_schemas.PENDING_MODULES, (
        "`project_costs` listeye geri dondu — ama unit_cost/sale_profit BAGLI"
    )


def test_pending_modules_etiketlerinin_HEPSI_CANLI_bir_izin_modulunu_adlandirir():
    """P-YT1'in sinif kusuru: zarf *"modul yok"* demek icin tasarlandi, ama
    etiketler canli modulleri adlandiriyor. `documents` ve `invoicing` icin bu
    ISIMLE cakilir — bekleyen sey MODUL degil, o modulle satis arasindaki BAGdir.
    """
    from app.core.sayfalar import MODUL_ANAHTARLARI

    modul_anahtarlari = set(MODUL_ANAHTARLARI)

    assert set(sales_schemas.PENDING_MODULES) <= modul_anahtarlari, (
        "etiketlerden biri artik canli bir izin modulunu adlandirmiyor: "
        f"{sorted(set(sales_schemas.PENDING_MODULES) - modul_anahtarlari)}"
    )
    assert sales_schemas.COST_MODULE not in modul_anahtarlari, (
        "`project_costs` bir izin modulu OLDU — `sales/summary` gerekcesi yeniden okunmali"
    )


def test_ozet_yanitinda_project_costs_etiketi_KALIR__ama_sebebi_DEGISTI():
    """(B) GECERLI — `SalesSummaryResponse.pending_modules` DEGISMEDI.

    Sebep tazelendi: eskiden "kaynak yok"tu, bugun kaynak VAR (`costs.allocation`
    canli ve satis satirinda kullaniliyor) — eksik olan sey **KPI ALANIDIR**
    (KALICI KARAR 3: maliyet/kâr karti acilmaz). Yani etiket artik bir MODUL
    boslugunu degil bir URUN KARARINI bildiriyor.
    """
    assert sales_schemas.SalesSummaryResponse.model_fields["pending_modules"].default_factory() == [
        "project_costs"
    ]

    kpi_alanlari = set(sales_schemas.SalesSummaryResponse.model_fields)
    assert not any("cost" in ad or "profit" in ad for ad in kpi_alanlari), (
        "ozete maliyet/kâr KPI'si eklenmis — etiket artik yaniltici"
    )


# --------------------------------------------------------------------------- #
# Davranis — zarflarin BAGLI oldugu GERCEK uctan dogrulanir
# --------------------------------------------------------------------------- #


async def test_satis_yanitinda_maliyet_GERCEK__pending_modules_maliyeti_ANMAZ(
    client, seeded_db, admin_headers, proje, blok, unite, musteri
):
    """🔑 Celiskinin YOK oldugunu TEK yanitta kanitlar: zarf dolu VE etiket temiz.

    Kurulum ELLE hesaplanir: butce 2.000.000,00 · projenin brut m² toplami
    500,00 (200 + 300) → m² basina 4.000,00 · unite 200 m² → maliyet
    **800.000,00**. Satis bedeli 1.000.000,00 → kâr **200.000,00**.
    """
    proje.budget_material = Decimal("2000000.00")
    unite.gross_area_m2 = Decimal("200.00")
    seeded_db.add(
        Unit(
            project_id=proje.id,
            block_id=blok.id,
            unit_no="777",
            unit_kind=UnitKind.apartment,
            gross_area_m2=Decimal("300.00"),
        )
    )
    await seeded_db.flush()

    resp = await client.post(
        f"/projects/{proje.id}/sales",
        json={
            "unit_id": str(unite.id),
            "customer_id": str(musteri.id),
            "sale_type": "sale",
            "sale_price": "1000000.00",
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    govde = resp.json()

    # 🔴 TAM KUME: uc olgu (maliyet · kâr · etiket) TEK karsilastirmada. Art
    # arda dizilmis `assert`lerde ilki kirilsa digerleri HIC KOSMAZ ve "zarf
    # dolu AMA etiket hâlâ bekliyor" celiskisi gorunmezdi.
    assert {
        "unit_cost": govde["unit_cost"],
        "sale_profit": govde["sale_profit"],
        "pending_modules": govde["pending_modules"],
    } == {
        "unit_cost": {"available": True, "value": "800000.00", "pending_module": None},
        "sale_profit": {"available": True, "value": "200000.00", "pending_module": None},
        "pending_modules": ["documents", "invoicing"],
    }, (
        "maliyet BAGLI degil ya da etiket onu hâlâ 'bekleniyor' sayiyor — "
        "P-YT3'un premise curutmesi yeniden olculmelidir"
    )
