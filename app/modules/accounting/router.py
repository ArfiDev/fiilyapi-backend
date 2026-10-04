"""Yevmiye uçları (MU-1 T3b) — spec §7'nin 6-14 numaralı yolları.

Kapı **`accounting`** iznidir (spec §2/K8, seed'de HAZIR — 🔴 **matris
DEĞİŞMEDİ, yeni izin modülü AÇILMADI, izin migration'ı YOKTUR**). Seviye sırası
`none < view < draft < request < approve < full < admin` (`app/core/access.py`):

| Uç | Yetki |
|---|---|
| `GET /journal-entries` | `view` |
| `POST /journal-entries` | `full` → **201** |
| `GET /journal-entries/summary` | `view` |
| `GET /journal-entries/{id}` | `view` |
| `PATCH /journal-entries/{id}` | `full` |
| `DELETE /journal-entries/{id}` | **`admin`** → 204 |
| `PUT /journal-entries/{id}/lines` | `full` |
| `POST /journal-entries/{id}/post` | `full` |
| `POST /journal-entries/{id}/reverse` | `full` → **201** |
| `GET /journal` | `view` |
| `GET /journal/export.xlsx` | `view` |

**Neden `DELETE` düz `admin`:** `full` silmeyi KAPSAMAZ (repo kanonu) ve
`procurement`in `can_delete` taslak istisnası burada GEÇERSİZDİR — fişin sahibi
onu giren muhasebeci değil ŞİRKETTİR (`invoicing` gerekçesinin birebiri).

**`posted` fişte 409, 403 DEĞİL:** kullanıcının yetkisi VARDIR; engelleyen şey
kaydın DURUMUDUR.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
altı yazma ucunun her biri TEK denetim satırı yazar ve metin servis katmanında,
kayıt değişmeden/yok olmadan ÖNCE kurulur. 🔴 Yeni `AuditAction` üyesi AÇILMAZ.

## 🔴 ROTA SIRASI (MK-2 dersi — `main.py:102-111`)

`/journal-entries/summary` **İKİ SEGMENTLİDİR** ve `/journal-entries/{entry_id}`
(UUID) ile AYNI şekli taşır; FastAPI yolları **KAYIT SIRASINA** göre eşler.
Sonra kaydedilseydi `summary` bir UUID sanılır ve uç **422**'ye düşerdi. Yeri
aşağıda AYRILMIŞTIR ve kural bir bekçi testiyle kilitlidir
(`test_rota_sirasi_summary_UUID_SANILMAZ`).

`/journal` ile `/journal-entries` AYRI köklerdir ve çakışmazlar: FastAPI
segment bazında eşler, önek benzerliği eşleşme üretmez.

## AÇILMAYAN uçlar (spec §9, icat yasağı)

Mizan (HP:33) · KDV beyanı (HP:36) · mali tablolar (HP:38) `reports_router`a
açıldı; banka mutabakatı (HP:34) hâlâ AÇILMAMIŞTIR. Fatura/hazine/bordro →
otomatik fiş üretimi **MU-3**'tür.

`Dışa Aktar` (E8:66) EXPORT-XLSX'te açıldı: `GET /journal/export.xlsx`, defter
ucuyla AYNI `build_ledger` çağrısından beslenir ve `limit=None` ile ayın
TAMAMINI yazar.

🔴 `/journal/export.xlsx` iki segmentlidir ama `/journal/{param}` biçiminde bir
rota YOKTUR — çakışma yoktur ve sıra serbesttir.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES, DELETE_403_YANITI
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.modules.accounting import (
    export,
    guards,
    ledger,
    periods_service,
    service,
    state_service,
    summary,
)
from app.modules.accounting.models import JournalEntryStatus
from app.modules.accounting.schemas import (
    JournalEntryCreate,
    JournalEntryDetailResponse,
    JournalEntryListResponse,
    JournalEntryUpdate,
    JournalLinesReplace,
    JournalSummaryResponse,
    LedgerResponse,
)
from app.modules.accounting.transitions import JournalAction
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.users.models import User

router = APIRouter(tags=["accounting"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(guards.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(guards.PERMISSION_MODULE, AccessLevel.full)
#: IZN-B2: fiş Kaydet (post) = Yevmiye sayfası ONAYLAR (eşik eski `full`).
_POST_APPROVE = require_pages(("mali.yevmiye",), "approve")

# K7 sayfalama standardı: varsayılan 50, tavan 200 — tavan aşımı sessizce
# KIRPILMAZ, 422 döner (ST/SA/`invoicing`/`treasury` ile birebir).
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]
# Dönem penceresi. `month` üst sınırı 12'dir: 13. ay bir yazım hatasıdır ve
# sessizce boş küme dönmek yerine 422 almak kullanıcıya durumu söyler.
_YEAR = Annotated[int | None, Query(ge=1900, le=2999)]
_MONTH = Annotated[int | None, Query(ge=1, le=12)]

_NOT_FOUND = {404: {"description": "Fiş bulunamadı"}}
_DURUM_CAKISMASI = {409: {"description": "Fiş bu işlem/düzenleme için uygun durumda değil"}}
_K1 = {422: {"description": "Denge · en az iki satır · yaprak hesap kuralı ihlali"}}


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satırı (B5 deseni). Metin PARAMETREDİR, burada kurulmaz.

    🔴 Yeni `AuditAction` üyesi AÇILMADI (TB3/T3 kanonu): `action` gerçek bir
    Postgres enum tipidir ve yeni üye migration ister. Ayrım METİNDEDİR.
    """
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


# --- Uç 6: fiş listesi ---


@router.get("/journal-entries", response_model=JournalEntryListResponse, dependencies=[_VIEW])
async def list_journal_entries_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    status_filter: Annotated[JournalEntryStatus | None, Query(alias="status")] = None,
    year: _YEAR = None,
    month: _MONTH = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> JournalEntryListResponse:
    """🔴 **ONAYLI SAPMA (K-Ş4):** mockup'ta fiş listesi ekranı YOKTUR.

    Yine de vardır çünkü K2 gereği `draft` fişler deftere (`/journal`) GİRMEZ:
    bu uç olmasaydı açılan bir taslağı bulup kayıtlaştırmanın BAŞKA HİÇBİR YOLU
    kalmazdı. Yapısal bir boşluğu kapatır ve "mockup'ta yok" diye geri alınmaz.

    Süzgeçler: `status` · `year` · `month`. `limit` varsayılan 50, tavan 200 —
    aşım **422** (kırpma DEĞİL). Sıralama `entry_date DESC` ve son ölçütü `id`dir
    (aynı gün girilen iki fiş keyfî sırada dönmesin).

    🔴 **Proje/şantiye kapsam süzgeci YOKTUR (spec §3):** üç tabloda da böyle bir
    kolon yoktur; erişimi `accounting` izni denetler.
    """
    return await service.list_entries(
        session, status=status_filter, year=year, month=month, limit=limit, offset=offset
    )


# --- Uç 7: oluştur ---


@router.post(
    "/journal-entries",
    response_model=JournalEntryDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={404: {"description": "Fiş satırındaki hesap bulunamadı"}, **_K1},
    dependencies=[_FULL],
)
async def create_journal_entry_endpoint(
    request: Request,
    data: JournalEntryCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> JournalEntryDetailResponse:
    """Yeni fiş (E8:67 `+ Yevmiye Kaydı`) — başlık + bacaklar TEK gövde, ATOMİK.

    * durum **SUNUCUDAN** gelir (`INITIAL_STATUS` = `draft`); gövde `status`
      GÖNDEREMEZ (**422**)
    * 🔴 **K1 kapısı:** `Σ borç = Σ alacak` · en az iki satır · yalnız yaprak
      hesap — üç engel TEK **422**'de toplanır
    * 🔴 gövde içi hesap referansı yoksa **404** (biçim hatası değil, var
      olmayan KAYIT)
    * `total_debit`/`total_credit`/`period_*`/`reversal_of_id` gövdeden GELEMEZ
      (**422**): türev ya da sunucu alanlarıdır

    Bozuk bir satır varsa HİÇBİR ŞEY yazılmaz — ne başlık ne bacak.
    """
    entry, detail = await service.create_entry(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await service.build_detail(session, entry)


# --------------------------------------------------------------------------- #
# 🔴 AYRILMIŞ YER — iki segmentli LİTERAL yollar (`/journal-entries/summary`).
#
# Aşağıdaki `/journal-entries/{entry_id}` (UUID) rotasıyla ÇAKIŞIR: FastAPI
# yolları KAYIT SIRASINA göre eşler, sonra kaydedilirse `summary` bir UUID
# sanılıp 422'ye düşer (MK-2 dersi). Yeni iki segmentli LİTERAL yolların hepsi
# bu satırın ÜSTÜNE eklenir; bekçi testi
# `test_rota_sirasi_summary_UUID_SANILMAZ`.
# --------------------------------------------------------------------------- #


# --- Uç 8: özet (üç KPI) ---


@router.get("/journal-entries/summary", response_model=JournalSummaryResponse, dependencies=[_VIEW])
async def journal_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR = None,
    month: _MONTH = None,
) -> JournalSummaryResponse:
    """E8:79-88 KPI şeridi — `total_debit` · `total_credit` · `net_balance`.

    🔴 `net_balance = **ALACAK − BORÇ**` (E8:88 `4.120.000−3.842.600=277.400`
    aritmetiğinden KANITLI).

    🔴 **Hesap süzgeci ALMAZ** (E8:72 — şerit tablonun ve filtre çubuğunun
    DIŞINDADIR). Varsayılan dönem `timezone.today()`nin ayıdır (K6 sınır
    çağrısı); `draft` sayılmaz, `reversed` SAYILIR (`POSTING_STATUSES`).
    """
    varsayilan_yil, varsayilan_ay = ledger.default_period()
    return await summary.build_summary(
        session,
        year=year if year is not None else varsayilan_yil,
        month=month if month is not None else varsayilan_ay,
    )


# --- Uç 9: detay ---


@router.get(
    "/journal-entries/{entry_id}",
    response_model=JournalEntryDetailResponse,
    responses=_NOT_FOUND,
    dependencies=[_VIEW],
)
async def get_journal_entry_endpoint(
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> JournalEntryDetailResponse:
    """Başlık + bacaklar. Toplamlar okuma anında YENİDEN HESAPLANMAZ: fiş,
    kayıtlaştırıldıktan sonra donmuş bir belgedir."""
    entry = await service.entry_or_404(session, entry_id)
    return await service.build_detail(session, entry)


# --- Uç 10: PATCH ---


@router.patch(
    "/journal-entries/{entry_id}",
    response_model=JournalEntryDetailResponse,
    responses={**_NOT_FOUND, **_DURUM_CAKISMASI, 422: {"description": "Gövde kuralı ihlali"}},
    dependencies=[_FULL],
)
async def update_journal_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    data: JournalEntryUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> JournalEntryDetailResponse:
    """**Yalnız `draft`** — aksi **409** (yetki değil DURUM engeli).

    Kayıt DENETİMLERDEN ÖNCE kilitlenir (TOCTOU). `entry_date` değişirse dönem
    kolonları onunla BİRLİKTE taşınır (K9); satır kümesi buradan DEĞİŞMEZ.

    🔴 **DÖNEM ÇİFT KONTROLÜ** (MU-2 T3): `entry_date` değişiyorsa HEM ESKİ HEM
    YENİ dönem kilitlenir ve ikisinden biri kapalıysa **409**. Yalnız birine
    bakmak deliği YARIM kapatır: yalnız yeniye bakılsaydı kapalı aydaki fiş açık
    aya TAŞINABİLİR (mali iz boşalır), yalnız eskiye bakılsaydı açık aydaki fiş
    kapalı aya SOKULABİLİRDİ (kapanmış mizan geçmişe dönük değişir).
    """
    ek_donemler = () if data.entry_date is None else (periods_service.period_of(data.entry_date),)
    entry = await service.entry_for_write(session, entry_id, extra_periods=ek_donemler)
    entry, detail = await service.update_entry(session, entry, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_detail(session, entry)


# --- Uç 11: DELETE ---


@router.delete(
    "/journal-entries/{entry_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_403_YANITI,
        **_NOT_FOUND,
        409: {"description": "Yalnızca taslak fiş silinebilir; kapalı dönemdeki fiş de silinemez"},
    },
    dependencies=[require_system_admin()],
)
async def delete_journal_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> None:
    """Yevmiye fişini siler. YALNIZ Sistem Yöneticisi.

    Yalnız `draft` fiş silinir; `posted`/`reversed` fiş **409**. Kapalı muhasebe döneminde de
    **409** (engel yetki değil DÖNEMDİR: silinebilseydi kapalı dönemin mizanı geçmişe dönük
    değişirdi). Bacaklar fişle birlikte silinir. Yanıt gövdesizdir.
    """
    entry = await service.entry_for_write(session, entry_id)
    detail = await service.delete_entry(session, entry)
    await _audit(request, session, user, AuditAction.delete, detail)


# --- Uç 12: PUT lines ---


@router.put(
    "/journal-entries/{entry_id}/lines",
    response_model=JournalEntryDetailResponse,
    responses={
        404: {"description": "Fiş ya da satırdaki hesap bulunamadı"},
        **_DURUM_CAKISMASI,
        **_K1,
    },
    dependencies=[_FULL],
)
async def replace_journal_lines_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    data: JournalLinesReplace,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> JournalEntryDetailResponse:
    """Bacak kümesini TOPTAN yazar (hakediş/puantaj emsali) — **yalnız `draft`**.

    🔴 R5: "posted fişin satırı UPDATE edilemez" iddiası DB'de zorlanamaz
    (trigger yoktur); satır yazan TEK yol budur ve kapı burada durur.

    K1 kapısı burada da koşar: boş küme "en az iki satır" engeline takılır ve
    **422** döner. Başlık toplamları aynı kümeden yeniden yazılır — satırlar ile
    başlık ASLA ayrışmaz.

    🔴 Kapalı dönemde **409** (MU-2 T3): satır kümesi fişin TUTARLARINI
    değiştirir ve kapalı bir dönemin toplamı değişemez.
    """
    entry = await service.entry_for_write(session, entry_id)
    entry, detail = await service.replace_lines(session, entry, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_detail(session, entry)


# --- Uç 13: kayıtlaştır ---


@router.post(
    "/journal-entries/{entry_id}/post",
    response_model=JournalEntryDetailResponse,
    responses={**_NOT_FOUND, **_DURUM_CAKISMASI, **_K1},
    dependencies=[_POST_APPROVE],
)
async def post_journal_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> JournalEntryDetailResponse:
    """`draft → posted` — fişi MALİ İZE sokar.

    🔴 **EŞİK = KİLİT:** satır kilitlenerek okunur, matris ve K1 kapısı KİLİTLİ
    satır üzerinde koşar (ayrıntı `state_service.py` modül docstring'i). İki
    eşzamanlı istekten yalnız biri geçer, öteki **409** alır.

    🔴 K1 kapısı burada **YENİDEN** koşar (**422**): fiş taslakken yaprak olan
    bir hesabın altına sonradan çocuk açılmış olabilir ve o fiş artık deftere
    girmemelidir — yoksa MU-2 mizanı üst hesabı ÇİFT SAYARDI.

    🔴 Kapalı dönemde **409** (MU-2 T3): kayıtlaştırma fişi MALİ İZE sokar ve
    kapalı bir dönemin mali izi tanım gereği DONMUŞTUR.

    Denetim satırı `AuditAction.approve` ile yazılır (yeni üye AÇILMADI); ayrım
    metindedir.
    """
    sonuc = await state_service.perform_transition(session, user, entry_id, JournalAction.post)
    await _audit(request, session, user, sonuc.audit_action, sonuc.detail)
    return await service.build_detail(session, sonuc.entry)


# --- Uç 14: ters kayıt (storno) ---


@router.post(
    "/journal-entries/{entry_id}/reverse",
    response_model=JournalEntryDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **_NOT_FOUND,
        409: {"description": "Fiş kayıtlı değil · stornosu zaten var · fişin kendisi storno"},
    },
    dependencies=[_FULL],
)
async def reverse_journal_entry_endpoint(
    request: Request,
    entry_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> JournalEntryDetailResponse:
    """`posted → reversed` + 🔴 **YENİ bir storno fişi** → **201**.

    Yanıt STORNONUN detayıdır (yeni doğan kayıt): orijinali döndürmek, kullanıcıyı
    ekranda göremeyeceği bir fişle baş başa bırakırdı.

    * storno doğrudan `posted`tır ve 🔴 tarihi **`timezone.today()`**dir (K6
      sınır çağrısı — orijinalin tarihi KAPALI bir döneme düşerdi)
    * bacaklar `debit ↔ credit` TAKASLIDIR, `sort_order` KORUNUR
    * **409**: fiş `posted` değil (matris) · stornosu zaten var · fişin kendisi
      bir stornodur (sonsuz zincir)

    🔴 K3: orijinal `reversed` olur ama defterden ÇIKMAZ (`POSTING_STATUSES`e
    dahildir) — ikisi birlikte hesabın bakiyesini TAM SIFIRA götürür. Yalnız
    `posted` sayılsaydı net `−orijinal` çıkardı (çift ters kayıt).

    🔴 **İKİ DÖNEM denetlenir** (MU-2 T3): orijinalin dönemi VE stornonun kendi
    dönemi (`timezone.today()`). İkisinden biri kapalıysa **409, istisna YOK** —
    yalnız orijinale bakılsaydı KAPALI bir aya taptaze bir `posted` fiş düşerdi.
    """
    sonuc = await state_service.perform_transition(session, user, entry_id, JournalAction.reverse)
    await _audit(request, session, user, sonuc.audit_action, sonuc.detail)
    return await service.build_detail(session, sonuc.entry)


# --- Uç 15: defter (koşan bakiye) ---


@router.get("/journal", response_model=LedgerResponse, dependencies=[_VIEW])
async def journal_ledger_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR = None,
    month: _MONTH = None,
    account_id: uuid.UUID | None = None,
    status_filter: Annotated[JournalEntryStatus | None, Query(alias="status")] = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> LedgerResponse:
    """E8:101-106 defteri — 🔴 tablo **SATIR bazlıdır**, fiş bazlı değil.

    Zarf K7'nindir + kökünde 🔴 **`carried_balance`** (pencere ÖNCESİ toplam)
    taşır: olmasaydı ay değişince ya da sayfa atlanınca koşan bakiye sıfırdan
    başlar ve anlamsız bir seri çıkardı.

    * dönem varsayılanı `timezone.today()`nin ayıdır (E8:75, K6 sınır çağrısı)
    * `account_id` OPSİYONELDİR (E8:96 `Tüm Hesaplar`)
    * `draft` deftere GİRMEZ, `reversed` GİRER (`POSTING_STATUSES`)
    * gösterim tarih **DESC** (E8:111-157), birikim **ASC** — ayrıntı
      `ledger.py` modül docstring'indedir
    * `running_balance` HAM `net`tir (borç `+`, alacak `−`); türe göre
      ÇEVRİLMEZ, çünkü karışık hesaplarda tür-bazlı işaret tanımsızdır
    """
    varsayilan_yil, varsayilan_ay = ledger.default_period()
    return await ledger.build_ledger(
        session,
        year=year if year is not None else varsayilan_yil,
        month=month if month is not None else varsayilan_ay,
        account_id=account_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )


# --- Uç 16: defterin Excel çıktısı ---


@router.get(
    "/journal/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {export.XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def journal_export_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR = None,
    month: _MONTH = None,
    account_id: uuid.UUID | None = None,
    status_filter: Annotated[JournalEntryStatus | None, Query(alias="status")] = None,
) -> Response:
    """Yevmiye defterinin Excel çıktısı (E8:66 / `Muhasebe - Profesyonel` satır 116).

    🔴 Zarf **`ledger.build_ledger` ile — DEFTER UCUYLA AYNI ÇAĞRIDAN** gelir:
    dönem varsayılanı (`ledger.default_period()`), `account_id` ve `status`
    süzgeçleri, `POSTING_STATUSES` kuralı ve DESC sırası birebir aynıdır.
    İkinci bir sorgu yolu AÇILMAZ — ayrışsalardı süzgeç dışı satırlar dosyaya
    sızabilir ve bu hiçbir yerden görülmezdi.

    🔴 **`limit`/`offset` YOKTUR: ayın eşleşen TÜM satırları yazılır**
    (`limit=None`). Bu GÜVENLİDİR ve `ledger.py`nin 3. tuzağının doğrudan
    sonucudur: koşan bakiye ALT sorgudaki pencere fonksiyonundan gelir ve ayın
    TAMAMI üzerinde koşar, `LIMIT` yalnız DIŞ sorguya uygulanır — sınır
    kaldırılınca `carried_balance` ve `running_balance` aynen doğru kalır.
    Defter ucunun kendi tavanı (200, aşımı **422**) DEĞİŞMEDİ.

    Dönem, dosya adı ile sorgunun AYNI ayı göstermesi için TEK yerde çözülür.

    Okuma ucudur: `_audit` ÇAĞIRMAZ ve `Request` parametresi bile ALMAZ.
    """
    varsayilan_yil, varsayilan_ay = ledger.default_period()
    donem_yili = varsayilan_yil if year is None else year
    donem_ayi = varsayilan_ay if month is None else month
    defter = await ledger.build_ledger(
        session,
        year=donem_yili,
        month=donem_ayi,
        account_id=account_id,
        status=status_filter,
        limit=None,
        offset=0,
    )
    return Response(
        content=export.build_journal_workbook(defter).getvalue(),
        media_type=export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": http.content_disposition(
                export.journal_filename(donem_yili, donem_ayi)
            )
        },
    )
