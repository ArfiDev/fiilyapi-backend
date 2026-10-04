"""DSC-B2 test yardımcıları: yanıt özeti, boş grup kurucusu, ham satır dökümü."""

from __future__ import annotations

import uuid

from httpx import Response
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup
from app.modules.earned_value.models import EvGroupDiscipline, EvRevision, RevisionStatus
from tests._disiplin_dunyasi import Dunya, _kimlik

YOK_KIMLIK = uuid.UUID(int=0xD15C)
YETKI_YOK = {"detail": "Bu işlem için yetkiniz yok"}
#: SIL-B1: silme kapısı (Sistem Yöneticisi) — rol anahtarı, seviye değil.
SISYON_ONLY = {"detail": "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"}


def ozet(resp: Response) -> tuple[int, str | None, object]:
    """Durum + content-type + gövde: "yabancı == olmayan" karşılaştırmasının birimi."""
    ham = resp.json() if resp.content else None
    return resp.status_code, resp.headers.get("content-type"), ham


async def bos_grup_ac(
    session: AsyncSession, d: Dunya, ad: str, sira: int, disiplin_id: uuid.UUID | None
) -> BoqGroup:
    """AKTİF revizyonda `disiplin_id`ye eşli (None → eşlemesiz) KALEMSİZ grup. Görünürlük
    R(site)=aktif ?? taslak'a bakar; taslak eşlemesi görünürlüğü değiştirmez."""
    grup = BoqGroup(id=_kimlik(15, sira), site_id=d.santiye.id, name=ad, sort_order=sira)
    session.add(grup)
    await session.flush()
    if disiplin_id is not None:
        aktif = (
            await session.execute(
                select(EvRevision.id).where(
                    EvRevision.site_id == d.santiye.id, EvRevision.status == RevisionStatus.ACTIVE
                )
            )
        ).scalar_one()
        session.add(
            EvGroupDiscipline(revision_id=aktif, boq_group_id=grup.id, discipline_id=disiplin_id)
        )
        await session.flush()
    d.etiketler[grup.id] = f"<{ad}>"
    return grup


async def ham_satirlar(session: AsyncSession, tablo: str, kosul: str, **kw: object) -> list:
    """Ham `SELECT *` (ORM önbelleğini atlar): bayt bayt öncesi/sonrası kıyası için."""
    sonuc = await session.execute(text(f'SELECT * FROM "{tablo}" WHERE {kosul} ORDER BY id'), kw)
    return [tuple(r) for r in sonuc.all()]
