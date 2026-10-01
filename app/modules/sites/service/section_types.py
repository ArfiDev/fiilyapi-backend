"""BLF-B1 — sirket geneli bolum tipi listesi: okuma + ekleme.

Silme / yeniden adlandirma YOKTUR (spec). Tekillik KATALOG-UQ deseniyle: normalize
anahtar (`SectionType.name_key`, uygulama turetir) + DB `uq_section_types_name_key`.
Servis once SELECT'le alana ozel 409 metnini uretir; yarista (iki es zamanli ekleme)
UQ'ya carpan taraf SAVEPOINT icinde yakalanir, kazanan satir yeniden okunur ve AYNI
409 doner — genel "Veri butunlugu hatasi" degil.
"""

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import SectionTypeTakenError
from app.core.labels import normalize_label
from app.modules.sites import guards, repository
from app.modules.sites.models import SectionType
from app.modules.sites.schemas import SectionTypeCreate


async def list_section_types(session: AsyncSession) -> list[SectionType]:
    return await repository.list_section_types(session)


async def _assert_name_free(session: AsyncSession, name: str) -> None:
    taken = await repository.get_section_type_by_key(session, normalize_label(name))
    if taken is not None:
        raise SectionTypeTakenError(
            guards.SECTION_TYPE_TAKEN_AS.format(name=taken.name),
            existing_id=taken.id,
            existing_name=taken.name,
        )


async def create_section_type(session: AsyncSession, data: SectionTypeCreate) -> SectionType:
    await _assert_name_free(session, data.name)
    section_type = SectionType(
        name=data.name, sort_order=await repository.next_section_type_sort_order(session)
    )
    try:
        # SAVEPOINT: UQ ihlali oturumu bozmaz, asagida kazanan satir okunabilir.
        async with session.begin_nested():
            session.add(section_type)
            await session.flush()
    except IntegrityError:
        await _assert_name_free(session, data.name)
        raise
    return section_type
