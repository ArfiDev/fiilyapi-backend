"""Günlük poz satırlarının ve işçi kırılımının TEK yazma yolu (T3; spec §2).

## ⚠️ Semantik: DEĞİŞTİRME (replace) — iki gövdede de aynı

`PUT /diary/{entry_id}/lines` gövdesi ekranın TAMAMIDIR: gövdede geçmeyen satır
**SİLİNİR**. `PATCH /diary/{entry_id}` içindeki `worker_counts[]` de aynı kuraldadır.
Taşeron `subcontractor_progress_payments/lines.py` deseninin birebiri.

## Taşeron modülünden İKİ FARK (bilinçli)

1. **Kota YOK.** Günlük kayıt bir TAAHHÜT değil bir GÖZLEMDİR: "bugün BOQ
   miktarından fazlasını yaptım" fiziksel olarak mümkündür ve günlük onu
   engellemez; tavan denetimi hakediş katmanının işidir.
2. **Katsayı YOK.** ₺ katkısı KATSAYISIZ `quantity × unit_price`tır (spec §2) —
   fiyat farkı katsayısı hakedişin işidir, günlüğün değil.

## PLN-B2.1 — bölüm kırılımı ve taşeron firma satırı

* Poz satırının kimliği (`boq_item_id`, `section_id`) İKİLİSİDİR (yaprak = kalem ×
  bölüm). `section_id` boş = "Bölümsüz": kalemin bölüme tahsis EDİLMEMİŞ kalanı.
  **Bölümsüz her zaman yazılabilir** (kural, ölçüldü): eski istemci yalnız bu dalı
  bilir ve canlıda tahsisli kalemler VARDIR (BOQ-SEC, 2026-08-17) — Bölümsüz'ü
  "tahsis edilmemiş kalan > 0" şartına bağlamak eski ekranın `PUT …/lines`ını
  tahsisli her kalemde 422'ye çevirirdi. Kalanı aşan Bölümsüz miktar ENGEL
  değildir, okumada `remaining_quantity < 0` (aşım) olarak görünür; gerekçesi
  `overrun_reason`dır (B2-8, zorunluluğu Gönder portunda).
* Bölümlü YENİ satır için kalemin o bölüme TAHSİSİ şarttır (422). Kayıtta ZATEN
  var olan bölümlü satır, tahsisi sonradan kalksa da gövdede kalabilir — DEĞİŞTİRME
  gövdesi tam kümedir; aksi hâlde BOQ'da tahsis silinen tek kalem, aynı günün
  diğer bütün satırlarının düzeltilmesini kilitlerdi (`timesheet.service.
  _assert_odenebilir_personel` emsali: kapı DEĞİŞİME bağlıdır).
* İşçi satırında `subcontractor_id` doluysa kimlik FİRMADIR (firma başına tek
  satır); boşsa eski (`trade`, `source`) kimliği aynen geçerlidir.

## Sıra — ÖNCE TÜM DOĞRULAMALAR, SONRA TEK YAZMA

`_resolve` hiçbir şey YAZMAZ, uygulama hiçbir şey DOĞRULAMAZ: ikinci satırda
patlayan istek birincisini session'a eklemiş OLMAMALIDIR (kısmi yazma yok).
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope, visible_item_set
from app.core.errors import ConflictError, DuplicateError, SiteValidationError
from app.modules.boq.models import BoqItem
from app.modules.site_diary import guards, repository
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine, SiteDiaryWorkerCount
from app.modules.site_diary.schemas import SiteDiaryLineInput, SiteDiaryWorkerCountInput

#: Poz satırının kimliği — (kalem, bölüm | Bölümsüz).
LineKey = tuple[uuid.UUID, uuid.UUID | None]


@dataclass(frozen=True)
class _ResolvedLine:
    """Doğrulaması BİTMİŞ satır planı — henüz hiçbir şey yazılmadı."""

    item: BoqItem
    section_id: uuid.UUID | None
    quantity: Decimal
    overrun_reason: str | None

    @property
    def key(self) -> LineKey:
        return (self.item.id, self.section_id)


def line_key(line: SiteDiaryLine) -> LineKey | None:
    """Bağı kopmuş satırın (`boq_item_id IS NULL`) kimliği YOKTUR."""
    if line.boq_item_id is None:
        return None
    return (line.boq_item_id, line.section_id)


def _assert_unique_keys(inputs: list[SiteDiaryLineInput]) -> None:
    seen: set[LineKey] = set()
    for line_input in inputs:
        key = (line_input.boq_item_id, line_input.section_id)
        if key in seen:
            # Kismi UQ'lar (`uq_site_diary_lines_item_section` / `_item_nosection`)
            # GOVDE ICINDE yakalanir; `IntegrityError` emniyet agi olarak kalir.
            raise DuplicateError(guards.DUPLICATE_LINE)
        seen.add(key)


async def _assert_sections(
    session: AsyncSession,
    entry: SiteDiaryEntry,
    inputs: list[SiteDiaryLineInput],
    existing_keys: set[LineKey],
) -> None:
    """Bölüm şantiyeye ait mi (422) + YENİ bölümlü satırın tahsisi var mı (422).

    İki toplu sorgu (bölümler + tahsisler) — satır başına sorgu YOK.
    """
    section_ids = {i.section_id for i in inputs if i.section_id is not None}
    if not section_ids:
        return
    site_ids = await repository.section_site_ids(session, section_ids)
    if any(site_ids.get(section_id) != entry.site_id for section_id in section_ids):
        # Var OLMAYAN bolum ile BASKA santiyenin bolumu AYNI 422 (IDOR yuzeyi).
        raise SiteValidationError(guards.LINE_SECTION_MISMATCH)
    allocations = await repository.allocations_for_items(
        session, {i.boq_item_id for i in inputs if i.section_id is not None}
    )
    for line_input in inputs:
        key = (line_input.boq_item_id, line_input.section_id)
        if line_input.section_id is None or key in existing_keys:
            continue
        if key not in allocations:
            raise SiteValidationError(guards.LINE_SECTION_NOT_ALLOCATED)


async def _resolve(
    session: AsyncSession,
    entry: SiteDiaryEntry,
    inputs: list[SiteDiaryLineInput],
    existing_keys: set[LineKey],
) -> list[_ResolvedLine]:
    """Gövde-içi çift → poz-şantiye sahipliği → bölüm/tahsis. **Hiçbir yazma YAPMAZ.**

    Sorgular satır başına DEĞİL gövdenin tamamı için TEK kez koşar (N+1 yok).
    """
    _assert_unique_keys(inputs)
    items = await repository.get_boq_items_by_ids(
        session, [entry_input.boq_item_id for entry_input in inputs]
    )
    for line_input in inputs:
        item = items.get(line_input.boq_item_id)
        if item is None or item.site_id != entry.site_id:
            # Var OLMAYAN poz ile BASKA santiyenin pozu AYNI 422'yi alir
            # (IDOR yuzeyi: kimlik varligi sizdirilmaz).
            raise SiteValidationError(guards.LINE_ITEM_MISMATCH)
    await _assert_sections(session, entry, inputs, existing_keys)
    return [
        _ResolvedLine(
            item=items[line_input.boq_item_id],
            section_id=line_input.section_id,
            quantity=line_input.quantity,
            overrun_reason=line_input.overrun_reason,
        )
        for line_input in inputs
    ]


def _new_line(plan: _ResolvedLine) -> SiteDiaryLine:
    """Snapshot DÖRTLÜSÜ yalnız BOQ kaleminden kopyalanır — istekten ASLA.

    Mevcut satırın snapshot'ı DONMUŞTUR (yeniden kopyalanmaz): bir günün kaydı
    o gün geçerli olan fiyatı taşır, BOQ sonradan güncellenirse geçmiş gün
    kendiliğinden değişmemelidir.
    """
    return SiteDiaryLine(
        boq_item_id=plan.item.id,
        section_id=plan.section_id,
        code=plan.item.code,
        description=plan.item.description,
        unit=plan.item.unit,
        unit_price=plan.item.unit_price,
        quantity=plan.quantity,
        overrun_reason=plan.overrun_reason,
    )


def assert_current_line_client(entry: SiteDiaryEntry, inputs: list[SiteDiaryLineInput]) -> None:
    """PLN-B2.x-A: kayitta bolumlu (`section_id` dolu) satir varken govdedeki HICBIR satir
    `section_id` ANAHTARINI tasimiyorsa bu B2 oncesi istemcidir → 409, veri SILINMEZ.

    Anahtarin VARLIGINA bakilir (`model_fields_set`): yeni istemci Bolumsuz satiri
    `section_id: null` ile yollar ve gecer. Bos govde ayirt edilemez (yeni istemcinin
    "hepsini temizle"si de bostur) → eski imza SAYILMAZ. Kayitta bolumlu satir yoksa
    eski imza da gecer (bugunku canli akis bozulmaz).
    """
    _assert_current_line_client(entry.lines, inputs)


def _assert_current_line_client(
    lines: Sequence[SiteDiaryLine], inputs: list[SiteDiaryLineInput]
) -> None:
    """`assert_current_line_client`in gerisi: HANGI satir kumesine bakacagi cagirandadir
    (kisitlida YALNIZ gorunur satirlar — gizli satirlar 409 ile baska disiplini sizdirirdi)."""
    legacy = bool(inputs) and all("section_id" not in i.model_fields_set for i in inputs)
    if legacy and any(line.section_id is not None for line in lines):
        raise ConflictError(guards.STALE_CLIENT)


def assert_current_worker_client(
    entry: SiteDiaryEntry, inputs: list[SiteDiaryWorkerCountInput]
) -> None:
    """PLN-B2.x-A (isci kirilimi): kayitta firma/saat alanli satir (`subcontractor_id` ya
    da `hours` dolu) varken govdedeki HICBIR satir bu iki anahtari tasimiyorsa → 409.
    Kurallar `assert_current_line_client` ile ayni (bos govde eski imza sayilmaz)."""
    new_keys = {"subcontractor_id", "hours"}
    legacy = bool(inputs) and all(not (new_keys & i.model_fields_set) for i in inputs)
    stored = any(
        wc.subcontractor_id is not None or wc.hours is not None for wc in entry.worker_counts
    )
    if legacy and stored:
        raise ConflictError(guards.STALE_CLIENT)


async def apply_lines(
    session: AsyncSession, entry: SiteDiaryEntry, inputs: list[SiteDiaryLineInput]
) -> int:
    """Gövdeyi kayda uygular (DEĞİŞTİRME semantiği); düşen bağı-kopmuş satır
    sayısını döner.

    Var olan satır KORUNUR (kimliği ve snapshot'ı ile), yalnız `quantity` ve
    `overrun_reason` güncellenir. Kimlik (kalem, bölüm) ikilisidir. Bağı kopmuş
    satır (`boq_item_id IS NULL`) gövdeden ADRESLENEMEZ, bu yüzden ilk kaydetmede
    düşer — kaçınılmaz ama SESSİZ değil: sayısı döndürülür ve yanıtın
    `dropped_orphan_count` alanıyla bildirilir.
    """
    existing: dict[LineKey, SiteDiaryLine] = {}
    for line in entry.lines:
        key = line_key(line)
        if key is not None:
            existing[key] = line
    dropped_orphan_count = sum(1 for line in entry.lines if line.boq_item_id is None)
    resolved = await _resolve(session, entry, inputs, set(existing))

    # --- Buradan itibaren yazma; dogrulama YOK (yukaridaki sira kisiti). ---
    new_lines: list[SiteDiaryLine] = []
    for plan in resolved:
        line = existing.get(plan.key)
        if line is None:
            line = _new_line(plan)
        else:
            line.quantity = plan.quantity
            line.overrun_reason = plan.overrun_reason
        new_lines.append(line)
    entry.lines = new_lines
    await session.flush()
    return dropped_orphan_count


def _assert_body_visible(inputs: list[SiteDiaryLineInput], gorunur: set[uuid.UUID]) -> None:
    """Gövdedeki her kalem kapsamda görünür olmalı; yoksa OLMAYANLA AYNI 422 (Ü7)."""
    if any(line_input.boq_item_id not in gorunur for line_input in inputs):
        raise SiteValidationError(guards.LINE_ITEM_MISMATCH)


async def apply_lines_scoped(
    session: AsyncSession,
    entry: SiteDiaryEntry,
    inputs: list[SiteDiaryLineInput],
    scope: DisciplineScope,
) -> int:
    """KISITLI kullanıcının satır kaydı: gövde yalnız GÖRÜNÜR satırları DEĞİŞTİRİR, gizli
    satırlar (başka disiplin + `boq_item_id IS NULL`) AYNI NESNELER olarak korunur.

    Sıra: görünürlük (gövdede görünmeyen/yabancı kalem → 422 `LINE_ITEM_MISMATCH`, olmayanla
    aynı) → `assert_current_line_client` YALNIZ görünür satırlarla → doğrulama → tek yazma.
    Gizli satırlara DOKUNULMAZ (id, created_at, updated_at, kolonlar aynı kalır: nesne
    yeniden oluşturulmaz). Bağı kopmuş satır kısıtlıya görünmez ve KORUNUR → düşen sayısı
    HER ZAMAN 0'dır. Kısıtsız yol `apply_lines`tir, buradan geçmez.
    """
    gorunur = await visible_item_set(
        session,
        scope,
        [
            *(line.boq_item_id for line in entry.lines if line.boq_item_id is not None),
            *(line_input.boq_item_id for line_input in inputs),
        ],
    )
    gorunur = gorunur or set()
    _assert_body_visible(inputs, gorunur)
    gizli = [line for line in entry.lines if line.boq_item_id not in gorunur]
    acik = [line for line in entry.lines if line.boq_item_id in gorunur]
    _assert_current_line_client(acik, inputs)
    existing = {key: line for line in acik if (key := line_key(line)) is not None}
    resolved = await _resolve(session, entry, inputs, set(existing))

    # --- Buradan itibaren yazma; dogrulama YOK (yukaridaki sira kisiti). ---
    new_lines: list[SiteDiaryLine] = []
    for plan in resolved:
        line = existing.get(plan.key)
        if line is None:
            line = _new_line(plan)
        else:
            line.quantity = plan.quantity
            line.overrun_reason = plan.overrun_reason
        new_lines.append(line)
    entry.lines = [*gizli, *new_lines]
    await session.flush()
    return 0


async def merge_lines(
    session: AsyncSession,
    entry: SiteDiaryEntry,
    inputs: list[SiteDiaryLineInput],
    scope: DisciplineScope = UNRESTRICTED,
) -> None:
    """GKS-B1 — `POST /diary` gövdesindeki `lines[]`: iskeletle BİRLEŞTİRİR (silme YOK).

    `apply_lines`in DEĞİŞTİRME semantiğinden BİLİNÇLİ FARK: günlük henüz yazılmamıştır ve
    iskelet satırları sunucunun kurduğu başlangıç kümesidir; gövde yalnız doldurulanları
    gönderirse iskelet BOŞALMAMALI. Gövde miktarı/gerekçesi iskelet satırının üzerine yazar,
    iskelette olmayan geçerli satırı ekler, hiçbir iskelet satırını silmez.

    Doğrulamalar `apply_lines`inkiyle AYNI `_resolve`tur (poz sahipliği, bölüm sahipliği,
    tahsissiz bölüm 422); iskelet anahtarları "mevcut" sayılır. Kısıtlı kullanıcıda gövdedeki
    görünmeyen kalem, olmayanla AYNI 422'dir. **Önce TÜM doğrulama, sonra bellekte uygulama:**
    `entry` çağıran tarafından henüz session'a EKLENMEMİŞTİR — patlayan istek günlük bırakmaz.

    STALE_CLIENT kontrolü BİLİNÇLİ OLARAK YOKTUR (`apply_lines`teki gibi sürüm anahtarı aranmaz):
    yeni kayıtta kaybolacak veri yoktur (silme yok, üzerine yazılan yalnız sunucunun kurduğu
    sıfır-miktarlı iskelet satırıdır). Anahtarsız (`section_id` verilmemiş) gövde satırı
    Bölümsüz satır olarak eklenir/eşleşir. GKS-F1 istemcisi anahtarı gönderecektir.
    """
    if scope.is_restricted:
        gorunur = await visible_item_set(session, scope, [i.boq_item_id for i in inputs])
        _assert_body_visible(inputs, gorunur or set())
    existing = {key: line for line in entry.lines if (key := line_key(line)) is not None}
    resolved = await _resolve(session, entry, inputs, set(existing))

    # --- Buradan itibaren yazma; dogrulama YOK (yukaridaki sira kisiti). ---
    merged = list(entry.lines)
    for plan in resolved:
        line = existing.get(plan.key)
        if line is None:
            merged.append(_new_line(plan))
        else:
            line.quantity = plan.quantity
            line.overrun_reason = plan.overrun_reason
    entry.lines = merged


#: İşçi satırının kimliği: (firma satırı mı, firma | meslek, kaynak). Etiket ilk
#: öğededir — "firma" adlı bir meslek bir firma kimliğiyle ÇAKIŞAMAZ.
_WorkerKey = tuple[bool, str, str]


def _worker_key(subcontractor_id: uuid.UUID | None, trade: str, source: str) -> _WorkerKey:
    if subcontractor_id is not None:
        return (True, str(subcontractor_id), "")
    return (False, trade, source)


async def apply_worker_counts(
    session: AsyncSession, entry: SiteDiaryEntry, inputs: list[SiteDiaryWorkerCountInput]
) -> None:
    """`PATCH` gövdesindeki `worker_counts[]` — DEĞİŞTİRME semantiği.

    Satır kimliği firmasız satırda (`trade`, `source`) İKİLİSİDİR: kısmi UQ
    (entry_id, trade, source) WHERE subcontractor_id IS NULL ihlali
    `IntegrityError`a DÜŞMEDEN, gövde içinde 409 olur. Aynı meslek FARKLI kaynakla
    meşrudur (GK418-430) — çakışan yalnız ÜÇLÜNÜN tamamıdır.

    PLN-B2.1 (B2-5): `subcontractor_id` dolu satırın kimliği FİRMADIR — firma
    başına tek satır (409); firma var olmalıdır (422, TEK toplu sorgu).

    `trade` kırpılması Pydantic katmanındadır (`SiteDiaryWorkerCountInput`):
    çakışma kontrolü KIRPILMIŞ değer üzerinden koşar, yoksa " Kalıpçı" gövdede
    çakışmadan geçip DB'de UQ'ya takılırdı.
    """
    seen: set[_WorkerKey] = set()
    for row_input in inputs:
        key = _worker_key(row_input.subcontractor_id, row_input.trade, row_input.source.value)
        if key in seen:
            raise DuplicateError(
                guards.DUPLICATE_WORKER_SUBCONTRACTOR
                if row_input.subcontractor_id is not None
                else guards.DUPLICATE_WORKER_COUNT
            )
        seen.add(key)

    firm_ids = {r.subcontractor_id for r in inputs if r.subcontractor_id is not None}
    if firm_ids and await repository.existing_subcontractor_ids(session, firm_ids) != firm_ids:
        raise SiteValidationError(guards.WORKER_SUBCONTRACTOR_UNKNOWN)

    # --- Buradan itibaren yazma; dogrulama YOK. ---
    existing = {
        _worker_key(row.subcontractor_id, row.trade, row.source.value): row
        for row in entry.worker_counts
    }
    new_rows: list[SiteDiaryWorkerCount] = []
    for row_input in inputs:
        key = _worker_key(row_input.subcontractor_id, row_input.trade, row_input.source.value)
        row = existing.get(key)
        if row is None:
            row = SiteDiaryWorkerCount(
                trade=row_input.trade,
                source=row_input.source,
                count=row_input.count,
                subcontractor_id=row_input.subcontractor_id,
                hours=row_input.hours,
            )
        else:
            # Firma satirinda meslek kimlik DEGILDIR, guncellenir.
            row.trade = row_input.trade
            row.count = row_input.count
            row.hours = row_input.hours
        new_rows.append(row)
    entry.worker_counts = new_rows
