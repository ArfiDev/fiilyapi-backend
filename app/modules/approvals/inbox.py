"""Onay kutusu SATIRININ evraktan okunan yarısı (T4, sözleşme Y7).

Motor bir zincirin `document_type`ını, `document_id`sini ve adımlarını bilir;
mockup kartı ise BEŞ şey basar (`projedesign/Onay Kutusu.dc.html`):

| Kart parçası | Mockup | Kaynak |
|---|---|---|
| tip rozeti | `:123` `:157` `:216` | zincir (`document_type`) |
| oluşturan + zaman | `:124` `:159` `:217` | zincir (`created_by` · `created_at`) |
| **başlık** | `:126` `:161` `:219` | **evrak ailesi** |
| **alt başlık** | `:127` `:162` `:220` | **evrak ailesi** |
| adım şeridi | `:129-135` `:164-170` `:222-224` | zincir (`approval_steps`) |
| **tutar(lar)** | `:138-139` `:173` `:227-228` | **evrak ailesi** |

Kalın olan üçü BURADA okunur. Mockup'taki beş karttan **bordro (`:91`)** ve
**günlük kayıt (`:194`)** OK-1B'nindir (K4) ve bu dosyada karşılıkları YOKTUR.

## 🔴 N+1 YOK — aile başına SABİT, satır başına DEĞİL

Her aile için sorgu sayısı SABİTTİR ve sayfadaki satır sayısından bağımsızdır:
kimlikler toplanır, aile başına toplu okuma yapılır. Hesap zincirleri de repo
kanonuna uyar — taşeron `amounts.bulk_calculations`, işveren
`repository.list_completed_payments_by_projects` üzerinden gider; ikisi de
"liste ucunun N+1 çözümü" olarak zaten vardı ve KOPYALANMAZ.

## 🔴 SUNUM DEĞİL, OLGU

`title`/`subtitle` evrağın KİMLİĞİDİR (kim · ne · nerede · hangi dönem).
Aciliyet, renk ve "sizden onay bekleniyor" gibi KARAR metinleri (`:106` `:140`
`:202`) burada ÜRETİLMEZ — K10 kanonu, `treasury/upcoming.py` emsali.

⚠️ ÖLÇÜLMÜŞ SAPMA (mockup `:127` `:220` "Temmuz 2026"): backend'de Türkçe AY ADI
sözlüğü YOKTUR ve bu dilimde AÇILMADI. Ay adı üretmek, ekranın zaten sahip
olduğu yerelleştirme katmanının ikinci bir kopyasını sunucuda açardı (K10).
Dönem `MM/YYYY` basılır.
"""

import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals.models import ApprovalDocumentType
from app.modules.contracts.models import SubcontractorContract
from app.modules.inventory.models import StockItem
from app.modules.procurement import repository as procurement_repository
from app.modules.procurement.models import PurchaseRequest, PurchaseRequestLine
from app.modules.progress_payments import calculations
from app.modules.progress_payments import repository as employer_repository
from app.modules.progress_payments.models import ProgressPayment
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments import amounts as subcontractor_amounts
from app.modules.subcontractor_progress_payments.models import SubcontractorProgressPayment

__all__ = ["DocumentFacts", "EMPTY_FACTS", "load_facts"]

#: Alt başlık parçalarının ayracı — mockup `:127` `:162` `:220` üçünde de aynı.
_AYRAC = " · "
#: Şantiye kırılımının ayracı — mockup `:220` "A-Blok + B-Blok".
_SANTIYE_AYRACI = " + "


@dataclass(frozen=True)
class DocumentFacts:
    """Satırın evraktan gelen dört alanı.

    🔴 `net_amount` OPSİYONELDİR ve satın alma talebinde HER ZAMAN `None`dur:
    talebin brüt/net ayrımı YOKTUR (mockup `:173` TEK kutu basar, hakediş
    kartlarındaki ikinci kutu orada YOKTUR). Uydurulmuş bir net, denetim
    yüzeyinde ikinci bir "gerçek" olarak yaşamaya başlardı.
    """

    title: str | None = None
    subtitle: str | None = None
    gross_amount: Decimal | None = None
    net_amount: Decimal | None = None
    #: Evrağın PROJESİ (IZN-B4b onarımı): satır kendi projesindeki rolün gizli kategorileriyle
    #: maskelenir (`field_mask.PROJE_ALANI`). Çözülemezse `None` → birleşim (fail-closed).
    project_id: uuid.UUID | None = None


#: Evrağı okunamayan satırın zarif düşüşü. Pratikte ULAŞILMAZ: görünürlük
#: süzgeci (`documents.visible_document_clause`) evrağı çözülemeyen zinciri
#: zaten eler. Yine de `None` yerine bu nesne döner ki çağıran `if` yığmasın.
EMPTY_FACTS = DocumentFacts()

DocumentKey = tuple[ApprovalDocumentType, uuid.UUID]


# --------------------------------------------------------------------------- #
# Ortak metin yardımcıları
# --------------------------------------------------------------------------- #


def _birlestir(parcalar: Iterable[str | None]) -> str | None:
    """Boş/None parçaları ELEYEREK birleştirir.

    Eleme şart: `None` bir parçayı "· ·" gibi boş bir aralığa çevirmek, kartta
    veri varmış gibi görünen bir boşluk basardı.
    """
    dolu = [parca for parca in parcalar if parca]
    return _AYRAC.join(dolu) if dolu else None


def _donem(year: int | None, month: int | None) -> str | None:
    """Dönem etiketi — `MM/YYYY` (modül docstring'indeki ölçülmüş sapma)."""
    if year is None:
        return None
    return f"{month:02d}/{year}" if month is not None else str(year)


def _miktar(value: Decimal) -> str:
    """`Numeric(14,3)`ün gösterimi: anlamsız sıfırlar ATILIR (`320.000` -> `320`).

    Mockup `:161` "320 m³" yazar; ölçek artığı bir `320.000` aynı sayıyı üç kat
    gürültüyle basardı.
    """
    normalize = value.normalize()
    # `normalize()` tam sayılarda üstel biçime kayar (`3.2E+2`); `:f` onu geri açar.
    return f"{normalize:f}"


# --------------------------------------------------------------------------- #
# Taşeron hakedişi (mockup `:118-149`)
# --------------------------------------------------------------------------- #


def _taseron_stmt(document_ids: list[uuid.UUID]) -> Select:
    return (
        select(
            SubcontractorProgressPayment,
            SubcontractorContract.subcontractor_name,
            SubcontractorContract.work_category,
            Project.name,
        )
        .join(
            SubcontractorContract,
            SubcontractorContract.id == SubcontractorProgressPayment.contract_id,
        )
        .join(Project, Project.id == SubcontractorProgressPayment.project_id)
        .where(SubcontractorProgressPayment.id.in_(document_ids))
    )


def _taseron_basligi(
    sequence_no: int, subcontractor_name: str | None, work_category: str | None
) -> str:
    """Mockup `:126` "Akın İnşaat — Hakediş #47 (Betonarme)".

    Taşeron adı taslak sözleşmede NULL olabilir; o hâlde başlık numarayla
    başlar. Uydurma bir "Bilinmeyen taşeron" YAZILMAZ.
    """
    govde = f"Hakediş #{sequence_no}"
    baslik = f"{subcontractor_name} — {govde}" if subcontractor_name else govde
    return f"{baslik} ({work_category})" if work_category else baslik


async def _taseron_facts(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[DocumentKey, DocumentFacts]:
    rows = (await session.execute(_taseron_stmt(document_ids))).all()
    if not rows:
        return {}
    payments = [row[0] for row in rows]
    # Brüt/net TEK kopyadan gelir (`progress_payments/calculations.py`); bu çağrı
    # sözleşme bedellerini ve tamamlanmış hakedişleri TOPLU okur (N+1 çözümü).
    hesaplar = await subcontractor_amounts.bulk_calculations(session, payments)
    sonuc: dict[DocumentKey, DocumentFacts] = {}
    for payment, subcontractor_name, work_category, project_name in rows:
        hesap = hesaplar[payment.id]
        sonuc[(ApprovalDocumentType.subcontractor_progress_payment, payment.id)] = DocumentFacts(
            title=_taseron_basligi(payment.sequence_no, subcontractor_name, work_category),
            subtitle=_birlestir(
                (
                    project_name,
                    payment.description,
                    _donem(payment.period_year, payment.period_month),
                )
            ),
            gross_amount=hesap.gross,
            net_amount=hesap.net,
            project_id=payment.project_id,
        )
    return sonuc


# --------------------------------------------------------------------------- #
# İşveren hakedişi (mockup `:211-238`)
# --------------------------------------------------------------------------- #


async def _isveren_facts(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[DocumentKey, DocumentFacts]:
    rows = (
        await session.execute(
            select(ProgressPayment, Project.name)
            .join(Project, Project.id == ProgressPayment.project_id)
            .where(ProgressPayment.id.in_(document_ids))
        )
    ).all()
    if not rows:
        return {}
    payments = [row[0] for row in rows]
    project_ids = sorted({payment.project_id for payment in payments})
    # Sözleşme bedeli avans TAVANININ paydasıdır; `Project` VARLIĞI çekilseydi
    # beş `selectin` bağıntısı (employer/contract/investment/…) da gelirdi.
    contract_amounts = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(ProjectContract.project_id, ProjectContract.amount).where(
                    ProjectContract.project_id.in_(project_ids)
                )
            )
        ).all()
    }
    completed = await employer_repository.list_completed_payments_by_projects(session, project_ids)
    site_names = await _site_adlari(
        session, {line.site_id for payment in payments for line in payment.lines}
    )

    sonuc: dict[DocumentKey, DocumentFacts] = {}
    for payment, project_name in rows:
        contract_amount = contract_amounts.get(payment.project_id)
        prior = [
            other
            for other in completed.get(payment.project_id, [])
            if other.sequence_no < payment.sequence_no
        ]
        recovered = calculations.cumulative_state(prior, contract_amount).advance_recovered
        gross = calculations.gross_total(payment.lines)
        vat = calculations.vat_amount(gross, payment.vat_pct)
        advance = calculations.advance_or_uncapped(
            gross, payment.advance_pct, contract_amount, recovered
        )
        retention = calculations.retention_amount(gross, payment.retainage_pct)
        sonuc[(ApprovalDocumentType.progress_payment, payment.id)] = DocumentFacts(
            title=f"{project_name} — İşveren Hakediş #{payment.sequence_no}",
            subtitle=_birlestir(
                (
                    _santiye_kirilimi(payment, site_names),
                    _donem(payment.period_year, payment.period_month),
                    payment.description,
                )
            ),
            gross_amount=gross,
            net_amount=calculations.net_amount(gross, vat, advance, retention),
            project_id=payment.project_id,
        )
    return sonuc


async def _site_adlari(session: AsyncSession, site_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not site_ids:
        return {}
    rows = await session.execute(select(Site.id, Site.name).where(Site.id.in_(site_ids)))
    return {row[0]: row[1] for row in rows}


def _santiye_kirilimi(payment: ProgressPayment, site_names: dict[uuid.UUID, str]) -> str | None:
    """Mockup `:220` "A-Blok + B-Blok".

    İşveren hakedişi PROJEYE bağlıdır, şantiye kırılımı SATIR düzeyindedir
    (`ProgressPaymentLine.site_id`) — bu yüzden kırılım satırlardan türer.
    Satır sırası KORUNUR (`lines` `sort_order`a göre yüklenir); alfabetik
    sıralamak ekranın gördüğü sırayla çelişirdi.
    """
    adlar: list[str] = []
    for line in payment.lines:
        ad = site_names.get(line.site_id)
        if ad and ad not in adlar:
            adlar.append(ad)
    return _SANTIYE_AYRACI.join(adlar) if adlar else None


# --------------------------------------------------------------------------- #
# Satın alma talebi (mockup `:152-183`)
# --------------------------------------------------------------------------- #


async def _satinalma_facts(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[DocumentKey, DocumentFacts]:
    rows = (
        await session.execute(
            select(PurchaseRequest, Project.name, Site.name, Section.name)
            .join(Project, Project.id == PurchaseRequest.project_id)
            .outerjoin(Site, Site.id == PurchaseRequest.site_id)
            .outerjoin(Section, Section.id == PurchaseRequest.section_id)
            .where(PurchaseRequest.id.in_(document_ids))
        )
    ).all()
    if not rows:
        return {}
    # 🔴 Tahmini toplamın TEK formül kopyası `procurement.repository.request_totals`
    # içindedir ve eşiğin baktığı sayı da odur; ikinci bir toplam yazılsaydı
    # ekrandaki tutar ile eşiğin gördüğü tutar aynı talep için ayrışabilirdi.
    totals = procurement_repository.request_totals()
    toplamlar = {
        row[0]: row[1]
        for row in (
            await session.execute(
                select(totals.c.request_id, totals.c.estimated_total).where(
                    totals.c.request_id.in_(document_ids)
                )
            )
        ).all()
    }
    ilk_kalemler = await _ilk_kalem_etiketleri(session, document_ids)

    sonuc: dict[DocumentKey, DocumentFacts] = {}
    for request, project_name, site_name, section_name in rows:
        toplam = toplamlar.get(request.id)
        sonuc[(ApprovalDocumentType.purchase_request, request.id)] = DocumentFacts(
            title=ilk_kalemler.get(request.id) or request.request_no,
            subtitle=_birlestir((project_name, site_name, section_name, request.justification)),
            # 🔴 OLCEK BIRLESTIRME: `quantity Numeric(14,3) × price Numeric(18,2)`
            # bes hanelik bir carpim uretir. Ayni yanitin diger iki ailesi
            # `Numeric(18,2)` olceginde gelir; kirpilmadan birakilsaydi TEK
            # alanin (`gross_amount`) ailesine gore iki farkli olcegi olurdu.
            # Kirpma `calculations.quantize2` — para olceginin TEK kopyasi.
            gross_amount=calculations.quantize2(toplam) if toplam is not None else None,
            # 🔴 Mockup `:173`te İKİNCİ kutu YOKTUR (bkz. `DocumentFacts`).
            net_amount=None,
            project_id=request.project_id,
        )
    return sonuc


async def _ilk_kalem_etiketleri(
    session: AsyncSession, document_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Mockup `:161` "C25/30 Hazır Beton — 320 m³".

    Kalem İKİ KAPILIDIR (`procurement/models.py`): ya stok kartına bağlıdır ya
    da serbest metindir; ad ve birim hangisi doluysa ORADAN okunur.

    ⚠️ TEDARİKÇİ ADI (`:161` "· KarTaş Yapı") BU BAŞLIKTA YOKTUR ve olamaz:
    talep zinciri `pending_approval`da koşar, teklifler ise ondan SONRAKİ
    (`quote_wait`) adımda toplanır. Onay anında kazanan tedarikçi YAPISAL
    OLARAK bilinmez; mockup kartı akışın ilerisindeki bir anı çiziyor.
    """
    rows = (
        await session.execute(
            select(PurchaseRequestLine, StockItem.name, StockItem.unit)
            .outerjoin(StockItem, StockItem.id == PurchaseRequestLine.stock_item_id)
            .where(PurchaseRequestLine.request_id.in_(document_ids))
            .order_by(PurchaseRequestLine.request_id, PurchaseRequestLine.sort_order)
        )
    ).all()
    etiketler: dict[uuid.UUID, str] = {}
    for line, stock_name, stock_unit in rows:
        if line.request_id in etiketler:
            continue  # Sıralı okuma: ilk gelen İLK kalemdir.
        ad = stock_name or line.free_text_name
        birim = stock_unit or line.free_text_unit
        miktar = _miktar(line.quantity)
        olcu = f"{miktar} {birim}" if birim else miktar
        etiketler[line.request_id] = f"{ad} — {olcu}" if ad else olcu
    return etiketler


# --------------------------------------------------------------------------- #
# Toplu okuma
# --------------------------------------------------------------------------- #

_LOADERS = {
    ApprovalDocumentType.subcontractor_progress_payment: _taseron_facts,
    ApprovalDocumentType.progress_payment: _isveren_facts,
    ApprovalDocumentType.purchase_request: _satinalma_facts,
}


async def load_facts(
    session: AsyncSession, references: Sequence[DocumentKey]
) -> dict[DocumentKey, DocumentFacts]:
    """Sayfadaki evrakları AİLE AİLE toplu okur.

    🔴 Sorgu sayısı SATIR SAYISINDAN bağımsızdır: her aile için sabit sayıda
    sorgu koşar ve sayfada BULUNMAYAN aile için HİÇ sorgu koşmaz.
    """
    kimlikler: dict[ApprovalDocumentType, list[uuid.UUID]] = {}
    for document_type, document_id in references:
        kimlikler.setdefault(document_type, []).append(document_id)

    sonuc: dict[DocumentKey, DocumentFacts] = {}
    for document_type, document_ids in kimlikler.items():
        sonuc.update(await _LOADERS[document_type](session, document_ids))
    return sonuc
