"""İstekten PROJE BAĞLAMI çıkarma (IZN-B3, IZN-PLAN §2.2): yol parametresi / gövde → proje kimliği.

İKİ tüketicisi vardır ve ikisi AYNI çözücüyü kullanır (`request_project`):

* Rota kapısı (`core/permissions`): bağlam çözülürse O projedeki rolle, çözülmezse yalnız ANA rolle
  karar verir (ekip rolü şirket geneli uçları açamaz).
* Disiplin kapsamı (`core/discipline_deps`): bağlam çözülürse o projedeki atama, çözülmezse ÇOK
  PROJE kapsamı (kısıtlı olduğu projeler haritası).

Kaynaklar: (1) yol parametresi (`RESOLVERS`: öneke + parametre adına göre; UUID ya da SLUG),
(2) JSON gövdesinde `project_id` / `site_id` (oluşturma uçları) — YALNIZ eşleşen rotanın gövde
modeli o anahtarı ÜST DÜZEYDE bildiriyorsa (`_body_declared_keys`). Modelin bildirmediği bir
anahtar (ör. `PATCH /personnel/{id}` gövdesine eklenen `project_id`) doğrulamada yok sayılır ama
bağlamı DEĞİŞTİRMEMELİDİR: aksi hâlde istemci ekip rolünü şirket geneli uca taşır (maske + kapı
atlatma, IZN-B4c çürütmesi). Rota/gövde modeli çözülemezse bağlam çözülmez (fail-closed).
TAŞIMA kuralı (IZN-HF1): yol çözücüsü eşleştiyse gövde bağlamı DEĞİŞTİREMEZ — gövde yol projesinden
(P) farklı Q gösterirse ya da kayıt projesizken (P yok) Q gösterirse bağlam `None` olur (kayıt
başka projeye "taşınıyormuş" gibi o projenin rolüyle okunup yazılamaz); P == Q → P; açık `null`
(hedef = projesiz) P doluyken de taşımadır → `None`. Rota bağlam anahtarı bildirip gövde JSON olarak
okunamıyorsa (tür/boş/bozuk/dict değil/değer dize-null değil) bağlam `None`. İçerik türü FastAPI'nin
kuralıyla çözülür (büyük harf, `+json`). `RESOLVERS` dışı PATCH/PUT ucu kayıt çözücüsü almadıkça
gövdeden bağlam alır; böyle uç KALMADI (`/financial-instruments` HF1'de, saha ve satınalma
IZN-B4d'de çözücü aldı). Bekçi: `BILINEN_COZUCUSUZ_PATCH` boş küme testi.
Slug (`/projects/{slug}`, `/sites/{slug}` …) ÇÖZÜLÜR: UUID ile slug aynı projeyi verir.
Slug kapsamı tekil değilse (şantiye / bölüm slug'ı proje / şantiye içinde tekildir)
ve birden çok projede eşleşirse bağlam ÇÖZÜLMEZ
(`None`): ekip rolü sayılmaz, kesin karar `visible_projects` süzgecine kalır (fail-closed).

IZN-B4d onarımı: saha kayıt yolları (`/equipment/...`, `/purchase-requests/...`,
`/purchase-orders/...`) da `RESOLVERS`ta: kaydın kendi projesi (makine/yakıt/kira → `site_id` →
şantiyenin projesi; talep/sipariş → `project_id`) yol parametresinden çözülür;
projesiz kayıt `None`.

`None` = proje bağlamı YOK ya da çözülemedi (liste ucu, bilinmeyen/belirsiz kayıt). Yeni bir
proje bağlamlı uç eklenip çözücü yazılmazsa `tests/core/test_proje_baglami_cozucu_bekcisi.py`
kırmızı verir.
"""

from __future__ import annotations

import email.message
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import UnionType
from typing import NamedTuple, Union, get_args, get_origin

from fastapi import Request, params
from fastapi.routing import APIRoute
from pydantic import BaseModel
from sqlalchemy import Select, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.slug import parse_ref, ref_filter
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.progress_payments.models import ProgressPayment
from app.modules.projects.models import Project
from app.modules.site_diary.models import SiteDiaryEntry
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import SubcontractorProgressPayment

Resolver = Callable[[AsyncSession, uuid.UUID | str], Awaitable[uuid.UUID | None]]

SCOPE_KEY = "izn_b3.project_id"
BODY_KEYS = ("project_id", "site_id")


async def _unique(session: AsyncSession, stmt: Select) -> uuid.UUID | None:
    """İfadenin TEK ve benzersiz proje kimliği (birden çok proje / hiç → `None`)."""
    rows = {row[0] for row in (await session.execute(stmt.limit(2))).all()}
    return next(iter(rows)) if len(rows) == 1 else None


async def _project_itself(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    if isinstance(ref, uuid.UUID):
        return ref  # kimlik zaten projedir (sorgu yok); var olmayan kimlik üye değildir → ana rol
    return await _unique(
        session, select(Project.id).where(ref_filter(Project.id, Project.slug, ref))
    )


async def _site_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    return await _unique(
        session, select(Site.project_id).where(ref_filter(Site.id, Site.slug, ref))
    )


async def _section_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    return await _unique(
        session,
        select(Site.project_id)
        .join(Section, Section.site_id == Site.id)
        .where(ref_filter(Section.id, Section.slug, ref)),
    )


async def _boq_item_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(Site.project_id).join(BoqItem, BoqItem.site_id == Site.id).where(BoqItem.id == ref),
    )


async def _boq_group_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(Site.project_id)
        .join(BoqGroup, BoqGroup.site_id == Site.id)
        .where(BoqGroup.id == ref),
    )


async def _diary_entry_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(SiteDiaryEntry.project_id).where(SiteDiaryEntry.id == ref))


async def _payment_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    return await _unique(
        session,
        select(ProgressPayment.project_id).where(
            ref_filter(ProgressPayment.id, ProgressPayment.slug, ref)
        ),
    )


async def _sub_payment_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    model = SubcontractorProgressPayment
    return await _unique(
        session, select(model.project_id).where(ref_filter(model.id, model.slug, ref))
    )


async def _sub_contract_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.contracts.models import SubcontractorContract  # döngüyü önler

    model = SubcontractorContract
    return await _unique(
        session, select(model.project_id).where(ref_filter(model.id, model.slug, ref))
    )


async def _document_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.documents.models.core import Document  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(Document.project_id).where(Document.id == ref))


async def _folder_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.documents.models.core import DocumentFolder  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(DocumentFolder.project_id).where(DocumentFolder.id == ref))


async def _unit_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.units.models import Unit  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(Unit.project_id).where(Unit.id == ref))


async def _block_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.units.models import Block  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(Block.project_id).where(Block.id == ref))


async def _sale_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.sales.models import UnitSale  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(UnitSale.project_id).where(UnitSale.id == ref))


async def _installment_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.sales.models import SaleInstallment, UnitSale  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(UnitSale.project_id)
        .join(SaleInstallment, SaleInstallment.sale_id == UnitSale.id)
        .where(SaleInstallment.id == ref),
    )


async def _invoice_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.invoicing.models import Invoice  # döngüyü önler

    # Detay ucu fatura NUMARASINI da kabul eder (`visible_invoice` str dalı); numara belirsizse
    # (birden çok proje) `_unique` → `None`. IZN-B5f: numarayla detay UUID ile AYNI kapıdan geçer.
    cond = Invoice.id == ref if isinstance(ref, uuid.UUID) else Invoice.invoice_no == ref
    # Şirket geneli fatura `project_id IS NULL` → `None` (birleşim, fail-closed).
    return await _unique(session, select(Invoice.project_id).where(cond))


async def _instrument_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.treasury.models import FinancialInstrument  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    # Şirket geneli çek/senet `project_id IS NULL` → `None` (birleşim, fail-closed).
    return await _unique(
        session, select(FinancialInstrument.project_id).where(FinancialInstrument.id == ref)
    )


async def _employer_item_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.contracts.models import EmployerContractItem  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    model = EmployerContractItem
    return await _unique(session, select(model.project_id).where(model.id == ref))


async def _employer_group_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.contracts.models import EmployerContractGroup  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    model = EmployerContractGroup
    return await _unique(session, select(model.project_id).where(model.id == ref))


async def _sub_item_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.contracts.models import (  # döngüyü önler
        SubcontractorContract,
        SubcontractorContractItem,
    )

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(SubcontractorContract.project_id)
        .join(
            SubcontractorContractItem,
            SubcontractorContractItem.contract_id == SubcontractorContract.id,
        )
        .where(SubcontractorContractItem.id == ref),
    )


async def _equipment_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.equipment.models import Equipment  # döngüyü önler

    # Makinenin projesi bağlı olduğu ŞANTİYEDEN gelir; şirket havuzundaki makine (`site_id IS
    # NULL`) projesizdir → `None` (birleşim, fail-closed).
    return await _unique(
        session,
        select(Site.project_id)
        .join(Equipment, Equipment.site_id == Site.id)
        .where(ref_filter(Equipment.id, Equipment.slug, ref)),
    )


async def _equipment_log_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.equipment.models import EquipmentFuelLog, EquipmentWorkLog  # döngüyü önler

    # `/equipment/fuel-logs/{log_id}` ile `/equipment/work-logs/{log_id}` AYNI (önek, parametre)
    # anahtarını paylaşır; kimlikler tekil olduğundan iki tabloya da bakılır.
    if not isinstance(ref, uuid.UUID):
        return None
    for model in (EquipmentFuelLog, EquipmentWorkLog):
        found = await _unique(
            session,
            select(Site.project_id).join(model, model.site_id == Site.id).where(model.id == ref),
        )
        if found is not None:
            return found
    return None


async def _equipment_document_project(
    session: AsyncSession, ref: uuid.UUID | str
) -> uuid.UUID | None:
    from app.modules.equipment.models import Equipment, EquipmentDocument  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(Site.project_id)
        .join(Equipment, Equipment.site_id == Site.id)
        .join(EquipmentDocument, EquipmentDocument.equipment_id == Equipment.id)
        .where(EquipmentDocument.id == ref),
    )


async def _rental_invoice_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.equipment.models import EquipmentRentalInvoice  # döngüyü önler

    model = EquipmentRentalInvoice
    # IZN-B5f: detay ucu kira faturasını SLUG ile de açar → kimlik ya da slug aynı çözücüden.
    return await _unique(
        session,
        select(Site.project_id)
        .join(model, model.site_id == Site.id)
        .where(ref_filter(model.id, model.slug, ref)),
    )


async def _rental_line_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.equipment.models import EquipmentRentalInvoiceLine  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    model = EquipmentRentalInvoiceLine
    return await _unique(
        session,
        select(Site.project_id).join(model, model.site_id == Site.id).where(model.id == ref),
    )


async def _purchase_request_project(
    session: AsyncSession, ref: uuid.UUID | str
) -> uuid.UUID | None:
    from app.modules.procurement.models import PurchaseRequest  # döngüyü önler

    model = PurchaseRequest
    # Detay ucu `request_no` (slug) de kabul eder; kimlik ya da numara tekildir.
    cond = model.id == ref if isinstance(ref, uuid.UUID) else model.request_no == ref
    return await _unique(session, select(model.project_id).where(cond))


async def _purchase_order_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.procurement.models import PurchaseOrder  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(session, select(PurchaseOrder.project_id).where(PurchaseOrder.id == ref))


async def _warehouse_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.inventory.models import Warehouse  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    # Deponun projesi bağlı olduğu ŞANTİYEDEN gelir; merkez depo (`site_id IS NULL`) projesizdir
    # → `None` (iç birleşim satır vermez; birleşim, fail-closed).
    return await _unique(
        session,
        select(Site.project_id)
        .join(Warehouse, Warehouse.site_id == Site.id)
        .where(Warehouse.id == ref),
    )


async def _treasury_payment_project(
    session: AsyncSession, ref: uuid.UUID | str
) -> uuid.UUID | None:
    from app.modules.invoicing.models import Invoice  # döngüyü önler
    from app.modules.treasury.models import Payment  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    # Ödemenin projesi FATURASININ projesidir; şirket geneli fatura `project_id IS NULL` → `None`.
    return await _unique(
        session,
        select(Invoice.project_id)
        .join(Payment, Payment.invoice_id == Invoice.id)
        .where(Payment.id == ref),
    )


async def _section_link_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.documents.models.links import SectionDocument  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(Site.project_id)
        .join(Section, Section.site_id == Site.id)
        .join(SectionDocument, SectionDocument.section_id == Section.id)
        .where(SectionDocument.id == ref),
    )


async def _unit_link_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.documents.models.links import UnitDocument  # döngüyü önler
    from app.modules.units.models import Unit  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(Unit.project_id)
        .join(UnitDocument, UnitDocument.unit_id == Unit.id)
        .where(UnitDocument.id == ref),
    )


async def _sale_link_project(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    from app.modules.documents.models.links import UnitSaleDocument  # döngüyü önler
    from app.modules.sales.models import UnitSale  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(UnitSale.project_id)
        .join(UnitSaleDocument, UnitSaleDocument.unit_sale_id == UnitSale.id)
        .where(UnitSaleDocument.id == ref),
    )


async def _sub_contract_link_project(
    session: AsyncSession, ref: uuid.UUID | str
) -> uuid.UUID | None:
    from app.modules.contracts.models import SubcontractorContract  # döngüyü önler
    from app.modules.documents.models.links import SubcontractorContractDocument  # döngüyü önler

    if not isinstance(ref, uuid.UUID):
        return None
    return await _unique(
        session,
        select(SubcontractorContract.project_id)
        .join(
            SubcontractorContractDocument,
            SubcontractorContractDocument.subcontractor_contract_id == SubcontractorContract.id,
        )
        .where(SubcontractorContractDocument.id == ref),
    )


async def _company_wide(session: AsyncSession, ref: uuid.UUID | str) -> uuid.UUID | None:
    """Proje kolonu OLMAYAN şirket geneli kayıtlar (stok kartı `stock_items`, EV disiplini
    `ev_disciplines`, EV katalog kalemi `ev_catalog_items`): proje bağlamı HİÇBİR ZAMAN yoktur →
    `None` (ana rol kararı). Anahtarın RESOLVERS'ta AÇIKÇA durması "unutulmadı, ölçüldü: projeden
    bağımsız" kaydıdır. UYARI: bu önek altına PROJEYE BAĞLI bir kimlik parametresi eklenirse
    (`/earned-value/.../{item_id}` BOQ kalemi gibi) kendi çözücüsü yazılmalıdır."""
    return None


#: (yol öneki, yol parametresi) → çözücü. Önek eşleşen rotanın köksüz yolunun ilk segmentidir
#: (`_route_path`; rota yoksa `request.url.path`).
RESOLVERS: Mapping[tuple[str, str], Resolver] = {
    ("/projects", "project_id"): _project_itself,
    ("/sites", "site_id"): _site_project,
    ("/sections", "section_id"): _section_project,
    ("/boq", "item_id"): _boq_item_project,
    ("/boq", "group_id"): _boq_group_project,
    ("/diary", "entry_id"): _diary_entry_project,
    ("/progress-payments", "payment_id"): _payment_project,
    ("/subcontractor-progress-payments", "payment_id"): _sub_payment_project,
    ("/subcontractor-contracts", "contract_id"): _sub_contract_project,
    # IZN-B4a: maske + yazma kapısı proje başına karar verir (ekip rolü ≠ ana rol).
    ("/subcontractor-contracts", "item_id"): _sub_item_project,
    # IZN-B4b onarımı: fatura + ödeme alt yolu (`/invoices/{id}/payments`) faturanın projesinde.
    ("/invoices", "invoice_id"): _invoice_project,
    # IZN-HF1: çek/senet PATCH'i `project_id` bildirir → taşıma kuralı kayıt çözücüsü ister.
    ("/financial-instruments", "instrument_id"): _instrument_project,
    ("/contracts", "item_id"): _employer_item_project,
    ("/contracts", "group_id"): _employer_group_project,
    ("/units", "unit_id"): _unit_project,
    ("/blocks", "block_id"): _block_project,
    ("/sales", "sale_id"): _sale_project,
    ("/sales", "installment_id"): _installment_project,
    # Belge bağları (`/{varlık}/{owner_id}/documents`) ve belge/klasör uçları.
    ("/sections", "owner_id"): _section_project,
    ("/subcontractor-contracts", "owner_id"): _sub_contract_project,
    ("/units", "owner_id"): _unit_project,
    ("/documents", "document_id"): _document_project,
    ("/document-folders", "folder_id"): _folder_project,
    # IZN-B4d onarımı: saha KAYIT bağlamı (makine, yakıt/iş kaydı, kira faturası + kalemi, belge,
    # satınalma talebi + teklif alt yolu, sipariş). Gövdedeki `site_id`/`project_id` kaydı
    # TAŞIMADAN okuma/yazma kararını vermesin diye kaydın KENDİ projesi önceliklidir.
    ("/equipment", "equipment_id"): _equipment_project,
    ("/equipment", "log_id"): _equipment_log_project,
    ("/equipment", "document_id"): _equipment_document_project,
    ("/equipment", "invoice_id"): _rental_invoice_project,
    ("/equipment", "line_id"): _rental_line_project,
    ("/purchase-requests", "request_id"): _purchase_request_project,
    ("/purchase-orders", "order_id"): _purchase_order_project,
    # IZN-B5a: belge bağı uçları (`/<sahip>/documents/{link_id}`) bağın SAHİBİNİN projesinde;
    # satış belge listesi/bağlama (`/sales/{owner_id}/documents`) satışın projesinde.
    ("/sections", "link_id"): _section_link_project,
    ("/units", "link_id"): _unit_link_project,
    ("/sales", "link_id"): _sale_link_project,
    ("/sales", "owner_id"): _sale_project,
    ("/subcontractor-contracts", "link_id"): _sub_contract_link_project,
    # IZN-B5a: depo (şantiyesinin projesi; merkez depo `None`) ve ödeme (faturasının projesi).
    ("/warehouses", "warehouse_id"): _warehouse_project,
    ("/payments", "payment_id"): _treasury_payment_project,
    # IZN-B5a: şirket geneli kayıtlar — projeden bağımsız (ölçüldü, bkz. `_company_wide`).
    ("/stock", "item_id"): _company_wide,
    ("/earned-value", "discipline_id"): _company_wide,
    ("/earned-value", "item_id"): _company_wide,
}

#: Gövdedeki kimlik anahtarı → çözücü (oluşturma uçları: `project_id` / `site_id`).
BODY_RESOLVERS: Mapping[str, Resolver] = {
    "project_id": _project_itself,
    "site_id": _site_project,
}


def _route_path(request: Request) -> str:
    """Bağlam öneki için yol: istek yolundan `root_path` ÖNEKİ atılmış hâli (Starlette'in
    `get_route_path` kuralı). `request.url.path` uygulama `root_path` ile yayınlanınca
    ('/api/units/…') o öneki taşır (IZN-B5a 23c). `scope["route"].path_format` KULLANILMAZ:
    FastAPI'de orijinal rotadır ve iç içe `include_router(prefix=…)` önekini taşımaz; ETKİN
    (önekli) yol istek yolundan güvenle türer."""
    path = request.scope.get("path") or request.url.path
    root = request.scope.get("root_path") or ""
    if root and path.startswith(root):
        rest = path[len(root) :]
        if not rest or rest.startswith("/"):
            return rest or "/"
    return path


def _segment(path: str) -> str:
    return "/" + path.lstrip("/").split("/", 1)[0]


async def resolve_path_project(
    session: AsyncSession, path: str, path_params: Mapping[str, object]
) -> tuple[bool, uuid.UUID | None]:
    """YOL çözücüsü: `(eşleşti_mi, proje)`. `(True, None)` = çözücü eşleşti ama kayıt projesiz /
    bilinmiyor / belirsiz; `(False, None)` = hiçbir `RESOLVERS` girdisi eşleşmedi."""
    prefix = _segment(path)
    for (resolver_prefix, param), resolver in RESOLVERS.items():
        if resolver_prefix != prefix or param not in path_params:
            continue
        ref = parse_ref(str(path_params[param]))
        return True, await resolver(session, ref)
    return False, None


async def resolve_project(
    session: AsyncSession, path: str, path_params: Mapping[str, object]
) -> uuid.UUID | None:
    """İsteğin projesi YOL parametresinden: önek + parametre adına göre; çözülemezse `None`."""
    return (await resolve_path_project(session, path, path_params))[1]


def _model_keys(model: type[BaseModel]) -> set[str]:
    """Pydantic modelinin ÜST DÜZEY JSON anahtarları (alan adı + takma adlar)."""
    keys: set[str] = set()
    for name, field in model.model_fields.items():
        keys.add(name)
        for alias in (field.alias, field.validation_alias):
            if isinstance(alias, str):
                keys.add(alias)
    return keys


def _body_declared_keys(request: Request) -> frozenset[str]:
    """Eşleşen rotanın JSON gövdesinde ÜST DÜZEYDE bildirdiği anahtarlar.

    Tek (gömülü olmayan) gövde parametresi pydantic modeliyse modelin alanları; birden çok / gömülü
    gövde parametresinde parametre adları (FastAPI gövdeyi `{ad: ...}` olarak bekler). Rota yoksa,
    gövde parametresi yoksa ya da model değilse (`dict`, liste …) BOŞ: bildirilmeyen anahtar
    güvenilmez (fail-closed).
    """
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return frozenset()
    params = route.dependant.body_params
    if not params:
        return frozenset()
    embedded = any(getattr(p.field_info, "embed", None) for p in params)
    if len(params) > 1 or embedded:
        return frozenset({p.alias for p in params} | {p.name for p in params})
    annotation = params[0].field_info.annotation
    candidates = (
        get_args(annotation) if get_origin(annotation) in (Union, UnionType) else (annotation,)
    )
    keys: set[str] = set()
    for candidate in candidates:
        if isinstance(candidate, type) and issubclass(candidate, BaseModel):
            keys |= _model_keys(candidate)
    return frozenset(keys)


def _is_json_content_type(value: str | None) -> bool:
    """FastAPI ile AYNI kural (`fastapi/routing.py`): `email.message` ayrıştırması, ana tür
    `application`, alt tür (küçük harfe çevrilmiş) `json` ya da `*+json`. Büyük/küçük harf,
    parametre (`; charset=utf-8`) ve `+json` çeşitlemeleri uç ile bağlam arasında AYRIŞMAMALI."""
    if not value:
        return False
    message = email.message.Message()
    message["content-type"] = value
    if message.get_content_maintype() != "application":
        return False
    subtype = message.get_content_subtype()
    return subtype == "json" or subtype.endswith("+json")


def _is_form_route(request: Request) -> bool:
    """Rotanın gövdesi form/multipart mı (`POST /documents`, birim içe aktarma): bu uçlarda
    `project_id`/`site_id` FORM alanıdır, JSON gövde yoktur — fail-closed kuralı uygulanmaz."""
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return False
    return any(isinstance(p.field_info, params.Form) for p in route.dependant.body_params)


class _Body(NamedTuple):
    """Gövdenin bağlam okuması: `projects` = dize değerli bildirilmiş anahtarların çözülmüş
    projeleri; `has_null` = bildirilmiş bir anahtar AÇIKÇA `null` ("hedef = projesiz");
    `invalid` = gövde bağlam için GÜVENİLEMEZ (JSON okunamadı / dict değil / değer dize-null
    değil)."""

    projects: tuple[uuid.UUID | None, ...] = ()
    has_null: bool = False
    invalid: bool = False


_INVALID = _Body(invalid=True)


async def _read_body(session: AsyncSession, request: Request) -> _Body:
    """JSON gövdesinde, rotanın gövde modelinin ÜST DÜZEYDE bildirdiği `project_id`/`site_id`
    anahtarlarının okuması. Rota bu anahtarlardan birini BİLDİRMİYORSA gövde okunmaz (boş sonuç).
    Bildiriyorsa (POST/PUT/PATCH) ve gövde JSON olarak okunamıyorsa (içerik türü JSON değil, boş,
    bozuk, dict değil) ya da bir değer dize/`null` değilse → `invalid` (fail-closed)."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return _Body()
    declared = _body_declared_keys(request)
    if not declared.intersection(BODY_KEYS):
        return _Body()
    if _is_form_route(request):
        return _Body()  # form alanı ≠ JSON: bağlama girmez (eski davranış)
    if not _is_json_content_type(request.headers.get("content-type")):
        return _INVALID
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        return _INVALID
    if not isinstance(body, dict):
        return _INVALID
    projects: list[uuid.UUID | None] = []
    has_null = False
    for key in BODY_KEYS:
        if key not in declared or key not in body:
            continue
        value = body[key]
        if value is None:
            has_null = True
        elif isinstance(value, str):
            projects.append(await BODY_RESOLVERS[key](session, parse_ref(value)))
        else:
            return _INVALID
    return _Body(tuple(projects), has_null)


async def _body_project(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """Gövdedeki bağlam (oluşturma uçları): YALNIZ rotanın gövde modeli anahtarı bildiriyorsa.
    Bildirilen anahtarlar farklı projelere çözülüyorsa / çözülemiyorsa / gövde güvenilemezse
    `None` (fail-closed)."""
    body = await _read_body(session, request)
    distinct = set(body.projects)
    return None if body.invalid or len(distinct) != 1 else next(iter(distinct))


async def request_project(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """İsteğin PROJESİ (yol → gövde sırasıyla); istek başına BİR kez çözülür (önbellekli).

    TAŞIMA kuralı (IZN-HF1): yol çözücüsü eşleştiyse gövde YALNIZ doğrulayabilir, bağlamı
    DEĞİŞTİREMEZ. Gövde (modelin bildirdiği `project_id`/`site_id`) yol projesi P'den farklı bir
    projeye işaret ediyorsa — ya da kayıt projesizken (P yok) bir proje gösteriyorsa — sonuç `None`
    (maske: ana rol ∪ ekip rolleri birleşimi, kapı: yalnız ana rol; fail-closed). Aksi hâlde kayıt
    "taşınıyormuş" gibi hedef projenin rolüyle okunup yazılabilirdi. P == Q → P. Açık `null`
    ("hedef = projesiz") P doluyken aynı taşımadır (proje rolüyle şirket geneline taşıma) → `None`;
    P yokken P == Q (ikisi de projesiz) → değişmez. Yol çözücüsü HİÇ eşleşmediyse (oluşturma
    uçları, `RESOLVERS` dışı yollar) gövde bağlamı geçerlidir (`null` yok sayılır).

    Fail-closed: rota bağlam anahtarı bildirip gövde JSON olarak okunamıyorsa / değer dize-null
    değilse bağlam her durumda `None`. İçerik türü FastAPI ile aynı kuralla çözülür
    (`_is_json_content_type`): `application/JSON`, `application/vnd.x+json` gövdesi de okunur.

    Bilinen borç: yol çözücüsü OLMAYAN PATCH/PUT uçları gövdeden bağlam alır (taşıma kuralı
    uygulanamaz); liste `tests/modules/test_izn_hf1_govde_baglam.py::BILINEN_COZUCUSUZ_PATCH`.
    """
    if SCOPE_KEY in request.scope:
        return request.scope[SCOPE_KEY]
    matched, project_id = await resolve_path_project(
        session, _route_path(request), request.path_params
    )
    body = await _read_body(session, request)
    if body.invalid:
        project_id = None
    elif matched:
        moved = any(target != project_id for target in body.projects)
        if moved or (body.has_null and project_id is not None):
            project_id = None  # taşıma / çelişki: bağlam çözülmez
    else:
        distinct = set(body.projects)
        project_id = next(iter(distinct)) if len(distinct) == 1 else None
    request.scope[SCOPE_KEY] = project_id
    return project_id


__all__ = ["RESOLVERS", "request_project", "resolve_path_project", "resolve_project"]
