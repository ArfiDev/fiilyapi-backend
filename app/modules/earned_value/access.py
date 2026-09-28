"""Planlama (EV) izin kapilari ve santiye kapsami (PLANLAMA-SPEC §3.8 K17, §3.9 B1-8).

Seviye eslemesi `progress_payments` emsalidir (`AccessLevel` sirali:
none < view < draft < request < approve < full < admin):

| kapi        | seviye  | ne acar |
|-------------|---------|---------|
| `VIEW`      | view    | butun okumalar, onizleme |
| `WRITE`     | draft   | ayarlar, butce girdileri (oran/esleme/dagilim/pencere), taslak ac |
| `APPROVE`   | approve | baseline dondur, taslak sil (B3: rapor onayi + kilit acma) |
| `CATALOG`   | full    | sirket katalogu + disiplin yazma, "gerceklesen standart yap" |
| `ADMIN`     | admin   | disiplin silme (B1-9) |

Kapsam maskesi BAGLANMAZ (adam-saat para degil): router duz `APIRoute`dir,
`roles/service.py` limited/finance atamasini bu modulde zaten reddeder.
Santiye gorunurlugu iki katmanlidir (`site_planning/service.py` deseni): izin YETKIYI,
`visible_projects` KAPSAMI verir; gorunmeyen santiye ile olmayan santiye AYNI 404'u alir.

## Tamamlanmis santiye SALT OKUNUR (§3.10 F0-8, §3.11 B1-12) — kural TEK yerde
* `assert_site_writable` — her YAZMA servisinin girisi. Santiye satirini `FOR UPDATE`
  kilitler ve `status`u kilit ALTINDA SUTUN sorgusuyla yeniden okur (`ctx.site` istegin
  basinda yuklenmis bir kopyadir; `session.get`/`select(Site)` kimlik haritasindaki bayat
  nesneyi dondururdu). Metin ekrana gore parametredir (`guards.SITE_COMPLETED_*`).
* `completed_site_guard(metin)` — yazma UCLARININ bagimliligi: 404 (gorunmeyen) → 409
  (tamamlanmis) sirasini GOVDE DOGRULAMASINDAN ONCE verir. 📏 fastapi 0.141.1'de olculdu:
  rota `dependencies` → imzadaki `Depends` → path/query → govde. Bozuk JSON (ayristirilamayan
  metin) bagimliliklardan once 422'dir; sema-gecersiz govde 409 alir. Bagimlilik kilit
  ALMAZ ve ek sorgu yapmaz (gorunurluk sorgusunun yukledigi durumu okur); yetkili denetim
  servisteki kilitli yeniden okumadir. Donen `SiteContext` ucun `visible_site` cagrisinin
  yerine gecer.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated, NamedTuple

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.errors import ConflictError, NotFoundError
from app.core.permissions import require_permission
from app.modules.projects.models import Project
from app.modules.projects.service import visible_projects
from app.modules.sites import repository as sites_repository
from app.modules.sites.guards import SITE_MISSING
from app.modules.sites.models import Site, SiteStatus
from app.modules.users.models import User

PERMISSION_MODULE = "earned_value"

VIEW = require_permission(PERMISSION_MODULE, AccessLevel.view)
WRITE = require_permission(PERMISSION_MODULE, AccessLevel.draft)
APPROVE = require_permission(PERMISSION_MODULE, AccessLevel.approve)
CATALOG = require_permission(PERMISSION_MODULE, AccessLevel.full)
ADMIN = require_permission(PERMISSION_MODULE, AccessLevel.admin)


class SiteContext(NamedTuple):
    site: Site
    project: Project


async def visible_site(session: AsyncSession, actor: User, site_id: uuid.UUID) -> SiteContext:
    site = await sites_repository.get_site(session, site_id)
    if site is None:
        raise NotFoundError(SITE_MISSING)
    visible = await visible_projects(session, actor)
    project = next((p for p in visible if p.id == site.project_id), None)
    if project is None:
        raise NotFoundError(SITE_MISSING)
    return SiteContext(site=site, project=project)


def is_site_completed(status: SiteStatus | None) -> bool:
    """TEK kural: tamamlanmis santiye salt okunurdur (yazma 409, ekran `editable=false`)."""
    return status is SiteStatus.completed


def _refuse_completed(status: SiteStatus | None, message: str) -> None:
    if is_site_completed(status):
        raise ConflictError(message)


async def assert_site_writable(
    session: AsyncSession, site_id: uuid.UUID, *, message: str, lock: bool = True
) -> None:
    """Tamamlanmis santiyede yazma → 409 `message`. Durum VERITABANINDAN okunur.

    `lock=True` (varsayilan): satir `FOR UPDATE` kilitlenir ve durum AYNI sorguda kilit
    altinda okunur — santiyeyi "tamamlandi"ya ceken esanli guncelleme ya bizden once biter
    (onu goruruz) ya da bizim transaction'imiz bitene kadar bekler. Bekci:
    `tests/earned_value_budget/test_b30_relock_guard.py`.
    """
    stmt = select(Site.status).where(Site.id == site_id)
    if lock:
        stmt = stmt.with_for_update()
    _refuse_completed(await session.scalar(stmt), message)


def completed_site_guard(message: str) -> Callable[..., Awaitable[SiteContext]]:
    """Yazma ucu bagimliligi: `site_id` → 404 (gorunmeyen) → 409 (tamamlanmis) → `SiteContext`."""

    async def _guard(
        site_id: uuid.UUID,
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> SiteContext:
        context = await visible_site(session, user, site_id)
        _refuse_completed(context.site.status, message)
        return context

    return _guard
