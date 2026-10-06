"""Fatura iş kuralları (T3) — liste · oluştur · detay · PATCH · DELETE · kalemler.

Spec: `docs/superpowers/specs/2026-08-14-fat1-fatura-cekirdegi-design.md`
§5, §6, §7, §8.

İKİ KATMANLI koruma (`procurement/service.py` deseninin birebiri): `invoicing`
izni router'da YETKİYİ verir, bu modül `projects.service.visible_projects` ile
KAPSAMI belirler.

## 🔴 Bu dosyanın YAZMADIĞI üç şey

1. **Para.** Hiçbir toplam burada hesaplanmaz; tek kaynak `amounts.compute`tur
   ve bu modül yalnızca çıktısını kolonlara YAZAR (`_apply_amounts`). İkinci bir
   formül açılsaydı `PUT lines` ile `PATCH` aynı faturaya farklı toplam yazardı.
2. **Durum denetimi.** `if invoice.status == …` YOKTUR; düzenleme/silme
   kapıları `transitions.assert_*` fonksiyonlarındadır (matrisin yanında).
3. **Numara.** Giden faturanın numarasını `numbering.generate_invoice_number`
   üretir (danışma kilidiyle); gelen faturanınki istemciden gelir (S5).

## Kilit sırası SABİT: fatura → kalemler (spec §8)

Yazan tüm uçlar `visible_invoice(..., for_update=True)` ile BAŞLAR ve kilit
DENETİMLERDEN ÖNCE alınır. Sıra uçtan uca aynı olmasaydı, kalemden başlayan bir
yol ile başlıktan başlayan bir yol karşılıklı kilitlenme üretirdi.

## Gövde içi referanslar: hepsi 404 (ST kanonu)

Proje · şantiye · dört taraf kartı · dört kaynak kaydı. Görünmeyen ile var
olmayan AYNI cümleyi alır — 403 verilseydi elinde kimlik olan kullanıcı kaydın
var olduğunu öğrenirdi. Biçim/alanlar-arası ihlaller ise 422'dir (`guards`
tablosu).
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import (
    ConflictError,
    DuplicateError,
    InvoicingValidationError,
    NotFoundError,
)
from app.modules.audit import messages
from app.modules.contracts.models import Subcontractor
from app.modules.customers.models import Customer
from app.modules.equipment.models import EquipmentRentalInvoice
from app.modules.invoicing import (
    amounts,
    guards,
    numbering,
    repository,
    source_amounts,
    transitions,
    validation,
)
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceLine,
    InvoiceStatus,
)
from app.modules.invoicing.schemas import (
    InvoiceCreate,
    InvoiceDetailResponse,
    InvoiceLineCreate,
    InvoiceLineResponse,
    InvoiceLinesReplace,
    InvoiceListResponse,
    InvoiceResponse,
    InvoiceUpdate,
)
from app.modules.procurement.models import PurchaseOrder, Supplier
from app.modules.progress_payments.models import ProgressPayment
from app.modules.projects.models import Employer
from app.modules.projects.service import sirket_liste_ciftleri, visible_projects
from app.modules.sites import repository as sites_repository
from app.modules.subcontractor_progress_payments.models import SubcontractorProgressPayment
from app.modules.users.models import User

PERMISSION_MODULE = guards.PERMISSION_MODULE
"""İzin anahtarı — TEK KOPYA `guards.PERMISSION_MODULE`dedir; bu ad router ve
testler için geriye dönük takma addır (`procurement.service` emsali)."""

#: Taraf izi FK'ları — hepsi GLOBAL kataloglardır (tabloda `project_id` yoktur),
#: bu yüzden yalnız VARLIK denetimi yapılır. Kapsam süzgeci EKLENMEZ: aynı
#: "Güneşkent Gayrimenkul A.Ş." her projede kullanılır (`suppliers` kanonu).
_PARTY_MODELS: dict[str, type] = {
    "employer_id": Employer,
    "customer_id": Customer,
    "supplier_id": Supplier,
    "subcontractor_id": Subcontractor,
}

#: Kaynak izi FK'ları ve KAPSAM kolonları. `None` "bu kaydın proje kapsamı
#: yoktur" demektir: makine kira hakedişi tedarikçi+döneme bağlıdır, projeye
#: değil (MK-2 tablosunda `project_id` kolonu YOKTUR) — uydurma bir süzgeç
#: yazmak yerine varlık denetimiyle yetinilir.
_SOURCE_MODELS: dict[str, tuple[type, str | None]] = {
    "progress_payment_id": (ProgressPayment, "project_id"),
    "subcontractor_progress_payment_id": (SubcontractorProgressPayment, "project_id"),
    "equipment_rental_invoice_id": (EquipmentRentalInvoice, None),
    "purchase_order_id": (PurchaseOrder, "project_id"),
}

#: PATCH'te `null` gönderilse bile KORUNAN alanlar: kolonları NOT NULL'dır ve
#: şema hepsini `| None` yazar (PATCH gövdesi kısmidir). `procurement.
#: update_request` deseninin aynısı.
_NOT_NULL_FIELDS = ("document_type", "issue_date", "party_name")

_ENGEL_AYRACI = " · "


async def _visible_project_ids(
    session: AsyncSession, actor: User, *, liste: bool = False
) -> list[uuid.UUID]:
    """`liste=True`: şirket türü liste/özet ucu (IZN-B5f 23b) — satırlar kişinin O PROJEDEKİ
    rolünün `fatura` sayfa izniyle süzülür (detay ucuyla aynı kural)."""
    ciftler = sirket_liste_ciftleri(PERMISSION_MODULE) if liste else ()
    return [p.id for p in await visible_projects(session, actor, sirket_ciftleri=ciftler)]


def _raise_blockers(*bloklar: list[str]) -> None:
    """Engellerin HEPSİ TEK 422'de gösterilir (`validation` modül docstring'i).

    Kullanıcıya eksikleri birer birer keşfettirmek FK gibi uzun bir formda
    kabul edilemez.
    """
    engeller = [engel for blok in bloklar for engel in blok]
    if engeller:
        raise InvoicingValidationError(_ENGEL_AYRACI.join(engeller))


# --- Kapsam ve gövde referansları ---


def _invoice_visible(invoice: Invoice, gorunen: list[uuid.UUID]) -> bool:
    """`project_id` NULL fatura (şirket geneli) modül izniyle GÖRÜNÜR (§6)."""
    return invoice.project_id is None or invoice.project_id in gorunen


async def visible_invoice(
    session: AsyncSession, actor: User, invoice_ref: uuid.UUID | str, *, for_update: bool = False
) -> Invoice:
    """Tekil erişimin TEK kapısı — okuma da yazma da buradan geçer.

    `for_update=True` satırı DENETİMLERDEN ÖNCE kilitler (spec §8): kapsam
    denetimi kilitli satır üzerinde koşar, böylece kilit ile karar arasına başka
    bir işlem giremez.

    Projesi görünür kümede değilse **404** döner ve gövde var OLMAYAN
    kimliğinkiyle BİREBİR AYNIDIR.

    ## URL-4 — `invoice_no` ile açılış ve BELİRSİZLİK (yönetim kararı)

    `invoice_no` şirket geneli tekil DEĞİLDİR (`uq_invoices_no_direction` yön
    başına tekil). Numarayla gelen istekte:
    **0 isabet -> 404 · 1 isabet -> döner · 2 isabet -> 409.**
    Sessizce biri SEÇİLMEZ.

    🔴 GÖRÜNÜRLÜK SÜZGECİ SAYMADAN ÖNCE UYGULANIR ve bu SIRA GÜVENLİK
    GEREĞİDİR, üslup değil: önce sayılsaydı, kullanıcının GÖREMEDİĞİ bir
    faturanın varlığı 409'un kendisiyle sızardı (görünen tek faturası olan
    kullanıcı, "demek ki bir tane daha var" bilgisini alırdı). Süzgeçten sonra
    sayınca, göremediği fatura onun için HİÇ YOKTUR ve tek isabetini normal
    şekilde açar.
    """
    if isinstance(invoice_ref, uuid.UUID):
        invoice = await repository.get_invoice(session, invoice_ref, for_update=for_update)
        if invoice is None:
            raise NotFoundError(guards.INVOICE_MISSING)
        if not _invoice_visible(invoice, await _visible_project_ids(session, actor)):
            raise NotFoundError(guards.INVOICE_MISSING)
        return invoice

    adaylar = await repository.list_invoices_by_no(session, invoice_ref)
    # IZN-B5f: numara dalı liste ile AYNI kapsamı kullanır (UUID'siz yoldan sızma yok).
    gorunen = await _visible_project_ids(session, actor, liste=True)
    gorunur = [fatura for fatura in adaylar if _invoice_visible(fatura, gorunen)]
    if not gorunur:
        raise NotFoundError(guards.INVOICE_MISSING)
    if len(gorunur) > 1:
        raise ConflictError(guards.INVOICE_NO_AMBIGUOUS)
    if not for_update:
        return gorunur[0]
    # Belirsizlik ÇÖZÜLDÜKTEN sonra kilit KİMLİKLE alınır: numara üzerinden
    # kilitlemek, tek satır seçilmeden birden çok satırı kilitlerdi.
    # (Bugün bu dal ÜRETİMDE ateşlenmez — `for_update` yalnız yazma yollarından
    # gelir ve onlar URL-2 kararı 3 gereği `uuid.UUID` taşır. Yine de yazılıdır:
    # eksik bırakılsaydı, ileride bir yazma yolu anahtar kabul ettiği gün
    # SESSİZCE KİLİTSİZ koşardı.)
    locked = await repository.get_invoice(session, gorunur[0].id, for_update=True)
    if locked is None:
        raise NotFoundError(guards.INVOICE_MISSING)
    return locked


async def _assert_references(
    session: AsyncSession, actor: User, degerler: dict[str, uuid.UUID | None]
) -> None:
    """Gövdedeki ON varlık referansı: proje · şantiye · 4 taraf · 4 kaynak.

    Hepsi **404**tür (ST §4b kanonu). Zincir SIKIDIR: şantiye faturanın
    PROJESİNE ait olmalıdır — gevşek bırakılsaydı fatura, projesiyle ilgisi
    olmayan bir şantiyeye bağlanabilir ve raporlar sessizce yanlış kırılırdı.

    Projesiz (`project_id` NULL) faturada şantiye seçilebilir: şantiyenin KENDİ
    projesi görünür olmak zorundadır, aksi hâlde görünmeyen bir projenin
    şantiyesi projesiz bir fatura üzerinden okunabilirdi.
    """
    gorunen = await _visible_project_ids(session, actor)

    project_id = degerler.get("project_id")
    if project_id is not None and project_id not in gorunen:
        raise NotFoundError(guards.INVOICE_PROJECT_INVALID)

    site_id = degerler.get("site_id")
    if site_id is not None:
        site = await sites_repository.get_site(session, site_id)
        if site is None:
            raise NotFoundError(guards.INVOICE_SITE_INVALID)
        if project_id is not None and site.project_id != project_id:
            raise NotFoundError(guards.INVOICE_SITE_INVALID)
        if project_id is None and site.project_id not in gorunen:
            raise NotFoundError(guards.INVOICE_SITE_INVALID)

    for alan, model in _PARTY_MODELS.items():
        kimlik = degerler.get(alan)
        if kimlik is not None and await session.get(model, kimlik) is None:
            raise NotFoundError(guards.INVOICE_PARTY_INVALID)

    for alan, (model, kapsam_kolonu) in _SOURCE_MODELS.items():
        kimlik = degerler.get(alan)
        if kimlik is None:
            continue
        kayit = await session.get(model, kimlik)
        if kayit is None:
            raise NotFoundError(guards.INVOICE_SOURCE_INVALID)
        if kapsam_kolonu is not None and getattr(kayit, kapsam_kolonu) not in gorunen:
            raise NotFoundError(guards.INVOICE_SOURCE_INVALID)


def _single_link_blockers(degerler: dict[str, uuid.UUID | None]) -> list[str]:
    """`ck_invoices_single_party` / `ck_invoices_single_source` — SERVİSTE ÖNCE.

    DB CHECK'leri (T1) SON savunmadır; ihlalleri 409 "Veri bütünlüğü hatası"
    olarak dönerdi ve kullanıcı hangi iki alanı birden doldurduğunu öğrenemezdi.
    """
    engeller: list[str] = []
    if sum(1 for alan in _PARTY_MODELS if degerler.get(alan) is not None) > 1:
        engeller.append(guards.SINGLE_PARTY_ONLY)
    if sum(1 for alan in _SOURCE_MODELS if degerler.get(alan) is not None) > 1:
        engeller.append(guards.SINGLE_SOURCE_ONLY)
    return engeller


# --- Para: TEK KAYNAK `amounts.py` ---


def _apply_amounts(invoice: Invoice, lines) -> amounts.InvoiceAmounts:
    """`amounts.compute` çıktısını başlık kolonlarına YAZAR — hesap YAPMAZ.

    Oranlar faturanın ÜZERİNDEN okunur (çağıran onları önce güncellemiş olmak
    zorundadır): iki ayrı oran kaynağı olsaydı PATCH'te gövdedeki oranla
    kayıttaki oran ayrışabilir ve tutar hangisine göre hesaplandığı belirsiz
    kalırdı.
    """
    hesap = amounts.compute(
        lines,
        advance_rate=invoice.advance_rate,
        retention_rate=invoice.retention_rate,
        withholding_rate=invoice.withholding_rate,
    )
    invoice.subtotal = hesap.subtotal
    invoice.advance_amount = hesap.advance_amount
    invoice.retention_amount = hesap.retention_amount
    invoice.tax_base = hesap.tax_base
    invoice.vat_amount = hesap.vat_amount
    invoice.withholding_amount = hesap.withholding_amount
    invoice.total = hesap.total
    return hesap


def _new_lines(
    invoice_id: uuid.UUID, lines: list[InvoiceLineCreate], line_totals: tuple[Decimal, ...]
) -> list[InvoiceLine]:
    """Kalemleri gövdedeki SIRAYLA kurar; `sort_order` DİZİNİN KENDİSİDİR.

    `line_total` de burada gövdeden DEĞİL `amounts`ın çıktısından yazılır —
    böylece satır toplamı ile başlık toplamı aynı hesaptan doğar ve kuruşu
    kuruşuna tutar. İki yazma yolu (create · `PUT lines`) bu tek fonksiyondan
    geçtiği için sıralama ve yuvarlama İKİYE BÖLÜNMEZ.
    """
    return [
        InvoiceLine(
            invoice_id=invoice_id,
            sort_order=sira,
            description=data.description.strip(),
            unit=data.unit,
            quantity=data.quantity,
            unit_price=data.unit_price,
            vat_rate=data.vat_rate,
            line_total=line_totals[sira],
            detail_note=data.detail_note,
        )
        for sira, data in enumerate(lines)
    ]


async def _refresh_stamps(session: AsyncSession, invoice: Invoice) -> None:
    """`updated_at` SUNUCU tarafından üretilir (`onupdate=func.now()`), yani
    UPDATE'ten sonra ORM'deki değer BAYATTIR ve SQLAlchemy onu "expired"
    işaretler.

    Yanıt şeması onu okuduğunda tembel yükleme tetiklenir ve async bağlamda bu
    `MissingGreenlet` = **500** demektir (P11'de bire bir yaşandı). Açık
    `refresh` bu pencereyi kapatır: yenilenen değer aynı zamanda istemciye
    DOĞRU damgayı verir — bayat bir `updated_at` iyimser kilit kuran bir
    istemciyi yanıltırdı.
    """
    await session.refresh(invoice)


# --- Uç 1: liste ---


async def list_invoices(
    session: AsyncSession,
    actor: User,
    *,
    direction: InvoiceDirection | None,
    status: InvoiceStatus | None,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    q: str | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> InvoiceListResponse:
    """FY tablosunun veri kaynağı. Kapsam süzgeci `total`a da uygulanır:
    görünmeyen projenin faturası "sayfa dışında kalmış" gibi bile görünmez."""
    suzgecler = {
        "direction": direction,
        "status": status,
        "project_id": project_id,
        "site_id": site_id,
        "q": q,
        "date_from": date_from,
        "date_to": date_to,
    }
    project_ids = await _visible_project_ids(session, actor, liste=True)
    kayitlar = await repository.list_invoices(
        session, project_ids, limit=limit, offset=offset, **suzgecler
    )
    total = await repository.count_invoices(session, project_ids, **suzgecler)
    return InvoiceListResponse(
        items=[InvoiceResponse.model_validate(k) for k in kayitlar],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Uç 4: detay ---


async def build_detail(session: AsyncSession, invoice: Invoice) -> InvoiceDetailResponse:
    """Künye + kalemler. Toplamlar SAKLANAN kolonlardan gelir (K7): okuma
    anında yeniden hesaplansalardı donmuş bir fatura canlı sayı gösterirdi."""
    lines = await repository.load_lines(session, invoice.id)
    return InvoiceDetailResponse(
        **InvoiceResponse.model_validate(invoice).model_dump(),
        lines=[InvoiceLineResponse.model_validate(satir) for satir in lines],
    )


# --- Uç 3: oluştur ---


async def create_invoice(
    session: AsyncSession, actor: User, data: InvoiceCreate
) -> tuple[Invoice, str]:
    """Başlık + kalemler ATOMİK yazılır: doğrulamaların HEPSİ yazımdan ÖNCEDİR.

    Sıra bilinçlidir:
      1. gövde kuralları — numaranın sahibi (§4/S5), oran toplamı, tek taraf /
         tek kaynak: DB'ye hiç dokunulmadan **422**;
      2. varlık referansları: proje · şantiye · taraf · kaynak (**404**);
      3. numara üretimi (danışma kilidi) ve `session.add`.

    Numara EN SONDA üretilir: `pg_advisory_xact_lock` işlem boyu tutulur ve
    doğrulama başarısız olacaksa kilidi boşuna almamak gerekir.

    **DURUM YÖNDEN gelir** (`transitions.INITIAL_STATUS`): giden → `draft`,
    gelen → `pending` (K2). Gövde durum GÖNDEREMEZ.
    """
    degerler = data.model_dump()
    _raise_blockers(
        validation.invoice_no_blockers(data.direction, data.invoice_no),
        validation.body_blockers(
            advance_rate=data.advance_rate, retention_rate=data.retention_rate
        ),
        _single_link_blockers(degerler),
    )
    await _assert_references(session, actor, degerler)

    if data.direction is InvoiceDirection.outgoing:
        invoice_no = await numbering.generate_invoice_number(session, data.direction)
    else:
        invoice_no = data.invoice_no.strip() if data.invoice_no else ""
        if await repository.invoice_no_exists(session, data.direction, invoice_no):
            raise DuplicateError(guards.INVOICE_DUPLICATE_NO)

    invoice = Invoice(
        direction=data.direction,
        invoice_no=invoice_no,
        document_type=data.document_type,
        status=transitions.INITIAL_STATUS[data.direction],
        issue_date=data.issue_date,
        due_date=data.due_date,
        payment_method=data.payment_method,
        note=data.note,
        party_name=data.party_name.strip(),
        party_tax_number=data.party_tax_number,
        party_tax_office=data.party_tax_office,
        party_address=data.party_address,
        employer_id=data.employer_id,
        customer_id=data.customer_id,
        supplier_id=data.supplier_id,
        subcontractor_id=data.subcontractor_id,
        progress_payment_id=data.progress_payment_id,
        subcontractor_progress_payment_id=data.subcontractor_progress_payment_id,
        equipment_rental_invoice_id=data.equipment_rental_invoice_id,
        purchase_order_id=data.purchase_order_id,
        project_id=data.project_id,
        site_id=data.site_id,
        advance_rate=data.advance_rate,
        retention_rate=data.retention_rate,
        withholding_rate=data.withholding_rate,
        created_by_id=actor.id,
    )
    hesap = _apply_amounts(invoice, data.lines)
    # 🔴 FAT-HAK — GELEN faturada tutar kapısı OLUŞTURMA ANINDADIR. Gerekçe
    #    ÖLÇÜLDÜ ve `send`/`approve` kapısıyla çelişmez, onu TAMAMLAR:
    #
    #    Gelen fatura `pending` DOĞAR (`INITIAL_STATUS`) ve o andan sonra
    #    tutarı DÜZELTİLEMEZ:
    #      · `DELETABLE_STATUS = {draft}`            → silinemez,
    #      · `LINES_EDITABLE_STATUS = {draft}`       → kalemleri değiştirilemez
    #        (ve `draft` YALNIZ giden taraftadır, K2),
    #      · `INCOMING_PATCHABLE_FIELDS` yalnız not/vade/ödeme şeklini taşır
    #        → oranları ve dolayısıyla tutarı değiştirilemez.
    #
    #    Yani yanlış tutarlı bir gelen fatura KALICIDIR ve kısmi UNIQUE indeks
    #    (`SOURCE_UNIQUE_INDEXES`) yüzünden hakedişin fatura slotunu SONSUZA DEK
    #    işgal eder: doğrusu bir daha hiç bağlanamaz, hakediş bir daha hiç
    #    ödenemez. Kapı `approve`a bırakılsaydı kayıt çoktan doğmuş olurdu.
    #
    #    GİDEN faturada kapı BURADA YOKTUR ve olmamalıdır: `draft` yarım formu
    #    saklayabilmelidir (K6'nın taslak-farkındalığı, FK:24 "Taslak Kaydet"),
    #    kalemler `PUT lines` ile sonradan gelir ve taslak SİLİNEBİLİR — yani
    #    kalıcı bir kilit doğmaz. Orada kapı `send` geçişindedir.
    if data.direction is InvoiceDirection.incoming:
        _raise_blockers(
            validation.source_amount_blockers(
                hesap.subtotal, await source_amounts.source_gross_for_invoice(session, invoice)
            ),
            # 🔴 MU-3D TAKAS-TABAN — FAT-HAK'ın KARDEŞİ, gerekçesi
            #    `validation.source_posting_base_blockers`ta TEK KOPYA.
            #    Kapı BURADADIR (ve `state_service`te) çünkü gelen fatura
            #    `pending` DOĞAR ve o andan sonra oranları DÜZELTİLEMEZ:
            #    yalnız geçişte yakalansaydı kayıt çoktan KALICILAŞMIŞ olurdu.
            validation.source_posting_base_blockers(
                hesap.tax_base,
                await source_amounts.source_posting_base_for_invoice(session, invoice),
            ),
        )
    session.add(invoice)
    await session.flush()

    if data.lines:
        session.add_all(_new_lines(invoice.id, data.lines, hesap.line_totals))
        await session.flush()

    return invoice, messages.invoice_created(invoice.invoice_no)


# --- Uç 5: PATCH ---


async def update_invoice(
    session: AsyncSession, actor: User, invoice: Invoice, data: InvoiceUpdate
) -> tuple[Invoice, str]:
    """Kısmi güncelleme — kapılar `transitions`tan, para `amounts`tan.

    Kayıt ÇAĞIRAN tarafından KİLİTLENMİŞ olarak gelir (spec §8) ve durum kapısı
    bu kilitli satır üzerinde koşar.

    Doğrulamalar BİRLEŞİK değerler üzerindedir: kullanıcı yalnız projeyi
    değiştirse bile eski `site_id` yeni projeye ait olmayabilir ve fatura
    sessizce tutarsız kalırdı.

    Kalemler DEĞİŞMEZ (onların tek yolu `PUT lines`) ama başlık toplamları
    YENİDEN hesaplanır: oran değiştiğinde tutarın eski değerinde kalması,
    ekranda tutmayan bir fatura demek olurdu.
    """
    transitions.assert_editable(invoice.direction, invoice.status)
    verilen = data.model_dump(exclude_unset=True)

    if invoice.direction is InvoiceDirection.incoming:
        if set(verilen) - guards.INCOMING_PATCHABLE_FIELDS:
            raise InvoicingValidationError(guards.INCOMING_PATCH_FIELDS_LIMITED)

    birlesik = {
        alan: verilen.get(alan, getattr(invoice, alan))
        for alan in (*_PARTY_MODELS, *_SOURCE_MODELS, "project_id", "site_id")
    }
    _raise_blockers(
        validation.body_blockers(
            advance_rate=verilen.get("advance_rate", invoice.advance_rate),
            retention_rate=verilen.get("retention_rate", invoice.retention_rate),
        ),
        _single_link_blockers(birlesik),
    )
    await _assert_references(session, actor, birlesik)

    for alan, deger in verilen.items():
        if alan in _NOT_NULL_FIELDS and deger is None:
            continue
        setattr(invoice, alan, deger.strip() if alan == "party_name" else deger)

    _apply_amounts(invoice, await repository.load_lines(session, invoice.id))
    await session.flush()
    await _refresh_stamps(session, invoice)
    return invoice, messages.invoice_updated(invoice.invoice_no)


# --- Uç 6: DELETE ---


# --- Uç 7: PUT lines ---


async def replace_lines(
    session: AsyncSession, invoice: Invoice, data: InvoiceLinesReplace
) -> tuple[Invoice, str]:
    """Kalem kümesini TOPTAN yazar (hakediş/puantaj emsali) — yalnız `draft`.

    `sort_order` dizinin indeksinden, `line_total` `amounts`tan gelir; ikisi de
    gövdeden GELEMEZ (şema 422). Başlık toplamları aynı hesabın çıktısıyla
    güncellenir, yani satırlar ile başlık ASLA ayrışmaz.

    Boş liste hepsini SİLER ve tutarı sıfırlar; K6 kapısı (kalemsiz fatura
    gönderilemez) `send`/`approve` anındadır (T4) — taslak yarım kalabilir.
    """
    transitions.assert_lines_editable(invoice.status)
    await repository.delete_lines(session, invoice.id)
    await session.flush()

    hesap = _apply_amounts(invoice, data.lines)
    if data.lines:
        session.add_all(_new_lines(invoice.id, data.lines, hesap.line_totals))
    await session.flush()
    await _refresh_stamps(session, invoice)
    return invoice, messages.invoice_lines_replaced(invoice.invoice_no)
