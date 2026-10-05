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
başka projeye "taşınıyormuş" gibi o projenin rolüyle okunup yazılamaz); P == Q → P. Bilinen kapsam:
`RESOLVERS` dışı PATCH/PUT uçları kayıt çözücüsü almadıkça (IZN-B4d/B5) gövdeden bağlam alır.
Slug (`/projects/{slug}`, `/sites/{slug}` …) ÇÖZÜLÜR: UUID ile slug aynı projeyi verir.
Slug kapsamı tekil değilse (şantiye / bölüm slug'ı proje / şantiye içinde tekildir)
ve birden çok projede eşleşirse bağlam ÇÖZÜLMEZ
(`None`): ekip rolü sayılmaz, kesin karar `visible_projects` süzgecine kalır (fail-closed).

`None` = proje bağlamı YOK ya da çözülemedi (liste ucu, bilinmeyen/belirsiz kayıt). Yeni bir
proje bağlamlı uç eklenip çözücü yazılmazsa `tests/core/test_proje_baglami_cozucu_bekcisi.py`
kırmızı verir.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from types import UnionType
from typing import Union, get_args, get_origin

from fastapi import Request
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

    if not isinstance(ref, uuid.UUID):
        return None
    # Şirket geneli fatura `project_id IS NULL` → `None` (birleşim, fail-closed).
    return await _unique(session, select(Invoice.project_id).where(Invoice.id == ref))


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
    # IZN-B4b onarımı: fatura + ödeme alt yolu (`/invoices/{id}/payments`) faturanın projesinde.
    ("/invoices", "invoice_id"): _invoice_project,
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


async def _body_projects(session: AsyncSession, request: Request) -> list[uuid.UUID | None]:
    """JSON gövdesinde, rotanın gövde modelinin ÜST DÜZEYDE bildirdiği `project_id`/`site_id`
    anahtarlarının çözülmüş projeleri (anahtar başına bir öğe; bildirilmemiş / dize olmayan anahtar
    sayılmaz). Gövde yok / okunamıyor / bildirim yok → boş liste."""
    if request.method not in ("POST", "PUT", "PATCH"):
        return []
    if "json" not in request.headers.get("content-type", ""):
        return []
    declared = _body_declared_keys(request)
    if not declared.intersection(BODY_KEYS):
        return []
    try:
        body = json.loads((await request.body()) or b"null")
    except (ValueError, UnicodeDecodeError):
        return []
    if not isinstance(body, dict):
        return []
    return [
        await BODY_RESOLVERS[key](session, parse_ref(body[key]))
        for key in BODY_KEYS
        if key in declared and isinstance(body.get(key), str)
    ]


async def _body_project(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """Gövdedeki bağlam (oluşturma uçları): YALNIZ rotanın gövde modeli anahtarı bildiriyorsa.
    Bildirilen anahtarlar farklı projelere çözülüyorsa / çözülemiyorsa `None` (fail-closed)."""
    projects = await _body_projects(session, request)
    distinct = set(projects)
    return next(iter(distinct)) if len(distinct) == 1 else None


async def request_project(session: AsyncSession, request: Request) -> uuid.UUID | None:
    """İsteğin PROJESİ (yol → gövde sırasıyla); istek başına BİR kez çözülür (önbellekli).

    TAŞIMA kuralı (IZN-HF1): yol çözücüsü eşleştiyse gövde YALNIZ doğrulayabilir, bağlamı
    DEĞİŞTİREMEZ. Gövde (modelin bildirdiği `project_id`/`site_id`) yol projesi P'den farklı bir
    projeye işaret ediyorsa — ya da kayıt projesizken (P yok) bir proje gösteriyorsa — sonuç `None`
    (maske: ana rol ∪ ekip rolleri birleşimi, kapı: yalnız ana rol; fail-closed). Aksi hâlde kayıt
    "taşınıyormuş" gibi hedef projenin rolüyle okunup yazılabilirdi. P == Q → P. Yol çözücüsü HİÇ
    eşleşmediyse (oluşturma uçları, `RESOLVERS` dışı yollar) gövde bağlamı geçerlidir.

    Bilinen kapsam: `RESOLVERS` dışı PATCH/PUT uçları (kayıt çözücüsü olmayanlar) gövdeden bağlam
    alır; bunlar IZN-B4d/B5'te kayıt çözücüsü kazanınca taşıma kuralına girer.
    """
    if SCOPE_KEY in request.scope:
        return request.scope[SCOPE_KEY]
    matched, project_id = await resolve_path_project(session, request.url.path, request.path_params)
    body_projects = await _body_projects(session, request)
    if matched:
        if any(body != project_id for body in body_projects):
            project_id = None  # taşıma / çelişki: bağlam çözülmez
    else:
        distinct = set(body_projects)
        project_id = next(iter(distinct)) if len(distinct) == 1 else None
    request.scope[SCOPE_KEY] = project_id
    return project_id


__all__ = ["RESOLVERS", "request_project", "resolve_path_project", "resolve_project"]
