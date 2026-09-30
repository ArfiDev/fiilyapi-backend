"""ILR-1/2 — proje KARTININ iki ilerleme alani, TOPLU ve IZNE DUYARLI.

`cost_cards` emsalinin kardesi: kart turevleri PROJE BASINA sorgu ACMAZ.

🔴 **IKI ALAN, IKI KAYNAK, KASTEN AYRISIR:**
  * `physical` — GONDERILMIS santiye gunlugunden (`boq.progress`), sahada fiilen
    ne imal edildigi; ANINDA gunceldir.
  * `financial` — ONAYLANMIS ISVEREN hakedisinden (`progress_payments`), ne
    kadarinin onaylandigi; fizikselin GERISINDE kalir.
Aradaki fark yonetimin baktigi asil sayidir (fiziksel %60 · mali %35 → hakedis
gecikmis ya da uyusmazlik var). Bir "ortalama ilerleme" URETILMEZ.

🔴 **IZIN AYRI OLCULUR:** bir rol gunlugu okuyup hakedisi okuyamayabilir (ya da
tersi). Tek bir "ilerleme izni" YOKTUR; her alan KENDI modulunun kapisina
bakar, yoksa biri otekini sizdirirdi.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import DisciplineScope
from app.core.permissions import can_read
from app.modules.boq import progress as boq_progress
from app.modules.progress_payments import project_progress
from app.modules.projects.models import Project
from app.modules.projects.schemas import MetricPlaceholder, metric, restricted
from app.modules.users.models import User

_SITE_DIARY = "site_diary"
_PROGRESS_PAYMENTS = "progress_payments"


@dataclass(frozen=True)
class CardProgress:
    physical: MetricPlaceholder
    financial: MetricPlaceholder


def _empty() -> CardProgress:
    """Fail-closed varsayilan: izin OLCULMEDIYSE iki alan da KAPALI dogar."""
    return CardProgress(physical=restricted(), financial=restricted())


EMPTY = _empty()


async def by_projects(
    session: AsyncSession, actor: User, projects: list[Project], scope: DisciplineScope
) -> dict[uuid.UUID, CardProgress]:
    """Proje -> iki ilerleme zarfi. En fazla IKI toplu sorgu ailesi acar.

    `scope` ZORUNLU (DSC-B4): YALNIZ fiziksel % suzulur (kendi disiplininin kalemleri, pay ve
    payda kendi); mali ilerleme (hakedis) suzulmez — Ü3."""
    project_ids = [p.id for p in projects]
    if not project_ids:
        return {}

    gunluk_izni = await can_read(session, actor, _SITE_DIARY)
    hakedis_izni = await can_read(session, actor, _PROGRESS_PAYMENTS)

    fiziksel = (
        await boq_progress.physical_for_projects(session, project_ids, scope) if gunluk_izni else {}
    )
    mali = (
        await project_progress.financial_for_projects(session, project_ids) if hakedis_izni else {}
    )

    return {
        pid: CardProgress(
            physical=metric(fiziksel[pid], _SITE_DIARY) if gunluk_izni else restricted(),
            financial=metric(mali[pid], _PROGRESS_PAYMENTS) if hakedis_izni else restricted(),
        )
        for pid in project_ids
    }
