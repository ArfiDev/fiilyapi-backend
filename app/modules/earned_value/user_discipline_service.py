"""Kullanici → disiplin atamasi servisi (DSC-B0, spec Ü9/Ü10).

TAM DEGISTIRME: verilen kume son durumdur; bos kume = kisitsiz. Kullanici satiri
`FOR UPDATE` ile KILITLENIR (`users.repository.get_user_locked`, proje erisimi emsali) —
ayni kullanicinin atamalarini yazan iki istek serilesir. Kilit olmadan iki PUT birbirinin
silme/ekleme arasina girip iki kumenin KARISIMINI birakabilirdi.

FARK UYGULANIR (sil-yeniden-ekle DEGIL): yalniz cikarilan satirlar silinir, yalniz eklenen
satirlar yazilir. Gerekce: degismeyen atamanin `created_at`i korunur, ayni kumeyi yeniden
gondermek hic `user_disciplines` yazmasi uretmez (`changed=False` → router DENETIM satirini
da atlar) ve FK denetimi gereksiz yere tekrarlanmaz. Kilit altinda
oldugu icin tutarlilik sil-ekleden farksizdir.

Ü10: kendine / admin'e atama ENGELLENMEZ (ekran uyarir).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_ref import DisciplineRef
from app.core.errors import NotFoundError
from app.modules.earned_value import guards
from app.modules.earned_value.models import EvDiscipline, UserDiscipline
from app.modules.users import repository as users_repository
from app.modules.users.models import User

USER_MISSING = "Kullanıcı bulunamadı"  # users/service.py ile ayni "yok" govdesi


@dataclass(frozen=True, slots=True)
class DisciplineAssignment:
    """Atama sonucu: hedef kullanici + son disiplin kumesi (id → kod)."""

    user: User
    refs_by_id: dict[uuid.UUID, DisciplineRef]
    changed: bool = False  # PUT kumeyi degistirdi mi (degismediyse denetim yazilmaz)

    @property
    def discipline_ids(self) -> list[uuid.UUID]:
        return sorted(self.refs_by_id, key=str)

    @property
    def disciplines(self) -> list[DisciplineRef]:
        """`discipline_ids` ile AYNI sira."""
        return [self.refs_by_id[i] for i in self.discipline_ids]

    @property
    def codes(self) -> list[str]:
        return sorted(ref.code for ref in self.refs_by_id.values())


async def _assigned_ids(session: AsyncSession, user_id: uuid.UUID) -> set[uuid.UUID]:
    rows = await session.execute(
        select(UserDiscipline.discipline_id).where(UserDiscipline.user_id == user_id)
    )
    return set(rows.scalars())


async def _refs(session: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, DisciplineRef]:
    if not ids:
        return {}
    rows = await session.execute(select(EvDiscipline).where(EvDiscipline.id.in_(ids)))
    return {row.id: DisciplineRef.model_validate(row) for row in rows.scalars()}


async def _assert_all_exist(
    session: AsyncSession, ids: set[uuid.UUID]
) -> dict[uuid.UUID, DisciplineRef]:
    """Bilinmeyen disiplin → 404 (FK RESTRICT'e carpip 409'a dusmesin); hangileri govdede."""
    refs = await _refs(session, ids)
    missing = sorted(ids - refs.keys(), key=str)
    if missing:
        raise NotFoundError(f"{guards.DISCIPLINE_MISSING}: {', '.join(map(str, missing))}")
    return refs


async def get_assignment(session: AsyncSession, user_id: uuid.UUID) -> DisciplineAssignment:
    user = await users_repository.get_user(session, user_id)
    if user is None:
        raise NotFoundError(USER_MISSING)
    return DisciplineAssignment(user, await _refs(session, await _assigned_ids(session, user_id)))


async def replace_assignment(
    session: AsyncSession, user_id: uuid.UUID, discipline_ids: list[uuid.UUID]
) -> DisciplineAssignment:
    """Kullanici satirini kilitler, kumeyi dogrular, farki uygular (atomik: hata → hicbir
    satir degismez, cunku dogrulama yazmadan ONCE)."""
    user = await users_repository.get_user_locked(session, user_id)
    if user is None:
        raise NotFoundError(USER_MISSING)
    wanted = set(discipline_ids)  # yinelenenler tekillesir
    refs = await _assert_all_exist(session, wanted)
    current = await _assigned_ids(session, user_id)
    removed = current - wanted
    if removed:
        await session.execute(
            delete(UserDiscipline).where(
                UserDiscipline.user_id == user_id, UserDiscipline.discipline_id.in_(removed)
            )
        )
    session.add_all(UserDiscipline(user_id=user_id, discipline_id=d) for d in wanted - current)
    await session.flush()
    return DisciplineAssignment(user, refs, changed=bool(removed or wanted - current))
