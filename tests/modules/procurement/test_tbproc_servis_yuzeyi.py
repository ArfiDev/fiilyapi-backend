"""TB-PROC anlik goruntusu — `procurement.service` MODUL YUZEYI donmus haldedir.

## Neden bu dosya var

`service.py` 1009 satirdi (tavan 800) ve bir PAKETE bolundu. Bolme davranis
KORUYAN olmak zorundaydi; ama "686 test yesil" bunu KANITLAMAZ: TB-AUDIT turunda
tek Turkce karakterlik bir mutasyon tum test kumesine GORUNMEZ kaldi, yalniz
bolme-oncesi anlik goruntu yakaladi. Servis icin ayni tuzagin karsiligi sudur:

* bir ad cepheden **dusebilir** (cagiran ancak CANLIDA `AttributeError` yer),
* bir imza **sessizce degisebilir** (varsayilan deger, `*` sinirinin yeri,
  parametre sirasi),
* saf bir yardimcinin **cikti METNI** kayabilir (denetim gunlugu cumleleri
  Turkce'dir ve hicbir test tam metni okumaz).

Bu dosya ucunu de dondurur. Referans metin `tbproc_servis_yuzeyi.txt`tir ve
**bolmeden ONCE** (`47d53f7` tabani, tek dosyalik `service.py`) uretilmistir.

## Sozlesme: ESKI AD KUMESI ALT KUMEDIR

Bolme sonrasi cephe pakete alt modul adlari EKLER (`core`, `suppliers`, ...);
bu bir kayip degildir. Bu yuzden kural sudur: **anlik goruntudeki her satir
sonrasinda AYNEN bulunmalidir**; yeni ad eklenmesi serbesttir. Kayip ya da
degisen tek satir bile KIRMIZIDIR.

`app.modules.procurement.service[.altmodul]` modul yollari `<service>` olarak
NORMALLESTIRILIR: bir adin hangi alt dosyada durdugu cephenin sozlesmesi
DEGILDIR, tasinmasi serbesttir. Yabanci modul yollari (`app.core.errors`,
`app.modules.audit.messages`, ...) normallestirilmez — onlarin degismesi
gercek bir sozlesme kaymasidir.

## 🔴 URL-4 (2026-09-05) — anlik goruntude TEK satir BILEREK degistirildi

`visible_request`in ucuncu parametresi `request_id: uuid.UUID`ten
`request_ref: uuid.UUID | str`e GENISLETILDI: okuma ucu artik `request_no` ile
de cozuluyor (URL-2 kanonunun genisletilmesi). Bu bekci degisimi DOGRU YAKALADI
ve satir korukorune yeniden uretilmedi — elle, TEK satir olarak guncellendi.

Degisim GERIYE UYUMLUDUR: tip DARALMADI, GENISLEDI; UUID geciren her mevcut
cagiran (`visible_request_locked` dahil) aynen calisir. `visible_request_locked`
BILEREK `uuid.UUID` KALDI — yazma yollarinin girisidir ve URL-2 karari 3 yalniz
OKUMA uclarini anahtara acar.

## 🔴 SIL-B1 (2026-10-04) — anlik goruntuden BILEREK dusen satirlar

`can_delete` (`app.core.access`), `DeleteNotAllowedError` ve `_DeletableRequest` koprusu KALDIRILDI
(K4: silme YALNIZ Sistem Yoneticisi'nin, "talebi acan kendi taslagini siler" istisnasi YOK).
`can_delete_request(session, actor, request)` -> `async can_delete_request(session, actor)` oldu
(yanittaki `can_delete` bayragi = `page_gate.is_admin_role`; senkron `is_system_admin` onay
yolunda suresi dolmus aktorde `User.role` lazy=raise ile 500 atiyordu, PR #172 CI).
Referans ELLE guncellendi (yeniden uretim bolme-oncesi tabani bozardi): yalniz bu
satirlar dustu/degisti, `diff` ile dogrulandi.

## 🔴 IZN-B3 (2026-10-05) — `visible_projects` imzasi BILEREK degisti

`projects.service.visible_projects` (procurement'a ice aktarilan sembol) anahtar-kelime ile
`pairs=` aldi: kapisiz yollar (onay kutusu, AI, panel) gerekli (sayfa, bayrak) ciftlerini ACIKCA
verir. Eski cagrilar aynen calisir (varsayilan `None`). Referansin TEK satiri elle guncellendi
(yeniden uretim bolme-oncesi tabani bozardi).

## Yeniden uretim

    python -c "import importlib.util as u; \
      s=u.spec_from_file_location('y','tests/modules/procurement/test_tbproc_servis_yuzeyi.py'); \
      m=u.module_from_spec(s); s.loader.exec_module(m); print(m.build_surface())" \
      > tests/modules/procurement/tbproc_servis_yuzeyi.txt

DB gerektirmez — bilinclidir: anlik goruntu ne fikstur ne semaya bagimlidir.
"""

import asyncio
import datetime as dt
import inspect
import pathlib
import uuid
from decimal import Decimal

from app.modules.inventory.models import StockItem
from app.modules.procurement import service
from app.modules.procurement.models import (
    PaymentTerms,
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchasePriority,
    PurchaseQuote,
    PurchaseRequest,
    PurchaseRequestLine,
    PurchaseRequestStatus,
    Supplier,
)
from app.modules.procurement.schemas import PurchaseRequestLineCreate

ANLIK_GORUNTU = pathlib.Path(__file__).with_name("tbproc_servis_yuzeyi.txt")

_SERVICE_ROOT = "app.modules.procurement.service"

# Anlik goruntu deterministik olmak zorunda: UUID uretilmez, SABIT degerler kullanilir.
_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
_ID2 = uuid.UUID("22222222-2222-2222-2222-222222222222")
_ID3 = uuid.UUID("33333333-3333-3333-3333-333333333333")
_ID4 = uuid.UUID("44444444-4444-4444-4444-444444444444")
_ID5 = uuid.UUID("55555555-5555-5555-5555-555555555555")
_ZAMAN = dt.datetime(2026, 3, 4, 5, 6, 7, tzinfo=dt.UTC)
_GUN = dt.date(2026, 3, 4)


def _normalize_module(ad: str) -> str:
    """Servis paketinin IC yolunu `<service>` yapar; yabanci yollara dokunmaz."""
    if ad == _SERVICE_ROOT or ad.startswith(_SERVICE_ROOT + "."):
        return "<service>"
    return ad


def _describe(ad: str, nesne: object) -> list[str]:
    if inspect.isfunction(nesne):
        onek = "async def" if inspect.iscoroutinefunction(nesne) else "def"
        return [f"{onek} {ad}{inspect.signature(nesne)}"]
    if inspect.isclass(nesne):
        yol = _normalize_module(nesne.__module__)
        satirlar = [f"class {ad} <- {yol}.{nesne.__qualname__}"]
        if yol == "<service>":
            # Servisin KENDI sinifi: kurucusu ve alanlari da sozlesmenin parcasi.
            satirlar.append(f"    __init__{inspect.signature(nesne.__init__)}")
            satirlar.append(f"    __slots__ = {tuple(getattr(nesne, '__slots__', ()))!r}")
        return satirlar
    if inspect.ismodule(nesne):
        return [f"module {ad} -> {_normalize_module(nesne.__name__)}"]
    if isinstance(nesne, dict):
        # `repr` fonksiyon nesnelerinde BELLEK ADRESI basar — deterministik degil.
        satirlar = [f"dict {ad}:"]
        for anahtar, deger in nesne.items():
            if inspect.isfunction(deger):
                gosterim = f"{_normalize_module(deger.__module__)}.{deger.__qualname__}"
            else:
                gosterim = repr(deger)
            satirlar.append(f"    {anahtar!r}: {gosterim}")
        return satirlar
    return [f"other {ad}: {type(nesne).__module__}.{type(nesne).__qualname__} = {nesne!r}"]


def _yuzey_satirlari() -> list[str]:
    satirlar: list[str] = []
    for ad in sorted(vars(service)):
        if ad.startswith("__"):
            continue
        satirlar.extend(_describe(ad, getattr(service, ad)))
    return satirlar


class _StubRow:
    """`sqlalchemy.Row` yerine gecer: `row[0]` + adlandirilmis turev sutunlar.

    Gercek `Row` uretmek bir motor/sorgu ister; anlik goruntu DB'siz kalmalidir.
    Servis satirdan yalniz bu iki yuzeyi okur, taklit BIREBIR yeterlidir.
    """

    def __init__(self, varlik: object, **turevler: object) -> None:
        self._varlik = varlik
        for anahtar, deger in turevler.items():
            setattr(self, anahtar, deger)

    def __getitem__(self, indis: int) -> object:
        if indis != 0:
            raise IndexError(indis)
        return self._varlik


def _supplier() -> Supplier:
    return Supplier(
        id=_ID,
        name="Demirsan A.S.",
        category="Hirdavat",
        tax_no="1234567890",
        phone="0312 000 00 00",
        payment_terms=PaymentTerms.days_30,
        is_active=True,
        created_at=_ZAMAN,
    )


def _request() -> PurchaseRequest:
    return PurchaseRequest(
        id=_ID,
        request_no="SAT-2026-0001",
        request_date=_GUN,
        priority=PurchasePriority.urgent,
        project_id=_ID2,
        site_id=_ID3,
        section_id=_ID4,
        needed_by=_GUN,
        justification="Kalip iskelesi eksigi",
        status=PurchaseRequestStatus.draft,
        quote_deadline=_GUN,
        approved_by_user_id=None,
        approved_at=None,
        rejected_at=None,
        rejection_reason=None,
        created_by_user_id=_ID5,
        created_at=_ZAMAN,
    )


def _quote() -> PurchaseQuote:
    return PurchaseQuote(
        id=_ID,
        request_id=_ID2,
        supplier_id=_ID3,
        unit_price=Decimal("1250.75"),
        delivery_time="10 gun",
        warranty_note="2 yil",
        payment_terms=PaymentTerms.days_60,
        shipping_included=False,
        shipping_cost=Decimal("500.00"),
        is_selected=False,
        created_at=_ZAMAN,
    )


def _order() -> PurchaseOrder:
    return PurchaseOrder(
        id=_ID,
        order_no="SIP-2026-0007",
        request_id=_ID2,
        quote_id=_ID3,
        supplier_id=_ID4,
        project_id=_ID5,
        total_amount=Decimal("98765.43"),
        expected_delivery=_GUN,
        status=PurchaseOrderStatus.approved,
        note="Sahaya teslim",
        created_by_user_id=_ID5,
        created_at=_ZAMAN,
    )


def _line(*, fiyatli: bool, kartli: bool) -> PurchaseRequestLine:
    return PurchaseRequestLine(
        id=_ID,
        request_id=_ID2,
        stock_item_id=_ID3 if kartli else None,
        free_text_name=None if kartli else "Serbest kalem",
        free_text_unit=None if kartli else "Adet",
        quantity=Decimal("12.500"),
        estimated_unit_price=Decimal("40.25") if fiyatli else None,
        sort_order=3,
    )


def _stock_item() -> StockItem:
    return StockItem(id=_ID3, code="CMT-042", name="Cimento CEM I", unit="Ton")


def _hata_metni(cagri: object) -> str:
    """Fonksiyonu kosar; yukselen istisnayi `TIP: metin` olarak dondurur."""
    try:
        sonuc = cagri()
        if inspect.iscoroutine(sonuc):
            asyncio.run(sonuc)
    except Exception as hata:
        return f"{type(hata).__name__}: {hata}"
    return "OK"


def _davranis_satirlari() -> list[str]:
    satirlar: list[str] = []

    satirlar.append(f"PERMISSION_MODULE = {service.PERMISSION_MODULE!r}")

    for girdi in (None, "", "   ", " Demirsan ", "Demirsan"):
        satirlar.append(f"_strip({girdi!r}) = {service._strip(girdi)!r}")

    for fiyatli in (True, False):
        satir = _line(fiyatli=fiyatli, kartli=True)
        satirlar.append(f"_line_total(fiyatli={fiyatli}) = {service._line_total(satir)!r}")

    for alan, deger in sorted(service._base_fields(_request()).items()):
        satirlar.append(f"_base_fields[{alan}] = {deger!r}")

    for alan, deger in sorted(service._quote_fields(_quote()).items()):
        satirlar.append(f"_quote_fields[{alan}] = {deger!r}")

    bakiyeler = {_ID3: Decimal("7.250")}
    for kartli in (True, False):
        for fiyatli in (True, False):
            yanit = service._to_line_response(
                _line(fiyatli=fiyatli, kartli=kartli),
                _stock_item() if kartli else None,
                bakiyeler,
            )
            satirlar.append(
                f"_to_line_response(kartli={kartli}, fiyatli={fiyatli}) = {yanit.model_dump()!r}"
            )
    bos_bakiye = service._to_line_response(_line(fiyatli=True, kartli=True), _stock_item(), {})
    satirlar.append(f"_to_line_response(bakiyesiz) = {bos_bakiye.model_dump()!r}")

    kart = service._to_supplier_card(
        _StubRow(_supplier(), orders_total=Decimal("125000.00"), orders_count=4)
    )
    satirlar.append(f"_to_supplier_card = {kart.model_dump()!r}")

    siparis = service._to_order_response(
        _StubRow(_order(), supplier_name="Demirsan A.S.", request_no="SAT-2026-0001")
    )
    satirlar.append(f"_to_order_response = {siparis.model_dump()!r}")

    yeni = service._new_lines(
        _ID2,
        [
            PurchaseRequestLineCreate(
                stock_item_id=_ID3, quantity=Decimal("2.000"), estimated_unit_price=None
            ),
            PurchaseRequestLineCreate(
                free_text_name="  Serbest  ",
                free_text_unit="  Adet  ",
                quantity=Decimal("1.500"),
                estimated_unit_price=Decimal("10.00"),
            ),
        ],
    )
    for satir in yeni:
        satirlar.append(
            "_new_lines[] = "
            + repr(
                {
                    "request_id": satir.request_id,
                    "stock_item_id": satir.stock_item_id,
                    "free_text_name": satir.free_text_name,
                    "free_text_unit": satir.free_text_unit,
                    "quantity": satir.quantity,
                    "estimated_unit_price": satir.estimated_unit_price,
                    "sort_order": satir.sort_order,
                }
            )
        )

    for durum in PurchaseRequestStatus:
        talep = _request()
        talep.status = durum
        taslak_sonuc = _hata_metni(lambda t=talep: service._assert_draft(t))
        satirlar.append(f"_assert_draft({durum.value}) -> {taslak_sonuc}")
        satirlar.append(
            f"_assert_quote_wait({durum.value}) -> "
            f"{_hata_metni(lambda t=talep: service._assert_quote_wait(t))}"
        )

    for dahil in (True, False):
        for bedel in (None, Decimal("0.00"), Decimal("500.00")):
            satirlar.append(
                f"_assert_shipping_rule(included={dahil}, cost={bedel!r}) -> "
                + _hata_metni(lambda d=dahil, b=bedel: service._assert_shipping_rule(d, b))
            )

    for eylem, uretici in service._TRANSITION_MESSAGES.items():
        satirlar.append(f"_TRANSITION_MESSAGES[{eylem.value}] = {uretici('SAT-2026-0001')!r}")

    return satirlar


def build_surface() -> str:
    """Iki bolumluk anlik goruntu metni: MODUL YUZEYI + SAF YARDIMCI CIKTILARI."""
    parcalar = [
        "# TB-PROC anlik goruntusu — `app.modules.procurement.service`",
        "# Uretim: tests/modules/procurement/test_tbproc_servis_yuzeyi.py :: build_surface()",
        "",
        "## A. MODUL YUZEYI (ad + imza + varsayilanlar)",
        "",
        *_yuzey_satirlari(),
        "",
        "## B. SAF YARDIMCI CIKTILARI (deger + hata metni)",
        "",
        *_davranis_satirlari(),
        "",
    ]
    return "\n".join(parcalar)


def test_servis_yuzeyi_anlik_goruntuyle_ayni() -> None:
    """Bolme oncesi yuzeyin HER SATIRI hala var ve AYNEN ayni.

    Alt kume kurali bilinclidir (modul docstring'i): cephe yeni ad EKLEYEBILIR,
    ama eskisini dusuremez ya da degistiremez.
    """
    beklenen = ANLIK_GORUNTU.read_text(encoding="utf-8").splitlines()
    guncel = set(build_surface().splitlines())

    kayip = [satir for satir in beklenen if satir not in guncel]
    assert not kayip, "Bolme sonrasi YUZEY KAYBI/DEGISIMI:\n" + "\n".join(kayip)
