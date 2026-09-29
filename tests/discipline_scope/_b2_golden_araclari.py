"""DSC-B2 golden araçları: yazma sonrası DB durum dökümü + yeni audit satırları.

Döküm SIRALI ve DETERMİNİSTİKTİR: satırlar, bilinmeyen (rastgele) UUID'leri `<?>` ile maskelenmiş
metne göre sıralanır; sonra tek `normalize` çağrısı bilinmeyen UUID'leri ilk görünme sırasıyla
numaralar. Rastgele kimlikli revizyon/yaprak/gün-satırı gibi satırlar dünya etiketlerine
bağlanır (`rastgele_kimlikleri_etiketle`) ki sıralama bağı kalmasın.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base
from app.modules.audit.models import AuditLog
from app.modules.earned_value.models import EvBaselineLeaf, EvDayRow, EvRevision
from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._golden import normalize

_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_DUZ_TABLOLAR = frozenset(
    {
        "boq_groups",
        "boq_items",
        "boq_item_section_allocations",
        "site_diary_entries",
        "site_diary_lines",
        "site_diary_worker_counts",
    }
)


def dokum_tablolari() -> list[str]:
    """Yazma uçlarının dokunabildiği tablolar (ev_* + BOQ + günlük), ada göre sıralı."""
    adlar = {t for t in Base.metadata.tables if t.startswith("ev_") or t in _DUZ_TABLOLAR}
    return sorted(adlar)


def _duz(deger: object) -> object:
    if isinstance(deger, uuid.UUID | Decimal):
        return str(deger)
    if isinstance(deger, datetime | date):
        return deger.isoformat()
    return deger


async def rastgele_kimlikleri_etiketle(session: AsyncSession, d: Dunya) -> None:
    """Rastgele UUID'li satırları (revizyon, baseline yaprağı, gün satırı) ada bağla."""
    revizyonlar = (
        await session.execute(select(EvRevision).where(EvRevision.site_id == d.santiye.id))
    ).scalars()
    numara = {}
    for rev in revizyonlar:
        d.etiketler[rev.id] = f"<rev:{rev.number}>"
        numara[rev.id] = rev.number
    for lf in (await session.execute(select(EvBaselineLeaf))).scalars():
        bolum = "none" if lf.section_id is None else lf.section_id
        d.etiketler[lf.id] = f"<bleaf:r{numara.get(lf.revision_id, '?')}:{lf.boq_item_id}:{bolum}>"
    for satir in (await session.execute(select(EvDayRow))).scalars():
        ref = satir.personnel_id or satir.subcontractor_id
        d.etiketler[satir.id] = f"<gunsatiri:{satir.day}:{ref}>"


async def audit_kimlikleri(session: AsyncSession) -> set[uuid.UUID]:
    return set((await session.execute(select(AuditLog.id))).scalars())


async def yeni_audit(session: AsyncSession, once: set[uuid.UUID]) -> list[dict[str, object]]:
    satirlar = (await session.execute(select(AuditLog))).scalars()
    return [
        {
            "action": getattr(s.action, "value", s.action),
            "detail": s.detail,
            "actor": _duz(s.actor_user_id),
        }
        for s in satirlar
        if s.id not in once
    ]


def _maske(satir: dict[str, object], etiketler: dict[uuid.UUID, str]) -> str:
    ham = json.dumps(satir, ensure_ascii=False, sort_keys=True, default=str)
    return _UUID.sub(
        lambda m: etiketler.get(uuid.UUID(m.group(0)), "<?>"),
        ham,
    )


async def durum_dokumu(session: AsyncSession, d: Dunya) -> dict[str, list[dict[str, object]]]:
    """Tablo → sıralı satırlar (tüm kolonlar). Boş tablolar atlanır."""
    sonuc: dict[str, list[dict[str, object]]] = {}
    for tablo in dokum_tablolari():
        satirlar = (await session.execute(text(f'SELECT * FROM "{tablo}"'))).mappings().all()
        duz = [{k: _duz(v) for k, v in s.items()} for s in satirlar]
        if duz:
            sonuc[tablo] = sorted(duz, key=lambda s: _maske(s, d.etiketler))
    return sonuc


def anlik_goruntu(
    d: Dunya,
    yanit: dict[str, object],
    dokum: dict[str, list[dict[str, object]]],
    audit: list[dict[str, object]],
) -> object:
    """Tek `normalize`: yanıt + döküm + audit aynı `<uuid:N>` numaralamasını paylaşır."""
    audit = sorted(audit, key=lambda s: _maske(s, d.etiketler))
    return normalize({"audit": audit, "db": dokum, "yanit": yanit}, d.etiketler)
