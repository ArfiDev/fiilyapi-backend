"""DET-1.B — salt okunur günlük detayının BAĞLAMI: adlar, gün kilidi, önceki/sonraki.

Bölüm Detay › Günlük Kayıt'tan açılan detay sayfası TEK istekte kurulsun diye yanıta
eklenen alanların kaynağı. Her alan kayıt başına SABİT sayıda toplu sorguyla gelir
(ad sözlükleri `IN (…)`, komşular iki `LIMIT 1`) — satır başına sorgu yoktur.

Gün kilidi `app.core.day_hooks` portundan okunur: çekirdek günlük kilit sağlayıcı modülü
(EV) İMPORT ETMEZ; port boşsa (modülsüz kurulum) kayıt kilitsizdir. Bu yüzden kilit, EV
izni olmayan günlük görüntüleyicisine de ulaşır.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import day_hooks
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.site_diary import repository
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine


@dataclass(frozen=True, slots=True)
class DetailContext:
    section_names: dict[uuid.UUID, str]
    subcontractor_names: dict[uuid.UUID, str]
    user_names: dict[uuid.UUID, str]
    locked: bool
    lock_report_date: date | None
    prev: repository.Neighbour | None
    next: repository.Neighbour | None

    def section_name(self, section_id: uuid.UUID | None) -> str | None:
        return None if section_id is None else self.section_names.get(section_id)

    def subcontractor_name(self, subcontractor_id: uuid.UUID | None) -> str | None:
        return None if subcontractor_id is None else self.subcontractor_names.get(subcontractor_id)

    def user_name(self, user_id: uuid.UUID | None) -> str | None:
        return None if user_id is None else self.user_names.get(user_id)


async def load(
    session: AsyncSession,
    entry: SiteDiaryEntry,
    section_context: uuid.UUID | None,
    lines: Sequence[SiteDiaryLine],
    scope: DisciplineScope = UNRESTRICTED,
) -> DetailContext:
    """`section_context` = önceki/sonraki için bölüm bağlamı (Kural A); `None` = şantiye.

    Bölümün şantiyeye aitliğini ÇAĞIRAN doğrular (`service.validate_section`).

    DSC-B1: `lines` = kullanıcıya GÖRÜNEN satırlar; bölüm adları YALNIZ bunlardan çözülür
    (görünmeyen satırın bölüm adı sızmaz). `scope` komşu (önceki/sonraki) kümesini süzer.
    """
    sections = {entry.section_id, *(line.section_id for line in lines)} - {None}
    firms = {row.subcontractor_id for row in entry.worker_counts} - {None}
    users = {entry.created_by, entry.submitted_by_user_id} - {None}
    locks = await day_hooks.day_locks(session, entry.site_id, [entry.entry_date])
    prev, next_ = await repository.neighbours(
        session, entry.site_id, entry.entry_date, section_context, scope
    )
    return DetailContext(
        section_names=await repository.section_names(session, sections),
        subcontractor_names=await repository.subcontractor_names(session, firms),
        user_names=await repository.user_names(session, users),
        locked=bool(locks),
        lock_report_date=locks[0].report_date if locks else None,
        prev=prev,
        next=next_,
    )
