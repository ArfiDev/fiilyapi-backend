"""Teklif sablonu yazma/okuma mantigi (TKL-B5.1). Servis COMMIT ETMEZ; denetim ROUTER isidir.

## Kilitler
* Tek-sablon yazimi: sablon satiri `FOR UPDATE` (`lock_template`).
* Sablondan teklif: sablon satiri `FOR SHARE` (`lock_template_shared`): olusturma sirasinda
  sablon silinemez/degismez; eszamanli olusturmalar birbirini bekletmez.
* 🔴 TEK VARSAYILAN: kismi tekil indeks (`uq_offer_templates_single_default`) SON savunmadir;
  `is_default`i dogru yapan HER yazim once `pg_advisory_xact_lock(TEMPLATE_DEFAULT_LOCK)` alir,
  SONRA eski varsayilani AYNI islemde dusurur. Kilitsiz iki "varsayilan yap" ikisi de eski
  varsayilani bos gorur, ikinci INSERT/UPDATE tekil indekste patlar (500) — pozitif kontrol:
  `tests/modules/offers/test_template_default_race.py`.

## Icerik
`replace_content`: gruplar + kalemler TAM degistirilir (tek uc; surukle-birak ekrani tek kaydeder).
Fiyat/miktar SAKLANMAZ (T12). Katalog kalemi yoksa 404.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError, OfferValidationError
from app.modules.catalog.models import EvCatalogItem
from app.modules.offers import locking
from app.modules.offers.models import (
    Offer,
    OfferGroup,
    OfferItem,
    OfferRevision,
    OfferTemplate,
    OfferTemplateGroup,
    OfferTemplateItem,
)
from app.modules.offers.offer_views import sort_groups, sort_items
from app.modules.offers.template_schemas import (
    TEMPLATE_GROUPS_MAX,
    TEMPLATE_GROUPS_TOO_MANY,
    TEMPLATE_ITEMS_MAX,
    TEMPLATE_ITEMS_TOO_MANY,
    TemplateContentReplace,
    TemplateCreate,
    TemplateDetailRead,
    TemplateGroupRead,
    TemplateItemRead,
    TemplateListItem,
    TemplateListResponse,
    TemplateUpdate,
)
from app.modules.users.models import User

TEMPLATE_MISSING = "Teklif şablonu bulunamadı"
CATALOG_ITEM_MISSING = "Katalog iş tipi bulunamadı"
COPY_SUFFIX = " (kopya)"

#: `pg_advisory_xact_lock` anahtari (tek-anahtar formu): varsayilan sablon degisimi serilesir.
TEMPLATE_DEFAULT_LOCK = 7_450_021


def _now() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------------- kilitler


async def lock_template(session: AsyncSession, template_id: uuid.UUID) -> OfferTemplate:
    template = await session.scalar(
        select(OfferTemplate)
        .where(OfferTemplate.id == template_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if template is None:
        raise NotFoundError(TEMPLATE_MISSING)
    return template


async def lock_template_shared(session: AsyncSession, template_id: uuid.UUID) -> OfferTemplate:
    template = await session.scalar(
        select(OfferTemplate)
        .where(OfferTemplate.id == template_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if template is None:
        raise NotFoundError(TEMPLATE_MISSING)
    return template


async def _lock_default_slot(session: AsyncSession) -> None:
    await session.execute(select(func.pg_advisory_xact_lock(TEMPLATE_DEFAULT_LOCK)))


async def _make_default(session: AsyncSession, template: OfferTemplate, user: User) -> None:
    """Varsayilan yap: ONCE kilit, SONRA eskiyi dusur (ayni islem), sonra bunu isaretle."""
    await _lock_default_slot(session)
    await session.execute(
        update(OfferTemplate)
        .where(OfferTemplate.is_default.is_(True), OfferTemplate.id != template.id)
        .values(is_default=False, updated_at=_now(), updated_by_user_id=user.id)
        .execution_options(synchronize_session=False)
    )
    template.is_default = True


# --------------------------------------------------------------------------- yazma


async def create_template(session: AsyncSession, user: User, data: TemplateCreate) -> OfferTemplate:
    template = OfferTemplate(
        name=data.name,
        description=data.description,
        overhead_pct=data.overhead_pct,
        profit_pct=data.profit_pct,
        is_default=False,
        created_by_user_id=user.id,
        updated_by_user_id=user.id,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(template)
    await session.flush()
    return template


async def update_template(
    session: AsyncSession, user: User, template_id: uuid.UUID, data: TemplateUpdate
) -> tuple[OfferTemplate, bool, bool]:
    """Doner: `(sablon, alan_degisti_mi, varsayilan_oldu_mu)`. Hicbir sey fiilen degismediyse
    yazilmaz (denetim satiri da yazilmaz)."""
    if data.is_default:
        await _lock_default_slot(session)  # kilit sirasi: varsayilan yuvasi → sablon satiri
    template = await lock_template(session, template_id)
    changes = data.model_dump(exclude_unset=True)
    want_default = changes.pop("is_default", None)
    changed = False
    for field, value in changes.items():
        if getattr(template, field) != value:
            setattr(template, field, value)
            changed = True
    became_default = False
    if want_default is True and not template.is_default:
        await _make_default(session, template, user)
        became_default = True
    elif want_default is False and template.is_default:
        template.is_default = False
        changed = True
    if changed or became_default:
        template.updated_at = _now()
        template.updated_by_user_id = user.id
        await session.flush()
        await session.refresh(template)
    return template, changed, became_default


async def set_default(
    session: AsyncSession, user: User, template_id: uuid.UUID
) -> tuple[OfferTemplate, bool]:
    """`(sablon, degisti_mi)`: zaten varsayilansa yazilmaz."""
    template, _changed, became = await update_template(
        session, user, template_id, TemplateUpdate(is_default=True)
    )
    return template, became


async def delete_template(session: AsyncSession, template_id: uuid.UUID) -> OfferTemplate:
    """Sil: bagli teklifler korunur (`offers.template_id` → NULL, FK SET NULL)."""
    template = await lock_template(session, template_id)
    await session.delete(template)
    await session.flush()
    return template


def check_content_ceilings(groups: list[tuple[str, list[uuid.UUID]]]) -> None:
    """Grup/kalem tavani (`template_schemas` ile ORTAK sabitler). `PUT content` semada da dener;
    tekliften sablon sema disindan girer, bu yuzden TEK yazma yolu `_replace_rows` da uygular."""
    if len(groups) > TEMPLATE_GROUPS_MAX:
        raise OfferValidationError(TEMPLATE_GROUPS_TOO_MANY)
    if sum(len(ids) for _name, ids in groups) > TEMPLATE_ITEMS_MAX:
        raise OfferValidationError(TEMPLATE_ITEMS_TOO_MANY)


async def _replace_rows(
    session: AsyncSession,
    template: OfferTemplate,
    groups: list[tuple[str, list[uuid.UUID]]],
) -> None:
    check_content_ceilings(groups)  # tavan sema disinda da (tekliften/kopyadan) gecerli
    catalog_ids = {cid for _name, ids in groups for cid in ids}
    if catalog_ids:
        found = set(
            await session.scalars(select(EvCatalogItem.id).where(EvCatalogItem.id.in_(catalog_ids)))
        )
        if not catalog_ids <= found:
            raise NotFoundError(CATALOG_ITEM_MISSING)
    await session.execute(
        delete(OfferTemplateGroup)
        .where(OfferTemplateGroup.template_id == template.id)
        .execution_options(synchronize_session=False)
    )  # kalemler bilesik FK CASCADE ile gider
    group_rows: list[OfferTemplateGroup] = []
    for index, (name, _ids) in enumerate(groups):
        group_rows.append(
            OfferTemplateGroup(
                id=uuid.uuid4(), template_id=template.id, name=name, sort_order=index
            )
        )
    session.add_all(group_rows)
    await session.flush()
    for group_row, (_name, ids) in zip(group_rows, groups, strict=True):
        session.add_all(
            OfferTemplateItem(
                template_id=template.id,
                group_id=group_row.id,
                sort_order=position,
                catalog_item_id=catalog_id,
            )
            for position, catalog_id in enumerate(ids)
        )
    await session.flush()


async def replace_content(
    session: AsyncSession,
    user: User,
    template_id: uuid.UUID,
    data: TemplateContentReplace,
) -> tuple[OfferTemplate, int, int]:
    """Doner `(sablon, grup_adedi, kalem_adedi)` (denetim satiri icin)."""
    template = await lock_template(session, template_id)
    groups = [(g.name, [i.catalog_item_id for i in g.items]) for g in data.groups]
    await _replace_rows(session, template, groups)
    template.updated_at = _now()
    template.updated_by_user_id = user.id
    await session.flush()
    return template, len(groups), sum(len(ids) for _n, ids in groups)


async def create_from_offer(
    session: AsyncSession,
    user: User,
    *,
    offer_id: uuid.UUID,
    rev_no: int,
    name: str,
    description: str | None,
) -> tuple[OfferTemplate, Offer]:
    """Tekliften sablon: gruplar + kalemlerin KATALOG BAGLARI + revizyon GG/kar oranlari.
    Fiyat/miktar KOPYALANMAZ. Kaynak teklif satiri `FOR SHARE` (kopya sirasinda degismez)."""
    offer = await session.scalar(
        select(Offer)
        .where(Offer.id == offer_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if offer is None:
        raise NotFoundError(locking.OFFER_MISSING)
    revision = await session.scalar(
        select(OfferRevision)
        .where(OfferRevision.offer_id == offer_id, OfferRevision.rev_no == rev_no)
        .execution_options(populate_existing=True)
    )
    if revision is None:
        raise NotFoundError(locking.REVISION_MISSING)
    groups = list(
        await session.scalars(select(OfferGroup).where(OfferGroup.revision_id == revision.id))
    )
    items = list(
        await session.scalars(select(OfferItem).where(OfferItem.revision_id == revision.id))
    )
    ordered_items = sort_items(items)
    plan = [
        (g.name, [i.catalog_item_id for i in ordered_items if i.group_id == g.id])
        for g in sort_groups(groups)
    ]
    check_content_ceilings(plan)  # sablon satiri YAZILMADAN once (yarim sablon kalmaz)
    template = await create_template(
        session,
        user,
        TemplateCreate(
            name=name,
            description=description,
            overhead_pct=revision.overhead_pct,
            profit_pct=revision.profit_pct,
        ),
    )
    await _replace_rows(session, template, plan)
    return template, offer


async def copy_template(
    session: AsyncSession, user: User, template_id: uuid.UUID, name: str | None
) -> tuple[OfferTemplate, OfferTemplate]:
    """Sablondan sablon kopyasi (varsayilan DEGIL). Doner `(yeni, kaynak)`."""
    source = await lock_template_shared(session, template_id)
    new_name = name if name is not None else source.name[: 80 - len(COPY_SUFFIX)] + COPY_SUFFIX
    template = await create_template(
        session,
        user,
        TemplateCreate(
            name=new_name,
            description=source.description,
            overhead_pct=source.overhead_pct,
            profit_pct=source.profit_pct,
        ),
    )
    groups = list(
        await session.scalars(
            select(OfferTemplateGroup).where(OfferTemplateGroup.template_id == source.id)
        )
    )
    items = list(
        await session.scalars(
            select(OfferTemplateItem).where(OfferTemplateItem.template_id == source.id)
        )
    )
    plan = []
    for group in sorted(groups, key=lambda g: (g.sort_order, g.name, str(g.id))):
        members = sorted(
            (i for i in items if i.group_id == group.id),
            key=lambda i: (i.sort_order, str(i.id)),
        )
        plan.append((group.name, [i.catalog_item_id for i in members]))
    await _replace_rows(session, template, plan)
    return template, source


# --------------------------------------------------------------------------- okuma


def _usage_count_column() -> Any:
    return (
        select(func.count(Offer.id))
        .where(Offer.template_id == OfferTemplate.id)
        .correlate(OfferTemplate)
        .scalar_subquery()
    )


async def list_templates(session: AsyncSession) -> TemplateListResponse:
    groups = (
        select(func.count(OfferTemplateGroup.id))
        .where(OfferTemplateGroup.template_id == OfferTemplate.id)
        .correlate(OfferTemplate)
        .scalar_subquery()
    )
    items = (
        select(func.count(OfferTemplateItem.id))
        .where(OfferTemplateItem.template_id == OfferTemplate.id)
        .correlate(OfferTemplate)
        .scalar_subquery()
    )
    rows = (
        await session.execute(
            select(OfferTemplate, groups, items, _usage_count_column()).order_by(
                OfferTemplate.is_default.desc(), func.lower(OfferTemplate.name), OfferTemplate.id
            )
        )
    ).all()
    result = [
        _list_item(template, group_count, item_count, usage)
        for template, group_count, item_count, usage in rows
    ]
    return TemplateListResponse(items=result, total=len(result))


def _list_item(
    template: OfferTemplate, group_count: int, item_count: int, usage: int
) -> TemplateListItem:
    return TemplateListItem(
        id=template.id,
        name=template.name,
        description=template.description,
        overhead_pct=template.overhead_pct,
        profit_pct=template.profit_pct,
        is_default=template.is_default,
        group_count=group_count,
        item_count=item_count,
        usage_count=usage,
        updated_at=template.updated_at,
    )


async def get_template_detail(session: AsyncSession, template_id: uuid.UUID) -> TemplateDetailRead:
    template = await session.scalar(
        select(OfferTemplate)
        .where(OfferTemplate.id == template_id)
        .execution_options(populate_existing=True)
    )
    if template is None:
        raise NotFoundError(TEMPLATE_MISSING)
    groups = sorted(
        await session.scalars(
            select(OfferTemplateGroup).where(OfferTemplateGroup.template_id == template.id)
        ),
        key=lambda g: (g.sort_order, g.name, str(g.id)),
    )
    rows = (
        await session.execute(
            select(OfferTemplateItem, EvCatalogItem)
            .join(EvCatalogItem, EvCatalogItem.id == OfferTemplateItem.catalog_item_id)
            .where(OfferTemplateItem.template_id == template.id)
        )
    ).all()
    by_group: dict[uuid.UUID, list[TemplateItemRead]] = {g.id: [] for g in groups}
    for item, entry in sorted(rows, key=lambda r: (r[0].sort_order, str(r[0].id))):
        by_group[item.group_id].append(
            TemplateItemRead(
                id=item.id,
                sort_order=item.sort_order,
                catalog_item_id=entry.id,
                poz_no=entry.poz_no,
                description=entry.name,
                unit=entry.uom,
            )
        )
    usage = await session.scalar(
        select(func.count(Offer.id)).where(Offer.template_id == template.id)
    )
    return TemplateDetailRead(
        **_list_item(
            template, len(groups), sum(len(v) for v in by_group.values()), usage or 0
        ).model_dump(),
        created_at=template.created_at,
        groups=[
            TemplateGroupRead(id=g.id, name=g.name, sort_order=g.sort_order, items=by_group[g.id])
            for g in groups
        ],
    )
