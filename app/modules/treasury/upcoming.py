"""Yaklaşan Ödemeler (HZ-1 T5) — spec §4 uç 9, E9:109-125.

`GET /treasury/upcoming-payments`: önümüzdeki `days` gün içinde ÖDEYECEĞİMİZ
paranın satır satır dökümü. Kart beş şey basar — karşı taraf · evrak atfı ·
vade · kalan gün · tutar — ve bunların yalnız biri (kalan gün) TÜREVDİR.

## 🔴 K9 — üç kaynak çizili, ÜÇÜ DE var (TB8)

E9:113/117/121 üç satır gösterir ve üçü üç ayrı kaynaktır:

| Mockup satırı | Kaynak | Vadenin kolonu |
|---|---|---|
| `Akın İnşaat – Hakediş #47` | `subcontractor_progress_payments` | TÜREVDİR (aşağıda) |
| `Bordro – Temmuz` | `payroll_periods` | `payment_due_date` |
| `Yılmaz Elektrik – Fatura` | `invoices` | `due_date` |

Bu dosya HZ-1'de bordronun ÜRETİLEMEZ olduğunu yazıyordu; gerekçesi
"`payroll_periods` bir ödeme vadesi taşımaz, İK-3'te vade kolonu açılmadı"
idi. **TB8'de ÖLÇÜLDÜ ve ÇÜRÜTÜLDÜ:** `PayrollPeriod.payment_due_date`
(BY 63 "Son ödeme") İK-3'ten beri GERÇEK bir kolondur — `PayrollPeriodCreate`
ve `PayrollPeriodUpdate` ile YAZILIR, dönem detay/liste şemalarıyla OKUNUR ve
`PATCH /payroll/periods/{id}` ucu onu belgeler. Kaynak İCAT EDİLMEDİ; var olan
bir kolon nihayet okundu.

🔴 Eski gerekçenin HAKLI olduğu kısım aynen DURUR ve uygulama tam olarak odur:
**vade UYDURULMAZ.** "Ayın 15'i" ya da "dönem sonu + 5 gün" gibi bir kural
kanunî/şirket politikasına aittir ve sunucuda SAKLANMAZ; uydurulsaydı o gün
geldiğinde ekran gerçek olmayan bir aciliyet gösterirdi. Bu yüzden vadesi NULL
olan dönem listeye GİRMEZ: sunucu tarih ÜRETMEZ, varsayılan KOYMAZ.

## Vade nereden gelir

* **Fatura:** `invoices.due_date` — gerçek bir kolon. NULL ise vade
  BİLİNMİYOR demektir ve satır listeye GİRMEZ (bugün varsayılsaydı vadesiz her
  fatura en acil sırada görünürdü — NULL-EŞİK kanonunun fail-closed yüzü).
* **Hakediş:** `approved_at` (TR gününe çevrilmiş) + sözleşmenin
  `payment_term_days`ı. Bu bir TÜRETİMDİR, icat değil: ödeme vadesi (gün)
  sözleşmede SAKLIDIR ve onay damgası da vardır. `approved_at` NULL ise satır
  düşer (faturadaki NULL vade ile aynı kural).
* **Bordro:** `payroll_periods.payment_due_date` — gerçek bir kolon ve
  NULLABLE. NULL ise satır listeye GİRMEZ; faturanın NULL vade kuralının
  BİREBİR aynısı, aynı fail-closed yüzü.

🔴 **Bordronun İKİ EK KAPISI vardır ve ikisi de mevcut kaynakların kuralının
kardeşidir, bordro için icat edilmemiştir.** Birincisi DURUMDUR: yalnız
`approved` dönem listelenir. `payroll/router.py`nin PATCH ucu →
`service.update_period` vadeyi `draft`/`pending_approval`da serbestçe
değiştirir, `approved`/`paid`de **409** verir; yani `payment_due_date` ancak
onaydan sonra bir TAAHHÜTTÜR. Değişebilir bir tarihten "3 gün kaldı" üretmek,
eski gerekçenin haklı olarak korktuğu SAHTE ACİLİYETİN ta kendisi olurdu. Onay
öncesi TUTAR da taahhüt değildir: satırlar `uncomputed` olabilir ve `compute`
netleri baştan yazar. `paid` dönem ise borcu kapandığı için dışarıda kalır.
Aynı `approved` şartını iki mevcut kaynak da taşır (`InvoiceStatus.approved` ·
`SubcontractorPaymentStatus.approved`). İkinci kapı İZİNDİR — aşağıda, Kapsam
bölümünde.

Bordronun TUTARI bir kolon değil TÜREVDİR: `Σ net_amount`, yalnız
`PAYABLE_LINE_STATUSES` durumundaki ve neti NULL olmayan satırlar üzerinden.
Formülün TEK EVİ `payroll/payable.py`dir ve durum kümesini `payroll/summary.py`
modülünden İTHAL EDER; burada ikinci kez yazılsaydı bordro ekranı ile bu kart
aynı dönem için farklı para basardı.

## Pencere: `[bugün, bugün + days]`, VADESİ GEÇMİŞ DIŞARIDA

Karar DAR olanıdır ve bilinçlidir: kartın adı "Yaklaşan Ödemeler"dir, mockup
negatif bir "kalan gün" ÇİZMEZ ve geçmişi içeri almak pencerenin alt sınırını
SINIRSIZ yapardı (bir yıl önce vadesi dolmuş bir fatura da "yaklaşan" sayılır,
liste ödenmemiş her eski borçla dolardı). Gecikmiş borç takibi ayrı bir
yüzeydir ve hiçbir mockup'ta çizilmemiştir. Sonuç: `days_remaining` asla
negatif olmaz.

## 🔴 ÇİFT SAYIM KAPISI

Onaylı bir hakediş FATURALANDIYSA (`invoices.subcontractor_progress_payment_id`)
ödenecek olan FATURADIR: vadesi, tutarı ve ödeme kaydı onun üzerindedir. İkisi
de listelenseydi aynı borç iki satır üretir ve nakit ihtiyacı sessizce iki
katına çıkardı. Süzgeç `NOT EXISTS`tir — hakediş başına sorgu değil.

🔴 **"FATURALANDI" ŞARTI, faturanın LİSTEYE GİRME şartıyla AYNI olmak
ZORUNDADIR** ve HZ-CIFT'e kadar DEĞİLDİ: dışlama durumsuzdu, dahil etme
`incoming + approved` istiyordu ve arada kalan her borç İKİ LİSTEDEN BİRDEN
kayboluyordu. Yüklem artık `invoiced_condition()`ta TEK KOPYADIR (gerekçe ve
her şartın neden var/yok olduğu orada) ve `dashboard/risks.py` de onu OKUR.

🔴 **Aynı kapının BORDRO hâli YAPISALDIR, süzgeç istemez.** Taşeron
personelinin bordro satırı `excluded`tır ve neti dönemin ödenebilir toplamına
GİRMEZ (`payroll/models.py` `PayrollLineStatus` docstring'i +
`payroll/summary.py` `PAYABLE_LINE_STATUSES`): o emeğin ödemesi taşerona
HAKEDİŞ üzerinden yapılır — İK-3'ün K2 kararı, "çift ödeme yapısal olarak
imkânsızdır". Yani bordro toplamı ile hakediş kaynağı AYRIK kümelerdir;
`excluded` satır toplama katılsaydı aynı emeğin parası bu kartta iki kez
sayılırdı.

## 🔴 K10 — aciliyet SUNUCUDA üretilmez

E9 renk kodlaması kendi içinde tutarsızdır (2 gün→turuncu, 3 gün→**kırmızı**,
7 gün→yeşil). Sunucu `days_remaining` (sayı) + `source_type` döner; eşik/renk
kararı istemcinindir (SA'nın "EN HIZLI rozeti sunucuda üretilmez" kanonu).

## 🔴 Kapsam (IDOR) — K3'ün SINIRI burada biter

Banka HESABI şirket genelidir (K3) ama bu ucun KAYNAKLARI proje kapsamı taşır
ve her satır karşı taraf + evrak + tutar SIZDIRIR. Süzgeç düşseydi
`treasury=_V` olan bir proje müdürü, göremediği projenin taşeronunu ve borç
tutarını okurdu. Bu yüzden iki kaynak da KENDİ modülünün süzgecinden geçer:
`invoicing.repository.scope_clause` (proje NULL = şirket geneli, modül izniyle
görünür) ve hakediş için `project_id IN visible` (hakedişte NULL proje YOKTUR).
İkinci bir görünürlük tanımı yazılmaz — liste uçlarıyla ayrışırdı.

🔴 **Bordronun kapsamı AYRIDIR ve sebebi ölçülmüştür:** `payroll_periods`ta
`project_id` KOLONU YOKTUR — dönem şirket genelindedir, yani proje süzgeci
bordroya UYGULANAMAZ. Bordronun görünürlük tanımı saf MODÜL iznidir
(`payroll:view`) ve `page_gate.effective_level` ile okunur; burada da
İKİNCİ bir tanım yazılmaz. Sızıntı yolu ÖLÇÜLDÜ: `project_manager` bu ucu OKUR
(`treasury=_V`) ama `payroll=_N`dir — kapı olmasaydı şirketin AYLIK TOPLAM
PERSONEL MALİYETİNİ okurdu. Ayrıntı `_payroll_visible`dedir.

## N+1

Sorgu sayısı SATIR SAYISINDAN bağımsızdır: üç `select` (fatura · hakediş ·
bordro) + hakediş tutarları için `amounts.bulk_calculations`ın iki toplu
sorgusu + bordro kapısının TEK izin okuması. Bordro toplamı dönem başına değil
`GROUP BY payroll_period_id` ile tek seferde kurulur. Tutarı hakediş başına
`calculation_for` ile çeken ya da `summary.build_period_summary`i dönem başına
çağıran bir uygulama `test_N_ARTI_1_YAPMAZ`ı geçemez.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import Date, Integer, cast, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.permissions import can_read
from app.core.timezone import today
from app.modules.contracts.models import SubcontractorContract
from app.modules.invoicing import repository as invoicing_repository
from app.modules.invoicing.models import Invoice, InvoiceDirection, InvoiceStatus
from app.modules.payroll import payable
from app.modules.payroll.guards import PERMISSION_MODULE as PAYROLL_PERMISSION_MODULE
from app.modules.payroll.models import PayrollPeriod, PayrollPeriodStatus
from app.modules.projects.service import sirket_liste_ciftleri, visible_projects
from app.modules.subcontractor_progress_payments import amounts
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.treasury import repository
from app.modules.treasury.schemas import (
    UpcomingPaymentItem,
    UpcomingPaymentsResponse,
    UpcomingSourceType,
)
from app.modules.treasury.service import PERMISSION_MODULE
from app.modules.users.models import User

__all__ = [
    "DEFAULT_DAYS",
    "MAX_DAYS",
    "MIN_DAYS",
    "build_upcoming_payments",
    "invoiced_condition",
    "progress_payment_due_expression",
]

#: E9:110 `Yaklaşan Ödemeler (7 Gün)` — mockup'tan OKUNUR, seçilmez.
DEFAULT_DAYS = 7
#: Router'ın `Query(ge=…, le=…)` sınırları buradan gelir; tavan aşımı **422**
#: (kırpma DEĞİL — TB3 kanonu). 90 gün bir çeyrektir: daha uzun bir pencere
#: "yaklaşan" kelimesini anlamsızlaştırır ve kartı bir borç listesine çevirirdi.
MIN_DAYS = 1
MAX_DAYS = 90


def progress_payment_due_expression():
    """`approved_at` (TR gününe çevrilmiş) + `payment_term_days` → `date`.

    Çevrim SQL'de yapılır çünkü pencere süzgeci de SQL'dedir: Python'da
    hesaplansaydı pencereye girmeyecek TÜM onaylı hakedişler önce çekilirdi.
    Saat dilimi adı `settings.display_timezone`dan gelir — `app/core/timezone`
    ile AYNI kaynak, gömülü string YOKTUR.

    🔴 `timezone(...)` ŞART ve DIŞA AÇIK: `approved_at` `timestamptz`tir;
    doğrudan `date`e çevrilirse PostgreSQL OTURUMUN `TimeZone` ayarını kullanır.
    Yerel geliştirme makinesinde bu ayar TR olduğu için kusur GÖRÜNMEZ, CI ve
    Railway'de (UTC) ise TR gecesi 00:00-03:00 arasında onaylanan hakedişin
    vadesi bir gün geri kayar ve satır listeden düşer. Yani bu kusur KARA KUTU
    testiyle yakalanamaz (ölçüldü: mutasyon yerelde hayatta kalıyor) — bu yüzden
    ifade dışarı açıktır ve `test_vade_ifadesi_SAAT_DILIMI_cevirir` derlenmiş
    SQL'i denetler.
    """
    tr_gunu = cast(
        func.timezone(settings.display_timezone, SubcontractorProgressPayment.approved_at), Date
    )
    return tr_gunu + cast(SubcontractorContract.payment_term_days, Integer)


def invoiced_condition():
    """🔴 ÇİFT SAYIM KAPISININ TEK KOPYASI — "bu hakediş FATURALANDI mı?".

    ## Kusur (HZ-CIFT, kullanıcının bildirdiği canlı hâl)

    Bu yüklem eskiden DURUM ŞARTI TAŞIMIYORDU: yalnız
    `Invoice.subcontractor_progress_payment_id == …` bakıyordu. Oysa fatura dalı
    (`_invoice_rows`) satırı listeye almak için `direction == incoming` **ve**
    `status == approved` şart koşar. Sonuç, borcun İKİ LİSTEDEN BİRDEN
    kaybolmasıydı:

    * hakediş düşerdi — "faturalanmış" sayıldığı için,
    * fatura da girmezdi — henüz `approved` olmadığı için.

    Ve bu istisnai bir hâl DEĞİLDİ: gelen fatura sisteme **`pending`** olarak
    girer (`InvoiceStatus` K2 — *"`draft` yalnız GİDEN tarafta anlamlıdır"*),
    yani taşeron faturası kesildiği andan onaylandığı ana kadar HER borç
    görünmez oluyordu. Dosyanın kendi kanonu (yukarıda) *"Aynı `approved`
    şartını iki mevcut kaynak da taşır"* diyordu — kod bunu yapmıyordu.

    ## Neden TAM OLARAK bu iki şart, ne bir eksik ne bir fazla

    Çift sayım ancak dışlama kümesi, faturanın GÖRÜNME koşullarını KAPSIYORSA
    engellenir. Listeye girebilen her fatura `incoming` + `approved`tır;
    dolayısıyla bu iki şart yeter ve KAPSAR.

    * 🔴 **`due_date IS NOT NULL` ŞARTTIR (denetim bulgusu 3).** İlk uygulama
      onu ATLAMIŞTI ve kusurun aynısı başka bir hücrede ayakta kalmıştı:
      muhasebe taşeron faturasını **vade alanını boş bırakarak** girip
      onaylarsa, fatura satırı listeye giremez (`due_date` NULL fail-closed
      elenir) ve hakediş de "faturalanmış" sayılıp düşerdi — borç YİNE iki
      listeden birden kaybolurdu.
    * **Pencere ve `kalan > 0` şartları EKLENMEZ.** Eklenseydi, faturası onaylı
      ama vadesi pencere DIŞINDA olan bir hakediş kendi TÜRETİLMİŞ vadesiyle
      listeye geri gelirdi — oysa artık gerçek vade faturanınkidir. Borç
      kaybolmaz: pencere dışı olması "yaklaşan" olmamasıdır, gecikirse
      `dashboard/risks.py` yüzeyi onu basar.
    * **`document_type <> 'refund'` EKLENMEZ.** Eklenseydi, hakedişe bağlı
      onaylı bir GELEN iade faturası hem kendisi listelenir hem hakedişi geri
      getirir, yani tam olarak kaçınılan çift satır doğardı.
    * **`disputed` DIŞLAMAZ** ve bu bilinçlidir (fail-closed): itiraz edilmiş
      faturanın borcu ORTADAN KALKMAZ, yalnız fatura satırı listeye giremez —
      hakediş listede KALIR ve borç görünür olmayı sürdürür. Sessizce kaybolması
      fazladan görünmesinden çok daha pahalıdır.
    * **`collected` GİDEN tarafın durumudur**; `direction == incoming` şartı onu
      zaten eler.

    🔴 **BU YÜKLEM YALNIZ BU UCA AİTTİR ve paylaşılmaz (denetim bulgusu 4).**
    Meşruiyeti bu dosyada bir FATURA DALI bulunmasından gelir: iki satır aynı
    borcu üretebildiği için biri elenmelidir. `dashboard/risks.py`de fatura dalı
    YOKTUR (ölçüldü: `dashboard` paketi `app.modules.invoicing`i hiç import
    etmez) — orada aynı yüklem hiçbir çift sayımı engellemez, yalnızca gecikmiş
    borcu görünmez yapardı. Kod tekrarını silmek uğruna GEREKÇESİ olmayan bir
    yere yüklem taşınmaz.
    """
    return exists().where(
        Invoice.subcontractor_progress_payment_id == SubcontractorProgressPayment.id,
        Invoice.direction == InvoiceDirection.incoming,
        Invoice.status == InvoiceStatus.approved,
        Invoice.due_date.is_not(None),
    )


async def _invoice_rows(
    session: AsyncSession, project_ids: list[uuid.UUID], ilk: date, son: date
) -> list[UpcomingPaymentItem]:
    """Gelen · onaylı · vadesi pencerede · TAM ÖDENMEMİŞ faturalar.

    "Tam ödenmemiş" ölçütü `Σ payments < total`dır ve toplam T2/T4'ün TEK
    kaynağından (`repository.paid_sum`) gelir; ikinci bir formül yazılsaydı bu
    uç ile fatura detayının "kalan"ı ayrışır ve kart, ödenmiş bir borcu
    ödenecek gösterirdi.

    `outerjoin` ŞART: hiç ödemesi olmayan fatura (asıl kitle) INNER join'de
    listeden tamamen DÜŞERDİ.

    ⚠️ `due_date.is_not(None)` KANITLANMIŞ BİÇİMDE GEREKSİZDİR (mutasyon
    denetiminde hayatta kaldı): SQL'de `NULL >= tarih` sonucu NULL'dır ve
    `WHERE` onu zaten eler. Satır BELGELEME amacıyla durur — "vadesiz fatura
    listeye girmez" kuralı okuyucuya üç değerli mantığı çıkarttırmadan
    görünsün diye. Silinirse davranış DEĞİŞMEZ; aynı şey
    `_progress_payment_rows`taki `approved_at.is_not(None)` için de geçerlidir.
    """
    odenen = repository.paid_totals_by_invoice()
    kalan = Invoice.total - func.coalesce(odenen.c.paid, 0)
    stmt = (
        select(
            Invoice.id,
            Invoice.invoice_no,
            Invoice.party_name,
            Invoice.due_date,
            kalan,
            Invoice.project_id,
        )
        .outerjoin(odenen, odenen.c.invoice_id == Invoice.id)
        .where(
            Invoice.direction == InvoiceDirection.incoming,
            Invoice.status == InvoiceStatus.approved,
            Invoice.due_date.is_not(None),
            Invoice.due_date >= ilk,
            Invoice.due_date <= son,
            kalan > 0,
            invoicing_repository.scope_clause(project_ids),
        )
    )
    return [
        UpcomingPaymentItem(
            source_type=UpcomingSourceType.invoice,
            source_id=invoice_id,
            counterparty=party_name,
            document_no=invoice_no,
            due_date=due_date,
            days_remaining=(due_date - ilk).days,
            amount=Decimal(kalan_tutar),
            project_id=proje_id,
        )
        for invoice_id, invoice_no, party_name, due_date, kalan_tutar, proje_id in (
            await session.execute(stmt)
        ).all()
    ]


async def _progress_payment_rows(
    session: AsyncSession, project_ids: list[uuid.UUID], ilk: date, son: date
) -> list[UpcomingPaymentItem]:
    """Onaylı · henüz FATURALANMAMIŞ · vadesi pencerede taşeron hakedişleri.

    Tutar **NET**tir (brüt + KDV − avans − teminat) ve
    `amounts.bulk_calculations`tan gelir: brüt basılsaydı kesintili her hakediş
    fazla, KDV'li her hakediş eksik görünürdü. Hesabın kendisi burada İKİNCİ
    KEZ YAZILMAZ — E15 tfoot'unun tek kaynağı o modüldür.
    """
    if not project_ids:
        return []
    vade = progress_payment_due_expression()
    faturalanmis = invoiced_condition()
    stmt = (
        select(SubcontractorProgressPayment, SubcontractorContract.subcontractor_name, vade)
        .join(
            SubcontractorContract,
            SubcontractorContract.id == SubcontractorProgressPayment.contract_id,
        )
        .where(
            SubcontractorProgressPayment.status == SubcontractorPaymentStatus.approved,
            SubcontractorProgressPayment.approved_at.is_not(None),
            SubcontractorProgressPayment.project_id.in_(project_ids),
            vade >= ilk,
            vade <= son,
            ~faturalanmis,
        )
    )
    rows = (await session.execute(stmt)).all()
    bloklar = await amounts.bulk_calculations(session, [payment for payment, _, _ in rows])
    return [
        UpcomingPaymentItem(
            source_type=UpcomingSourceType.subcontractor_progress_payment,
            source_id=payment.id,
            counterparty=taseron_adi,
            document_no=str(payment.sequence_no),
            due_date=vade_gunu,
            days_remaining=(vade_gunu - ilk).days,
            amount=bloklar[payment.id].net,
            project_id=payment.project_id,
        )
        for payment, taseron_adi, vade_gunu in rows
    ]


async def _payroll_visible(session: AsyncSession, actor: User) -> bool:
    """Aktörün `payroll` modülünde en az `view` seviyesi var mı — TEK sorgu.

    🔴 Bu ucun kapsam süzgeci burada İKİYE AYRILIR ve sebebi ölçülmüştür:
    `PayrollPeriod`da `project_id` KOLONU YOKTUR. Dönem şirket genelindedir,
    yani fatura/hakediş satırlarının proje süzgeci bordroya UYGULANAMAZ ve
    bordronun görünürlük tanımı saf MODÜL iznidir.

    Süzgeç yazılmasaydı matris tam burada ayrışırdı:

        `"treasury": [_A, _F, _N, _N, _N, _F, _V, _N]`
        `"payroll":  [_A, _F, _N, _N, _F, _F, _N, _N]`

    `project_manager` bu ucu OKUR (`treasury=_V`) ama bordroya HİÇ erişimi
    yoktur (`payroll=_N`) — süzgeç düşerse şirketin AYLIK TOPLAM PERSONEL
    MALİYETİNİ okurdu.

    İkinci bir görünürlük tanımı YAZILMAZ: seviye `roles/repository`den okunur
    ve `core/access.satisfies` ile karşılaştırılır — `site_diary`, `sales` ve
    `contracts` servislerindeki emsalin aynısı. Kapı YALNIZ bordroyu susturur;
    ucun tamamına uygulansaydı (403 ya da boş liste) `treasury=_V` olan bir rol
    çalışan iki kaynağı da kaybederdi.
    """
    return await can_read(session, actor, PAYROLL_PERMISSION_MODULE)


async def _payroll_rows(
    session: AsyncSession, actor: User, ilk: date, son: date
) -> list[UpcomingPaymentItem]:
    """ONAYLI · vadesi pencerede · ödenebilir toplamı 0'dan büyük bordro dönemleri.

    Üç süzgecin üçü de mevcut iki kaynağın kuralının kardeşidir, bordro için
    icat edilmiş DEĞİLDİR:

    * **`approved` ŞART.** `payroll/router.py` → `service.update_period` vadeyi
      `draft`/`pending_approval`da serbestçe değiştirir ve `approved`/`paid`de
      **409** verir; yani `payment_due_date` ancak onaydan sonra bir
      TAAHHÜTTÜR. Onay öncesi tutar da taahhüt değildir: satırlar `uncomputed`
      olabilir ve `compute` netleri baştan yazabilir. `paid` dönem ise DIŞARIDA
      kalır çünkü borcu kapanmıştır (`paid` hakedişin kuralı).
    * **NULL vade DÜŞER** (fail-closed, NULL-EŞİK kanonu). Bugün varsayılsaydı
      vadesi hiç girilmemiş her dönem listenin en acil sırasında görünürdü —
      vadesiz faturanın kuralıyla birebir aynı. Vade UYDURULMAZ.
      ⚠️ Şarttaki `payment_due_date.is_not(None)` **KANITLANMIŞ BİÇİMDE
      GEREKSİZDİR** (mutasyon denetiminde hayatta kaldı): SQL'de `NULL >= tarih`
      sonucu NULL'dır ve `WHERE` onu zaten eler. Satır — `_invoice_rows`taki
      kardeşi gibi — BELGELEME amacıyla durur; silinirse davranış DEĞİŞMEZ.
      Kuralı bekçileyen şey bu satır değil `test_vadesiz_bordro_donemi_LISTELENMEZ`tir.
    * **Ödenebilir toplam > 0.** Faturanın `kalan > 0` süzgecinin kardeşi:
      "₺0 · 3 gün kaldı" satırı ödenecek bir para varmış gibi görünür,
      tıklanır, hiçbir şey bulunmaz.

    Tutar bir KOLON DEĞİL TÜREVDİR ve formülü burada İKİNCİ KEZ YAZILMAZ:
    `payroll.payable` alt sorgusu hangi satırın sayıldığını (durum kümesi +
    `net_amount IS NOT NULL`) bordro modülünde tutar. INNER JOIN'dir — ödenebilir
    satırı hiç olmayan dönem alt sorguda grup açmaz ve kendiliğinden düşer.

    Sorgu dönem sayısından BAĞIMSIZ olarak TEKTİR (`GROUP BY` + JOIN);
    `summary.build_period_summary`i dönem başına çağırmak N+1 olurdu.

    `document_no` `"YYYY-MM"`dir: sunucu E9:117'nin "Temmuz"unu ÜRETMEZ, çeviri
    kararı istemciye aittir. `counterparty` `None`dur — mockup bordro satırında
    bir karşı taraf adı ÇİZMEZ.
    """
    if not await _payroll_visible(session, actor):
        return []
    odenebilir = payable.payable_net_totals_by_period()
    stmt = (
        select(
            PayrollPeriod.id,
            PayrollPeriod.year,
            PayrollPeriod.month,
            PayrollPeriod.payment_due_date,
            odenebilir.c.payable_net,
        )
        .join(odenebilir, odenebilir.c.payroll_period_id == PayrollPeriod.id)
        .where(
            PayrollPeriod.status == PayrollPeriodStatus.approved,
            PayrollPeriod.payment_due_date.is_not(None),
            PayrollPeriod.payment_due_date >= ilk,
            PayrollPeriod.payment_due_date <= son,
            odenebilir.c.payable_net > 0,
        )
    )
    return [
        UpcomingPaymentItem(
            source_type=UpcomingSourceType.payroll,
            source_id=donem_id,
            counterparty=None,
            document_no=f"{yil:04d}-{ay:02d}",
            due_date=vade,
            days_remaining=(vade - ilk).days,
            amount=Decimal(tutar),
        )
        for donem_id, yil, ay, vade, tutar in (await session.execute(stmt)).all()
    ]


async def build_upcoming_payments(
    session: AsyncSession, actor: User, *, days: int
) -> UpcomingPaymentsResponse:
    """ÜÇ kaynağı birleştirir, VADEYE göre artan sıralar.

    Sıralama Python'dadır ve olmalıdır: ayrı `select`lerin birleşimi tek bir
    `ORDER BY` ile sıralanamaz (UNION yazmak, üç farklı sütun kümesini yapay
    olarak aynı şekle sokmak demekti). Küme pencere ile zaten küçüktür.

    İkincil ölçüt `document_no`dur: aynı güne düşen iki satır her istekte aynı
    sırada gelmelidir, yoksa ekran sebepsiz oynardı.
    """
    bugun = today()
    son = bugun + timedelta(days=days)
    project_ids = [
        p.id
        for p in await visible_projects(
            session, actor, sirket_ciftleri=sirket_liste_ciftleri(PERMISSION_MODULE)
        )
    ]

    satirlar = await _invoice_rows(session, project_ids, bugun, son)
    satirlar += await _progress_payment_rows(session, project_ids, bugun, son)
    satirlar += await _payroll_rows(session, actor, bugun, son)
    satirlar.sort(key=lambda satir: (satir.due_date, satir.document_no))
    return UpcomingPaymentsResponse(items=satirlar, days=days, as_of=bugun)
