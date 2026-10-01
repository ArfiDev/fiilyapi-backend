"""Şantiye iş kalemlerinin bölümlere dağılımı — kalem × bölüm matrisi (BDG-B1).

Emsal: `contracts/distribution.py` (`build_distribution` + `save_distribution`) —
sözleşmede "kalem × şantiye" matrisi, burada "kalem × bölüm". Tahsis tablosu,
invariant ve kilit kuralı `BoqItemSectionAllocation` docstring'indeki ve
`service.replace_allocations`taki ile AYNIDIR; bu modül onların YERİNE geçmez,
tek-kalem uçları (`GET/PUT /boq/items/{id}/allocations`) DEĞİŞMEDİ.

## OKUMA (`build_section_distribution`)

* Bölümler şantiyenin TÜM bölümleridir, TASLAKLAR DAHİL (ölçüldü:
  `BoqAssignmentCard` ve `_resolve_sections` `is_draft`a bakmaz), sıra
  `sort_order, id`. Bölümler disiplinsizdir → disiplinle SÜZÜLMEZ.
* Gruplar/kalemler BOQ ekranıyla aynı sırada (`list_groups_for_site`,
  `BoqGroup.items`). Kısıtlı kullanıcıda yalnız görünür kalemler
  (`visible_item_set`; kısıtsızda ek sorgu YOK); görünür kalemi olmayan grup
  kısıtlıda listelenmez, kısıtsızda boş gruplar da listelenir.
* Sayaçlar, kod listesi ve bölüm özetleri YALNIZ görünen kalemlerden türer ve
  maskeden ÖNCEKİ gerçek miktarlarla hesaplanır (rota maskesi sonradan uygulanır).
  `unallocated_item_count` = atanmamışı > 0 kalem (kısmen dağıtılmışlar dahil),
  `distributed_item_count` = tam dağıtılmış; toplamları `total_item_count`tur.
* N+1 YOK: sorgu sayısı kalem/bölüm sayısından bağımsızdır (şantiye, bölümler,
  gruplar + kalemler selectin, tüm tahsis satırları tek sorgu, görünürlük).

## YAZMA (`save_section_distribution`) — BİRLEŞTİRME

Gövdede geçmeyen (kalem, bölüm) hücresi KORUNUR; `quantity` `null` ya da `0` ise
satır silinir (`quantity > 0` CHECK'i), `> 0` ise güncellenir (kimlik KORUNUR) ya
da eklenir. Sıra ZORUNLU: ÖNCE TÜM DOĞRULAMA, SONRA YAZMA — ikinci hücrede
patlayan istek hiçbir şey yazmaz.

1. Şantiye (görünmeyen/olmayan → 404; yolda 404, gövdede 422).
2. Gövde şekli: aynı (kalem, bölüm) çifti iki kez → 422 (sunucu SESSİZCE TOPLAMAZ).
3. Gövdedeki kalemler: bu şantiyenin değilse / yoksa / disiplin kapsamında
   görünmüyorsa → 422 `_ITEM_MISSING`; ÜÇÜ AYIRT EDİLEMEZ (DSC Ü7). Bölüm bu
   şantiyenin değilse → 422 `SECTION_MISSING` (bu bölüm kontrolü kilitten SONRA
   yapılır; kalem kilitten sonra eksikse de `_ITEM_MISSING` — silinme yarışı).
4. 🔴 EŞİK = KİLİT: gövdedeki TÜM kalemler tek sorguda `ORDER BY id FOR UPDATE`
   (`repository.lock_items`). Kilit, mevcut payları okuyan sorgudan ÖNCE alınır;
   aşım kararı kilitli/taze `quantity` ile verilir. Sabit id sırası kilitlenme
   çevrimini önler.
5. Kilitten SONRA mevcut paylar okunur. Kalem başına yeni toplam = gövdede
   GEÇMEYEN mevcut paylar + gövdedeki > 0 değerler; kota aşılırsa 422 (sözleşme
   dağılımı `DISTRIBUTION_EXCEEDS` emsali; tek kalem ucunun 409'u değişmez).
   Eşitlik geçerlidir.
6. Yazma + `flush`.

Servis, GERÇEKTEN DEĞİŞEN kalemleri `ChangedItem` olarak döndürür; router her biri
için denetim kaydı yazar (değişmeyen hücre = kayıt yok).
"""

import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import DisciplineScope, visible_group_set, visible_item_set
from app.core.errors import SiteValidationError
from app.modules.boq import repository
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.boq.schemas import (
    SectionDistributionAllocation,
    SectionDistributionGroup,
    SectionDistributionItem,
    SectionDistributionResponse,
    SectionDistributionSave,
    SectionDistributionSection,
    SectionDistributionSectionItem,
    SectionDistributionSectionSummary,
    quantize_money,
    quantize_quantity,
)
from app.modules.boq.service import _ITEM_MISSING
from app.modules.sites import guards as sites_guards
from app.modules.sites.service import _visible_site
from app.modules.users.models import User

_ZERO = Decimal("0.000")
_DUPLICATE_CELL = "Aynı iş kalemi ve bölüm hücresi gövdede birden fazla kez gönderildi"


def _exceeds_message(code: str) -> str:
    return f"{code}: bölümlere dağıtılan toplam şantiye kotasını aşıyor"


@dataclass(frozen=True)
class ChangedItem:
    """Yazmada GERÇEKTEN değişen kalem (denetim kaydı için)."""

    code: str
    section_count: int  # yazma SONRASI bu kalemin bölüm sayısı


@dataclass(frozen=True)
class SaveResult:
    matrix: SectionDistributionResponse
    changed: list[ChangedItem]


async def build_section_distribution(
    session: AsyncSession, actor: User, site_id: uuid.UUID, scope: DisciplineScope
) -> SectionDistributionResponse:
    site, project = await _visible_site(session, actor, site_id, sites_guards.SITE_MISSING)
    sections = await repository.list_sections_for_distribution(session, site.id)
    boq_groups = await repository.list_groups_for_site(session, site.id)
    gorunur = await visible_item_set(
        session, scope, [item.id for group in boq_groups for item in group.items]
    )
    rows = await repository.allocation_rows_for_site(session, site.id)

    # kalem -> {bolum -> pay}
    paylar: dict[uuid.UUID, dict[uuid.UUID, Decimal]] = {}
    for row in rows:
        paylar.setdefault(row.boq_item_id, {})[row.section_id] = row.quantity

    bos_gorunur: set[uuid.UUID] | None = None
    if gorunur is not None:
        bos_gorunur = await visible_group_set(
            session, scope, [g.id for g in boq_groups if not g.items]
        )

    groups: list[SectionDistributionGroup] = []
    gorunen: list[BoqItem] = []
    unallocated_codes: list[str] = []
    for group in boq_groups:
        items: list[SectionDistributionItem] = []
        for item in group.items:
            if gorunur is not None and item.id not in gorunur:
                continue
            gorunen.append(item)
            item_paylar = paylar.get(item.id, {})
            allocated = sum(item_paylar.values(), _ZERO)
            unallocated = item.quantity - allocated
            if unallocated > 0:
                unallocated_codes.append(item.code)
            items.append(
                SectionDistributionItem(
                    id=item.id,
                    code=item.code,
                    description=item.description,
                    unit=item.unit,
                    quantity=item.quantity,
                    unit_price=item.unit_price,
                    allocations=[
                        SectionDistributionAllocation(
                            section_id=section.id, quantity=item_paylar[section.id]
                        )
                        for section in sections
                        if section.id in item_paylar
                    ],
                    allocated_quantity=allocated,
                    unallocated_quantity=unallocated,
                )
            )
        if items or (gorunur is None) or group.id in (bos_gorunur or ()):
            groups.append(
                SectionDistributionGroup(
                    id=group.id, name=group.name, sort_order=group.sort_order, items=items
                )
            )

    summaries = _section_summaries(sections, gorunen, paylar)
    return SectionDistributionResponse(
        site_id=site.id,
        site_name=site.name,
        project_name=project.name,
        sections=[
            SectionDistributionSection(
                id=s.id, name=s.name, code=s.code, sort_order=s.sort_order, is_draft=s.is_draft
            )
            for s in sections
        ],
        groups=groups,
        unallocated_item_count=len(unallocated_codes),
        unallocated_item_codes=unallocated_codes,
        distributed_item_count=len(gorunen) - len(unallocated_codes),
        total_item_count=len(gorunen),
        section_summaries=summaries,
    )


def _section_summaries(
    sections: list,
    items: list[BoqItem],
    paylar: dict[uuid.UUID, dict[uuid.UUID, Decimal]],
) -> list[SectionDistributionSectionSummary]:
    """Bölüm başına: o bölüme payı olan (görünür) kalemler + Σ(pay × birim fiyat)."""
    summaries: list[SectionDistributionSectionSummary] = []
    for section in sections:
        rows = [
            SectionDistributionSectionItem(
                boq_item_id=item.id,
                code=item.code,
                description=item.description,
                unit=item.unit,
                quantity=paylar[item.id][section.id],
                unit_price=item.unit_price,
                amount=quantize_money(paylar[item.id][section.id] * item.unit_price),
            )
            for item in items
            if section.id in paylar.get(item.id, {})
        ]
        summaries.append(
            SectionDistributionSectionSummary(
                section_id=section.id,
                section_name=section.name,
                items=rows,
                total_amount=quantize_money(sum((r.amount for r in rows), Decimal("0"))),
            )
        )
    return summaries


async def _validate_body(
    session: AsyncSession,
    site_id: uuid.UUID,
    data: SectionDistributionSave,
    scope: DisciplineScope,
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """Gövde şekli + kalem/bölüm kapsamı. Hiçbir şey yazmaz, kilit almaz."""
    istenen: dict[tuple[uuid.UUID, uuid.UUID], Decimal] = {}
    for cell in data.allocations:
        key = (cell.boq_item_id, cell.section_id)
        if key in istenen:
            raise SiteValidationError(_DUPLICATE_CELL)
        istenen[key] = _ZERO if cell.quantity is None else quantize_quantity(cell.quantity)

    item_ids = {item_id for item_id, _ in istenen}
    items = await repository.items_in_site(session, site_id, item_ids)
    gorunur = await visible_item_set(session, scope, items.keys())
    gorunur_ids = set(items) if gorunur is None else gorunur
    # 🔴 üç hâl (yok / başka şantiye / görünmüyor) AYNI mesajı alır.
    if any(item_id not in gorunur_ids for item_id in item_ids):
        raise SiteValidationError(_ITEM_MISSING)

    return istenen


async def _validate_sections(
    session: AsyncSession,
    site_id: uuid.UUID,
    istenen: dict[tuple[uuid.UUID, uuid.UUID], Decimal],
) -> None:
    """Bölüm kapsamı — kalem kilidinden SONRA (bölüm satırı kilitlenmez; pencereyi daraltır)."""
    section_ids = {section_id for _, section_id in istenen}
    bulunan = await repository.sections_in_site(session, site_id, section_ids)
    if section_ids - bulunan:
        raise SiteValidationError(sites_guards.SECTION_MISSING)


async def save_section_distribution(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    data: SectionDistributionSave,
    scope: DisciplineScope,
) -> SaveResult:
    site, _ = await _visible_site(session, actor, site_id, sites_guards.SITE_MISSING)
    istenen = await _validate_body(session, site.id, data, scope)

    item_ids = {item_id for item_id, _ in istenen}
    # 🔴 KİLİT mevcut payları okuyan sorgudan ÖNCE; karar kilitli/taze quantity ile.
    kilitli = await repository.lock_items(session, item_ids)
    # Doğrulama ile kilit arasında kalem silindiyse aşım kontrolü atlanırdı → RED.
    if len(kilitli) != len(item_ids):
        raise SiteValidationError(_ITEM_MISSING)
    await _validate_sections(session, site.id, istenen)
    mevcut_satirlar = await repository.allocation_rows_for_items(session, item_ids)
    mevcut: dict[tuple[uuid.UUID, uuid.UUID], BoqItemSectionAllocation] = {
        (row.boq_item_id, row.section_id): row for row in mevcut_satirlar
    }

    # --- Aşım kontrolü (YAZMADAN önce, tüm kalemler için) ---
    for item_id, item in kilitli.items():
        dokunulmayan = sum(
            (
                row.quantity
                for (i_id, s_id), row in mevcut.items()
                if i_id == item_id and (i_id, s_id) not in istenen
            ),
            _ZERO,
        )
        yeni = sum((q for (i_id, _), q in istenen.items() if i_id == item_id and q > 0), _ZERO)
        if dokunulmayan + yeni > item.quantity:
            raise SiteValidationError(_exceeds_message(item.code))

    # --- YAZMA (doğrulama YOK) ---
    degisen: dict[uuid.UUID, None] = {}
    for (item_id, section_id), quantity in istenen.items():
        row = mevcut.get((item_id, section_id))
        if quantity <= 0:
            if row is not None:
                await session.delete(row)
                degisen[item_id] = None
        elif row is None:
            session.add(
                BoqItemSectionAllocation(
                    boq_item_id=item_id, section_id=section_id, quantity=quantity
                )
            )
            degisen[item_id] = None
        elif row.quantity != quantity:
            row.quantity = quantity
            degisen[item_id] = None
    await session.flush()

    changed = [
        ChangedItem(
            code=kilitli[item_id].code,
            section_count=sum(
                1
                for (i_id, s_id), q in _after(mevcut, istenen).items()
                if i_id == item_id and q > 0
            ),
        )
        for item_id in degisen
    ]
    matrix = await build_section_distribution(session, actor, site.id, scope)
    return SaveResult(matrix=matrix, changed=changed)


def _after(
    mevcut: dict[tuple[uuid.UUID, uuid.UUID], BoqItemSectionAllocation],
    istenen: dict[tuple[uuid.UUID, uuid.UUID], Decimal],
) -> dict[tuple[uuid.UUID, uuid.UUID], Decimal]:
    """Yazma SONRASI (kalem, bölüm) → pay haritası (yalnız dokunulan kalemler için kullanılır)."""
    sonra = {key: row.quantity for key, row in mevcut.items()}
    for key, quantity in istenen.items():
        sonra[key] = quantity
    return sonra
