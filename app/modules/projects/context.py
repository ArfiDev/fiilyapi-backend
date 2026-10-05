"""İstekten PROJE BAĞLAMI çıkarma (IZN-B3, IZN-PLAN §2.2): yol parametresi / gövde → proje kimliği.

İKİ tüketicisi vardır ve ikisi AYNI çözücüyü kullanır (`request_project`):

* Rota kapısı (`core/permissions`): bağlam çözülürse O projedeki rolle, çözülmezse yalnız ANA rolle
  karar verir (ekip rolü şirket geneli uçları açamaz).
* Disiplin kapsamı (`core/discipline_deps`): bağlam çözülürse o projedeki atama, çözülmezse ÇOK
  PROJE kapsamı (kısıtlı olduğu projeler haritası).

Kaynaklar: (1) yol parametresi (`RESOLVERS`: öneke + parametre adına göre; UUID ya da SLUG),
(2) JSON gövdesinde `project_id` / `site_id` (oluşturma uçları). Slug (`/projects/{slug}`,
`/sites/{slug}` …) ÇÖZÜLÜR: UUID ile slug aynı projeyi verir. Slug kapsamı tekil değilse (şantiye /
bölüm slug'ı proje / şantiye içinde tekildir) ve birden çok projede eşleşirse bağlam ÇÖZÜLMEZ
(`None`): ekip rolü sayılmaz, kesin karar `visible_projects` süzgecine kalır (fail-closed).

`None` = proje bağlamı YOK ya da çözülemedi (liste ucu, bilinmeyen/belirsiz kayıt). Yeni bir
proje bağlamlı uç eklenip çözücü yazılmazsa `tests/core/test_proje_baglami_cozucu_bekcisi.py`
kırmızı verir.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping

from fastapi import Request
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


#: (yol öneki, yol parametresi) → çözücü. Önek `request.url.path`in ilk segmentidir.
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
}

#: Gövdedeki kimlik anahtarı → çözücü (oluşturma uçları: `project_id` / `site_id`).
BODY_RESOLVERS: Mapping[str, Resolver] = {
    "project_id": _project_itself,
    "site_id": _site_project,
}


def _segment(path: str) -> str:
    return "/" + path.lstrip("/").split("/", 1)[0]


async def resolve_project(
    session: AsyncSession, path: str, path_params: Mapping[str, object]
) -> uuid.UUID | None:
    """İsteğin projesi YOL parametresinden: önek + parametre adına göre; çözülemezse `None`."""
    prefix = _segment(path)
    for (resolver_prefix, param), resolver in RESOLVERS.items():
        if resolver_prefix != prefix or param not in path_params:
            continue
        ref = parse_ref(str(path_params[param]))
        return await resolver(session, ref)
    return None


async def _body_project(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """JSON gövdesinde `project_id`/`site_id` (oluşturma uçları). Gövde okunamazsa `None`."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return None
    if "json" not in request.headers.get("content-type", ""):
        return None
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(body, dict):
        return None
    for key in BODY_KEYS:
        raw = body.get(key)
        if isinstance(raw, str):
            ref = parse_ref(raw)
            return await BODY_RESOLVERS[key](session, ref)
    return None


async def request_project(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """İsteğin PROJESİ (yol → gövde sırasıyla); istek başına BİR kez çözülür (önbellekli)."""
    if SCOPE_KEY in request.scope:
        return request.scope[SCOPE_KEY]
    project_id = await resolve_project(session, request.url.path, request.path_params)
    if project_id is None:
        project_id = await _body_project(session, request)
    request.scope[SCOPE_KEY] = project_id
    return project_id


__all__ = ["RESOLVERS", "request_project", "resolve_project"]
