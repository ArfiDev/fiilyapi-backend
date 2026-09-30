"""BOQ tahsislerinden türeyen BÖLÜM sayaçları (BLM-SAY).

`sites` servisindeki `boq_item_count` ve `budget` yer tutucularının TEK veri
kaynağı burasıdır. O modül kendi `SELECT`ini yazmaz — `timesheet/counts.py`
ile aynı gerekçe: iki ayrı sayım mantığı zamanla ayrışır ve bölüm satırı ile
bölüm detayı aynı bölüm için farklı sayı gösterir.

## 🔴 SAYAÇ NEYİN KÜMESİDİR — cevabı SORGU GÖVDESİNDEDİR

Tek sorgu, tek küme:

    SELECT a.section_id, a.boq_item_id, a.quantity, i.unit_price
      FROM boq_item_section_allocations a
      JOIN boq_items i ON i.id = a.boq_item_id
     WHERE a.section_id IN (...)

* `item_count` = o kümedeki **FARKLI `boq_item_id`** sayısı — yani "bu bölüme
  EN AZ BİR tahsis satırı düşmüş poz" sayısıdır. ŞANTİYENİN tüm pozları
  DEĞİLDİR (tahsis edilmemiş poz sayılmaz) ve "TAMAMLANAN poz" da DEĞİLDİR
  (böyle bir bayrak repoda yoktur, bkz. aşağıdaki kalıntı notu).
* `amount` = aynı kümedeki her satırın `miktar × birim fiyat` çarpımının
  toplamıdır.

İKİSİ DE AYNI SATIR KÜMESİNDEN türer; ayrışmaları yapısal olarak imkânsızdır.
Bu, `contracts/distribution_quantity.py`nin kanonudur ("kota neyi sayıyorsa
gösterge de onu sayar"): tahsis invariantının (`SUM(quantity) <= boq_items.
quantity`, `boq/service.replace_allocations`) kontrol ettiği küme de tam
olarak `boq_item_section_allocations` satırlarıdır — gösterge o kümeyi sayar,
ikinci bir küme tanımlanmaz.

## 🔴 "SATIR YOK" BURADA GERÇEK BİR SIFIRDIR

Tahsisi olmayan bölüm sonuçta YOKTUR; çağıran `.get(id, _BOS)` ile okur ve
`0`/`0.00` basar — YER TUTUCU DEĞİL. Bu, `subcontractor_count`u yer tutucuda
bırakan K2 tuzağına DÜŞMEZ: orada birleşim anahtarı (`subcontractor_id`)
NULLABLE'dır ve `COUNT(DISTINCT ...)` serbest metinle açılmış kaydı SESSİZCE
düşürür; burada `boq_item_id` ve `section_id` NOT NULL'dur ve FK'leri CASCADE
olduğu için sahipsiz satır da birikemez. Yani boş küme "kayıt bağlanmamış"
anlamına GELEMEZ, yalnızca "bu bölüme hiçbir poz tahsis edilmemiş" der.

## 🔴 PARANIN TEK FORMÜLÜ

Çarpım SATIR BAŞINA `boq.schemas.quantize_money` ile yuvarlanır ve toplam
yeniden yuvarlanır — `BoqItemResponse.amount` + `BoqGroupResponse.group_total`
ikilisinin BİREBİR aynı şekli. `SUM(quantity * unit_price)` diye SQL'de tek
sefer yuvarlamak, BOQ ekranının kendi toplamından KURUŞ FARKLI bir "Bölüm
Bedeli" üretirdi (K3 — aynı paranın iki formülü).

## KALINTI (bilinçli, raporlandı)

Mockup bu kutuya "16 / 26" basar (`Bölüm Detay.dc.html:86`,
`Şantiye Detay.dc.html:174`), yani TAMAMLANAN / TOPLAM. Burada üretilen sayı
PAYDA'dır (toplam). PAYIN KAYNAĞI YOKTUR: `BoqItemSectionAllocation`ta
"tamamlandı" bayrağı yoktur ve gerçekleşen taraf (`progress_pct`) hâlâ yer
tutucudur. `CountPlaceholder.count` tek `int` taşır; paydayı basmak
DOĞRUDUR ("bu bölümde 26 iş kalemi var"), payı UYDURMAK olurdu.

## N+1 YOK

Fonksiyon TEK gruplu sorgudur; çağıran bölüm kimliklerini TOPLU geçirir.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import DisciplineScope, item_visible_clause
from app.modules.boq.models import BoqItem, BoqItemSectionAllocation
from app.modules.boq.schemas import quantize_money


@dataclass(frozen=True)
class SectionBoqTotals:
    """Bir bölümün BOQ türevleri — İKİSİ DE aynı satır kümesinden.

    Tek bir sözlük değeri olarak taşınır ki çağıran iki ayrı sözlüğü ayrı ayrı
    `.get(...)` etmek zorunda kalmasın: iki kaynağı ayrı taşımak, birini
    doldurup öbürünü unutan bir çağrı yerine kapı açardı.
    """

    item_count: int
    amount: Decimal


#: Tahsisi olmayan bölümün ölçülmüş hâli (yer tutucu DEĞİL — yukarıdaki nota bak).
EMPTY = SectionBoqTotals(item_count=0, amount=Decimal("0.00"))


async def by_section(
    session: AsyncSession, section_ids: Sequence[uuid.UUID], scope: DisciplineScope
) -> dict[uuid.UUID, SectionBoqTotals]:
    """Bölüm başına tahsis edilmiş poz sayısı + tutar — TEK sorgu.

    `scope` ZORUNLU (DSC-B4): yalnız görünür kalemler sayılır (FARKLI kalem sayısı ve
    tahsis tutarı aynı görünür kümeden). Görünür kalemi olmayan bölüm sonuçta YOKTUR →
    çağıran `EMPTY` (0 / 0.00) okur; 404 değildir."""
    if not section_ids:
        return {}
    stmt = (
        select(
            BoqItemSectionAllocation.section_id,
            BoqItemSectionAllocation.boq_item_id,
            BoqItemSectionAllocation.quantity,
            BoqItem.unit_price,
        )
        .join(BoqItem, BoqItem.id == BoqItemSectionAllocation.boq_item_id)
        .where(
            BoqItemSectionAllocation.section_id.in_(list(section_ids)),
            item_visible_clause(scope, BoqItem),
        )
    )
    pozlar: dict[uuid.UUID, set[uuid.UUID]] = {}
    tutarlar: dict[uuid.UUID, Decimal] = {}
    for section_id, item_id, quantity, unit_price in (await session.execute(stmt)).all():
        pozlar.setdefault(section_id, set()).add(item_id)
        tutarlar[section_id] = tutarlar.get(section_id, Decimal("0")) + quantize_money(
            quantity * unit_price
        )
    return {
        section_id: SectionBoqTotals(
            item_count=len(item_ids), amount=quantize_money(tutarlar[section_id])
        )
        for section_id, item_ids in pozlar.items()
    }
