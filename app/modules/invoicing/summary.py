"""Fatura KPI şeridi (FAT-1 spec §7 md.2) — `GET /invoices/summary`, FY:69-75.

`procurement/summary.py`nin kardeşi ve aynı sebeple AYRI dosyadadır: bu modül
hiçbir kaydı DEĞİŞTİRMEZ, yalnızca sayar.

## Mockup karşılıkları (kart etiketleri birebir)

| Kart | Alan | Tanım |
|---|---|---|
| FY:71 "Kesilen (Bu Ay)" | `issued_this_month` | bu ayın GİDEN faturaları — tutar + adet |
| FY:72 "Gelen (Bu Ay)" | `received_this_month` | bu ayın GELEN faturaları — tutar + adet |
| FY:73 "Tahsil Edilecek" | `receivable` | giden `sent` — tutar + adet |
| FY:74 "KDV Farkı" | `vat_difference` | giden KDV − gelen KDV |
| FY:75 "Onay Bekleyen" | `pending_approval` | gelen `pending` — 🔴 **ADET** |

🔴 **`pending_approval` ADETTİR, tutar DEĞİL.** FY:75 kartı tek bir sayı basar
(`3` · alt satır "Gelen fatura") — ötekilerin aksine para biçimi ve `₺` yoktur.
İlk üç kart HEM tutar HEM adet taşır (alt satırları "18 fatura" / "34 fatura" /
"4 fatura vadeli").

## Ay penceresi

`app/core/timezone.today()` ile **`DISPLAY_TIMEZONE`de içinde bulunulan ay**.
`date.today()` sunucunun yerel saatini (Railway'de UTC) okurdu ve TR gecesi
00:00-03:00 arasında ayın ilk/son günü BİR GÜN kayardı — ayın son gecesinde
kesilen fatura yanlış aya düşerdi. Sınırlar KAPALIDIR (ilk gün ve son gün
dahil).

**Yalnız adında `_this_month` geçen iki alan aya bağlıdır.** `receivable` ve
`pending_approval` DURUM kartlarıdır (geçen aydan kalan bir alacak da tahsil
edilecektir); `vat_difference` de spec §7'de ay taşımayan bir adla tanımlıdır
ve tüm görünür faturaları kapsar.

## 🔴 Kapsam (IDOR)

Liste ucundaki süzgecin AYNISI (`repository.scope_clause`): görünmeyen projenin
faturası hiçbir toplama girmez. `project_id` NULL fatura (şirket geneli) modül
izniyle sayılır (§6). Süzgeç düşseydi kullanıcı, listede göremediği bir
faturanın tutarını özet kartından okurdu.
"""

from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import today
from app.modules.invoicing import repository
from app.modules.invoicing.guards import PERMISSION_MODULE
from app.modules.invoicing.models import Invoice, InvoiceDirection, InvoiceStatus
from app.modules.invoicing.repository import DirectionAggregate
from app.modules.invoicing.schemas import InvoiceSummaryMetric, InvoiceSummaryResponse
from app.modules.projects.service import sirket_liste_ciftleri, visible_projects
from app.modules.users.models import User

__all__ = ["build_summary", "current_month_bounds"]

_MONEY = Decimal("0.01")
_BOS = DirectionAggregate(amount=Decimal("0"), count=0, vat_amount=Decimal("0"))


def current_month_bounds() -> tuple[date, date]:
    """Görüntüleme saat dilimindeki İÇİNDE BULUNULAN ayın ilk ve son günü.

    Son gün "bir sonraki ayın 1'inden bir gün önce" olarak bulunur: `month + 1`
    aritmetiği Aralık'ta yıl taşırdı ve ay uzunluklarını (28/29/30/31) elle
    bilmek gerekirdi.
    """
    bugun = today()
    ilk = bugun.replace(day=1)
    sonraki_ay = (ilk + timedelta(days=32)).replace(day=1)
    return ilk, sonraki_ay - timedelta(days=1)


def _metric(toplam: DirectionAggregate) -> InvoiceSummaryMetric:
    """Para her zaman İKİ HANELİDİR: kart "₺0" değil "₺0,00" tabanından
    biçimlenir (`procurement/summary.py` kuralı)."""
    return InvoiceSummaryMetric(amount=toplam.amount.quantize(_MONEY), count=toplam.count)


async def build_summary(session: AsyncSession, actor: User) -> InvoiceSummaryResponse:
    """Beş KPI'ı ÜÇ sorguyla kurar (modül docstring'i).

    Süzgeç parametresi YOKTUR: FY'de KPI şeridi tablo filtrelerinin ÜSTÜNDEDİR
    ve tarih/arama seçimleriyle birlikte değişmez (SAT şeridinin `project_id`
    süzgecinin aksine — orada mockup böyle çiziyordu, burada çizmiyor).
    """
    project_ids = [
        p.id
        for p in await visible_projects(
            session, actor, sirket_ciftleri=sirket_liste_ciftleri(PERMISSION_MODULE)
        )
    ]
    ay_ilk, ay_son = current_month_bounds()

    bu_ay = await repository.aggregate_by_direction(
        session,
        project_ids,
        conditions=(Invoice.issue_date >= ay_ilk, Invoice.issue_date <= ay_son),
    )
    tum_zamanlar = await repository.aggregate_by_direction(session, project_ids)
    # İki DURUM kartı tek gruplu sorgudan beslenir (durum başına ayrı sorgu
    # açılmaz). Koşul YÖNLE ÇİFTLENİR: `status` tek bir enum tipidir ve giden
    # bir faturanın `pending` görünmesi §3'e göre imkânsızdır — ama sadece
    # `status IN (sent, pending)` yazılsaydı bu imkânsızlığa GÜVENİLMİŞ olurdu
    # ve bir gün gerçekleşirse "Tahsil Edilecek" kartı sessizce şişerdi.
    durumlar = await repository.aggregate_by_direction(
        session,
        project_ids,
        conditions=(
            (
                (Invoice.direction == InvoiceDirection.outgoing)
                & (Invoice.status == InvoiceStatus.sent)
            )
            | (
                (Invoice.direction == InvoiceDirection.incoming)
                & (Invoice.status == InvoiceStatus.pending)
            ),
        ),
    )

    giden = InvoiceDirection.outgoing
    gelen = InvoiceDirection.incoming
    return InvoiceSummaryResponse(
        issued_this_month=_metric(bu_ay.get(giden, _BOS)),
        received_this_month=_metric(bu_ay.get(gelen, _BOS)),
        receivable=_metric(durumlar.get(giden, _BOS)),
        vat_difference=(
            tum_zamanlar.get(giden, _BOS).vat_amount - tum_zamanlar.get(gelen, _BOS).vat_amount
        ).quantize(_MONEY),
        pending_approval=durumlar.get(gelen, _BOS).count,
    )
