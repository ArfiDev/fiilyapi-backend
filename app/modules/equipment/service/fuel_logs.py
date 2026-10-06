"""Yakıt kaydı (M4 · spec §2.3, §4 · K13, K14, K20 · T5).

`work_logs`in kardeşi; kapsam kapısı yine `core`dadır. K12 tavanı BURADA
YOKTUR (yakıt bir SÜRE değildir), K14 gereği `entered_by_id` gövdeden DEĞİL
oturumdan damgalanır.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import NotFoundError
from app.modules.equipment import repository
from app.modules.equipment.models import EquipmentFuelLog
from app.modules.equipment.schemas import FuelLogCreate, FuelLogUpdate
from app.modules.equipment.service.core import (
    SITE_MISSING,
    _is_visible_site,
    _visible_project_ids,
    get_equipment_or_404,
    visible_equipment,
)
from app.modules.users.models import User

FUEL_LOG_MISSING = "Yakıt kaydı bulunamadı."
"""Görünmeyen VE var olmayan kaydın TEK cümlesi — ikisi ayırt EDİLEMEZ."""


async def visible_fuel_log(
    session: AsyncSession, actor: User, log_id: uuid.UUID
) -> EquipmentFuelLog:
    """Yakıt kaydı görünürlüğünün TEK kapısı (`visible_work_log`in kardeşi).

    İKİ kapı birden: kaydın KENDİ şantiyesi görünür olmalı VE makinesi görünür
    olmalı — ikincisi olmasaydı `site_id IS NULL` bir kayıt, görünmeyen bir
    projeye atanmış makinenin varlığını ele verirdi.
    """
    log = await repository.get_fuel_log(session, log_id)
    if log is None:
        raise NotFoundError(FUEL_LOG_MISSING)
    if not await _is_visible_site(session, actor, log.site_id):
        raise NotFoundError(FUEL_LOG_MISSING)
    if not await _is_visible_site(
        session, actor, (await get_equipment_or_404(session, log.equipment_id)).site_id
    ):
        raise NotFoundError(FUEL_LOG_MISSING)
    return log


async def list_fuel_logs(
    session: AsyncSession,
    actor: User,
    *,
    equipment_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
    limit: int,
    offset: int,
) -> tuple[list[EquipmentFuelLog], int]:
    """Liste + `total` TEK kapsam kararını paylaşır (TB3 kanonu)."""
    project_ids = await _visible_project_ids(session, actor, liste=True)
    suzgecler = {
        "equipment_id": equipment_id,
        "site_id": site_id,
        "date_from": date_from,
        "date_to": date_to,
    }
    items = await repository.list_fuel_logs(
        session, project_ids, limit=limit, offset=offset, **suzgecler
    )
    total = await repository.count_fuel_logs(session, project_ids, **suzgecler)
    return items, total


async def create_fuel_log(
    session: AsyncSession, actor: User, data: FuelLogCreate
) -> tuple[EquipmentFuelLog, str]:
    """`POST /equipment/fuel-logs` — M4 kaydı.

    Sıra ÖNEMLİ (`create_work_log`in aynısı): önce görünürlük/referans (404).
    `entered_by_id` GÖVDEDE YOKTUR (K14) — oturum kullanıcısından DAMGALANIR;
    istemci başka birini "giren" gösteremez.

    🔴 `site_id` GÖNDERİLMEMİŞSE makinenin O ANKİ ataması DAMGALANIR — K9'un
    yakıt kaydındaki eşi: aksi halde her yakıt kaydı varsayılan olarak "depoda"
    doğar ve `fuel-summary`nin `site_id` süzgeci (M4:109 "aynı hedef", K4) hiçbir
    zaman eşleşmez. Açıkça `null` GÖNDEREN istek (depoda yapılan ikmal)
    damgalanmaz — `model_fields_set` bu ikisini ayırır (F-İK "touched" dersi).
    """
    equipment = await visible_equipment(session, actor, data.equipment_id)
    if not await _is_visible_site(session, actor, data.site_id):
        raise NotFoundError(SITE_MISSING)

    alanlar = data.model_dump()
    if "site_id" not in data.model_fields_set:
        alanlar["site_id"] = equipment.site_id
    log = EquipmentFuelLog(**alanlar, entered_by_id=actor.id)
    session.add(log)
    await session.flush()
    return log, f"Yakıt kaydı eklendi: {equipment.name} · {data.fuel_date} · {data.liters} lt"


async def update_fuel_log(
    session: AsyncSession, actor: User, log: EquipmentFuelLog, data: FuelLogUpdate
) -> tuple[EquipmentFuelLog, str]:
    """`PATCH /equipment/fuel-logs/{id}` — kayıt hatası düzeltilebilir."""
    degisiklikler = data.model_dump(exclude_unset=True)
    hedef_equipment_id = degisiklikler.get("equipment_id", log.equipment_id)
    hedef_site_id = degisiklikler.get("site_id", log.site_id)

    if hedef_equipment_id != log.equipment_id:
        await visible_equipment(session, actor, hedef_equipment_id)
    if "site_id" in degisiklikler and not await _is_visible_site(session, actor, hedef_site_id):
        raise NotFoundError(SITE_MISSING)

    for alan, deger in degisiklikler.items():
        setattr(log, alan, deger)
    await session.flush()
    return log, f"Yakıt kaydı güncellendi: {log.fuel_date} · {log.liters} lt"


async def delete_fuel_log(session: AsyncSession, actor: User, log_id: uuid.UUID) -> str:
    """`DELETE /equipment/fuel-logs/{id}` — yakıt kaydı MALİ İZ DEĞİLDİR
    (`delete_work_log`in aynı gerekçesi): maliyet ondan TÜREVDİR, kayıt hatası
    silinebilir; ekipmanın kendisi silinemez."""
    log = await visible_fuel_log(session, actor, log_id)
    kunye = f"{log.fuel_date} · {log.liters} lt"
    await session.delete(log)
    await session.flush()
    return f"Yakıt kaydı silindi: {kunye}"
