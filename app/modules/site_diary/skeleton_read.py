"""GKS-B1 — `GET /sites/{site_id}/diary/skeleton`: kaydetmeden satır önizlemesi.

Hangi satırların geleceği `skeleton.skeleton_keys`in (kural A) kararıdır — POST iskeleti
(`service._build_lines`) AYNI fonksiyonu çağırır. Planlı/kümülatif/kalan değerleri detay
ucunun yardımcılarından (`read.line_fields`) gelir. Saf fonksiyonda (`SkeletonLine.planned`)
kural gereği bir planlı hesabı vardır; yanıt değeri `read.planned_quantity`den gelir ve iki
değerin eşitliği bekçilidir (`test_gks_b1_onizleme`). Satırlar henüz
kaydedilmediği için kimliksizdir (`SiteDiarySkeletonLine`) ve `quantity` 0'dır.

Kapsam: şantiye görünürlüğü 404 (`visible_site`), bölüm doğrulaması 422 (`validate_section`),
DSC süzgeci (kısıtlıda yalnız görünür kalemler; kısıtsızda ek sorgu YOK). BOQ izni İSTENMEZ —
kapı yalnız `site_diary` görüntülemedir (router).
GKS-B1.1: `own_crew_from_timesheet` kayıtsız günde de gelir — detay ucuyla AYNI fonksiyon.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import day_hooks
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, visible_item_set
from app.modules.site_diary import read, repository, skeleton
from app.modules.site_diary.models import SiteDiaryLine
from app.modules.site_diary.schemas import SiteDiarySkeleton, SiteDiarySkeletonLine
from app.modules.site_diary.service import validate_section, visible_site
from app.modules.users.models import User

_ZERO_QUANTITY = Decimal("0.000")


async def get_skeleton(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    entry_date: date,
    section_id: uuid.UUID | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> SiteDiarySkeleton:
    site, _ = await visible_site(session, actor, site_id)
    await validate_section(session, section_id, site)

    items = await repository.list_boq_items(session, site.id)
    gorunur = await visible_item_set(session, scope, [item.id for item in items])
    if gorunur is not None:
        items = [item for item in items if item.id in gorunur]
    keys = skeleton.skeleton_keys(
        [skeleton.SkeletonItem(item.id, item.quantity) for item in items],
        skeleton.group_allocations(
            await repository.allocations_for_items(session, [item.id for item in items])
        ),
        section_id,
        await repository.section_order(session, site.id),
    )
    by_id = {item.id: item for item in items}
    # Kaydedilmemiş (session'a EKLENMEMİŞ) satırlar: türevler detay ucuyla aynı yardımcıdan.
    lines = [
        SiteDiaryLine(
            boq_item_id=key.item_id,
            section_id=key.section_id,
            code=by_id[key.item_id].code,
            description=by_id[key.item_id].description,
            unit=by_id[key.item_id].unit,
            unit_price=by_id[key.item_id].unit_price,
            quantity=_ZERO_QUANTITY,
        )
        for key in keys
    ]
    prior = await repository.cumulative_quantities_before(session, site.id, entry_date)
    own = read.own_item_totals(lines)
    leaf = await read.leaf_context(session, site.id, entry_date, lines)
    names = await repository.section_names(
        session, {line.section_id for line in lines if line.section_id} | {section_id} - {None}
    )
    locks = await day_hooks.day_locks(session, site.id, [entry_date])
    existing = await repository.get_entry_by_date(session, site.id, entry_date)
    return SiteDiarySkeleton(
        entry_date=entry_date,
        section_id=section_id,
        section_name=names.get(section_id) if section_id else None,
        existing_entry_id=existing.id if existing is not None else None,
        locked=bool(locks),
        lock_report_date=locks[0].report_date if locks else None,
        lines=[
            SiteDiarySkeletonLine(
                **read.line_fields(
                    line,
                    prior,
                    own,
                    leaf,
                    names.get(line.section_id) if line.section_id else None,
                )
            )
            for line in lines
        ],
        lines_total=read.lines_total(lines),
        own_crew_from_timesheet=await read.own_crew_from_timesheet(session, site.id, entry_date),
    )
