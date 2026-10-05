"""Yevmiye fişi iş kuralları (MU-1 T3b) — liste · oluştur · detay · PATCH ·
`PUT lines` · DELETE (spec §7 yolları 6, 7, 9, 10, 11, 12).

Durum GEÇİŞLERİ (`post`/`reverse`) burada DEĞİL `state_service.py`dedir ve bu
ayrım bilinçlidir (FAT-1 emsali): bu dosya fişin İÇERİĞİNİ yazar, öteki yalnız
`status` damgalar ve stornoyu üretir. Tek dosyada toplansalardı 800 satır
tavanına doğru itilirdi (SA'nın 973 satırlık borcu TEKRARLANMAZ).

## 🔴 Bu dosyanın YAZMADIĞI üç şey

1. **Bakiye.** Hiçbir toplam burada TÜRETİLMEZ; `balance.py` (K3) ve `ledger.py`
   ayrı kaynaklardır. Buradaki `total_debit`/`total_credit` bir bakiye değil,
   K1'in DB katmanının ihtiyaç duyduğu BAŞLIK TOPLAMIDIR (spec §4).
2. **Durum kararı.** `if status == …` YOKTUR: kapılar `transitions.py`dedir.
3. **Yetki.** Üç kapı (`view`/`full`/`admin`) router'dadır.

## 🔴 KAPSAM SÜZGECİ YOKTUR (IDOR unutulmuş DEĞİLDİR)

`journal_entries`/`journal_lines` tablolarında `project_id`/`site_id` YOKTUR
(spec §3): E8'in altı sütununda hiçbir proje/şantiye alanı çizilmemiştir;
E8:113'teki `– Güneşkent` SERBEST METNİN içindedir. Bu yüzden `visible_projects`
çağrısı yoktur ve "görünmeyen kayıt" hâli de yoktur — 404 yalnız var OLMAYAN
kimlik içindir. Maliyet merkezi/proje kırılımı **MU-3**'ün işidir.

## 🔴 `_apply_totals` — K1'in TEK yazım yolu

Toplamlar TÜREV oldukları hâlde başlıkta SAKLANIR, çünkü bir CHECK **başka
satırların toplamını GÖREMEZ** (`treasury/models.py` aşırı-tahsilat notunun
aynısı) ve K1 "DB düzeyinde korunur" diyorsa toplam bir KOLON olmak zorundadır.
Sapma penceresi kapalıdır: satırlar YALNIZ `draft`ta ve YALNIZ bu fonksiyondan
geçerek yazılır; `posted`tan sonra satırlar değişmez (K2) ve CHECK `draft`
DIŞINDAKİ her deftere-giren durumda ısırır (`posted` + `reversed`, TB6 T2) —
pencere ile kısıt TAM ÖRTÜŞÜR.

İkinci bir yazım açılsaydı `ck_journal_entries_posting_balanced` o yolda SESSİZCE
devre dışı kalırdı: dengesiz satır kümesi, dengeli görünen bir başlıkla
`posted` olabilirdi.

## Hangi kural hangi koda düşer

| Durum | Kod | Sınıf |
|---|---|---|
| Var olmayan fiş | 404 | `NotFoundError` |
| 🔴 Gövde içi HESAP referansı yok | **404** | `NotFoundError` (ST kanonu) |
| Biçim ihlali (tutar, tek taraf, `limit` tavanı, türev alan) | 422 | Pydantic |
| Alanlar-arası kural (denge · satır sayısı · yaprak) | 422 | `validation.py` |
| Düzenlemeye/silmeye kapalı DURUM | 409 | `ConflictError` |

Bu modül BAŞKA BİR MODÜLÜ IMPORT ETMEZ (`audit` hariç — denetim metinleri repo
genelinde tek dosyadadır).
"""

import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import AccountingValidationError, ConflictError, NotFoundError
from app.core.timezone import today
from app.modules.accounting import (
    guards,
    numbering,
    periods_service,
    repository,
    transitions,
    validation,
)
from app.modules.accounting.models import (
    ChartAccount,
    JournalEntry,
    JournalEntryStatus,
    JournalLine,
)
from app.modules.accounting.schemas import (
    JournalEntryCreate,
    JournalEntryDetailResponse,
    JournalEntryListResponse,
    JournalEntryResponse,
    JournalEntryUpdate,
    JournalLineInput,
    JournalLineResponse,
    JournalLinesReplace,
)
from app.modules.audit import messages
from app.modules.users.models import User

__all__ = [
    "apply_totals",
    "assert_entry_date_not_future",
    "build_detail",
    "create_entry",
    "entry_for_write",
    "entry_or_404",
    "gate_lines",
    "list_entries",
    "raise_blockers",
    "replace_lines",
    "update_entry",
]

_ENGEL_AYRACI = " · "
_ZERO = Decimal("0")


def raise_blockers(engeller: list[str]) -> None:
    """Engellerin HEPSİ TEK 422'de gösterilir (FAT-1 `_raise_blockers` deseni).

    Çok satırlı bir fişte eksikleri birer birer keşfettirmek kabul edilemez;
    ayraç ve sıra sabittir ki aynı hata iki kez alındığında "başka bir hata"
    izlenimi doğmasın.
    """
    if engeller:
        raise AccountingValidationError(_ENGEL_AYRACI.join(engeller))


def assert_entry_date_not_future(entry_date: date) -> None:
    """🔴 `entry_date`in ÜST SINIRI — **422**, İKİ yazma yolunda da koşar.

    Bugün TAM SINIRDA GEÇER (`>`, `>=` DEĞİL): bugünün fişi kesilemeseydi kapı
    kuralı değil KULLANIMI engellerdi (T6/K6 sınır dersi).

    "Bugün" `timezone.today()`dir, `date.today()` DEĞİL: ikincisi sunucunun
    yerel saatini (Railway'de UTC) okur ve TR gecesi 21:00-24:00 arasında DÜNÜ
    döndürerek bugünün fişini reddederdi (TB5 bekçisi bunu ayrıca kırmızıya
    çevirir). Çağrı SERVİS SINIRINDA yapılır ve aşağı geçirilmez — `sales`
    emsalinin aynısı; şemaya konsaydı saf çekirdek takvimi bilir hâle gelirdi.

    Kapı `assert_periods_open`tan ÖNCE koşar: sonra koşsaydı reddedilen istek
    bile gelecek bir dönem satırını `open` olarak DOĞURURDU (`lock_period`
    UPSERT'tir ve "yoksa doğar" der).
    """
    if entry_date > today():
        raise AccountingValidationError(guards.ENTRY_DATE_IN_FUTURE)


async def entry_or_404(
    session: AsyncSession, entry_id: uuid.UUID, *, for_update: bool = False
) -> JournalEntry:
    """Tekil erişimin TEK kapısı — okuma da yazma da buradan geçer.

    `for_update=True` satırı DENETİMLERDEN ÖNCE kilitler (EŞİK = KİLİT):
    kilit ile karar arasına başka bir işlem giremez.
    """
    entry = await repository.get_entry(session, entry_id, for_update=for_update)
    if entry is None:
        raise NotFoundError(guards.JOURNAL_ENTRY_MISSING)
    return entry


async def entry_for_write(
    session: AsyncSession,
    entry_id: uuid.UUID,
    *,
    extra_periods: Sequence[periods_service.Period] = (),
) -> JournalEntry:
    """🔴 FİŞE BAĞLI BEŞ YAZMA UCUNUN TEK GİRİŞİ (`PATCH` · `DELETE` ·
    `PUT lines` · `post` · `reverse`).

    Kilit sırası **SABİT ve GLOBAL**: `accounting_periods` → `journal_entries` →
    `journal_lines` → `chart_of_accounts` (ayrıntı `periods_service.py` modül
    docstring'i). Adımlar:

        1. BAKIŞ  — fişin dönemini öğrenmek için KİLİTSİZ okuma (yoksa 404)
        2. DÖNEM  — bakıştaki dönem + `extra_periods` KİLİTLENİR, kapalıysa 409
        3. FİŞ    — `for_update=True` (mevcut TOCTOU kilidi, DEĞİŞMEDİ)
        4. EMNİYET— fişin dönemi bakıştan farklıysa 409 (`PERIOD_MOVED`)

    **1. adım neden kilitsiz:** dönemi kilitlemek için hangi dönem olduğunu
    bilmek gerekir; bu bir tavuk-yumurta değil, sıralamadır. Bakış hiçbir KARARA
    dayanak DEĞİLDİR — kapalı/açık kararı 2. adımda KİLİTLİ dönem satırından,
    durum kararı 3. adımdan sonra KİLİTLİ fişten okunur.

    **4. adım neden var:** bakış ile kilit arasında eşzamanlı bir
    `PATCH entry_date` fişi başka bir döneme taşımış olabilir; o hâlde elimizdeki
    dönem kilidi YANLIŞ satırdadır. Pratikte ulaşılması çok zordur (taşıyan
    isteğin ESKİ dönemi de kilitlemesi gerekir, yani bizimle serileşir) ama
    "zor" ≠ "imkânsız"tır ve sessizce yanlış kilitle karar vermektense 409
    dönmek doğrudur.

    🔴 `extra_periods` YALNIZ iki yolda doludur: `PATCH` yeni `entry_date`in
    dönemini, `reverse` STORNONUN dönemini (`timezone.today()`) ekler. İkisi de
    sıralanarak kilitlenir (deadlock önlemi).
    """
    bakis = await entry_or_404(session, entry_id)
    donemler = {(bakis.period_year, bakis.period_month), *extra_periods}
    await periods_service.assert_periods_open(session, donemler)

    entry = await entry_or_404(session, entry_id, for_update=True)
    if (entry.period_year, entry.period_month) not in donemler:
        raise ConflictError(guards.PERIOD_MOVED)
    return entry


async def _resolve_accounts(
    session: AsyncSession, lines: Sequence[JournalLineInput]
) -> list[ChartAccount]:
    """Gövdedeki hesap referanslarını TEK sorguda çözer; eksikse **404**.

    🔴 ST kanonu: gövde içi VARLIK referansı 404'tür, 422 DEĞİL — istenen şey
    bir biçim değil, var olmayan bir KAYITTIR. Sıra gövdedekiyle AYNI kalır ki
    yaprak denetimi ve satır kurulumu aynı diziyi görsün.
    """
    hesaplar = await repository.accounts_by_ids(session, [satir.account_id for satir in lines])
    cozulen: list[ChartAccount] = []
    for satir in lines:
        hesap = hesaplar.get(satir.account_id)
        if hesap is None:
            raise NotFoundError(guards.LINE_ACCOUNT_MISSING)
        cozulen.append(hesap)
    return cozulen


async def gate_lines(
    session: AsyncSession,
    lines: Sequence[JournalLineInput],
) -> list[ChartAccount]:
    """🔴 K1 kapısının gövde tarafındaki TEK çağrı noktası (POST + `PUT lines`).

    Sıra bilinçlidir: **önce 404** (referans çözümü), sonra **422** (K1). Ters
    olsaydı var olmayan bir hesaba kesilen dengesiz bir fiş "dengesiz" diye
    reddedilir, kullanıcı asıl sorunu (hesap yok) hiç öğrenemezdi.
    """
    hesaplar = await _resolve_accounts(session, lines)
    raise_blockers(await validation.balance_blockers(session, lines, hesaplar))
    return hesaplar


def apply_totals(entry: JournalEntry, lines: Sequence[JournalLineInput | JournalLine]) -> None:
    """🔴 K1'in başlık ayağı — başlık toplamlarının TEK yazım yolu.

    `_ZERO` başlangıcı ŞARTTIR: `sum([])` bir `int` `0` döndürür ve `Numeric`
    kolonuna yazıldığında para tipi kayan bir tabana düşebilirdi.
    """
    entry.total_debit = sum((satir.debit for satir in lines), _ZERO)
    entry.total_credit = sum((satir.credit for satir in lines), _ZERO)


def _new_lines(entry_id: uuid.UUID, lines: Sequence[JournalLineInput]) -> list[JournalLine]:
    """Bacakları gövdedeki SIRAYLA kurar; `sort_order` DİZİNİN KENDİSİDİR.

    Kolonun sunucu varsayılanı YOKTUR (spec §3c): varsayılan 0 olsaydı eksik
    dolduran bir yol tüm satırları aynı sırada bırakır ve koşan bakiyenin
    kanonik sıralaması (§6e) bozulurdu.
    """
    return [
        JournalLine(
            entry_id=entry_id,
            sort_order=sira,
            account_id=satir.account_id,
            debit=satir.debit,
            credit=satir.credit,
        )
        for sira, satir in enumerate(lines)
    ]


def _apply_period(entry: JournalEntry, entry_date) -> None:  # noqa: ANN001
    """Dönem `entry_date`ten TÜREtilir — ikisi birlikte taşınır (K9).

    Ayrı bırakılsaydı `ck_journal_entries_period_matches_date` ihlal edilir ve
    kullanıcıya ayrımsız bir 409 giderdi. Kolonlar türetilebilir oldukları hâlde
    VARDIR çünkü MU-2 dönem kilidini `(period_year, period_month)` üzerinden
    alacaktır; CHECK ikisini uzlaştırır: kolon vardır ve KAYAMAZ.
    """
    entry.entry_date = entry_date
    entry.period_year = entry_date.year
    entry.period_month = entry_date.month


# --- Yanıt kurulumu ---


async def build_detail(session: AsyncSession, entry: JournalEntry) -> JournalEntryDetailResponse:
    """Başlık + bacaklar — YEDİ ucun ortak yanıt kurucusu.

    Ayrı ayrı kurulsalardı `POST` ile `PATCH` farklı alan kümeleri basar ve
    frontend hangi ucun ne döndüğünü kodun iki köşesinden okurdu.
    """
    satirlar = await repository.load_lines_with_accounts(session, entry.id)
    return JournalEntryDetailResponse(
        **JournalEntryResponse.model_validate(entry).model_dump(),
        lines=[
            JournalLineResponse(
                id=satir.id,
                sort_order=satir.sort_order,
                account_id=hesap.id,
                account_code=hesap.code,
                account_name=hesap.name,
                debit=satir.debit,
                credit=satir.credit,
            )
            for satir, hesap in satirlar
        ],
    )


# --- Uç 6: liste ---


async def list_entries(
    session: AsyncSession,
    *,
    status: JournalEntryStatus | None,
    year: int | None,
    month: int | None,
    limit: int,
    offset: int,
) -> JournalEntryListResponse:
    """🔴 ONAYLI SAPMA (K-Ş4): mockup'ta fiş listesi ekranı YOKTUR.

    Yine de vardır çünkü K2 gereği `draft` fişler deftere (`/journal`) GİRMEZ:
    bu uç olmasaydı açılan bir taslağı bulup kayıtlaştırmanın BAŞKA HİÇBİR YOLU
    kalmazdı. Yapısal bir boşluğu kapatır, "mockup'ta yok" diye geri alınmaz.

    `total` liste ile AYNI süzgeçten geçer; ayrışsaydı fişler "sayfa dışında
    kalmış" gibi görünürdü.
    """
    satirlar = await repository.list_entries(
        session, status=status, year=year, month=month, limit=limit, offset=offset
    )
    total = await repository.count_entries(session, status=status, year=year, month=month)
    return JournalEntryListResponse(
        items=[JournalEntryResponse.model_validate(entry) for entry in satirlar],
        total=total,
        limit=limit,
        offset=offset,
    )


# --- Uç 7: oluştur ---


async def create_entry(
    session: AsyncSession, actor: User, data: JournalEntryCreate
) -> tuple[JournalEntry, str]:
    """Başlık + bacaklar ATOMİK yazılır: doğrulamaların HEPSİ yazımdan ÖNCEDİR.

    Sıra: (1) hesap referansları **404** · (2) K1 kapısı **422** · (3) yazım.
    Bozuk bir satır varsa HİÇBİR ŞEY yazılmaz — ne başlık ne bacak.

    🔴 **DURUM SUNUCUDAN gelir** (`transitions.INITIAL_STATUS`): gövde `status`
    gönderemez (şema 422), yoksa istemci taslak aşamasını atlayıp doğrudan
    `posted` yazabilirdi.

    🔴 **DÖNEM KAPISI EN ÖNDEDİR** (MU-2 T3): hedef dönem KİLİTLENEREK okunur ve
    kapalıysa **409**. Kilit sırasının başı burasıdır — eşzamanlı bir `close` ile
    serileşiriz, aksi hâlde kapanışın taslak sayımı ile bu INSERT arasındaki
    pencereden taze bir fiş kapalı döneme düşerdi. Kapı K1'den de ÖNCEDİR:
    kapalı bir aya kesilen fişin dengeli olup olmadığı ilgisizdir.

    🔴 **TEK İSTİSNA — `assert_entry_date_not_future` dönem kapısından da
    ÖNCEDİR.** Sıra bilinçlidir: `lock_period` istenen dönemi YOKSA `open`
    olarak DOĞURUR, dolayısıyla dönem kapısı önce koşsaydı REDDEDİLEN bir istek
    bile gelecek bir dönem satırı bırakırdı.
    """
    assert_entry_date_not_future(data.entry_date)
    await periods_service.assert_periods_open(session, [periods_service.period_of(data.entry_date)])
    await gate_lines(session, data.lines)

    entry = JournalEntry(
        description=data.description.strip(),
        detail_note=data.detail_note,
        status=transitions.INITIAL_STATUS,
        created_by_id=actor.id,
    )
    _apply_period(entry, data.entry_date)
    apply_totals(entry, data.lines)
    # 🔑 FIS-NO — numara TASLAK AÇILIRKEN verilir (karar 3) ve yıl
    # `period_year` KOLONUNDAN okunur (karar 1), `data.entry_date`ten DEĞİL:
    # ikisi `ck_journal_entries_period_matches_date` ile zaten kilitlidir ve
    # numaranın yılı fişin dönemiyle TEK kaynaktan gelmelidir.
    entry.entry_no = await numbering.generate_entry_no(session, year=entry.period_year)
    session.add(entry)
    await session.flush()

    session.add_all(_new_lines(entry.id, data.lines))
    await session.flush()
    await session.refresh(entry)
    return entry, messages.journal_entry_created(entry.entry_date, entry.description)


# --- Uç 10: PATCH ---


async def update_entry(
    session: AsyncSession, entry: JournalEntry, data: JournalEntryUpdate
) -> tuple[JournalEntry, str]:
    """Kısmi güncelleme — YALNIZ `draft` (aksi **409**, 403 DEĞİL).

    Kayıt çağıranda DENETİMLERDEN ÖNCE kilitlenmiştir (TOCTOU).

    `exclude_unset` ŞARTTIR: gönderilmeyen alan ile gönderilen alan AYNI şey
    değildir (İK dersi). `entry_date`/`description` kolonları NULLABLE olmadığı
    için açıkça `null` göndermek bir TEMİZLEME değildir ve "değişmedi" sayılır;
    🔴 `detail_note` bunun İSTİSNASIDIR — kolonu nullable'dır ve açık `null` onu
    GERÇEKTEN temizler.

    Toplamlara DOKUNULMAZ: satır kümesi değişmediği için başlık toplamı da
    değişmez; buradan yazılsaydı `apply_totals` ikinci bir yazım yolu kazanırdı.
    """
    transitions.assert_editable(entry.status)
    verilen = data.model_dump(exclude_unset=True)

    if verilen.get("entry_date") is not None:
        assert_entry_date_not_future(verilen["entry_date"])
        _apply_period(entry, verilen["entry_date"])
    if verilen.get("description") is not None:
        entry.description = verilen["description"].strip()
    if "detail_note" in verilen:
        entry.detail_note = verilen["detail_note"]

    await session.flush()
    # `updated_at` SUNUCU damgasıdır (`onupdate=func.now()`); UPDATE'ten sonra
    # ORM'deki değer BAYATTIR ve yanıt şeması onu okuduğunda async bağlamda
    # `MissingGreenlet` = 500 olurdu (P11 dersi).
    await session.refresh(entry)
    return entry, messages.journal_entry_updated(entry.entry_date, entry.description)


# --- Uç 12: PUT lines ---


async def replace_lines(
    session: AsyncSession, entry: JournalEntry, data: JournalLinesReplace
) -> tuple[JournalEntry, str]:
    """Bacak kümesini TOPTAN yazar — YALNIZ `draft`.

    🔴 R5: "posted fişin satırı UPDATE edilemez" iddiası DB'de ZORLANAMAZ (repo
    hiçbir yerde trigger kullanmıyor). Satır yazan TEK yol budur ve
    `assert_lines_editable` burada koşar; kapı düşerse iddia da düşer.

    Kilit sırası (fiş → satırlar) korunur: başlık çağıranda kilitlenmiştir,
    bacaklar burada silinir.

    K1 kapısı burada da koşar; boş küme "en az iki satır" engeline takılır ve
    422 döner — dengesiz bir taslak API üzerinden hiç doğmaz.
    """
    transitions.assert_lines_editable(entry.status)
    await gate_lines(session, data.lines)

    await repository.delete_lines(session, entry.id)
    await session.flush()

    session.add_all(_new_lines(entry.id, data.lines))
    apply_totals(entry, data.lines)
    await session.flush()
    await session.refresh(entry)
    return entry, messages.journal_entry_lines_replaced(entry.entry_date, entry.description)


# --- Uç 11: DELETE ---
