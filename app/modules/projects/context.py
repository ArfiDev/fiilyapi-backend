"""İstekten PROJE BAĞLAMI çıkarma (IZN-B3, IZN-PLAN §2.2): yol parametresi → proje kimliği.

Disiplin kapsamı PROJE BAŞINA olduğu için (`DisciplineScoped`) isteğin hangi projeye ait olduğu
bilinmelidir. Çoğu uç `site_id` ya da bir varlık kimliği taşır; proje, varlığın satırından
okunur. Tablo AÇIK ve küçüktür; kapsamlı uç eklenip buraya yazılmazsa
`tests/core/test_proje_baglami_cozucu_bekcisi.py` kırmızı verir (sessiz "çok proje" kapsamına
düşmesin diye).

`None` = proje bağlamı YOK ya da çözülemedi (liste ucu, bilinmeyen kayıt): çağıran ÇOK PROJE
kapsamı kullanır (kısıtlı olduğu herhangi bir projede kısıtlı sayılır: fail-closed).
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import SubcontractorContract
from app.modules.progress_payments.models import ProgressPayment
from app.modules.site_diary.models import SiteDiaryEntry
from app.modules.sites.models import Section, Site
from app.modules.subcontractor_progress_payments.models import SubcontractorProgressPayment

Resolver = Callable[[AsyncSession, uuid.UUID], Awaitable[uuid.UUID | None]]


async def _site_project(session: AsyncSession, site_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(select(Site.project_id).where(Site.id == site_id))


async def _section_project(session: AsyncSession, section_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(Site.project_id)
        .join(Section, Section.site_id == Site.id)
        .where(Section.id == section_id)
    )


async def _boq_item_project(session: AsyncSession, item_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(Site.project_id)
        .join(BoqItem, BoqItem.site_id == Site.id)
        .where(BoqItem.id == item_id)
    )


async def _boq_group_project(session: AsyncSession, group_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(Site.project_id)
        .join(BoqGroup, BoqGroup.site_id == Site.id)
        .where(BoqGroup.id == group_id)
    )


async def _diary_entry_project(session: AsyncSession, entry_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(SiteDiaryEntry.project_id).where(SiteDiaryEntry.id == entry_id)
    )


async def _payment_project(session: AsyncSession, payment_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(ProgressPayment.project_id).where(ProgressPayment.id == payment_id)
    )


async def _sub_payment_project(session: AsyncSession, payment_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(SubcontractorProgressPayment.project_id).where(
            SubcontractorProgressPayment.id == payment_id
        )
    )


async def _sub_contract_project(session: AsyncSession, contract_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(
        select(SubcontractorContract.project_id).where(SubcontractorContract.id == contract_id)
    )


async def _project_itself(session: AsyncSession, project_id: uuid.UUID) -> uuid.UUID | None:
    return project_id


#: (yol öneki, yol parametresi) → çözücü. Önek `request.url.path`in başıdır (tam eşleşen segment).
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
}


def _segment(path: str) -> str:
    return "/" + path.lstrip("/").split("/", 1)[0]


async def resolve_project(
    session: AsyncSession, path: str, path_params: Mapping[str, object]
) -> uuid.UUID | None:
    """İsteğin projesi: yol önekine ve parametrelerine göre; çözülemezse `None`."""
    prefix = _segment(path)
    for (resolver_prefix, param), resolver in RESOLVERS.items():
        if resolver_prefix != prefix or param not in path_params:
            continue
        try:
            entity_id = uuid.UUID(str(path_params[param]))
        except ValueError:
            return None
        return await resolver(session, entity_id)
    return None
