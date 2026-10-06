"""Çalışma kaydı (M3 · spec §4 · K9 · K10 · K11 · K12).

Kapsam kapısı `core`dadır ve buradan yalnız OKUNUR (`visible_equipment`,
`_is_visible_site`, `get_equipment_or_404`).

* **K11** `hours` sunucu hesabı → `_resolve_hours` (POST ve PATCH aynı fonksiyon);
* **🔴 K12** günlük 24 saat tavanı → `_lock_equipment` + `_assert_daily_cap`
  (EŞİK = KİLİT: kilit DENETİMDEN ÖNCE, sıra tüm uçlarda SABİT);
* **K9** kaydın kendi şantiyesi → `create_work_log` damgası.
"""

import uuid
from datetime import date, datetime, time
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import EquipmentValidationError, NotFoundError
from app.modules.equipment import repository
from app.modules.equipment.models import EquipmentWorkLog, WorkLogType
from app.modules.equipment.schemas import WorkLogCreate, WorkLogUpdate
from app.modules.equipment.service.core import (
    OPERATOR_MISSING,
    SITE_MISSING,
    _is_visible_site,
    _visible_project_ids,
    get_equipment_or_404,
    visible_equipment,
)
from app.modules.personnel.models import Personnel
from app.modules.users.models import User

WORK_LOG_MISSING = "Çalışma kaydı bulunamadı."
"""Görünmeyen VE var olmayan kaydın TEK cümlesi — ikisi ayırt EDİLEMEZ."""

HOURS_IS_SERVER_COMPUTED = (
    "Başlangıç ve bitiş saati verildiğinde çalışma süresi sunucu tarafından hesaplanır; "
    "'hours' alanı gönderilemez. Süreyi doğrudan yazmak için saat aralığını boşaltın."
)
TIME_PAIR_REQUIRED = (
    "Başlangıç ve bitiş saati birlikte verilmelidir: ikisini de girin ya da ikisini de boş bırakın."
)
OVERNIGHT_NOT_SUPPORTED = (
    "Bitiş saati başlangıç saatinden önce olamaz. Gece yarısını geçen vardiya bu sürümde "
    "desteklenmiyor; vardiyayı iki güne iki ayrı kayıt olarak girin."
)
HOURS_REQUIRED = (
    "Saat aralığı verilmediğinde çalışma süresi ('hours') zorunludur (arıza kaydı gibi)."
)

#: 🔴 K12 — bir günün saat TAVANI. Fizik kuralıdır, ekipmana göre değişmez;
#: `monthly_capacity_hours`un (K7, VERİ) aksine koda gömülüdür.
MAX_DAILY_HOURS = Decimal("24")

DAILY_HOURS_EXCEEDED = (
    "Aynı ekipmanın aynı gündeki kayıtları toplamı 24 saati aşamaz "
    "(mevcut {mevcut} saat + girilen {girilen} saat)."
)

#: Saat çözümlemesinin ölçeği — kolon `Numeric(6, 2)`.
_HOURS_QUANTUM = Decimal("0.01")
#: PATCH'te K11'i tetikleyen alanlar: gövde bunlardan HİÇBİRİNE dokunmuyorsa
#: satırdaki saat korunur (bkz. `update_work_log`).
_HOURS_INPUTS = frozenset({"start_time", "end_time", "hours"})
_SECONDS_PER_HOUR = Decimal("3600")


def _resolve_hours(
    *, start_time: time | None, end_time: time | None, hours: Decimal | None
) -> Decimal:
    """🔴 K11 — `hours` SUNUCU HESABIDIR. Kuralın TEK yaşadığı yer.

    POST ve PATCH aynı fonksiyonu çağırır; PATCH'e ikinci bir kopya yazılsaydı
    POST'ta 422 olan gövde PATCH'ten sızardı (kuralın yalnız POST'ta yaşadığı
    klasik kaçak — K2'nin aynı emsali).

    Dört kapı, hepsi 422:

    1. aralık YARIM verilmiş (yalnız biri) → anlamsız bir kayıt;
    2. aralık TAM verilmişken `hours` da gönderilmiş → sunucu hesabının üzerine
       yazma girişimi; SESSİZCE YOKSAYILSAYDI istemci kendi hesabının tutulduğunu
       sanır, ekranda başka bir sayı görürdü;
    3. `end < start` → gece vardiyası bu dilimde DESTEKLENMEZ (spec K11); sessiz
       bir negatif saatten iyidir;
    4. ne aralık ne saat → kaydın hiçbir süresi yok.

    `end == start` REDDEDİLMEZ: spec yalnız `end < start`ı yasaklar ve sıfır
    saatlik bir kayıt (yanlışlıkla açılıp düzeltilecek satır) yasak değildir.
    """
    if (start_time is None) != (end_time is None):
        raise EquipmentValidationError(TIME_PAIR_REQUIRED)
    if start_time is None or end_time is None:
        if hours is None:
            raise EquipmentValidationError(HOURS_REQUIRED)
        # Doğrudan alınan saat de KOLONUN ölçeğine çekilir: aksi halde aynı
        # kayıt yazıldığı istekte `8`, yeniden okunduğunda `8.00` dönerdi ve
        # istemci iki farklı dizge görürdü.
        return hours.quantize(_HOURS_QUANTUM)
    if hours is not None:
        raise EquipmentValidationError(HOURS_IS_SERVER_COMPUTED)
    if end_time < start_time:
        raise EquipmentValidationError(OVERNIGHT_NOT_SUPPORTED)
    fark = datetime.combine(date.min, end_time) - datetime.combine(date.min, start_time)
    return (Decimal(fark.total_seconds()) / _SECONDS_PER_HOUR).quantize(_HOURS_QUANTUM)


async def _lock_equipment(session: AsyncSession, *equipment_ids: uuid.UUID) -> None:
    """🔴 K12 EŞİK = KİLİT: tavan denetiminden ÖNCE `equipment` satır(lar)ı kilitlenir.

    Kilitsiz bir eşik denetimi iki eşzamanlı kayıtta HER İKİSİNİ de geçirir
    (İK-2 K5 / İK-3 çift ödeme dersi) ve TEK istekli bir test bunu ASLA görmez —
    regresyonu `tests/modules/equipment/test_mk1_work_log_concurrency.py`
    iki gerçek bağlantıyla tutar.

    **Kilit sırası SABİTTİR: kimliğe göre artan.** PATCH bir kaydı başka makineye
    taşırken İKİ ekipmanı birden kilitler; sıra sabitlenmeseydi iki eşzamanlı
    ters yönlü taşıma karşılıklı kilitlenme (deadlock) üretirdi. DELETE'te tavan
    yalnız AZALIR ama kilit yine de alınır ve sıra korunur: aynı satırlara iki
    farklı sırayla giren ikinci bir yol, kilit düzeninin tamamını bozardı.
    """
    for equipment_id in sorted(set(equipment_ids)):
        await repository.get_equipment_for_update(session, equipment_id)


async def _assert_daily_cap(
    session: AsyncSession,
    *,
    equipment_id: uuid.UUID,
    work_date: date,
    hours: Decimal,
    exclude_log_id: uuid.UUID | None = None,
) -> None:
    """🔴 K12 — günlük 24 saat tavanı. `_lock_equipment`ten SONRA çağrılır.

    Çakışma/vardiya örtüşmesi DENETLENMEZ (spec K12: mockup vardiya modeli
    çizmiyor) — yalnız günlük toplam.
    """
    mevcut = await repository.day_hours_total(
        session, equipment_id, work_date, exclude_log_id=exclude_log_id
    )
    if mevcut + hours > MAX_DAILY_HOURS:
        raise EquipmentValidationError(DAILY_HOURS_EXCEEDED.format(mevcut=mevcut, girilen=hours))


async def visible_work_log(
    session: AsyncSession, actor: User, log_id: uuid.UUID
) -> EquipmentWorkLog:
    """Çalışma kaydı görünürlüğünün TEK kapısı — detay, PATCH ve DELETE buradan geçer.

    İKİ kapı birden: kaydın KENDİ şantiyesi (K9) görünür olmalı VE makinesi
    görünür olmalı. İkincisi olmasaydı `site_id IS NULL` bir kayıt, görünmeyen
    bir projeye atanmış makinenin varlığını ele verirdi.
    """
    log = await repository.get_work_log(session, log_id)
    if log is None:
        raise NotFoundError(WORK_LOG_MISSING)
    if not await _is_visible_site(session, actor, log.site_id):
        raise NotFoundError(WORK_LOG_MISSING)
    if not await _is_visible_site(
        session, actor, (await get_equipment_or_404(session, log.equipment_id)).site_id
    ):
        raise NotFoundError(WORK_LOG_MISSING)
    return log


async def list_work_logs(
    session: AsyncSession,
    actor: User,
    *,
    equipment_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    date_from: date | None,
    date_to: date | None,
    record_type: WorkLogType | None,
    limit: int,
    offset: int,
) -> tuple[list[EquipmentWorkLog], int]:
    """Liste + `total` TEK kapsam kararını paylaşır (TB3 kanonu)."""
    project_ids = await _visible_project_ids(session, actor, liste=True)
    suzgecler = {
        "equipment_id": equipment_id,
        "site_id": site_id,
        "date_from": date_from,
        "date_to": date_to,
        "record_type": record_type,
    }
    items = await repository.list_work_logs(
        session, project_ids, limit=limit, offset=offset, **suzgecler
    )
    total = await repository.count_work_logs(session, project_ids, **suzgecler)
    return items, total


async def create_work_log(
    session: AsyncSession, actor: User, data: WorkLogCreate
) -> tuple[EquipmentWorkLog, str]:
    """`POST /equipment/work-logs`.

    Sıra ÖNEMLİ ve tüm yazma uçlarında AYNI:

    1. görünürlük/referans (404) — gövdesi eksik bir istek, görmediği makinenin
       varlığını 422 üzerinden öğrenmemelidir;
    2. 🔴 **KİLİT** (`equipment` satırı) — denetimlerden ÖNCE, TOCTOU penceresi
       açılmasın;
    3. K11 saat çözümlemesi (saf, DB'ye dokunmaz);
    4. K12 günlük tavan (kilit ALTINDA okunur).

    Operatör YALNIZ varlığı için aranır; "arıza kaydında operatör olamaz" gibi
    bir kural spec'te YOKTUR ve icat edilmez (K10 yalnız zorunlu olmadığını
    söyler).
    """
    equipment = await visible_equipment(session, actor, data.equipment_id)
    if not await _is_visible_site(session, actor, data.site_id):
        raise NotFoundError(SITE_MISSING)
    if data.operator_id is not None and await session.get(Personnel, data.operator_id) is None:
        raise NotFoundError(OPERATOR_MISSING)

    await _lock_equipment(session, equipment.id)
    hours = _resolve_hours(start_time=data.start_time, end_time=data.end_time, hours=data.hours)
    await _assert_daily_cap(
        session, equipment_id=equipment.id, work_date=data.work_date, hours=hours
    )

    alanlar = data.model_dump(exclude={"hours"})
    if "site_id" not in data.model_fields_set:
        # 🔴 K9 SNAPSHOT: şantiye verilmemişse kaydın şantiyesi, makinenin O ANKİ
        # ataması olarak DAMGALANIR. `NULL` bırakılsaydı K9 kâğıt üzerinde kalır
        # ve maliyet dağılımı ("hangi şantiye ne kadar makine yaktı") hiçbir
        # zaman üretilemezdi. Açıkça `null` GÖNDEREN istek (depoda yapılan iş)
        # damgalanmaz — `model_fields_set` bu ikisini ayırır.
        alanlar["site_id"] = equipment.site_id
    log = EquipmentWorkLog(**alanlar, hours=hours, created_by_id=actor.id)
    session.add(log)
    await session.flush()
    return log, f"Çalışma kaydı eklendi: {equipment.name} · {data.work_date} · {hours} saat"


async def update_work_log(
    session: AsyncSession, actor: User, log: EquipmentWorkLog, data: WorkLogUpdate
) -> tuple[EquipmentWorkLog, str]:
    """`PATCH /equipment/work-logs/{id}` — kayıt hatası düzeltilebilir.

    🔴 K11 ve K12 burada **BİRLEŞİK değerler** üzerinde koşar: yalnız gövdeye
    bakılsaydı `{"hours": 12}` tek başına aralığı duran bir kaydın sunucu
    hesabını ezerdi ve `{"work_date": …}` tek başına hedef günün tavanını hiç
    ölçmezdi.

    Kilit HER İKİ ekipmanı da kapsar (kayıt başka makineye taşınabilir) ve
    `_lock_equipment` sırayı sabitler.
    """
    degisiklikler = data.model_dump(exclude_unset=True)
    hedef_equipment_id = degisiklikler.get("equipment_id", log.equipment_id)
    hedef_gun = degisiklikler.get("work_date", log.work_date)
    hedef_site_id = degisiklikler.get("site_id", log.site_id)

    if hedef_equipment_id != log.equipment_id:
        await visible_equipment(session, actor, hedef_equipment_id)
    if "site_id" in degisiklikler and not await _is_visible_site(session, actor, hedef_site_id):
        raise NotFoundError(SITE_MISSING)
    operator_id = degisiklikler.get("operator_id")
    if operator_id is not None and await session.get(Personnel, operator_id) is None:
        raise NotFoundError(OPERATOR_MISSING)

    await _lock_equipment(session, log.equipment_id, hedef_equipment_id)
    if _HOURS_INPUTS & degisiklikler.keys():
        hours = _resolve_hours(
            start_time=degisiklikler.get("start_time", log.start_time),
            end_time=degisiklikler.get("end_time", log.end_time),
            # 🔴 `hours` YALNIZ gövdede varsa "verilmiş" sayılır: satırdaki mevcut
            # saat taşınsaydı, aralığı duran HER kayıt kendi eski saati yüzünden
            # 2. kapıya takılır ve bir daha hiç düzeltilemezdi.
            hours=degisiklikler.get("hours"),
        )
    else:
        # 🔴 Gövde saatin HİÇBİR girdisine dokunmuyorsa satırdaki saat korunur
        # (F-İK "touched" deseni, T3'ün K2'de uyguladığının kardeşi). Kural
        # koşulsuz koşsaydı ARALIKSIZ bir kayıt (arıza — M3:283) açıldıktan
        # sonra bir daha hiç düzeltilemezdi: notunu değiştiren istek `hours`
        # göndermez, gönderemez de (K11 sunucu hesabı) ve "saat zorunlu"
        # 422'sine takılırdı.
        hours = log.hours
    await _assert_daily_cap(
        session,
        equipment_id=hedef_equipment_id,
        work_date=hedef_gun,
        hours=hours,
        exclude_log_id=log.id,
    )

    for alan, deger in degisiklikler.items():
        setattr(log, alan, deger)
    log.hours = hours
    await session.flush()
    return log, f"Çalışma kaydı güncellendi: {hedef_gun} · {hours} saat"


async def delete_work_log(session: AsyncSession, actor: User, log_id: uuid.UUID) -> str:
    """`DELETE /equipment/work-logs/{id}` — çalışma kaydı MALİ İZ DEĞİLDİR.

    Maliyet ondan TÜREVDİR (K18: her okumada yeniden hesaplanır), dolayısıyla
    hakediş satırının aksine yanlış girilen kayıt silinebilir; ekipmanın kendisi
    (kartı) silinemez — orada iz `RESTRICT`lidir.

    Tavan yalnız AZALIR ama kilit yine de alınır: kilit sırası TÜM uçlarda
    sabittir (`_lock_equipment` gerekçesi).
    """
    log = await visible_work_log(session, actor, log_id)
    await _lock_equipment(session, log.equipment_id)
    kunye = f"{log.work_date} · {log.hours} saat"
    await session.delete(log)
    await session.flush()
    return f"Çalışma kaydı silindi: {kunye}"
