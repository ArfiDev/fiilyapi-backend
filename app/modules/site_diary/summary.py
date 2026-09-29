"""Şantiye günlüğü aylık agregasyonu (T4; spec §3) — Hakediş Özeti ekranının kaynağı.

## Tek kural: YALNIZ `submitted`

Taslak bir gün özete GİRMEZ (spec §3). Süzgeç `repository.summary_lines`tadır ve
`repository.cumulative_quantities_before` (T3 kümülatifi) ile BİREBİR AYNIDIR:
ikinci bir toplama kuralı açılsaydı ekrandaki kümülatif ile hakediş özeti iki
farklı sayı söylerdi. Aynı senaryoda ayın son gönderilmiş kaydının
`cumulative_quantity` türevi ile buradaki `quantity` EŞİTTİR (testle sabittir).

## Para bellekte toplanır, SQL'de değil

`amount` satır bazında yuvarlanmış `read.line_amount` değerlerinin toplamıdır
(taşeron `summary._gross` gerekçesinin aynısı): SQL'de `SUM(quantity *
unit_price)` almak, ekranda görünen satırların toplamıyla alttaki toplamı zamanla
kuruş bazında AYIRIRDI. Fiyat SATIRIN snapshot'ından gelir — BOQ fiyatı bugün
değiştiği için geçmiş ayın hakedişi yeniden yazılamaz.

## "Sözleşme" sütunu BOQ'dan gelir

Mockup `Şantiye - Hakediş Özeti` L132 "Sözleşme" hücresi (2.220.000), GK L226'nın
"Sözleşme: 1.200 m³ · Birim fiyat: ₺1.850" satırının çarpımıdır — yani ŞANTİYENİN
BOQ kalemidir ("Sözleşme BOQ'a bağlı" rozeti, GK L212). İşveren sözleşmesi
kalemi AYRI alanlarda (`contract_item_*`) KÖPRÜ olarak taşınır: T5'in "günlükten
doldur" önerisi o kimlik üzerinden eşleme yapacaktır.
"""

import uuid
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.boq.models import BoqItem
from app.modules.contracts.models import EmployerContractItem
from app.modules.progress_payments.calculations import quantize2
from app.modules.site_diary import read, repository
from app.modules.site_diary.schemas import SiteDiarySummary, SiteDiarySummaryItem
from app.modules.site_diary.service import visible_site
from app.modules.users.models import User

_ZERO_MONEY = Decimal("0.00")
_ZERO_QUANTITY = Decimal("0.000")
_RATIO_STEP = Decimal("0.0001")


def _completion_ratio(quantity: Decimal, boq_quantity: Decimal) -> Decimal | None:
    """HÖ L134 "%" sütunu. Sıfır sözleşme miktarı NULL döner: `boq_items.quantity`
    CHECK'i pozitif olmasını zorlar ama korkuluk burada da durur — sıfıra bölmenin
    sessiz alternatifi (0 ya da 100) ekranda YALAN bir ilerleme çubuğu olurdu."""
    if boq_quantity <= 0:
        return None
    return (quantity / boq_quantity).quantize(_RATIO_STEP)


class _Bucket:
    """Tek pozun biriktiricisi. Sözlük EKLEME SIRASINI korur ve sorgu
    `BoqItem.code` ile sıralı gelir — çıktı da koda göre sıralıdır."""

    __slots__ = ("item", "contract_item", "quantity", "amount")

    def __init__(self, item: BoqItem, contract_item: EmployerContractItem | None) -> None:
        self.item = item
        self.contract_item = contract_item
        self.quantity = _ZERO_QUANTITY
        self.amount = _ZERO_MONEY


def _to_schema(bucket: _Bucket) -> SiteDiarySummaryItem:
    item = bucket.item
    contract_item = bucket.contract_item
    return SiteDiarySummaryItem(
        boq_item_id=item.id,
        code=item.code,
        description=item.description,
        unit=item.unit,
        unit_price=item.unit_price,
        quantity=bucket.quantity,
        amount=bucket.amount,
        boq_quantity=item.quantity,
        boq_amount=quantize2(item.quantity * item.unit_price),
        completion_ratio=_completion_ratio(bucket.quantity, item.quantity),
        contract_item_id=contract_item.id if contract_item is not None else None,
        contract_item_quantity=contract_item.quantity if contract_item is not None else None,
        contract_item_unit_price=contract_item.unit_price if contract_item is not None else None,
    )


async def get_summary(
    session: AsyncSession,
    actor: User,
    site_id: uuid.UUID,
    *,
    year: int | None,
    month: int | None,
    scope: DisciplineScope = UNRESTRICTED,
) -> SiteDiarySummary:
    """Kapsam kararı ŞANTİYE üzerinden verilir (`visible_site`, T2 liste ucuyla
    aynı kapı): görünmeyen şantiyenin özeti boş özet DEĞİL 404'tür.

    Sorgu sayısı SABİTTİR (kapsam + satır sorgusu + gün sayacı); poz ya da gün
    başına sorgu KOŞULMAZ.

    DSC-B1 `scope`: satırlar `BoqItem` JOIN'inde görünür kalemle süzülür (`item_visible_clause`
    meşru: `BoqItem` FROM'da); `total_amount` süzülmüş kalemlerden, `contract_item_*` alanları
    kalemle BİRLİKTE gelir/gitir. `entry_count` ORTAK başlıktır (Ü5): dönemdeki gönderilmiş
    gün sayısı, kısıtlıda da değişmez.
    """
    site, _ = await visible_site(session, actor, site_id)
    rows = await repository.summary_lines(session, site.id, year=year, month=month, scope=scope)
    entry_count = await repository.count_submitted_entries(session, site.id, year=year, month=month)

    buckets: dict[uuid.UUID, _Bucket] = {}
    for line, item, contract_item in rows:
        bucket = buckets.get(item.id)
        if bucket is None:
            bucket = buckets[item.id] = _Bucket(item, contract_item)
        bucket.quantity += line.quantity
        bucket.amount += read.line_amount(line)

    items = [_to_schema(bucket) for bucket in buckets.values()]
    return SiteDiarySummary(
        site_id=site.id,
        year=year,
        month=month,
        entry_count=entry_count,
        items=items,
        total_amount=sum((row.amount for row in items), _ZERO_MONEY),
    )
