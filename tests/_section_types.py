"""Bolum tipi tohumu (BLF-B1.1).

Testler semayi `create_all` ile kurar, migration KOSMAZ — yani migration'in tohumladigi
7 bolum tipi testte YOKTUR. Eski enum degerleri bu yardimciyla ayni adlarla eklenir
(frontend `section-labels.ts` ve migration tohumuyla BIREBIR)."""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.sites.models import SectionType

#: Eski enum degeri -> (gorunen ad, sort_order).
SEED_SECTION_TYPES: dict[str, tuple[str, int]] = {
    "foundation_infra": ("Temel & Altyapı", 1),
    "structural": ("Kaba İnşaat", 2),
    "finishing": ("İnce İşler", 3),
    "facade_roof": ("Cephe & Çatı", 4),
    "mep": ("Mekanik / Elektrik", 5),
    "landscape": ("Peyzaj", 6),
    "handover": ("Teslimat & Kabul", 7),
}


#: Eski enum degeri -> SABIT kimlik (uuid5). Testler govde sabitlerinde (`PUBLISHED_PAYLOAD`)
#: tipi fixture'siz basabilsin diye kimlikler deterministiktir; yalniz test tohumunda gecerlidir.
SEED_TYPE_IDS: dict[str, uuid.UUID] = {
    key: uuid.uuid5(uuid.NAMESPACE_URL, f"fiil/section-type/{key}") for key in SEED_SECTION_TYPES
}


async def seed_section_types(session: AsyncSession) -> dict[str, SectionType]:
    """7 tipi ekler (flush, commit YOK); anahtar eski enum degeri. IDEMPOTENTTIR:
    ayni test icinde birden cok kez (ornegin iki santiye kuran yardimci) cagrilabilir."""
    rows: dict[str, SectionType] = {}
    for key, (name, order) in SEED_SECTION_TYPES.items():
        existing = await session.get(SectionType, SEED_TYPE_IDS[key])
        if existing is None:
            existing = SectionType(id=SEED_TYPE_IDS[key], name=name, sort_order=order)
            session.add(existing)
        rows[key] = existing
    await session.flush()
    return rows
