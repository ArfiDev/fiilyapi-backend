"""Teklif servisi (TKL-B4.1: yalniz ayar). Servis COMMIT ETMEZ — tek commit istek sonunda."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.offers.models import OfferSettings
from app.modules.offers.schemas import OfferSettingsUpdate


async def _get_or_create_settings(session: AsyncSession, *, lock: bool) -> OfferSettings:
    """Tekil ayar satiri. Migration tohumlar; tohumsuz ortamda (test semasi) `DO NOTHING` ile
    yarisa dayanikli olusturulur. `lock=True` satiri `FOR UPDATE` kilitler (eszamanli PUT'lar
    sirayla yazar, son yazan kalir) ve TAZE okur."""
    stmt = select(OfferSettings).where(OfferSettings.only_row.is_(True))
    if lock:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    row = await session.scalar(stmt)
    if row is None:
        await session.execute(
            pg_insert(OfferSettings.__table__)
            .values(id=uuid.uuid4())
            .on_conflict_do_nothing(index_elements=["only_row"])
        )
        row = await session.scalar(stmt)
    if row is None:  # pragma: no cover - DO NOTHING yarisi kaybeden yine satiri gorur
        raise RuntimeError("Teklif ayar satırı oluşturulamadı")
    return row


async def get_settings(session: AsyncSession) -> OfferSettings:
    return await _get_or_create_settings(session, lock=False)


async def update_settings(
    session: AsyncSession, data: OfferSettingsUpdate
) -> tuple[OfferSettings, dict[str, Any]]:
    """Tam degistirir; `(satir, ESKI degerler)` doner (denetim `eski → yeni` farki icin). Eski
    degerler satir KILIT ALTINDA okunduktan sonra alinir (eszamanli PUT'ta dogru onceki deger)."""
    row = await _get_or_create_settings(session, lock=True)
    before = {field: getattr(row, field) for field in data.model_dump()}
    for field, value in data.model_dump().items():
        setattr(row, field, value)
    await session.flush()
    await session.refresh(row)
    return row, before
