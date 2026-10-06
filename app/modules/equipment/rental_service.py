"""Kira hakedişi iş kuralları (MK-2 T3) — M5'in kapıları.

`service.py`den (MK-1 çekirdeği) AYRI bir dosyadır: o dosya zaten kendi üç
konusunu (kart · çalışma · yakıt) taşıyor ve kira hakedişi kendi durum makinesi +
snapshot'ıyla dördüncü bir konudur. Aynı dosyaya konsaydı tek modül 1300 satırı
aşar ve "durum kapısı nerede yaşıyor" sorusu kaybolurdu.

## İş kurallarının HARİTASI

* **🔴 K2 SNAPSHOT** → `_build_lines`: saat çalışma kaydından KOPYALANIR; okuma
  uçlarında CANLI SORGU YOKTUR. Tazeleme AÇIK bir eylemdir (`reload_invoice`,
  yalnız `draft`).
* **🔴 K3 çift ödeme imkânsızlığı** → burada DEĞİL `rental.py`de: `our_total`
  yalnız `rented` satırlardan beslenir. Bu modül İKİNCİ bir toplama yolu AÇMAZ —
  tüm toplamlar tek bir `compute_invoice` çağrısından gelir.
* **🔴 K5 durum makinesi** → `rental_transitions.py` (tek tablo) +
  `_assert_editable` (düzenleme kilidi).
* **🔴 K8 tedarikçi eşleşmesi** → `_assert_supplier_match` (422). Satır kurulumu
  zaten yalnız eşleşen makineleri alır; bu denetim başlığın tedarikçisi
  DEĞİŞTİRİLDİĞİNDE devreye girer.
* **🔴 K9 görünürlük** → `visible_invoice` / `_locked_visible_invoice`: liste,
  detay, PATCH, satır uçları ve durum uçlarının HEPSİ buradan geçer.
* **🔴 EŞİK = KİLİT** → `_locked_visible_invoice` + `lock_invoice_lines`; kilit
  DURUM DENETİMİNDEN ÖNCE, sıra tüm uçlarda SABİT (başlık → satırlar).

**Para formülü BURADA DEĞİLDİR:** KDV zinciri, saatlik bedel ve üç tür toplamı
`rental.py`nin (T2) `compute_invoice`ından TEK ÇAĞRIYLA gelir; ikinci bir çarpım
bu dosyada yazılmaz (P10 "tek formül" kanonu).
"""

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    DuplicateError,
    EquipmentValidationError,
    NotFoundError,
)
from app.core.slug import allocate_slug
from app.modules.equipment import (
    rental_para_gercek,
    rental_posting,
    rental_repository,
    rental_transitions,
    service,
)
from app.modules.equipment.models import (
    Equipment,
    EquipmentOwnership,
    EquipmentRentalInvoice,
    EquipmentRentalInvoiceLine,
    RentalInvoiceStatus,
    RentalLineKind,
    WorkLogType,
)
from app.modules.equipment.rental import (
    RentalLineInput,
    compute_invoice,
    compute_payable_total,
    compute_vat_amount,
)
from app.modules.equipment.rental_schemas import (
    RentalInvoiceCreate,
    RentalInvoiceDetailResponse,
    RentalInvoiceLineResponse,
    RentalInvoiceLineUpdate,
    RentalInvoiceResponse,
    RentalInvoiceTotals,
    RentalInvoiceUpdate,
    RentalSiteDistributionEntry,
    RentalSiteDistributionEquipment,
)
from app.modules.procurement.models import Supplier
from app.modules.users.models import User

#: İzin anahtarı MK-1'de açılan `equipment`tir — MK-2'de YENİ MODÜL AÇILMAZ
#: (spec §4, `payroll`/İK-3 emsali); izin migration'ı da yoktur.
PERMISSION_MODULE = service.PERMISSION_MODULE

INVOICE_MISSING = "Kira hakedişi bulunamadı."
"""Görünmeyen VE var olmayan faturanın TEK cümlesi — ikisi ayırt EDİLEMEZ."""

LINE_MISSING = "Kira hakedişi satırı bulunamadı."
"""Satırın görünürlüğü FATURASININ görünürlüğüdür; ayrı bir kapı yoktur."""

INVOICE_LOCKED = (
    "Onaylanmış ya da ödenmiş bir kira hakedişi düzenlenemez. "
    "Düzeltme için önce hakedişin onayını geri alın."
)
"""🔴 K5 — İK-3 S5 emsali. Başlık PATCH'i, satır PATCH'i ve satır silme AYNI
kapıdan geçer: biri açık kalsaydı onaylanmış bir ödemenin tutarı sessizce
değiştirilebilirdi."""

RELOAD_ONLY_DRAFT = (
    "Çalışma kaydından tazeleme yalnız taslak hakedişte yapılabilir. "
    "Doğrulama, kullanıcının gördüğü saatler üzerinde yapılır."
)
LINE_DELETE_ONLY_DRAFT = "Kira hakedişi satırı yalnız taslak hakedişte silinebilir."

NOT_APPROVABLE = "Bu kira hakedişi onaylanamaz."
NOT_REJECTABLE = "Yalnız onaylanmış bir kira hakedişinin onayı geri alınabilir."


SUPPLIER_MISMATCH = (
    "Bu hakedişteki kiralık ekipmanlar seçilen kiralama firmasına ait değil. "
    "Bir hakediş tek bir kiralama firmasına aittir."
)
"""🔴 K8. 404 DEĞİL (tedarikçi vardır ve görünür), 409 da DEĞİL (engel kaydın
DURUMU değil gövdedeki düzeltilebilir ALAN DEĞERİ)."""

INVOICE_NO_DUPLICATE = "Bu kiralama firması için aynı fatura numarası zaten kayıtlı."
"""UQ `(supplier_id, invoice_no)` — aynı faturayı iki kez ödemenin YAPISAL
engeli; `invoice_no` NULL iken taslaklar serbesttir (NULLS DISTINCT)."""

_ZERO = Decimal("0")


# --- Görünürlük (K9) + kilit ---


async def visible_invoice(
    session: AsyncSession, actor: User, invoice_ref: uuid.UUID | str
) -> EquipmentRentalInvoice:
    """OKUMA yolunun TEK kapısı — kilitsiz.

    Yetki seviyesi bu kararın ÖNÜNE GEÇMEZ: `equipment:admin` taşıyan ama projeyi
    görmeyen kullanıcı da 404 alır (ST IDOR dersi).
    """
    invoice = await rental_repository.get_invoice(session, invoice_ref)
    if invoice is None:
        raise NotFoundError(INVOICE_MISSING)
    # IZN-B5f: slug ile açılan detay liste ile AYNI kapsamı kullanır.
    liste = not isinstance(invoice_ref, uuid.UUID)
    if not await service._is_visible_site(session, actor, invoice.site_id, liste=liste):
        raise NotFoundError(INVOICE_MISSING)
    return invoice


async def _locked_visible_invoice(
    session: AsyncSession,
    actor: User,
    invoice_id: uuid.UUID,
    *,
    missing: str = INVOICE_MISSING,
) -> EquipmentRentalInvoice:
    """🔴 YAZMA yolunun TEK kapısı: kilit HER DENETİMDEN ÖNCE alınır (TOCTOU).

    Görünürlük ve durum denetimi kilitten SONRA koşar. Ters sırada olsaydı
    (önce kilitsiz oku, karar ver, sonra kilitle) iki eşzamanlı istek AYNI durumu
    okur ve ikisi de geçerdi — fatura iki kez ödenirdi. Bu sıra
    `test_mk2_rental_invoice_concurrency.py`de SQL düzeyinde iddia edilir.
    """
    invoice = await rental_repository.lock_invoice(session, invoice_id)
    if invoice is None:
        raise NotFoundError(missing)
    if not await service._is_visible_site(session, actor, invoice.site_id):
        raise NotFoundError(missing)
    return invoice


def _assert_editable(invoice: EquipmentRentalInvoice) -> None:
    """🔴 K5 — `approved`/`paid` faturada HİÇBİR ŞEY düzenlenemez (409)."""
    if invoice.status in rental_transitions.EDIT_LOCKED_STATUSES:
        raise ConflictError(INVOICE_LOCKED)


# --- 🔴 K2: satırların çalışma kaydından KURULMASI ---


def _dominant_site(kova: dict[uuid.UUID | None, Decimal]) -> uuid.UUID | None:
    """Satırın şantiyesi — 🔴 BELİRSİZLİKTE `None` (fail-closed).

    UQ `(invoice_id, equipment_id, line_kind)` bir makineye tür başına TEK satır
    verir (spec §2.2); makine dönem içinde İKİ ŞANTİYEDE çalıştıysa tek bir
    şantiye seçmek zorunludur. Saatlerin çoğunluğuna göre seçilseydi maliyetin
    tamamı çalışılmayan bir projeye yazılırdı — bu yüzden belirsiz durumda kova
    "Atanmamış" (`None`) olur: para KAYBOLMAZ, yalnız uydurma bir projeye
    yazılmaz (MK-1 K16 fail-closed ilkesi).
    """
    adaylar = [site_id for site_id, saat in kova.items() if saat > _ZERO]
    return adaylar[0] if len(adaylar) == 1 else None


def _line_targets(
    ham: list, ekipmanlar: dict[uuid.UUID, Equipment]
) -> dict[tuple[uuid.UUID, RentalLineKind], tuple[Decimal, Decimal, uuid.UUID | None]]:
    """Dönemin ham saatlerinden KURULACAK satır kümesi.

    🔴 **Tür MÜLKİYETTEN okunur** (K3): kiralık makine `rented`, kendi makinemiz
    `owned`. Arıza saati AYRI bir `breakdown` satırına yazılır (M5:128-139 üstü
    çizili satır) ve kira satırının `breakdown_hours`u 0 kalır — ikisi tek satırda
    toplansaydı "neyi ödemediğimiz" ekranda kaybolurdu.

    Saati sıfır olan satır KURULMAZ: hiç çalışmamış makineyi faturaya basmak
    (M3'ün 0 saatlik satırından farklı olarak) ödenecek bir kalem gibi görünürdü.
    """
    birikim: dict[uuid.UUID, dict[WorkLogType, dict[uuid.UUID | None, Decimal]]] = {}
    for equipment_id, site_id, record_type, hours in ham:
        kovalar = birikim.setdefault(equipment_id, {tip: {} for tip in WorkLogType})
        kova = kovalar[record_type]
        kova[site_id] = kova.get(site_id, _ZERO) + hours

    hedefler: dict[tuple[uuid.UUID, RentalLineKind], tuple[Decimal, Decimal, uuid.UUID | None]] = {}
    for equipment_id, kovalar in birikim.items():
        equipment = ekipmanlar.get(equipment_id)
        if equipment is None:
            continue
        calisma = kovalar[WorkLogType.worked]
        ariza = kovalar[WorkLogType.breakdown]
        calisma_toplami = sum(calisma.values(), _ZERO)
        ariza_toplami = sum(ariza.values(), _ZERO)
        if calisma_toplami > _ZERO:
            tur = (
                RentalLineKind.rented
                if equipment.ownership is EquipmentOwnership.rented
                else RentalLineKind.owned
            )
            hedefler[(equipment_id, tur)] = (calisma_toplami, _ZERO, _dominant_site(calisma))
        if ariza_toplami > _ZERO:
            hedefler[(equipment_id, RentalLineKind.breakdown)] = (
                _ZERO,
                ariza_toplami,
                _dominant_site(ariza),
            )
    return hedefler


async def _build_lines(session: AsyncSession, actor: User, invoice: EquipmentRentalInvoice) -> None:
    """🔴 K2 — satırları çalışma kaydından KURAR/TAZELER (M5:83).

    Kopyalanan BEŞ şey vardır: `worked_hours`, `breakdown_hours`, `site_id`,
    🔴 **`rate_amount`** (MK-2 T5 bulgusu) ve 🔴 **`capacity_hours`**
    (MK-3 K1). K4'ün "satırın bedeli boşsa ekipmanınki" kuralı bir ÇÖZÜM
    kuralıdır ve BURADA, satır kurulurken uygulanır — okuma yolunda karta canlı
    düşülseydi K2'nin kapattığı delik paranın İKİNCİ çarpanından yeniden açılır,
    onaylanmış bir faturanın tutarı kart üzerindeki bir bedel düzeltmesiyle
    sessizce oynardı. M5:93 alanı zaten dolu ve düzenlenebilir basıyor: mockup
    da bedeli satırın kendi verisi sayıyor.

    🔴 `capacity_hours` aynı deliğin ÜÇÜNCÜ çarpanıdır: `monthly` dönemde
    saatlik bedelin PAYDASI'dır ve canlı okunsaydı kapasite düzeltmesi
    onaylanmış bir faturanın tutarını geriye dönük oynatırdı. Kalıcı ders: bir
    türev para değeri N çarpandan oluşuyorsa snapshot iddiası N'in HEPSİNİ
    kapsamalıdır. **Dönem ne olursa olsun HER satırda doldurulur** (K3): dönem
    `draft`ta `PATCH`le `monthly`ye çevrilebilir ve o an değer yoksa geriye
    dönük doldurmak için tam da kapattığımız canlı okumaya dönmek gerekirdi.

    Kullanıcının girdiği alanlar (`rate_amount`, `invoiced_hours`) tazelemede
    KORUNUR — onlar bizim çalışma kaydımızdan gelmez; firmanın iddiası ve
    kullanıcının düzeltmesidir, silinselerdi doğrulama emeği her `reload`da
    çöpe giderdi. Bedel yalnız **boşken** karttan doldurulur: hiç yazılmamış bir
    değeri doldurmak veri kaybı değildir, yazılmış bir değeri ezmek olurdu.

    Dayanağı kalmayan satır SİLİNİR: kaydı geri alınmış bir makine faturada
    kalsaydı, ödenecek toplam artık var olmayan bir saatten beslenirdi.

    Kapsam süzgeci (K9/MK-1 K20) HEDEFLERİ kurarken koşar: kullanıcının
    göremediği bir projenin saatleri görünür bir faturaya SIZAMAZ.

    🔴 Ama kapsam bir OKUMA süzgecidir, SİLME yetkisi DEĞİLDİR. Satır kümesi
    `invoice_id`ye göre KAPSAMSIZ kilitlenir ve detay ucu da satırları kapsamsız
    basar — yani dar kapsamlı kullanıcı o satırları zaten GÖRÜR. Silme dalı
    kapsamla daraltılmasaydı, geniş kapsamlı birinin kurduğu "Tüm Projeler"
    faturasını dar kapsamlı biri tazelediğinde görmediği projelerin satırları ve
    onlara girilmiş `invoiced_hours`/`rate_amount` emeği SESSİZCE silinir,
    ödenecek toplam düşerdi. Bu yüzden kapsam DIŞI artık satıra DOKUNULMAZ:
    onu geniş kapsamlı bir `reload` temizler. Sessiz veri kaybı ile gecikmiş
    temizlik arasında doğru seçim gecikmiş temizliktir.
    """
    project_ids = await service._visible_project_ids(session, actor)
    ilk, son = service.month_bounds(invoice.period_year, invoice.period_month)
    ham = await rental_repository.period_hours(
        session,
        project_ids,
        supplier_id=invoice.supplier_id,
        date_from=ilk,
        date_to=son,
        site_id=invoice.site_id,
    )
    ekipmanlar = await rental_repository.equipment_by_ids(
        session, sorted({satir[0] for satir in ham})
    )
    hedefler = _line_targets(ham, ekipmanlar)

    mevcut = {
        (satir.equipment_id, satir.line_kind): satir
        for satir in await rental_repository.lock_invoice_lines(session, invoice.id)
    }
    for (equipment_id, tur), (calisma, ariza, site_id) in hedefler.items():
        # Bedel BURADA çözülür ve satıra KOPYALANIR (K4 çözüm kuralı + K2
        # snapshot ilkesi). Ekipmanın bedeli yoksa satırınki de `None` kalır —
        # uydurma 0 BASILMAZ (MK-1 K16 fail-closed) ve `our_amount` `None` olur.
        bedel = ekipmanlar[equipment_id].rate_amount
        # 🔴 MK-3 K1 — `monthly` paydası da BURADA donar. Çözülmüş saatlik bedel
        # KOLONLAŞMAZ (MK-2 K4: para tek formülden türer, `cost.py`de); donan
        # şey formülün GİRDİSİDİR.
        kapasite = ekipmanlar[equipment_id].monthly_capacity_hours
        satir = mevcut.pop((equipment_id, tur), None)
        if satir is None:
            session.add(
                EquipmentRentalInvoiceLine(
                    invoice_id=invoice.id,
                    equipment_id=equipment_id,
                    line_kind=tur,
                    site_id=site_id,
                    worked_hours=calisma,
                    breakdown_hours=ariza,
                    rate_amount=bedel,
                    capacity_hours=kapasite,
                )
            )
            continue
        satir.worked_hours = calisma
        satir.breakdown_hours = ariza
        satir.site_id = site_id
        if satir.rate_amount is None:
            satir.rate_amount = bedel
        # K5 — `rate_amount`la BİREBİR aynı kural: boşken doldurulur, DOLU değer
        # EZİLMEZ. Ezilseydi snapshot'ın verdiği güvence her tazelemede geri
        # alınır, fatura yeniden ekipman kartına bağlanırdı.
        if satir.capacity_hours is None:
            satir.capacity_hours = kapasite
    if mevcut:
        gorunen_santiyeler = await rental_repository.visible_site_ids(session, project_ids)
        for artik in mevcut.values():
            # 🔴 Kapsam DIŞI satır SİLİNMEZ (docstring). Şantiyesiz satır
            # ("Atanmamış") kapsam süzgecine TABİ DEĞİLDİR — `scope()`un depo
            # dalının birebir kardeşi.
            if artik.site_id is not None and artik.site_id not in gorunen_santiyeler:
                continue
            await session.delete(artik)
    await session.flush()


async def _assert_supplier_match(session: AsyncSession, invoice: EquipmentRentalInvoice) -> None:
    """🔴 K8 — `rented` satırların ekipmanı faturanın tedarikçisiyle EŞLEŞMELİ.

    Satır kurulumu zaten yalnız eşleşen makineleri alır; bu denetim başlığın
    tedarikçisi PATCH ile DEĞİŞTİRİLDİĞİNDE ihlali yakalar. `owned` satırlarda
    tedarikçi ARANMAZ (kendi makinemizin kirası yoktur).
    """
    for satir, equipment in await rental_repository.invoice_lines(session, invoice.id):
        if (
            satir.line_kind is RentalLineKind.rented
            and equipment.supplier_id != invoice.supplier_id
        ):
            raise EquipmentValidationError(SUPPLIER_MISMATCH)


# --- Referans denetimleri ---


async def _assert_references(
    session: AsyncSession,
    actor: User,
    *,
    supplier_id: uuid.UUID,
    site_id: uuid.UUID | None,
) -> None:
    """Gövdedeki varlık referansları — var olmayan/görünmeyen referans **404**.

    `site_id` görünmeyen bir şantiyeyi gösterdiğinde de AYNI 404 döner: aksi
    hâlde kullanıcı hakedişi görmediği bir projeye taşıyıp kaydı kendinden
    gizleyebilir, üstelik o projenin varlığını da öğrenirdi (ST kanonu).
    """
    if await session.get(Supplier, supplier_id) is None:
        raise NotFoundError(service.SUPPLIER_MISSING)
    if not await service._is_visible_site(session, actor, site_id):
        raise NotFoundError(service.SITE_MISSING)


async def _assert_invoice_no(
    session: AsyncSession,
    *,
    supplier_id: uuid.UUID,
    invoice_no: str | None,
    exclude_id: uuid.UUID | None = None,
) -> None:
    if invoice_no is None:
        return
    if await rental_repository.invoice_no_exists(
        session, supplier_id=supplier_id, invoice_no=invoice_no, exclude_id=exclude_id
    ):
        raise DuplicateError(INVOICE_NO_DUPLICATE)


# --- Yanıt kurulumu (M5'in TÜM türev yüzeyi) ---


async def _header(session: AsyncSession, invoice: EquipmentRentalInvoice) -> RentalInvoiceResponse:
    """Fatura başlığı — KDV zinciri `rental.py`den (K1), ad çözümü DB'den."""
    supplier = await session.get(Supplier, invoice.supplier_id)
    adlar = await rental_repository.site_names(
        session, [] if invoice.site_id is None else [invoice.site_id]
    )
    kdv = compute_vat_amount(invoice_amount=invoice.invoice_amount, vat_rate=invoice.vat_rate)
    return RentalInvoiceResponse(
        id=invoice.id,
        # 🔴 LİSTEYE DE GİRER: `RentalInvoiceResponse` hem liste satırı hem
        # detay başlığıdır, yani `makine/kira/[invoiceId]` bağlantısını üreten
        # şema budur.
        slug=invoice.slug,
        supplier_id=invoice.supplier_id,
        supplier_name=None if supplier is None else supplier.name,
        invoice_no=invoice.invoice_no,
        invoice_amount=invoice.invoice_amount,
        period_year=invoice.period_year,
        period_month=invoice.period_month,
        site_id=invoice.site_id,
        site_name=adlar.get(invoice.site_id) if invoice.site_id else None,
        rate_period=invoice.rate_period,
        vat_rate=invoice.vat_rate,
        vat_amount=kdv,
        payable_total=compute_payable_total(invoice_amount=invoice.invoice_amount, vat_amount=kdv),
        status=invoice.status,
        approved_by_id=invoice.approved_by_id,
        approved_at=invoice.approved_at,
        paid_at=invoice.paid_at,
        created_at=invoice.created_at,
    )


async def invoice_detail(
    session: AsyncSession, invoice: EquipmentRentalInvoice
) -> RentalInvoiceDetailResponse:
    """`GET …/{id}` — M5'in TAMAMI: satırlar + üç toplam + KDV + proje dağılımı.

    🔴 **K2:** tutarı etkileyen hiçbir sayı CANLI okunmaz — ne saat, ne bedel,
    ne payda. **İSTİSNA YOKTUR.** `rate_amount` (MK-2 T5) ve `capacity_hours`
    (MK-3) satır kurulurken kopyalandığı için (`_build_lines`) okuma yolu
    ekipman kartına hiç düşmez: `equipment_rate_amount=None` geçilir, payda
    `satir.capacity_hours`tan gelir.

    ✅ **MK-2'den devredilen "kalan tek canlı okuma" borcu BURADA KAPANDI.**
    `monthly_capacity_hours` kartta düzeltildiğinde onaylanmış (hatta ödenmiş)
    bir aylık-sabit faturanın tutarı geriye dönük oynuyordu; şimdi payda
    satırın kendi kolonundan okunur. Kapasite `null`/`0` ise saatlik bedel
    hesaplanamaz ve `our_amount` `null` durur (K2 fail-closed): uydurma 0 ya da
    enjekte edilmiş bir varsayılan BASILMAZ.

    ⚠️ Ekipmanın **adı/markası/plakası** canlı okunmaya DEVAM eder ve bu kusur
    değildir: fatura kartın adını dondurmaz. Ayrım şudur — **tutarı etkileyen
    girdi donar, sunum donmaz.**
    """
    satirlar = await rental_repository.invoice_lines(session, invoice.id)
    hesap = compute_invoice(
        invoice_amount=invoice.invoice_amount,
        vat_rate=invoice.vat_rate,
        rate_period=invoice.rate_period,
        lines=tuple(
            RentalLineInput(
                line_id=satir.id,
                equipment_id=satir.equipment_id,
                site_id=satir.site_id,
                line_kind=satir.line_kind,
                worked_hours=satir.worked_hours,
                breakdown_hours=satir.breakdown_hours,
                line_rate_amount=satir.rate_amount,
                equipment_rate_amount=None,
                invoiced_hours=satir.invoiced_hours,
                monthly_capacity_hours=satir.capacity_hours,
            )
            for satir, equipment in satirlar
        ),
    )

    ekipman_adlari = {equipment.id: equipment.name for _, equipment in satirlar}
    santiye_kimlikleri = {satir.site_id for satir, _ in satirlar if satir.site_id is not None}
    if invoice.site_id is not None:
        santiye_kimlikleri.add(invoice.site_id)
    santiye_adlari = await rental_repository.site_names(session, sorted(santiye_kimlikleri))

    baslik = await _header(session, invoice)
    return RentalInvoiceDetailResponse(
        **baslik.model_dump(),
        lines=[
            _line_response(satir, equipment, sonuc, santiye_adlari)
            for (satir, equipment), sonuc in zip(satirlar, hesap.lines, strict=True)
        ],
        totals=RentalInvoiceTotals(
            our_total=hesap.our_total,
            our_total_unknown_count=hesap.our_total_unknown_count,
            owned_total=hesap.owned_total,
            owned_total_unknown_count=hesap.owned_total_unknown_count,
            excluded_breakdown_amount=hesap.excluded_breakdown_amount,
            excluded_breakdown_unknown_count=hesap.excluded_breakdown_unknown_count,
            invoice_amount=hesap.invoice_amount,
            vat_rate=hesap.vat_rate,
            vat_amount=hesap.vat_amount,
            payable_total=hesap.payable_total,
        ),
        site_distribution=[
            RentalSiteDistributionEntry(
                site_id=kova.site_id,
                site_name=santiye_adlari.get(kova.site_id) if kova.site_id else None,
                hours=kova.hours,
                amount=kova.amount,
                unknown_count=kova.unknown_count,
                equipments=[
                    RentalSiteDistributionEquipment(
                        id=equipment_id, name=ekipman_adlari.get(equipment_id, "")
                    )
                    for equipment_id in kova.equipment_ids
                ],
            )
            for kova in hesap.site_distribution
        ],
    )


def _line_response(
    satir: EquipmentRentalInvoiceLine,
    equipment: Equipment,
    sonuc,  # noqa: ANN001 — `rental.RentalLineResult`; çember import açmamak için
    santiye_adlari: dict[uuid.UUID, str],
) -> RentalInvoiceLineResponse:
    """Satırın kolonları + `rental.py`nin türevleri TEK yerde birleşir."""
    return RentalInvoiceLineResponse(
        id=satir.id,
        equipment_id=satir.equipment_id,
        equipment_name=equipment.name,
        equipment_brand=equipment.brand,
        equipment_plate_no=equipment.plate_no,
        site_id=satir.site_id,
        site_name=santiye_adlari.get(satir.site_id) if satir.site_id else None,
        line_kind=satir.line_kind,
        worked_hours=satir.worked_hours,
        breakdown_hours=satir.breakdown_hours,
        rate_amount=satir.rate_amount,
        effective_rate_amount=sonuc.effective_rate_amount,
        our_amount=sonuc.our_amount,
        breakdown_amount=sonuc.breakdown_amount,
        invoiced_hours=satir.invoiced_hours,
        hours_variance=sonuc.hours_variance,
        variance_status=sonuc.variance_status,
    )


# --- Uçlar ---


async def list_invoices(
    session: AsyncSession,
    actor: User,
    *,
    supplier_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    equipment_id: uuid.UUID | None,
    status: RentalInvoiceStatus | None,
    period_year: int | None,
    period_month: int | None,
    limit: int,
    offset: int,
) -> tuple[list[RentalInvoiceResponse], int]:
    """Liste + `total` TEK kapsam kararını paylaşır (TB3 kanonu).

    🔴 `equipment_id` VARLIĞI DOĞRULANMAZ ve bu bilinçlidir: bu bir SÜZGEÇtir,
    bir varlık referansı değil. Var olmayan (ya da görünmeyen) bir kimlik BOŞ
    LİSTE döndürür, 404 değil — 404, süzgeci bir keşif aracına çevirir
    (kullanıcı hangi kimliklerin var olduğunu deneme yanılmayla öğrenirdi).
    """
    project_ids = await service._visible_project_ids(session, actor, liste=True)
    suzgecler = {
        "supplier_id": supplier_id,
        "site_id": site_id,
        "equipment_id": equipment_id,
        "status": status,
        "period_year": period_year,
        "period_month": period_month,
    }
    kayitlar = await rental_repository.list_invoices(
        session, project_ids, limit=limit, offset=offset, **suzgecler
    )
    total = await rental_repository.count_invoices(session, project_ids, **suzgecler)
    return [await _header(session, invoice) for invoice in kayitlar], total


async def create_invoice(
    session: AsyncSession, actor: User, data: RentalInvoiceCreate
) -> tuple[RentalInvoiceDetailResponse, str]:
    """`POST /equipment/rental-invoices` — başlık + 🔴 satırların KURULMASI.

    Sıra ÖNEMLİ ve tüm yazma uçlarında AYNI: önce referans/kapsam (404), sonra
    tekillik (409), sonra kayıt. Tersi olsaydı kapsam dışı bir şantiyeye POST
    atan kullanıcı 409 alır ve şantiyenin GÖRÜNMEDİĞİNİ değil fatura numarasının
    çakıştığını öğrenirdi.

    Satırlar gövdeden GELMEZ (M5:83 "Çalışma kaydından otomatik yüklendi").
    """
    await _assert_references(session, actor, supplier_id=data.supplier_id, site_id=data.site_id)
    await _assert_invoice_no(session, supplier_id=data.supplier_id, invoice_no=data.invoice_no)
    invoice = EquipmentRentalInvoice(**data.model_dump())
    # URL-4: taban `invoice_no`dur. Mockup ÖLÇÜLDÜ (`Makine - Kira Hakedişi
    # Liste`): listede tanımlayıcı olarak YALNIZ `Fatura No` vardır ve faturası
    # girilmemiş taslak satır ekranda `— (kayıt no yok)` basar — yani numarasız
    # taslağın İNSAN ADI YOKTUR. Bu yüzden slug de NULL kalır ve kayıt
    # UUID'siyle yaşar; uydurulmuş bir taban EKRANDAKİ gerçeği bozardı.
    invoice.slug = await allocate_slug(session, invoice.invoice_no, EquipmentRentalInvoice.slug)
    session.add(invoice)
    await session.flush()
    await _build_lines(session, actor, invoice)
    return (
        await invoice_detail(session, invoice),
        f"Kira hakedişi oluşturuldu: {invoice.period_year}-{invoice.period_month:02d}",
    )


async def _slug_gec_dogum(session: AsyncSession, invoice: EquipmentRentalInvoice) -> None:
    """Slug NULL iken numara İLK KEZ dolarsa slug'ı ayırır; aksi hâlde DOKUNMAZ.

    🔴 İKİ YÖNLÜ KAPI ve ikisi de bağlayıcı:

    * `slug is None` DEĞİLSE hiçbir şey yapılmaz — URL-2 kararı 4: slug
      oluşturulurken üretilir, **yeniden adlandırmayla DEĞİŞMEZ**. Numarası
      düzeltilen bir faturanın paylaşılmış bağlantısı ölmez.
    * `invoice_no` hâlâ boşsa da yapılmaz: uydurma taban yazılmaz.

    Yani bu yol YALNIZCA `NULL -> dolu` geçişinde ateşlenir ve kaydın ömrü
    boyunca EN FAZLA BİR KEZ çalışır.
    """
    if invoice.slug is not None:
        return
    if not (invoice.invoice_no or "").strip():
        return
    invoice.slug = await allocate_slug(session, invoice.invoice_no, EquipmentRentalInvoice.slug)


async def update_invoice(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID, data: RentalInvoiceUpdate
) -> tuple[RentalInvoiceDetailResponse, str]:
    """`PATCH …/{id}` — `draft` + `pending_verification`; ötesi 409 (K5).

    `exclude_unset` ŞART (F-İK dersi): dokunulmamış bir alan sunucudaki değeri
    EZMEMELİDİR.

    🔴 Dönem/şantiye değişikliği satırları KENDİLİĞİNDEN tazelemez (K2): tazeleme
    AÇIK bir eylemdir. Sessizce yeniden kurulsalardı kullanıcının girdiği fatura
    saatleri bir alan düzeltmesiyle silinirdi.
    """
    invoice = await _locked_visible_invoice(session, actor, invoice_id)
    _assert_editable(invoice)

    degisiklikler = data.model_dump(exclude_unset=True)
    hedef_supplier_id = degisiklikler.get("supplier_id", invoice.supplier_id)
    if {"supplier_id", "site_id"} & degisiklikler.keys():
        await _assert_references(
            session,
            actor,
            supplier_id=hedef_supplier_id,
            site_id=degisiklikler.get("site_id", invoice.site_id),
        )
    if {"supplier_id", "invoice_no"} & degisiklikler.keys():
        await _assert_invoice_no(
            session,
            supplier_id=hedef_supplier_id,
            invoice_no=degisiklikler.get("invoice_no", invoice.invoice_no),
            exclude_id=invoice.id,
        )

    for alan, deger in degisiklikler.items():
        setattr(invoice, alan, deger)
    # 🔴 URL-4 K1 — SLUG'IN GEÇ DOĞUMU. Kira faturası taslakta NUMARASIZ açılır
    # (mockup: `— (kayıt no yok)`) ve numara SONRADAN girilir. Slug yalnız
    # `create`te ayrılsaydı bu kayıt okunabilir URL'ini HİÇ ALMAZDI — ve
    # migration deploy anındaki aynı şekilli satırları doldurduğu için özellik
    # "verinin yaşının fonksiyonu" olurdu (deploy'dan önce numarası girilen
    # slug alır, sonra girilen almaz). Kabul edilemez bir ayrım.
    await _slug_gec_dogum(session, invoice)
    if "supplier_id" in degisiklikler:
        # 🔴 K8: tedarikçi değiştiyse mevcut `rented` satırlarla ÇELİŞMEMELİ.
        await _assert_supplier_match(session, invoice)
    await session.flush()
    return await invoice_detail(session, invoice), "Kira hakedişi güncellendi"


async def reload_invoice(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID
) -> tuple[RentalInvoiceDetailResponse, str]:
    """`POST …/{id}/reload` — 🔴 K2'nin AÇIK tazeleme eylemi, YALNIZ `draft`.

    `pending_verification`ta kapalıdır (K5): doğrulama, kullanıcının GÖRDÜĞÜ
    saatler üzerinde yapılır; altından veri çekilebilseydi onaylanan şey ile
    doğrulanan şey ayrışırdı.
    """
    invoice = await _locked_visible_invoice(session, actor, invoice_id)
    if invoice.status is not RentalInvoiceStatus.draft:
        raise ConflictError(RELOAD_ONLY_DRAFT)
    await _build_lines(session, actor, invoice)
    await _assert_supplier_match(session, invoice)
    return await invoice_detail(session, invoice), "Kira hakedişi çalışma kaydından tazelendi"


async def approve_invoice(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID
) -> tuple[RentalInvoiceResponse, str]:
    """`POST …/{id}/approve` — "Onayla ve Ödemeye Gönder" (ONAYLI SAPMA).

    M5:27'nin "Kiracıya Gönder" etiketi akış yönüyle ÇELİŞİR (gelen faturayı BİZ
    ödüyoruz, kiracıya göndermiyoruz); eylem adı bu yüzden değiştirildi ve sapma
    ROADMAP'e yazılır.

    Zinciri **TEK ADIM** ilerletir (`draft → pending_verification → approved`):
    tek çağrıda `draft → approved` yapılsaydı M5:65'in "Doğrulama Bekliyor" hâli
    hiç yaşanmaz ve doğrulama isteğe bağlı olurdu. Hedef `rental_transitions`tan
    TÜRETİLİR; burada ikinci bir zincir tanımı yoktur.

    Ödeme damgası bu uçtan BASILMAZ (`approved → paid` tabloda vardır ama kendi
    ucu vardır): "onayla"ya basan kullanıcı ödeme yapmış OLMAMALIDIR.

    🔴 Satırlar da kilitlenir: onay, o anki satır kümesinin onayıdır ve eşzamanlı
    bir satır PATCH'i onaylanan tutarı altından değiştirememelidir.
    """
    invoice = await _locked_visible_invoice(session, actor, invoice_id)
    await rental_repository.lock_invoice_lines(session, invoice.id)

    hedef = rental_transitions.next_forward_step(invoice.status)
    if hedef is None or hedef is RentalInvoiceStatus.paid:
        raise ConflictError(NOT_APPROVABLE)
    rental_transitions.assert_transition(invoice.status, hedef)

    invoice.status = hedef
    if hedef is RentalInvoiceStatus.approved:
        invoice.approved_by_id = actor.id
        invoice.approved_at = datetime.now(UTC)
        # 🔴 MU-3D — fiş DAMGADAN SONRA ve AYNI transaction'da. Kanca UCA DEĞİL
        #    HEDEF DURUMA bağlıdır: bu uç bir TEK ADIM İLERLETİCİDİR ve `draft`
        #    üzerinde çağrıldığında yalnız `pending_verification`a taşır —
        #    uca bağlansaydı doğrulanmamış bir kira bedeli deftere girerdi.
        await rental_posting.post_on_approval(session, actor, invoice)
    await session.flush()
    return await _header(session, invoice), f"Kira hakedişi durumu: {invoice.status.value}"


async def pay_invoice(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID
) -> tuple[RentalInvoiceResponse, str]:
    """`POST …/{id}/pay` — 🔴 ÖDENDİ damgası; `paid` bir UÇ DURUMDUR.

    Kapı geçiş tablosudur: `paid` hiçbir çiftin KAYNAĞI değildir, dolayısıyla
    ikinci çağrı 409 alır ve burada ayrıca bir sayaç tutulmaz. `draft`/
    `pending_verification`tan ödeme de aynı tablodan reddedilir (onay zinciri
    atlanamaz).

    🔴 EŞİK = KİLİT: fatura başlığı DENETİMDEN ÖNCE kilitlenir, sonra satırlar.
    Kilitsiz hâlde iki eşzamanlı çağrı aynı `approved` durumunu okur ve fatura
    İKİ KEZ ödenir — regresyon `test_mk2_rental_invoice_concurrency.py`dedir.
    """
    invoice = await _locked_visible_invoice(session, actor, invoice_id)
    await rental_repository.lock_invoice_lines(session, invoice.id)

    rental_transitions.assert_transition(invoice.status, RentalInvoiceStatus.paid)
    # 🔴 PARA-GERCEK (kullanıcı kararı 2026-09-19) — gövdesi `rental_para_gercek`
    #    modülündedir. Geçiş denetiminden SONRA koşar: yanlış durumdaki bir
    #    hakediş önce "onaylanmamış" demelidir, "parası gelmemiş" değil.
    await rental_para_gercek.assert_para_gercek(session, invoice)
    invoice.status = RentalInvoiceStatus.paid
    invoice.paid_at = datetime.now(UTC)
    await session.flush()
    return await _header(session, invoice), "Kira hakedişi ödendi olarak işaretlendi"


async def reject_invoice(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID
) -> tuple[RentalInvoiceResponse, str]:
    """`POST …/{id}/reject` — ONAYIN GERİ ALINMASI (`approved → pending_verification`).

    Ayrı bir `rejected` durumu YOKTUR (K5): reddedilen fatura "doğrulama
    bekleyen" listesine geri döner ve yeniden DÜZENLENEBİLİR hâle gelir.

    🔴 Kaynak durum AÇIKÇA `approved` olmalıdır. Yalnız geçiş tablosuna
    güvenilseydi `draft → pending_verification` çifti bu uçtan da kullanılabilir
    ve "red" bir ilerletme aracına dönüşürdü (İK-3 `reject_line` dersi).
    """
    invoice = await _locked_visible_invoice(session, actor, invoice_id)
    await rental_repository.lock_invoice_lines(session, invoice.id)

    if invoice.status is not RentalInvoiceStatus.approved:
        raise ConflictError(NOT_REJECTABLE)
    rental_transitions.assert_transition(invoice.status, RentalInvoiceStatus.pending_verification)
    invoice.status = RentalInvoiceStatus.pending_verification
    # Onay izi TEMİZLENİR: geri alınmış bir onayın damgası kalsaydı ekran
    # "onaylayan" gösterirken durum "doğrulama bekliyor" derdi.
    invoice.approved_by_id = None
    invoice.approved_at = None
    # 🔴 MU-3D · KARAR-5 — onay geri alındı, fiş STORNO edilir. Ayakta
    #    bırakılsaydı onaylı olmayan bir kira bedeli mizanda gider olarak
    #    kalırdı. Yeniden onay SERBESTTİR (tekillik CANLI fişlerle sınırlı).
    await rental_posting.reverse_rental_invoice(session, actor, invoice.id)
    await session.flush()
    return await _header(session, invoice), "Kira hakedişi onayı geri alındı"


# --- Satır uçları ---


async def _locked_line(
    session: AsyncSession, actor: User, line_id: uuid.UUID
) -> tuple[EquipmentRentalInvoice, EquipmentRentalInvoiceLine]:
    """Satırın TEK kapısı: önce BAŞLIK kilidi, sonra satır kilidi (sıra SABİT).

    Satır kilitsiz OKUNUR (yalnız faturasını öğrenmek için); kilit her zaman
    başlıktan başlar, yoksa satırdan başlayan bu yol öteki uçlarla karşılıklı
    kilitlenirdi.

    Görünmeyen faturanın satırı, var olmayan satırla AYNI 404'ü döner: satırın
    görünürlüğü faturasının görünürlüğüdür.
    """
    satir = await rental_repository.get_line(session, line_id)
    if satir is None:
        raise NotFoundError(LINE_MISSING)
    invoice = await _locked_visible_invoice(session, actor, satir.invoice_id, missing=LINE_MISSING)
    kilitli = {s.id: s for s in await rental_repository.lock_invoice_lines(session, invoice.id)}
    if line_id not in kilitli:
        raise NotFoundError(LINE_MISSING)
    return invoice, kilitli[line_id]


async def update_line(
    session: AsyncSession, actor: User, line_id: uuid.UUID, data: RentalInvoiceLineUpdate
) -> tuple[RentalInvoiceLineResponse, str]:
    """`PATCH /equipment/rental-invoice-lines/{id}` — YALNIZ iki alan (spec §4).

    🔴 `approved`/`paid` faturanın satırı düzenlenemez (K5 · İK-3 S5): kapı
    başlıkla AYNIDIR ve ayrıca testlenir — başlık kapısı kapalıyken satır
    kapısının açık kalması, onaylanmış bir ödemenin tutarını değiştirmenin en
    kolay yoludur.

    `pending_verification`ta AÇIKTIR: doğrulama tam olarak bu iki alanı girmektir.
    """
    invoice, satir = await _locked_line(session, actor, line_id)
    _assert_editable(invoice)

    for alan, deger in data.model_dump(exclude_unset=True).items():
        setattr(satir, alan, deger)
    await session.flush()

    detay = await invoice_detail(session, invoice)
    guncel = next(s for s in detay.lines if s.id == satir.id)
    return guncel, f"Kira hakedişi satırı güncellendi: {guncel.equipment_name}"


async def delete_line(session: AsyncSession, actor: User, line_id: uuid.UUID) -> str:
    """`DELETE /equipment/rental-invoice-lines/{id}` — YALNIZ `draft` (spec §4).

    `pending_verification`ta satır DÜZENLENİR ama SİLİNEMEZ: doğrulama aşamasında
    bir satırın yok olması, firmanın faturasıyla karşılaştırılan kümeyi sessizce
    küçültürdü. `approved`/`paid`te kapı zaten `_assert_editable`tır.
    """
    invoice, satir = await _locked_line(session, actor, line_id)
    _assert_editable(invoice)
    if invoice.status is not RentalInvoiceStatus.draft:
        raise ConflictError(LINE_DELETE_ONLY_DRAFT)

    equipment = await session.get(Equipment, satir.equipment_id)
    kunye = "?" if equipment is None else equipment.name
    await session.delete(satir)
    await session.flush()
    return f"Kira hakedişi satırı silindi: {kunye}"
