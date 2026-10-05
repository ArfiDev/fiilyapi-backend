"""BC-3 bağ iş kuralları — sahip-parametrik, İKİ KATMANLI koruma.

Sahibin izni router'da YETKİYİ verir (`OwnerSpec.permission_module`), bu modül
`projects.service.visible_projects` ile KAPSAMI belirler: görünmeyen projedeki
gerçek sahip ile var olmayan sahip AYNI 404 gövdesini (sahibin kendi cümlesi)
döner. Bağ satırının görünürlüğü SAHİBİNİNKİYLE AYNIDIR — ayrı kapı YOKTUR
(equipment `_visible_document` deseni).

## Kapı sırası (`attach`) — SABİT

1. sahip görünür mü → 404 (sahibin cümlesi)
2. `type_id` var mı VE bu bölmeye mi ait → 422 `SLOT_TYPE_INVALID`
3. `document_id` var mı VE künyesinin `project_id`si sahibin türetilen projesi
   mi → 422 `DOCUMENT_NOT_IN_SCOPE`

(2) DB'de de bileşik FK ile korunur; (3) YALNIZ buradadır — `documents.project_id`
ile bağ tablosu arasında DB kısıtı yoktur (bağ tablosu `project_id` kopyalamaz),
bu yüzden kapsam eşitliği servisin TEK sorumluluğudur ve bekçisi
`tests/documents/test_bc3_links_api.py`dedir.

## 🔴 BU DOSYANIN COVERAGE SAYISI YALAN SÖYLÜYOR — kod ÇALIŞIYOR

Araç bu dosya için **%42** raporluyor ve fonksiyon gövdelerinin çoğunu
"kapsanmamış" gösteriyor. ÖLÇÜLDÜ (2026-09-05): o satırlara `raise` enjekte
edildiğinde **32 test kırmızıya döndü**, geri alınınca `54 passed`. Ayrıca
mutasyonlar (proje eşitliği · slot bölmesi · IDOR kapısı) tam o satırları
öldürüyor. Kök sebep: `[tool.coverage.run]` bölümünde **`concurrency` ayarı
YOK**, SQLAlchemy asyncio ise greenlet kullanıyor — aynı körlük repo genelinde
(`equipment/document_service` %48 · `contracts/service` %51 · `sales/service`
%55 · `inventory/service` %50).
🔴 **Bu satırlar ÖLÜ KOD DEĞİLDİR; coverage'a bakıp silmeyin.** Küresel
`--cov-fail-under=80` bu dosya için KANIT DEĞİLDİR; kanıt mutasyondur.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DocumentValidationError, NotFoundError
from app.modules.documents import link_guards as guards
from app.modules.documents import link_repository as repository
from app.modules.documents.link_owners import OwnerContext, OwnerSpec
from app.modules.documents.link_repository import LinkRow
from app.modules.documents.link_schemas import EntityDocumentLinkCreate, EntityDocumentLinkUpdate
from app.modules.documents.models.links import EntityDocumentScope, EntityDocumentType
from app.modules.documents.service import _BELGE_GORUR
from app.modules.projects.service import visible_projects
from app.modules.users.models import User


async def list_slot_types(
    session: AsyncSession, scope: EntityDocumentScope | None
) -> list[EntityDocumentType]:
    """`GET /documents/slot-types` — okuma `documents=view` yeter (hiçbir rol
    `none` değildir), CRUD ucu YOK."""
    return await repository.list_slot_types(session, scope)


async def _visible_owner(
    session: AsyncSession, actor: User, spec: OwnerSpec, owner_id: uuid.UUID
) -> OwnerContext:
    """Sahip → proje; proje görünmüyorsa YA DA sahip yoksa AYNI 404."""
    context = await repository.get_owner_context(session, spec, owner_id)
    if context is None:
        raise NotFoundError(spec.owner_missing)
    visible = await visible_projects(session, actor, pairs=_BELGE_GORUR)
    if not any(p.id == context.project_id for p in visible):
        raise NotFoundError(spec.owner_missing)
    return context


async def _visible_link(
    session: AsyncSession, actor: User, spec: OwnerSpec, link_id: uuid.UUID
) -> tuple[LinkRow, OwnerContext]:
    row = await repository.get_link_row(session, spec, link_id)
    if row is None:
        raise NotFoundError(guards.LINK_MISSING)
    owner_id = getattr(row.link, spec.owner_column.key)
    try:
        context = await _visible_owner(session, actor, spec, owner_id)
    except NotFoundError as exc:
        # Sahip görünmüyorsa bağ da "yok"tur; cümle BAĞIN cümlesidir ki uç,
        # sahibin varlığını sızdırmasın.
        raise NotFoundError(guards.LINK_MISSING) from exc
    return row, context


async def list_links(
    session: AsyncSession, actor: User, spec: OwnerSpec, owner_id: uuid.UUID
) -> list[LinkRow]:
    await _visible_owner(session, actor, spec, owner_id)
    return await repository.list_links(session, spec, owner_id)


async def attach(
    session: AsyncSession,
    actor: User,
    spec: OwnerSpec,
    owner_id: uuid.UUID,
    data: EntityDocumentLinkCreate,
) -> tuple[LinkRow, str]:
    """Kapı sırası modül docstring'inde. Dönüş: (satır, denetim metni)."""
    context = await _visible_owner(session, actor, spec, owner_id)

    doc_type = await repository.get_slot_type(session, data.type_id, spec.scope)
    if doc_type is None:
        raise DocumentValidationError(guards.SLOT_TYPE_INVALID)

    document = await repository.get_document(session, data.document_id)
    if document is None or document.project_id != context.project_id:
        raise DocumentValidationError(guards.DOCUMENT_NOT_IN_SCOPE)

    link = spec.link_model(
        type_id=doc_type.id,
        scope=spec.scope,
        document_id=document.id,
        issued_at=data.issued_at,
        valid_until=data.valid_until,
        note=data.note,
    )
    setattr(link, spec.owner_column.key, owner_id)
    session.add(link)
    await session.flush()
    await session.refresh(link)

    detail = f"{spec.label} belgesi bağlandı: {context.display} · {doc_type.name}"
    return LinkRow(link=link, doc_type=doc_type, document=document), detail


async def update(
    session: AsyncSession,
    actor: User,
    spec: OwnerSpec,
    link_id: uuid.UUID,
    data: EntityDocumentLinkUpdate,
) -> tuple[LinkRow, str | None]:
    row, context = await _visible_link(session, actor, spec, link_id)
    degisiklik = data.model_dump(exclude_unset=True)
    if not degisiklik:
        # 🔴 BOŞ GÖVDE (`PATCH {}`) DENETİM SATIRI YAZMAZ. Yazsaydı denetim
        # günlüğü hiçbir alanın değişmediği "güncellendi" satırlarıyla dolar ve
        # gerçek değişikliğin izi bu gürültüde kaybolurdu. 200 döner (istek
        # geçerlidir, sonucu no-op'tur) ama `detail` NULL'dır.
        return row, None
    for field, value in degisiklik.items():
        setattr(row.link, field, value)
    await session.flush()
    await session.refresh(row.link)
    detail = f"{spec.label} belgesi güncellendi: {context.display} · {row.doc_type.name}"
    return row, detail


async def detach(session: AsyncSession, actor: User, spec: OwnerSpec, link_id: uuid.UUID) -> str:
    """Bağı siler; ARŞİVDEKİ DOSYAYA DOKUNMAZ (dosya silme `documents` admin
    kapısındadır)."""
    row, context = await _visible_link(session, actor, spec, link_id)
    await session.delete(row.link)
    await session.flush()
    return f"{spec.label} belgesi bağı kaldırıldı: {context.display} · {row.doc_type.name}"
