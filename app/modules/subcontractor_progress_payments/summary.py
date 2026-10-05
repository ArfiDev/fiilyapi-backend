"""Taşeron hakedişi KPI şeridi (T4) — mockup "Ekran 2" L105-122'nin DÖRT kartı.

| Kart (L107-121)  | Alan                          | Tanım                                        |
|------------------|-------------------------------|----------------------------------------------|
| Toplam Hakediş   | `total_gross`                 | Süzgeçteki TÜM hakedişlerin brütü            |
| Onay Bekliyor    | `pending_gross`               | `pending_approval` durumundakilerin brütü    |
| Bu Ay Ödenen     | `paid_period_gross`           | `paid` + ETKİN DÖNEM'dekilerin brütü         |
| Aktif Taşeron    | `active_subcontractor_count`  | Süzgeçteki farklı taşeron SÖZLEŞMESİ sayısı  |

## Para KPI'ları neden BRÜT?

Mockup'tan okunur, tahmin edilmez: L118 "Onay Bekliyor ₺1,24M", liste ekranındaki
tek `pending_approval` satırının **Brüt Tutar** hücresidir (L143 ₺1.240.000) —
"Net Ödeme" hücresi ₺1.016.800'dür. Brütün gövdesi spec §3 zinciridir
(`amounts.bulk_calculations` → `calculations.gross_total`); ikinci bir toplama
yolu AÇILMAZ.

## "Aktif Taşeron" neden SÖZLEŞME sayar?

Şemada ayrı bir taşeron (cari) tablosu YOKTUR: taşeron kimliği
`subcontractor_contracts` satırıdır (`subcontractor_name` yalnız serbest metindir
ve NULL olabilir). Ada göre saymak, adı boş bırakılmış sözleşmeleri tek bir
"taşeron" gibi gösterirdi.

## Süzgeçler LİSTE UCUYLA AYNI kümeyi verir

Aynı `repository._list_stmt` gövdesinden okunur (proje/dönem/durum/taşeron
araması) — iki uç ayrı süzgeç kopyası taşısaydı KPI şeridi ile altındaki tablo
zamanla FARKLI kümeleri gösterirdi. Sorgu sayısı SABİTTİR (kapsam + liste + iki
toplu hesap sorgusu); hakediş başına sorgu KOŞULMAZ.
"""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.core.discipline_scope import restricted_project_ids
from app.modules.progress_payments import calculations
from app.modules.projects.service import visible_projects
from app.modules.subcontractor_progress_payments import amounts, repository
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.subcontractor_progress_payments.schemas import (
    SubcontractorPaymentCalculation,
    SubcontractorProgressPaymentSummary,
)
from app.modules.users.models import User

_ZERO = Decimal("0.00")


def effective_period(
    period_year: int | None, period_month: int | None, *, today: datetime | None = None
) -> tuple[int, int]:
    """ "Bu Ay Ödenen" kartının dayandığı dönem.

    Dönem süzgeci verilmişse O dönem, verilmemişse İÇİNDE BULUNULAN ay. Kartın
    etiketi "Bu Ay"dır ama ekran dönem süzgeciyle geçmişe bakabilir; süzgeç
    yokken kartın tüm zamanların ödemesini göstermesi etiketi YALAN çıkarırdı.
    Seçilen dönem yanıtta ECHO edilir — ekran hangi ayı gösterdiğini bilmelidir.

    Bu bir İŞ TAKVİMİ penceresidir, olay damgası değil: UTC'den okunsaydı TR
    gecesi 21:00-24:00 arasında ayın 1'inde kart BİR ÖNCEKİ aya düşerdi. İmza
    `datetime` olarak KORUNUR (tek çağıran ve testler uyumlu; gereksiz kırıcı
    değişiklik yapılmaz), yalnız varsayılanın saat dilimi düzeltilir.
    """
    now = today or datetime.now(timezone.DISPLAY_TIMEZONE)
    if period_year is not None and period_month is not None:
        return period_year, period_month
    return now.year, now.month


def _gross(
    payments: list[SubcontractorProgressPayment],
    blocks: dict[uuid.UUID, SubcontractorPaymentCalculation],
) -> Decimal:
    """Brüt TOPLAM bellekte alınır, SQL'de değil: `line_total` kuruş yuvarlaması
    satır düzeyindedir (spec §3) — SQL'de `SUM` almak para matematiğinin ikinci
    bir kopyasını (ve zamanla ikinci bir doğruluk tanımını) doğururdu."""
    return sum((blocks[payment.id].gross for payment in payments), _ZERO)


async def get_summary(
    session: AsyncSession,
    actor: User,
    *,
    project_id: uuid.UUID | None,
    site_id: uuid.UUID | None,
    period_year: int | None,
    period_month: int | None,
    status_filter: SubcontractorPaymentStatus | None,
    q: str | None,
) -> SubcontractorProgressPaymentSummary:
    """Kapsam SQL'de kalır (spec §9.0): süzgeç `visible_projects`ten türeyen
    kimlik listesidir, ikinci bir görünürlük kararı VERİLMEZ. Görünmeyen projenin
    hakedişi hiç ÇEKİLMEZ — toplu çekimde kapsam sızıntısı klasik hatadır.

    Boş küme 404 DEĞİL, sıfırlı özettir (zarif düşüş): hakedişi olmayan proje de
    ekranı açabilmelidir.
    """
    # IZN-B3: disiplinle KISITLI projelerin hakedisleri ozetin DISINDA kalir (Ü2).
    kisitli = await restricted_project_ids(session, actor.id)
    visible_ids = [p.id for p in await visible_projects(session, actor) if p.id not in kisitli]
    rows = await repository.list_payments_for_summary(
        session,
        visible_ids,
        project_id=project_id,
        site_id=site_id,
        period_year=period_year,
        period_month=period_month,
        status_filter=status_filter,
        q=q,
    )
    payments = [payment for payment, _, _ in rows]
    blocks = await amounts.bulk_calculations(session, payments)

    year, month = effective_period(period_year, period_month)
    pending = [p for p in payments if p.status == SubcontractorPaymentStatus.pending_approval]
    paid_period = [
        p
        for p in payments
        if p.status == SubcontractorPaymentStatus.paid
        and p.period_year == year
        and p.period_month == month
    ]
    return SubcontractorProgressPaymentSummary(
        total_gross=_gross(payments, blocks),
        pending_gross=_gross(pending, blocks),
        paid_period_gross=_gross(paid_period, blocks),
        active_subcontractor_count=len({p.contract_id for p in payments}),
        period_year=year,
        period_month=month,
    )


async def cumulative_gross_by_contracts(
    session: AsyncSession, contract_ids: list[uuid.UUID]
) -> dict[uuid.UUID, Decimal]:
    """Taşeron sözleşmesi listesinin (SZL) sözleşme başına kümülatif brütü —
    işveren `progress_payments.summary.cumulative_gross_by_projects`in BİREBİR
    eşleniği: **TEK** toplu sorgu, sözleşme başına ayrı sorgu KOŞULMAZ.

    ## Anahtar neden `contract_id`?

    İşveren ucunda proje ile sözleşme 1:1'dir, orada proje kimliğiyle anahtarlamak
    yeterlidir. Taşeron ucunda bir projede N taşeron sözleşmesi olabilir; proje
    kimliğiyle anahtarlansaydı aynı projenin sözleşmeleri tek kutuda toplanır ve
    liste her satırda AYNI sayıyı gösterirdi.

    Yalnız `approved|paid` (`repository.COMPLETED_STATUSES`) sayılır: taslak ve
    onaya sunulmuş evrak henüz gerçekleşmiş iş değildir.

    Toplam SQL'de değil BELLEKTE alınır çünkü `line_total` kuruş yuvarlaması
    (`quantize2`) satır düzeyindedir (spec §3): SQL'de `SUM` almak para
    matematiğinin ikinci bir kopyasını (ve zamanla ikinci bir doğruluk tanımını)
    doğururdu. Kapsam süzgeci yine SQL'dedir (`contract_id IN (…)`) — istenmeyen
    sözleşmenin satırı hiç ÇEKİLMEZ.

    Hakedişi olmayan sözleşmenin anahtarı sonuçta YOKTUR (işverendeki davranış):
    çağıran katman `.get(id, Decimal("0.00"))` ile sıfırlar.

    🔴 Bu fonksiyon, bu dosyadaki `get_summary`in `total_gross` KPI'ı DEĞİLDİR ve
    onunla karıştırılmamalıdır: `total_gross` süzgeçteki TÜM statüleri (taslak
    dahil) sayar ve `/hakedisler/taseron` ekranının kartıdır; buradaki toplam
    yalnız gerçekleşmişi sayar ve sözleşme LİSTESİ kartı içindir.
    """
    grouped = await repository.list_completed_payments_by_contracts(session, contract_ids)
    return {
        contract_id: calculations.cumulative_state(payments, None).gross
        for contract_id, payments in grouped.items()
    }
