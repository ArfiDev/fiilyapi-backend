"""Ödeme (tahsilat/ödeme) iş kuralları (HZ-1 T4) — spec §4 uçları 6, 7, 8.

Spec: `docs/superpowers/specs/2026-08-14-hz1-hazine-cekirdegi-design.md`
§2.2, §3 (K5/K6/K7), §4, §5.

## Neden `treasury`de ama izni `invoicing`

Ödeme bir FATURAYA kaydedilir: kapsamı (`visible_invoice`), eşiği (`total`) ve
durum damgası faturanındır — bu yüzden üç ucun da izin kapısı **`invoicing`**tir
(spec §4). İŞ MANTIĞI buna karşılık Hazine'nindir: `payments` tablosu, bakiye
formülü (K2) ve aşırı tahsilat kararı (K6) bu modülde yaşar. `invoicing/router.py`
yalnız YOLU barındırır (spec §5 rota sırası tuzağı), tek satır kural taşımaz.

## 🔴 Import yönü TEK YÖNLÜDÜR (P10 `cost_cards` dersi)

`treasury` → `invoicing` okur (`service.visible_invoice`, `transitions`,
`guards`); `invoicing` paket düzeyinde `treasury`yi bir tek yerde okur.

🔴 **MU-3E İŞ 2 — O TEK YER `invoicing/state_service.py`dir** ve ödemesiz
`mark-collected` kapısı için `treasury.repository`yi (YALNIZ onu) ithal eder.
Çember AÇILMAZ, ölçüldü: `treasury.repository` → `treasury.balance` →
`invoicing.models`, ve `invoicing.models` bir YAPRAKTIR. 🔴 **BU MODÜL
(`payments_service`) `invoicing`ten ASLA ithal edilemez** — o gerçek bir
çemberdir (`payments_service` → `invoicing.service` → …). Kural budur:
`invoicing` yalnız `treasury.repository`yi okur, `treasury`nin iş mantığını
DEĞİL.

🔴 **ODM-HAK — bu modül AYRICA `progress_payments.models` ve
`subcontractor_progress_payments.models`i okur** (`_assert_source_not_paid`).
Çember AÇILMAZ ve bu ölçüldü: her iki `models.py` de YAPRAKTIR (yalnız
`app.core.db` + birbirlerinden `QuantitySource`); ters yöndeki tek kenar
`progress_payments.transitions → treasury.realized`tır ve `realized` bu modülü
İTHAL ETMEZ. Okunan tek şey durum KOLONUDUR — hakediş modüllerinin SERVİSLERİ
buradan çağrılmaz.

## 🔴 K7 — EŞİK = KİLİT (WORKFLOW §4, İK-2/İK-3 kanonu)

K6 bir EŞİK denetimidir → kilitsiz yapılamaz. İki eşzamanlı tahsilat AYNI
toplamı okur ve **her ikisi de kapıdan geçer** (İK-3'te iki eşzamanlı ödeme
bordroyu İKİ KEZ ödemişti). Bu yüzden:

    1. KİLİT   — `invoices` satırı, `with_for_update` + `populate_existing`
    2. ödemeler — `Σ payments` (kilitli satırın koruması altında)
    3. hesap    — gövde içi `bank_account_id` referansı
    4. karar    — K6 eşiği (**422**)
    5. yazma + K5 damgası

Kilit **TÜM denetimlerden ÖNCEDİR** (TOCTOU) ve sıra **SABİTTİR**: fatura →
ödemeler → hesap → (ODM-1) enstrüman. Ters yönden giren bir yol karşılıklı
kilitlenme üretirdi; enstrüman satırını ÖNCE kilitleyip `invoices`a SONRA giden
bir yol ölçüldü ve YOKTUR.
Uç 8 (silme) de durumu YENİDEN TÜRETTİĞİ için AYNI kilidi alır — okuma
tarafında kilitsiz bir silme, eşzamanlı bir tahsilatla birleşince faturayı
`collected` bırakıp parayı geri alırdı.

`UPDATE`in örtük satır kilidi YETMEZ: o yazma ANINDA alınır, yani kararın çok
geç bir noktasında. Pencereyi kapatan tek şey OKUMADAKİ açık `FOR UPDATE`tir.

## 🔴 K5 — kısmi tahsilat SATIRDIR, durum TÜRETİLİR

`invoices` üzerinde `paid_amount` kolonu YOKTUR (migration da yoktur). Ödenen =
`Σ payments`, kalan = `invoice.total − Σ payments`; durum bundan türetilerek
damgalanır ve damga **matrisin TANIDIĞI geçişle sınırlıdır**
(`transitions.OUTGOING_TRANSITIONS`). Yani:

* giden fatura, `Σ >= total` ve matris `(status, mark-collected)` çiftini
  tanıyorsa → `collected`;
* **`draft` bir giden fatura tam ödense bile durumu DEĞİŞMEZ** — matriste
  `(draft, mark-collected)` çifti yoktur ve burada uydurulmaz;
* gelen faturada durum Hazine kapsamında HİÇ değişmez (`collected` giden tarafın
  terminalidir, gelen makinede karşılığı yoktur);
* silmede damga GERİ ALINIR: `Σ < total` ise `collected` → o damganın TEK
  kaynağı olan duruma (`sent`) düşer. Hedef sabit yazılmaz, matristen TÜRETİLİR.

## Hangi kural hangi koda düşer

| Durum | Kod | Sınıf |
|---|---|---|
| Görünmeyen/olmayan fatura ya da ödeme | 404 | `NotFoundError` |
| Gövdedeki `bank_account_id` yok | 404 | `NotFoundError` |
| Gövdedeki `financial_instrument_id` yok **ya da görünmüyor** | 404 | `NotFoundError` |
| Biçim ihlali (ölçek, `gt=0`, `limit` tavanı, bilinmeyen alan) | 422 | Pydantic |
| **Aşırı tahsilat (K6)** · pasif hesap | 422 | `TreasuryValidationError` |
| **Çek/senet YÖN çelişkisi (FIN-PAY K3)** | 422 | `TreasuryValidationError` |
| 🔴 **Bağlanan çek/senet PORTFÖYDE DEĞİL (ODM-1 D4)** | 422 | `TreasuryValidationError` |
| 🔴 **Portföy dışı çeke bağlı ödemenin SİLİNMESİ (ODM-1 D5)** | 409 | `ConflictError` |

## 🔴 FIN-PAY — çek/senet bağı (`financial_instrument_id`)

Bağ **İSTEĞE BAĞLI bir ETİKETTİR** ve `create_payment`in belgelenmiş kilit
sırasına (fatura → Σ → hesap → eşik) **DOKUNMADAN** o sıranın SONUNA eklenir.
İki şey bilinçle YAPILMAZ:

* **Hiçbir para türevine girdi eklenmez.** `balance.py` bakiyeyi
  `Σ payments.amount`tan türetir, `cash_flow.py` aynı toplamdan; çek/senet
  portföyü AYRI bir yüzeydir. Bağ bir türeve sızsaydı aynı para İKİ KEZ
  sayılırdı ve bakiye SAKLANMADIĞI için hiçbir kolon farkı bunu ele vermezdi.
* **Enstrümanın DURUMU değiştirilmez.** Portföy geçişi (`portfolio → collected`)
  AYRI bir uçtur (`instruments/router.py`); buradan damgalansaydı geçiş
  matrisinin (`instruments/transitions.py`) tanımadığı bir ikinci yazma kapısı
  doğardı.

Değiştirme ucu (`PATCH /payments/{id}`) YOKTUR ve bu dilimde doğmaz: yanlış bağ
bugünkü yolla düzeltilir — `DELETE /payments/{id}` (admin) + yeniden yazma.

Yeni `AuditAction` üyesi AÇILMAZ (TB3/T3 kanonu): ayrım `messages.payment_*`
METNİNDEDİR.

## 🔴 MU-3C — ÖDEME ARTIK FİŞ ATAR (2026-08-26)

`create_payment` nakit bacağını yazar, `delete_payment` onu STORNO eder. İkisi
de `treasury/posting.py`den geçer ve o dosyanın modül docstring'i bu dilimin
BÜTÜN gerekçesini taşır; burada TEKRARLANMAZ. Bu dosyayı ilgilendiren üç şey:

* **Kilit sırası DEĞİŞMEDİ.** Fişleme belgelenmiş dört adımın (fatura → Σ →
  hesap → eşik) ve enstrüman bağının SONUNA eklenir; `post_document` kendi
  danışma kilidini orada alır ve satır kilidi sırasına halka SOKMAZ.
* **Fiş AYNI transaction'dadır.** Kapalı dönem (**409**) ya da eksik eşleme
  (**422**) ödemeyi de geri alır — "parası girmiş ama fişsiz" bir tahsilat
  DOĞMAZ.
🔴 **ODM-1 (2026-08-27) — BAĞLI ÖDEMENİN NAKİT BACAĞI `101`/`103`E KAYAR.**
Tetikleyici `financial_instrument_id` BAĞIDIR, `method` etiketi değil (D1,
gerekçe `posting.payment_cash_role`ta). Bu dosyaya iki YENİ KAPI düşer:
`_instrument_or_none` portföy dışı evrakı **422** ile reddeder (D4) ve
`_assert_instrument_deletable` portföy dışı evrağa bağlı ödemenin silinmesini
**409** ile durdurur (D5). İkisinin de gerekçesi kalıcı bir `101` kalıntısıdır.

* **`_rederive_status` FİŞ ATMAZ.** `collected` damgası bir GEÇİŞTİR, para
  hareketi DEĞİL; para zaten ödeme satırından fişlenmiştir. Aynı sebeple
  `InvoiceAction.mark_collected` `invoicing.posting.POSTING_ACTIONS` dışında
  KALIR ve MU-3C bunu DEĞİŞTİRMEZ (gerekçe `treasury/posting.py`de üç ölçümle).
"""

import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError, NotFoundError, TreasuryValidationError
from app.modules.audit import messages
from app.modules.invoicing import guards as invoicing_guards
from app.modules.invoicing import service as invoicing_service
from app.modules.invoicing import transitions
from app.modules.invoicing.models import Invoice, InvoiceDirection, InvoiceStatus
from app.modules.invoicing.transitions import InvoiceAction
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.treasury import posting, repository
from app.modules.treasury.instruments import repository as instruments_repository
from app.modules.treasury.instruments import service as instruments_service
from app.modules.treasury.models import (
    BankAccount,
    FinancialInstrument,
    FinancialInstrumentDirection,
    FinancialInstrumentStatus,
    Payment,
)
from app.modules.treasury.schemas import PaymentCreate, PaymentListResponse, PaymentResponse
from app.modules.users.models import User

__all__ = [
    "PAYMENT_INSTRUMENT_NOT_PORTFOLIO",
    "PAYMENT_INSTRUMENT_NOT_PORTFOLIO_DELETE",
    "PAYMENT_SOURCE_PAID",
    "PERMISSION_MODULE",
    "create_payment",
    "delete_payment",
    "list_payments",
]

PERMISSION_MODULE = invoicing_guards.PERMISSION_MODULE
"""🔴 İzin anahtarı **`invoicing`**tir, `treasury` DEĞİL (spec §4).

Tek kopya `invoicing/guards.py`dedir ve buradan takma adla okunur: ikinci bir
`"invoicing"` string'i yazılsaydı bir gün biri değişir, öteki kalırdı.
"""

# 404 — görünmeyen ile var OLMAYAN ödeme AYNI cümleyi alır. Faturası görünmeyen
# bir ödeme de buraya düşer: ayrı cümle verilseydi elinde kimlik olan kullanıcı
# ödemenin var olduğunu (ve dolayısıyla faturanın da) öğrenirdi.
PAYMENT_MISSING = "Ödeme kaydı bulunamadı"

# 404 — gövdedeki `bank_account_id` yok. Hesap ŞİRKET GENELİDİR (K3), yani
# "görünmeyen hesap" hâli yoktur; 404 yalnız var olmayan kimlik içindir.
PAYMENT_ACCOUNT_MISSING = "Seçilen banka hesabı bulunamadı"

# 422 — kullanımdan kaldırılmış hesaba YENİ para yazılamaz. Repo kanonunda silme
# yolu `is_active=false`tur; oraya yazmaya izin verilseydi o bayrak yalnızca
# listeyi süzen bir SÜS olurdu ve kapatılmış bir kasaya tahsilat girilebilirdi.
PAYMENT_ACCOUNT_INACTIVE = "Kullanımdan kaldırılmış hesaba ödeme kaydedilemez"

# 🔴 422 — K6. Fazla tahsilat hiçbir mockup'ta MODELLENMEMİŞTİR (iade/avans
# kavramı yoktur) ve sessizce kabul etmek bakiyeyi şişirirdi. Karşılaştırma
# `Decimal` üzerinde, KURUŞ BAZINDA ve TAM'dır — tolerans YOKTUR.
PAYMENT_EXCEEDS_TOTAL = "Toplam tahsilat fatura tutarını aşamaz"

# 🔴 422 — FIN-PAY K3. Ayrı bir metindir çünkü kullanıcının yapabileceği şey de
# ayrıdır: 404 "böyle bir çek yok" demektir, bu ise "çek var ama YANLIŞ YÖNDE"
# demektir. `instruments.guards.DIRECTION_MISMATCH`ten de AYRIDIR: o, DURUM
# GEÇİŞİNİN yönle çelişmesidir (409) — bu ise ödeme ile portföyün yön çelişkisi.
PAYMENT_INSTRUMENT_DIRECTION_MISMATCH = "Seçilen çek/senedin yönü, faturanın yönüyle uyuşmuyor"

# 🔴 422 — ODM-1 D4. Ayrı bir metindir çünkü kullanıcının yapabileceği şey de
# ayrıdır: yön hatası "başka bir çek seç", bu ise "bu evrak KAPANDI — parası
# zaten hesabına indi, ödemeyi çeke BAĞLAMADAN yaz" demektir.
PAYMENT_INSTRUMENT_NOT_PORTFOLIO = "Yalnızca portföydeki çek/senede ödeme bağlanabilir"

# 🔴 409 — ODM-1 D5. Tahsil/ödeme fişi `101`/`103`ü ZATEN boşalttı; bu ödemenin
# fişini stornolamak ara hesabı NEGATİFE düşürür ve nakit hesabında kaynağı
# olmayan bir para bırakırdı. Mizan yine dengeli görünürdü — kusuru hiçbir kolon
# farkı ele vermezdi. 409'dur (422 değil): gövde kusurlu DEĞİL, kaydın DURUMU
# bu işlemi imkânsız kılıyor (`instruments.guards.TERMINAL_STATUS_DELETE` emsali).
PAYMENT_INSTRUMENT_NOT_PORTFOLIO_DELETE = "Portföyden çıkmış bir çek/senede bağlı ödeme silinemez"

# 🔴 409 — ODM-HAK. Hakedişin `paid` damgası bu ödemeye DAYANIR
# (`realized.assert_realized_covers`: gerçekleşmiş ödeme faturanın `total`ini
# karşılamalı). Ödeme silinirse damganın altındaki para çekilmiş olur ve damga
# geri ALINAMAZ: `paid` dört evrak ailesinde de TERMİNALDİR (hiçbir geçiş
# çiftinin KAYNAĞI değildir) ve `paid` bir hakediş ADMİN DAHİL silinemez. Yani
# kayıt kalıcı olarak boş bir "Ödendi" rozeti taşırdı ve tek düzeltme yolu elle
# SQL olurdu. 409'dur (422 değil): gövde kusurlu DEĞİL, KAYNAĞIN DURUMU bu
# işlemi imkânsız kılıyor — `PAYMENT_INSTRUMENT_NOT_PORTFOLIO_DELETE` emsali.
PAYMENT_SOURCE_PAID = (
    "Ödenmiş hakedişe ait ödeme silinemez: hakediş 'Ödendi' damgasını bu ödemeye dayanarak aldı"
)

#: 🔴 FIN-PAY K3 — UYUMLU YÖN ÇİFTLERİ. Eşleme koddan ÖLÇÜLDÜ, tahmin edilmedi
#: (`balance.inflow_condition()` ve `models.FinancialInstrumentDirection`):
#:
#: * **giden** fatura bizim kestiğimizdir → tahsilat → hesaba GİRİŞ → karşılığında
#:   elimize **alınan** (`received`) bir çek girer;
#: * **gelen** fatura bize kesilmiştir → ödeme → hesaptan ÇIKIŞ → karşılığında
#:   **verilen** (`issued`) bir çek çıkar.
#:
#: Model docstring'i (`models.py:277`) bunu zaten söylüyor: *"`payments`ta yön
#: bağlı faturanın `direction`'ından gelir"*.
#:
#: 🔴 Eski hâlde anahtar `InvoiceDirection`dı ve okuma `.get()` ileydi: tablo
#: eksik kalabilecek bir enum'la anahtarlandığı için fail-closed bir savunma
#: gerekiyordu. Anahtar artık `bool`dur ve iki hâli de yazılıdır, yani tablo
#: YAPISAL OLARAK TAMDIR — `.get()` savunması artık ölçülemez bir dal olurdu ve
#: kaldırılmıştır. Yeni bir `InvoiceDirection` üyesi açıldığında REDDEDEN taraf
#: artık `posting.cash_inflow`un kendisidir (`INVOICE_DIRECTION_UNMAPPED`, 422)
#: ve savunma o TEK kopyada yaşar.
#:
#: 🔴 KRIT-IADE — anahtar YÖN DEĞİL, **PARANIN AKIŞIDIR** (`posting.cash_inflow`).
#: Normal faturada ikisi aynıdır ve bu tablo DAVRANIŞ DEĞİŞTİRMEZ; İADE
#: faturasında ayrışırlar: giden bir iadenin parası DIŞARI akar, yani bağlanacak
#: evrak VERİLEN (`issued`) bir çektir. `direction`a bakan eski tablo o ödemeye
#: ALINAN bir çek bağlanmasını ister, `posting.lines_for` de onu `101 Alınan
#: Çekler`e ALACAK yazardı — elde olmayan bir çek portföyden düşerdi.
_UYUMLU_YON: dict[bool, FinancialInstrumentDirection] = {
    True: FinancialInstrumentDirection.received,
    False: FinancialInstrumentDirection.issued,
}


def _collected_source_status() -> InvoiceStatus:
    """`collected` damgasının TEK kaynak durumu — matristen TÜRETİLİR.

    Sabit `InvoiceStatus.sent` yazılsaydı geri düşüş matrisin İKİNCİ bir kopyası
    olurdu: `OUTGOING_TRANSITIONS` bir gün değişse damga eski hedefe düşmeye
    devam eder ve iki dosya sessizce ayrışırdı.
    """
    return next(
        durum
        for (durum, islem), hedef in transitions.OUTGOING_TRANSITIONS.items()
        if islem is InvoiceAction.mark_collected and hedef is InvoiceStatus.collected
    )


def derived_status(invoice: Invoice, paid_total: Decimal) -> InvoiceStatus:
    """🔴 K5 — faturanın `Σ payments`e göre OLMASI GEREKEN durumu (SAF: hiçbir şeyi değiştirmez).

    Yazma yolu, silme yolu ve silme ÖNİZLEMESİ AYNI kodu kullanır: iki yol için iki ayrı türetim
    yazılsaydı biri geri düşüşü unutur ve fatura hiç tahsilatı olmadan `collected` kalırdı —
    saklanan bir `paid_amount` olmadığı için hiçbir kolon farkı bunu ele vermezdi.

    GELEN faturaya hiç dokunulmaz: `collected` GİDEN makinenin terminalidir,
    gelen makinede (`pending → approved | disputed`) karşılığı YOKTUR ve
    ödemenin gelen tarafta bir durum üretmesi Hazine kapsamı DIŞIDIR (spec K5).
    """
    if invoice.direction is not InvoiceDirection.outgoing:
        return invoice.status

    if paid_total >= invoice.total:
        # Damga yalnız matrisin TANIDIĞI geçişle konur: `(draft, mark-collected)`
        # çifti tabloda YOKTUR ve burada uydurulmaz (bir taslak fatura ödenmiş
        # olsa bile GÖNDERİLMEMİŞTİR).
        gecerli = transitions.classify_transition(
            invoice.direction, invoice.status, InvoiceAction.mark_collected
        )
        if gecerli is None:
            return transitions.next_status(
                invoice.direction, invoice.status, InvoiceAction.mark_collected
            )
    elif invoice.status is InvoiceStatus.collected:
        # Geri düşüş: damganın dayanağı kalmadı. Yalnız `collected`ten olur —
        # koşulsuz yazılsaydı bir TASLAK fatura, ödemesi silinerek "gönderilmiş"
        # sayılırdı.
        return _collected_source_status()
    return invoice.status


def _rederive_status(invoice: Invoice, paid_total: Decimal) -> None:
    """Durumu `derived_status`a çeker (yazma ve silme yolunda AYNI kod)."""
    yeni = derived_status(invoice, paid_total)
    if yeni is not invoice.status:
        invoice.status = yeni


async def _locked_invoice(session: AsyncSession, actor: User, invoice_id: uuid.UUID) -> Invoice:
    """🔴 KİLİT ADIMI — her yazma yolunun İLK işi (K7).

    `visible_invoice(for_update=True)` hem satırı kilitler hem kapsamı denetler;
    kapsam denetimi KİLİTLİ satır üzerinde koşar, böylece kilit ile karar
    arasına başka bir işlem giremez.
    """
    return await invoicing_service.visible_invoice(session, actor, invoice_id, for_update=True)


async def _account_or_404(session: AsyncSession, account_id: uuid.UUID) -> BankAccount:
    """Gövde içi varlık referansı — yok ise **404** (ST kanonu).

    Kilit sırasının SON halkasıdır (fatura → ödemeler → hesap) ve hesap
    KİLİTLENMEZ: hiçbir eşik hesabın durumuna bağlı değildir (bakiye türevdir,
    K2) ve gereksiz bir kilit yalnızca çekişme üretirdi.

    ⚠️ `is_active` denetimi BURADA DEĞİL yalnız YAZMA yolundadır: pasif hesap
    yeni para KABUL ETMEZ ama oraya yanlışlıkla girilmiş bir ödemenin
    SİLİNEBİLMESİ gerekir — kural silmeye de uygulansaydı hesap kapatılır
    kapatılmaz hatası da kalıcılaşırdı.
    """
    account = await repository.get_account(session, account_id)
    if account is None:
        raise NotFoundError(PAYMENT_ACCOUNT_MISSING)
    return account


async def _instrument_or_none(
    session: AsyncSession, actor: User, invoice: Invoice, instrument_id: uuid.UUID | None
) -> FinancialInstrument | None:
    """🔴 FIN-PAY K2 + K3 — bağın TEK kapısı.

    **K1:** kimlik gönderilmemişse (ya da açıkça `null`sa) bağ YOKTUR ve hiçbir
    denetim koşmaz — alan isteğe bağlıdır, `method='cheque'` iken bile.

    **K2 — 404, sessiz `None` DEĞİL.** Gövdede bir kimlik gönderilip kayda
    `NULL` yazılsaydı kullanıcı bağladığını SANIRDI ve hiçbir ekran onu
    yalanlamazdı. FK ihlaline bırakmak da olmaz: ham `IntegrityError` **500**
    olarak sızardı. Okuma `instruments.service.visible_instrument`tan geçer —
    yani KAPSAM süzgeci de uygulanır: görünmeyen bir projenin çeki var
    olmayanla AYNI 404'ü alır (repo kanonu), aksi hâlde elinde kimlik olan
    kullanıcı o çekin varlığını doğrulayan bir yan kanal bulurdu.

    **K3 — YÖN UYUMU.** Tablo `_UYUMLU_YON`dedir ve burada `if direction == ...`
    YAZILMAZ; ikinci bir yazım bir gün tersine dönebilir ve iki yer sessizce
    ayrışırdı.

    🔴 **ODM-1 D4 — DURUM `portfolio` OLMALIDIR, yoksa 422.** FIN-PAY *"durum
    denetimi YOKTUR ve uydurulmaz"* diyordu; ODM-1 o kararı DEĞİŞTİRİR ve
    gerekçe fişlemeyle birlikte DOĞDU (o gün yoktu, bugün ölçüldü):

    * bağlı bir ödemenin nakit bacağı `101`/`103`e yazılır (D1);
    * `101`/`103`ü boşaltan TEK olay `instruments.service.change_status`ın
      `collected`/`paid` geçişidir;
    * `transitions.TERMINAL_STATUSES`ten **ÇIKIŞ YOKTUR**.

    Yani `collected`/`returned`/`cancelled` bir evraka yeni bir ödeme
    bağlanırsa `101` borçlanır ve onu boşaltacak geçiş bir daha ASLA doğamaz —
    **kalıcı bir `101` kalıntısı**, yani defterde sonsuza dek "yolda" görünen
    bir para. Kullanıcının bugünkü doğru yolu bağsız ödeme yazmaktır (o para
    zaten hesaba inmiştir).

    🔴 **SATIR KİLİTLENİR** (`for_update=True`) — ve bu karar ODM-1 içinde
    DEĞİŞTİ. FIN-PAY *"kilit alınmaz, burada bir eşik sayılmıyor"* demişti ve o
    gün DOĞRUYDU: bağın hiçbir mali sonucu yoktu, en kötü ihtimalle yanlış bir
    etiket yazılırdı. ODM-1 bağa BİR MALİ SONUÇ yükledi ve kusuru KENDİSİ
    doğurdu:

    * "ödeme yaz" (bu yol) enstrümanı okur ve D4 kapısından `portfolio` diye
      geçer;
    * "tahsil et" (`instruments.service.change_status`) satırı `FOR UPDATE` ile
      kilitler, `Σ` BAĞLI ÖDEMELERİ okur (D3) ve fişi o toplamdan yazar.

    İkisi eşzamanlı koşarsa tahsil fişi, henüz commit etmemiş yeni ödemeyi
    KAÇIRIR. Ödeme `101`i borçlandırır, tahsil fişi onu boşaltmaz ve
    `TERMINAL_STATUSES`ten ÇIKIŞ OLMADIĞI için o borcu boşaltacak ikinci bir
    geçiş bir daha DOĞAMAZ — D4'ün kapattığı kalıcı `101` kalıntısının
    eşzamanlılık hâli. Bakiye SAKLANMADIĞI için hiçbir kolon farkı ele vermez.

    🔴 Kilit DENETİMLERDEN ÖNCE alınır (EŞİK = KİLİT, İK-2): `visible_instrument`
    satırı önce kilitler, kapsam ve D4 durum denetimi KİLİTLİ satır üzerinde
    koşar. Kilit denetimden sonra alınsaydı TOCTOU penceresi açık kalırdı.
    Kilit tek başına da yetmez: `repository.get_instrument` `FOR UPDATE`
    sorgusunu `populate_existing=True` ile koşar, aksi hâlde kimlik
    haritasındaki BAYAT nesne dönerdi ve kilit hiçbir şey bekçilemezdi.

    🔴 DEADLOCK YOK — ölçüldü. Bu yolun kilit sırası
    `invoices → payments → financial_instruments`tır. Ters sırayla (enstrüman
    ÖNCE, fatura SONRA) kilitleyen bir yol YOKTUR: `change_status`,
    `update_instrument` ve `delete_instrument` YALNIZ `financial_instruments`
    satırını kilitler; ardından koştukları fişleme/storno yolu
    (`posting.post_document`, `accounting.state_service`) `journal_entries` ve
    danışma kilidi alır, `invoices`a HİÇ DOKUNMAZ.
    """
    if instrument_id is None:
        return None
    instrument = await instruments_service.visible_instrument(
        session, actor, instrument_id, for_update=True
    )
    if _UYUMLU_YON[posting.cash_inflow(invoice)] is not instrument.direction:
        raise TreasuryValidationError(PAYMENT_INSTRUMENT_DIRECTION_MISMATCH)
    if instrument.status is not FinancialInstrumentStatus.portfolio:
        raise TreasuryValidationError(PAYMENT_INSTRUMENT_NOT_PORTFOLIO)
    return instrument


async def _assert_instrument_deletable(session: AsyncSession, payment: Payment) -> None:
    """🔴 ODM-1 D5 — bağlı evrak `portfolio` DEĞİLSE silme **409**.

    Silme yolu fişi STORNO eder (KARAR-5): `120 B / 101 A`. Evrak hâlâ
    portföydeyse bu TAM DOĞRUDUR — ödeme fişi `101`i açmıştı, storno onu kapatır
    ve net sıfırlanır.

    Evrak tahsil edilmişse (`collected`/`paid`) tahsil fişi `101`i ZATEN
    boşaltmıştır. O hâlde storno `101`i **NEGATİFE** düşürür ve nakit hesabında
    kaynağı olmayan bir para bırakır: iki fiş de tek başına dengelidir, mizan
    doğru görünür ve kusur hiçbir kolon farkıyla ele verilmez (bu deponun tekrar
    tekrar ölçtüğü sınıf).

    `returned`/`cancelled` de kapsanır ve bu bilinçlidir: o geçişler ödemenin
    fişini ZATEN stornoladı (D6); ikinci bir storno `reverse_payment`ta sessizce
    `False` dönerdi (canlı fiş kalmadı) ve kullanıcı, mali izi silinmiş bir
    ödemeyi tabloda kaybederdi. Tek kural TEK cümleyle bildirilir: **portföy
    dışı evrakın ödemesi silinmez.**

    🔴 **SATIR KİLİTLENİR** — `_instrument_or_none` ile AYNI gerekçe (ODM-1'in
    kendi doğurduğu yarış): kilitsiz okunsaydı bir silme ile eşzamanlı bir
    `mark-collected` birbirini kaçırırdı — silme evrağı `portfolio` görüp ödeme
    fişini stornolar, tahsil geçişi ise AYNI ödemeyi Σ'ya katıp `101`i boşaltır
    ve ara hesap NEGATİFE düşerdi. Kilit sırası BOZULMAZ: `invoices` satırı
    zaten alınmıştır ve enstrüman ondan SONRA gelir — yazma yolundaki
    (`create_payment`) sırayla BİREBİR aynı, yani yeni bir deadlock halkası yok.

    Kapsam süzgeci burada KOŞMAZ (`visible_instrument` değil, depo doğrudan):
    ödemenin faturası ZATEN görünürdür ve bağ kolonu doludur; ikinci bir kapsam
    denetimi, silme yetkisi olan bir kullanıcıyı kendi kaydında 404'e düşürürdü.
    """
    if payment.financial_instrument_id is None:
        return
    instrument = await instruments_repository.get_instrument(
        session, payment.financial_instrument_id, for_update=True
    )
    # `SET NULL` FK'si yüzünden satır silinmiş olabilir: bağ kolonu doluyken
    # satırın YOK olması yapısal olarak imkânsızdır ama okuma yine de savunmalıdır
    # (yok ise engelleyecek bir durum da yoktur).
    if instrument is None:
        return
    if instrument.status is not FinancialInstrumentStatus.portfolio:
        raise ConflictError(PAYMENT_INSTRUMENT_NOT_PORTFOLIO_DELETE)


#: 🔴 ODM-HAK — faturanın KAYNAK kolonu → o kaynağın durum modeli ve `paid` üyesi.
#:
#: Tabloda YALNIZ iki hakediş ailesi vardır ve bu uydurulmadı: `mark-paid`i
#: `realized.assert_realized_covers` kapısından geçiren kaynaklar tam olarak
#: bunlardır (`realized.SOURCE_DIRECTION` ile AYNI iki kolon). Öteki kaynak
#: kolonlarının (`equipment_rental_invoice_id` · `purchase_order_id`) "ödendi"
#: damgası bu ödemelerden TÜRETİLMEZ, o yüzden burada yoktur — eklenseydi
#: ölçülmemiş bir kapı açılırdı.
_PAID_SOURCES = (
    ("progress_payment_id", ProgressPayment, ProgressPaymentStatus.paid),
    (
        "subcontractor_progress_payment_id",
        SubcontractorProgressPayment,
        SubcontractorPaymentStatus.paid,
    ),
)


async def _assert_source_not_paid(session: AsyncSession, invoice: Invoice) -> None:
    """🔴 ODM-HAK — **ödenmiş hakedişin ödemesi silinemez** → 409.

    `transitions.py`nin açıkça ERTELEDİĞİ hâl buydu: *"Ödeme sonradan silinir
    ya da çek karşılıksız çıkarsa ne olacağı AYRI bir karardır."* Çek yolu
    kapalıydı (`instruments/transitions.py`: `collected` TERMİNAL, geri dönüş
    çifti YOK), ama SİLME yolu açıktı ve evraksız bir `transfer` ödemesi
    `_assert_instrument_deletable`ın erken dönüşü yüzünden HER ZAMAN
    silinebiliyordu — silme yolu hakedişi HİÇ sormuyordu.

    Sonuç kalıcıydı: `paid` dört evrak ailesinde de TERMİNALDİR
    (`_TRANSITION_SHAPE`te hiçbir çiftin KAYNAĞI değil) ve `paid` bir hakediş
    ADMİN DAHİL silinemez (`progress_payments/service.py` 409). Yani hakediş,
    arkasında tek kuruş olmadan kalıcı bir "Ödendi" rozeti taşırdı.

    🔴 **KAPI İLERİ YÖNÜ AÇMAZ.** `paid → approved` geçişi AÇILMIYOR (o hâlâ
    ayrı bir ürün kararıdır); yalnız damganın ALTINDAKİ paranın çekilmesi
    engelleniyor. Kural tek cümledir: **ödenmiş hakedişin parası geri alınmaz.**

    🔴 **KAPSAM DAR: yalnız `paid`.** `draft`/`pending_approval`/`approved` bir
    hakedişin faturasında silme AÇIK KALIR — yoksa yanlış girilmiş bir tahsilatı
    düzeltmenin tek yolu (`DELETE` + yeniden yazma; `PATCH /payments/{id}`
    YOKTUR) kapanır ve kapı kusurdan daha pahalı olurdu.

    🔴 **SATIR KİLİTLENİR** — İK-2'nin "EŞİK = KİLİT" kanonu: kilitsiz okunsaydı
    eşzamanlı bir `mark-paid` ile bu silme birbirini KAÇIRIRDI (silme hakedişi
    `approved` görüp ödemeyi siler, `mark-paid` ise AYNI ödemeyi Σ'ya katıp
    `paid` damgalar) ve kusur tam olarak yeniden doğardı. Kilit sırası
    BOZULMAZ: `invoices` satırı ZATEN alınmıştır ve hakediş ondan SONRA gelir;
    ters yönden (hakediş → fatura) kilitleyen bir yol ölçüldü ve YOKTUR
    (`mark-paid` sözleşme → hakediş kilitler, fatura satırına HİÇ dokunmaz).

    Okuma yalnız `status` kolonudur: `ProgressPayment.lines` `selectin` ile
    gelir ve burada hiç gerekmez; ayrıca kolon seçimi kimlik haritasındaki bayat
    bir nesneyi değil DB'nin kilit altındaki değerini verir (`populate_existing`
    tuzağı).
    """
    for kolon, model, paid in _PAID_SOURCES:
        source_id = getattr(invoice, kolon)
        if source_id is None:
            continue
        durum = (
            await session.execute(
                select(model.status).where(model.id == source_id).with_for_update()
            )
        ).scalar_one_or_none()
        if durum is paid:
            raise ConflictError(PAYMENT_SOURCE_PAID)


# --- Uç 6: GET /invoices/{id}/payments ---


async def list_payments(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID, *, limit: int, offset: int
) -> PaymentListResponse:
    """FGI'nin tahsilat listesi + K5'in iki türev toplamı.

    🔴 `paid_total` ve `remaining` **TÜM satırlardan** gelir, sayfadan DEĞİL:
    sayfadan hesaplansaydı `limit`li bir okumada "kalan" birdenbire büyür,
    kullanıcı ekranda gördüğü tutarı girer ve K6'dan 422 alırdı.

    Kilit YOKTUR: okuma bir eşik kararı vermez. Sayfalama ise repo kanonu gereği
    vardır — fatura başına ödeme sayısı küçüktür ama SINIRSIZ bir liste ucu
    açmak, bir gün kalabalıklaşan bir faturada tavanı olmayan tek yer olurdu.
    """
    invoice = await invoicing_service.visible_invoice(session, actor, invoice_id)
    satirlar = await repository.list_payments_for_invoice(
        session, invoice.id, limit=limit, offset=offset
    )
    total = await repository.count_payments_for_invoice(session, invoice.id)
    paid_total = await repository.paid_total_for_invoice(session, invoice.id)
    return PaymentListResponse(
        items=[PaymentResponse.model_validate(satir) for satir in satirlar],
        total=total,
        limit=limit,
        offset=offset,
        paid_total=paid_total,
        remaining=invoice.total - paid_total,
    )


# --- Uç 7: POST /invoices/{id}/payments ---


async def create_payment(
    session: AsyncSession, actor: User, invoice_id: uuid.UUID, data: PaymentCreate
) -> tuple[Payment, str]:
    """🔴 K6 + K7 — sıra modül docstring'indedir ve DEĞİŞTİRİLEMEZ.

    Eşik KİLİTLİ satırın koruması altında okunur; kilit sonraya bırakılsaydı iki
    eşzamanlı istek AYNI `Σ`yı okur, ikisi de kapıdan geçer ve fatura tutarının
    iki katı tahsil edilmiş görünürdü (İK-3 dersi, `test_hz1_payment_lock.py`).

    Eşik MEVCUT toplamı içerir: yalnız `yeni.amount > total` denetlenseydi
    999,99 + 0,02 sessizce geçerdi.
    """
    invoice = await _locked_invoice(session, actor, invoice_id)
    paid_total = await repository.paid_total_for_invoice(session, invoice.id)
    account = await _account_or_404(session, data.bank_account_id)
    if not account.is_active:
        raise TreasuryValidationError(PAYMENT_ACCOUNT_INACTIVE)

    yeni_toplam = paid_total + data.amount
    if yeni_toplam > invoice.total:
        raise TreasuryValidationError(PAYMENT_EXCEEDS_TOTAL)

    # 🔴 FIN-PAY — çek/senet bağı, kilit sırasının SONUNA eklenir ve yukarıdaki
    # dört adıma (kilitli fatura → Σ oku → hesap → eşik) DOKUNMAZ. Araya
    # sokulsaydı belgelenmiş sıra bozulur ve eşik kararı ile kilit arasına yeni
    # bir sorgu girerdi. 🔴 ODM-1 — enstrüman satırı artık KİLİTLENİR ve kilit
    # sıranın SONUNDADIR (`invoices → payments → financial_instruments`);
    # gerekçe ve deadlock ölçümü `_instrument_or_none` docstring'indedir.
    instrument = await _instrument_or_none(session, actor, invoice, data.financial_instrument_id)

    payment = Payment(
        invoice_id=invoice.id,
        bank_account_id=account.id,
        # 🔴 K4 — ÇİFT SAYIM YOK: bu bir ETİKETTİR. `balance.py` bakiyeyi
        # `Σ payments.amount` üzerinden türetir ve bu satır o toplama HİÇBİR
        # girdi eklemez; çek/senet portföyü AYRI bir yüzeydir.
        financial_instrument_id=None if instrument is None else instrument.id,
        method=data.method,
        amount=data.amount,
        paid_on=data.paid_on,
        note=data.note,
        created_by_id=actor.id,
    )
    session.add(payment)
    await session.flush()
    # `updated_at`/`created_at` sunucu damgalarıdır; yanıt şeması onları okuduğunda
    # async bağlamda tembel yükleme `MissingGreenlet` = 500 demektir (P11 dersi).
    await session.refresh(payment)

    # 🔴 MU-3C — NAKİT BACAĞI. Ödeme satırından SONRA ve AYNI transaction'da:
    #     fiş yazılamazsa (kapalı dönem 409 · eksik eşleme 422) ÖDEME DE GERİ
    #     ALINIR, yani "parası girmiş ama fişsiz" bir tahsilat DOĞMAZ.
    #
    #     🔴 ÇİFT SAYIM YOK: bacaklar yalnız cariyi (`120`/`320`) ve nakdi
    #     (`102`/`100`) taşır — faturanın gider/hasılatı MU-3B'de ZATEN
    #     yazılmıştır ve buradan bir daha yazılmaz (`treasury/posting.py`).
    #
    #     Kilit sırası KORUNUR: fatura → Σ → hesap → eşik → (enstrüman) → fiş.
    #     `post_document` kendi danışma kilidini EN SONDA alır, yani belgelenmiş
    #     satır kilidi sırasına yeni bir halka SOKMAZ.
    await posting.post_payment(session, actor, payment, invoice, account)

    _rederive_status(invoice, yeni_toplam)
    await session.flush()
    detail = messages.payment_created(invoice.invoice_no, account.bank_name, account.display_name)
    return payment, detail


# --- Uç 8: DELETE /payments/{id} ---


async def delete_payment(session: AsyncSession, actor: User, payment_id: uuid.UUID) -> str:
    """Yanlış tahsilat geri alınabilmelidir — YALNIZ `admin` (kapı router'da).

    🔴 Kilit sırası burada da SABİTTİR ve ödeme satırı kilidin ARDINDAN yeniden
    okunur: ilk okuma yalnızca faturanın KİMLİĞİNİ öğrenmek içindir, karar
    değildir. Kilit alındıktan sonra taze okuma yapılmasaydı iki eşzamanlı silme
    aynı satırı görür ve ikincisi bayat bir nesneyi silmeye çalışırdı.

    Silme sonrası durum **YENİDEN TÜRETİLİR** (K5): `collected` → `sent`e
    düşebilir. Türetim `create_payment` ile AYNI fonksiyondan geçer.
    """
    ilk_okuma = await repository.get_payment(session, payment_id)
    if ilk_okuma is None:
        raise NotFoundError(PAYMENT_MISSING)

    try:
        invoice = await _locked_invoice(session, actor, ilk_okuma.invoice_id)
    except NotFoundError as exc:
        # Faturası görünmeyen ödeme de "yok"tur: `invoicing`in "Fatura
        # bulunamadı" cümlesi buraya SIZDIRILMAZ, yoksa kullanıcı ödemenin var
        # olduğunu (ve görünmeyen bir faturaya ait olduğunu) öğrenirdi.
        raise NotFoundError(PAYMENT_MISSING) from exc

    payment = await repository.get_payment(session, payment_id, for_update=True)
    if payment is None:
        raise NotFoundError(PAYMENT_MISSING)

    # Hesap kilit sırasının SON halkasıdır ve yalnız denetim METNİ için okunur;
    # `bank_account_id` NOT NULL + FK RESTRICT olduğu için satır YAPISAL OLARAK
    # vardır (404 dalı ulaşılamazdır ama korkuluk olarak durur).
    await _assert_instrument_deletable(session, payment)
    await _assert_source_not_paid(session, invoice)

    account = await _account_or_404(session, payment.bank_account_id)
    # Denetim metni silmeden ÖNCE kurulur; sonra kurulsaydı hesap/numara
    # güvenilir okunamaz ve silinenin NE OLDUĞU kaybolurdu.
    detail = messages.payment_deleted(invoice.invoice_no, account.bank_name, account.display_name)

    # 🔴 MU-3C · KARAR-5 — GERİ ALMA = STORNO, silme DEĞİL. Ödeme satırı
    #     SİLİNMEDEN ÖNCE stornolanır: silindikten sonra `payment.id` hâlâ
    #     okunabilir olsa da, sıra tersine dönseydi bir hata hâlinde ödemesi
    #     silinmiş ama fişi CANLI kalan bir mali iz doğabilirdi.
    #
    #     Storno BUGÜNE yazılır ve o ayın dönemi kapalıysa **409** gelir; ödeme
    #     o zaman HİÇ silinmez (KARAR-6). MU-3C öncesi yazılmış, fişi hiç
    #     doğmamış ödemelerde `reverse_payment` `False` döner ve hiçbir şey
    #     yazılmaz.
    await posting.reverse_payment(session, actor, payment.id)

    await session.delete(payment)
    await session.flush()

    _rederive_status(invoice, await repository.paid_total_for_invoice(session, invoice.id))
    await session.flush()
    return detail
