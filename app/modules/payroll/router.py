"""Bordro dönem + satır uçları — İK-3 T3 (spec §5'in ilk beş satırı).

Mockup otoritesi: `projedesign/Bordro Yönetimi.dc.html` (BY) ·
`Bordro Geçmişi.dc.html` (BG).

| Uç | Yetki | Mockup |
|---|---|---|
| `GET /payroll/periods` | `view` | BG 44-47 + tbody |
| `POST /payroll/periods` | `full` | BY 52 ay seçici |
| `GET /payroll/periods/{id}` | `view` | BY 69-93 + 124/172/240/268 |
| `PATCH /payroll/periods/{id}` | `full` | BY 63 "Son ödeme" (T4b) |
| `POST /payroll/periods/{id}/compute` | `full` | BY tablosunun doldurulması |
| `PATCH /payroll/lines/{id}` | `full` | BY 142-147 iki `input` |

Kapı `payroll` iznidir (spec S9): okuma `view`, yazma `full`. **Seed'de ZATEN
VARDIR** (`roles/seed_data.py:182`) — yeni izin modülü AÇILMADI, matris
DEĞİŞMEDİ. Ayrı bir `payroll:admin` de açılmaz: silme ucu yoktur.

**`visible_projects` süzgeci YOKTUR** ve bu BİLİNÇLİDİR (`personnel`/
`timesheet` deseni): bordro şirket geneli bir İK varlığıdır, bir dönem tek bir
projeye ait değildir. Süzgeç konsaydı aynı ayın toplamı iki kullanıcıda iki
farklı sayı gösterirdi. Karar `service.py`de gerekçeli ve testle kilitlidir.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
üç yazma ucunun her biri TEK denetim satırı yazar ve metin servis katmanında
kurulur.

T4 onay/ödeme yolunu, T5 ise SGK bildirimini, oran tablosunu ve Excel
çıktısını ekler (aşağıdaki bölüm başlıkları).

## HİÇBİR dilimde AÇILMAYAN uçlar (icat yasağı)

EFT talimatı (BY 319), makbuz (BY 328) ve SGK'ya GERÇEK gönderim (SGK 44)
açılmaz (spec §1); SGK 96-118'in çalışan listesi de açılmaz (spec §5 özeti
55-95'e bağlar, `sgk_no` diye bir kolon yoktur).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_page, require_pages, require_permission
from app.core.ratelimit import client_ip
from app.modules.audit import messages
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.payroll import export, history_export, service, tax_brackets_service
from app.modules.payroll.models import IncomeKind
from app.modules.payroll.schemas import (
    MAX_PAYROLL_YEAR,
    MIN_PAYROLL_YEAR,
    PayrollComputeResult,
    PayrollLineResponse,
    PayrollLineUpdate,
    PayrollPeriodApproveResult,
    PayrollPeriodCreate,
    PayrollPeriodDetailResponse,
    PayrollPeriodListResponse,
    PayrollPeriodPayResult,
    PayrollPeriodUpdate,
    PayrollRateListResponse,
    PayrollRateResponse,
    PayrollRateUpdate,
    PayrollSgkSubmitResult,
    PayrollSgkSummaryResponse,
    PayrollTaxBracketListResponse,
    PayrollTaxBracketSetUpdate,
)
from app.modules.site_diary.models import WorkerSource
from app.modules.users.models import User

router = APIRouter(tags=["payroll"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
#: IZN-B2: satır/dönem Onayla-Reddet, Öde = Aylık Bordro ONAYLAR; SGK'ya Gönder = SGK Bildirimi
#: ONAYLAR (eşikler eski `full`).
_PAYROLL_APPROVE = require_pages(("mali.bordro",), "approve")
_SGK_SUBMIT = require_pages(("mali.sgk_bildirimi",), "approve")
#: 🔴 TB6 T1 — tarifenin TAM KÜME değiştirmesi fiilen bir SİLMEDİR (eski dilim
#: satırları GİDER, UQ yüzünden pasifleştirilemezler) ve WORKFLOW §8 gereği
#: **silme yalnız `admin`**tir. `payroll_rates`in PUT'u satırın ÜSTÜNE yazar,
#: o yüzden orası `full` kalır.
#: IZN-B2 §2.4: vergi dilimi = "Bordro Oranları" sayfası DÜZENLER (eşik eski `admin` ile aynı).
_ADMIN = require_page("ayarlar.bordro_oranlari", "edit")

# TB3 sayfalama standardı: varsayılan 50, tavan 200 — aşım sessizce KIRPILMAZ,
# 422 döner (SA/ST ile birebir).
_LIMIT = Annotated[int, Query(ge=1, le=200)]
_OFFSET = Annotated[int, Query(ge=0)]


async def _audit(
    request: Request,
    session: AsyncSession,
    user: User,
    action: AuditAction,
    detail: str,
) -> None:
    """Denetim satırı (B5 deseni). Metin PARAMETREDİR, burada kurulmaz."""
    await record_audit(
        session,
        action=action,
        detail=detail,
        actor_user_id=user.id,
        ip_address=client_ip(request),
    )


@router.get("/payroll/periods", response_model=PayrollPeriodListResponse, dependencies=[_VIEW])
async def list_payroll_periods_endpoint(
    session: DbSession,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> PayrollPeriodListResponse:
    """BG tablosu: dönem · çalışan · brüt · SGK işveren · net · toplam maliyet.

    En YENİ dönem başta (BG tbody: Temmuz · Haziran · Mayıs). Toplamlar
    TÜREVDİR — dönem tablosunda toplam kolonu yoktur (models.py: iki gerçek
    kaynak doğar ve `compute` sonrası sessizce çelişirdi).

    "Çalışan" sütunu dönemin TÜM satırlarını sayar (taşeron dahil, BY tfoot
    48 = 12+29+5+2); BY 71'in kart sayısı ise yalnız ÖDENEBİLİR satırlardır.
    """
    return await service.list_periods(session, limit=limit, offset=offset)


@router.get(
    "/payroll/periods/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={
        200: {"content": {history_export.XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}
    },
)
async def export_payroll_periods_endpoint(
    session: DbSession,
) -> Response:
    """BG "Excel İndir": bordro GEÇMİŞİNİN Excel çıktısı — EKRANLA AYNI KÜME.

    Satırlar liste ucuyla AYNI çağrıdan (`service.period_rows`) gelir: aynı
    sıralama (yıl/ay DESC), aynı toplam kaynağı (`summary.build_period_summary`),
    aynı kapı (`_VIEW`). İkinci bir sorgu/hesap yolu AÇILMAZ.

    `limit`/`offset` YOKTUR — TÜM dönemler yazılır (sessiz kırpma yok). Liste
    ucunun 200'lük tavanı yerinde KALIR.

    Okuma ucudur — `_audit` ÇAĞIRMAZ ve `Request` parametresi bile ALMAZ
    (`units/router.py` P4 T7 kuralı).
    """
    rows, _total = await service.period_rows(session, limit=None)
    return Response(
        content=history_export.build_period_history_workbook(rows).getvalue(),
        media_type=history_export.XLSX_MEDIA_TYPE,
        headers={"Content-Disposition": http.content_disposition(history_export.filename())},
    )


@router.post(
    "/payroll/periods",
    response_model=PayrollPeriodDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={409: {"description": "Bu ay için bordro dönemi zaten açılmış"}},
    dependencies=[_FULL],
)
async def create_payroll_period_endpoint(
    request: Request,
    data: PayrollPeriodCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollPeriodDetailResponse:
    """Ay AÇAR, doldurmaz — satırlar `compute` ucundan gelir.

    Var olan ay **409**dur (UQ `(year, month)`, spec §4). Yeni dönem HER ZAMAN
    `draft`tır; gövdeden durum alınmaz (`extra="forbid"`).
    """
    period, detail = await service.create_period(session, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await service.get_period_detail(session, period.id)


@router.patch(
    "/payroll/periods/{period_id}",
    response_model=PayrollPeriodDetailResponse,
    responses={409: {"description": "Onaylanmış veya ödenmiş dönemin ödeme tarihi değiştirilemez"}},
    dependencies=[_FULL],
)
async def update_payroll_period_endpoint(
    request: Request,
    period_id: uuid.UUID,
    data: PayrollPeriodUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollPeriodDetailResponse:
    """Ödeme takvimi (BY 63 "Son ödeme") düzeltmesi — T4b.

    * Yetki diğer yazma uçlarıyla AYNI: `payroll:full`.
    * **`draft` ve `pending_approval`** yazılabilir; **`approved`/`paid` 409** —
      ödeme gerçekleştikten sonra takvimi değiştirmek gerçekleşmiş bir olayın
      kaydını düzeltmek olurdu, `approved`ta da bordronun takvimi tek taraflı
      kaymamalıdır. Değişmesi gerekiyorsa dönem `pending_approval`a geri alınır.
    * `payment_due_date` **OPSİYONELDİR** (açma ucunda da): sunucu tarih
      ÜRETMEZ, varsayılan KOYMAZ, dönemin yıl/ayıyla tutarlılığını DENETLEMEZ
      (ödeme sonraki aya sarkabilir). Açıkça `null` göndermek tarihi TEMİZLER;
      boş gövde ise **422**'dir (bir işlem değildir).
    * Görünmeyen/var olmayan dönem **404** (ayırt edilemez).
    """
    period, detail = await service.update_period(session, period_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.get_period_detail(session, period.id)


@router.get(
    "/payroll/periods/{period_id}",
    response_model=PayrollPeriodDetailResponse,
    dependencies=[_VIEW],
)
async def get_payroll_period_endpoint(
    period_id: uuid.UUID,
    session: DbSession,
) -> PayrollPeriodDetailResponse:
    """BY ekranı: dört özet kartı (69-93) + tip bazında gruplanmış satırlar.

    🔴 İlk üç kart ÖDEME tabanını gösterir (`excluded` taşeron ve `uncomputed`
    satırlar HARİÇ, K2/S4); dördüncü kart MALİYET tabanını (`excluded` DAHİL) ve
    hesabı **`brüt + SGK işveren + işsizlik işveren + kısa çalışma`**dır
    (spec §7) — BY 92'nin "SGK işveren payı dahil" ETİKETİ mockup'tan, HESAP
    spec'ten gelir.

    Görünmeyen dönem var olmayanla AYNI 404'ü alır.
    """
    return await service.get_period_detail(session, period_id)


@router.post(
    "/payroll/periods/{period_id}/compute",
    response_model=PayrollComputeResult,
    responses={409: {"description": "Onaylanmış veya ödenmiş dönem yeniden hesaplanamaz"}},
    dependencies=[_FULL],
)
async def compute_payroll_period_endpoint(
    request: Request,
    period_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollComputeResult:
    """Puantaj + ücret + oranlardan satırları üretir/günceller (T2 akışı).

    Elle düzeltilmiş (`is_overridden`, S6) ve onaylı/ödenmiş (S5) satırlar
    KORUNUR ve **sayıyla raporlanır** — sessiz atlama yoktur (WORKFLOW §3).
    Dönem `approved`/`paid` ise **409**.
    """
    sonuc = await service.compute_period(session, period_id)
    period = await service.get_period(session, period_id)
    await _audit(
        request,
        session,
        user,
        AuditAction.update,
        messages.payroll_period_computed(period.year, period.month),
    )
    return sonuc


@router.patch(
    "/payroll/lines/{line_id}",
    response_model=PayrollLineResponse,
    responses={
        409: {"description": "Onaylanmış/ödenmiş satır ya da taşeron satırı değiştirilemez"},
        422: {"description": "Banka + elden toplamı nete eşit değil ya da oran seti yok"},
    },
    dependencies=[_FULL],
)
async def update_payroll_line_endpoint(
    request: Request,
    line_id: uuid.UUID,
    data: PayrollLineUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollLineResponse:
    """Brüt override (K3) + banka/elden bölüşümü (S3) — BY 142-147.

    * **S3 (spec §6/1):** `banka + elden ≠ net` → **422**, KURUŞ kaymasında da
      (`Decimal`; `float` karşılaştırması yapılmaz). İki alan BİRLİKTE gönderilir.
    * **S5 (spec §6/4):** `approved`/`paid` satır — ve onaylanmış DÖNEMİN her
      satırı — **409**. Ödeme izi geriye dönük düzeltilmez.
    * **K2 (spec §2):** taşeron (`excluded`) satırı **409** — bordrodan ödenmez,
      bölüşümü de düzenlenmez; çift ödeme yapısal olarak imkânsız kalmalıdır.
    * **K3:** brüt elle değişince iz yazılır (kim/ne zaman/önceki değer) ve
      kesinti/net/bölüşüm `compute.py`nin oran mantığıyla YENİDEN TÜRETİLİR —
      kesinti gövdeden alınmaz.
    """
    satir, detail = await service.update_line(session, user.id, line_id, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return satir


# --- T4: onay + ödeme yolu (spec §5'in 6.-8. satırları) --------------------
#
# | Uç | Yetki | Mockup |
# |---|---|---|
# | `POST /payroll/lines/{id}/approve` · `/reject` | `full` | BY satır durumu |
# | `POST /payroll/periods/{id}/approve` | `full` | BY 303 "Tümünü Onayla" |
# | `POST /payroll/periods/{id}/pay` | `full` | BY 56 "Ödemeyi Onayla" sonrası |
#
# Dördü de `payroll:full`dur: onay ve ödeme İZİN gerektiren PARA olaylarıdır,
# `view` seviyesi yalnız görmeye yeter. Dördü de TEK denetim satırı yazar ve
# `AuditAction.approve` kullanır (`audit/models.py` docstring'i: onay uçları) —
# yalnız RED bir onay değil bir geri alma olduğu için `update`tir.
#
# 🔴 EFT talimatı (BY 319) ve makbuz (BY 328) BU DİLİMDE DE AÇILMAZ (spec §1):
# `/pay` bir DAMGADIR, banka entegrasyonu yoktur.


@router.post(
    "/payroll/lines/{line_id}/approve",
    response_model=PayrollLineResponse,
    responses={
        409: {
            "description": (
                "Taşeron satırı, hesaplanamamış satır, zaten onaylı/ödenmiş satır "
                "ya da onaylanmış/ödenmiş dönem"
            )
        }
    },
    dependencies=[_PAYROLL_APPROVE],
)
async def approve_payroll_line_endpoint(
    request: Request,
    line_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollLineResponse:
    """Satır onayı — `pending → approved`.

    * **🔴 K2 (spec §2, §6/2):** taşeron (`excluded`) satırı **409**. Taşeronun
      ödemesi hakediş (TH) üzerinden yapılır; buradan da onaylanabilseydi aynı
      emek İKİ KEZ ödenirdi. Çift ödeme yapısal olarak imkânsız kalmalıdır.
    * **S4 (spec §6/3):** brütü `null` olan (`uncomputed`) satır **409** —
      "ödenecek bir şey yok" yalanı damgalanmaz; önce brüt girilir.
    * **S8:** `approved`/`paid` satır **409** (atlama ve tekrar yok).
    """
    satir, detail = await service.approve_line(session, line_id)
    await _audit(request, session, user, AuditAction.approve, detail)
    return satir


@router.post(
    "/payroll/lines/{line_id}/reject",
    response_model=PayrollLineResponse,
    responses={409: {"description": "Yalnız onaylanmış satırın onayı geri alınabilir"}},
    dependencies=[_PAYROLL_APPROVE],
)
async def reject_payroll_line_endpoint(
    request: Request,
    line_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollLineResponse:
    """Satır reddi = ONAYIN GERİ ALINMASI — `approved → pending` (S5 düzeltme yolu).

    Ayrı bir `rejected` durumu YOKTUR: satır durumu kümesi T1'de kapanmıştır.
    Geri alınan satır yeniden düzenlenebilir olur. `pending`/`uncomputed`/
    `paid`/`excluded` satırda **409**.
    """
    satir, detail = await service.reject_line(session, line_id)
    await _audit(request, session, user, AuditAction.update, detail)
    return satir


@router.post(
    "/payroll/periods/{period_id}/approve",
    response_model=PayrollPeriodApproveResult,
    responses={
        409: {
            "description": (
                "Dönem onay adımına geçirilemez, ödenecek satırı yok ya da muhasebe dönemi kapalı"
            )
        },
        422: {"description": "Bordro fişi yazılamadı: hesap eşlemesi ya da satır bileşeni eksik"},
    },
    dependencies=[_PAYROLL_APPROVE],
)
async def approve_payroll_period_endpoint(
    request: Request,
    period_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollPeriodApproveResult:
    """BY 303 "Tümünü Onayla" — dönemi TEK ADIM ilerletir, `pending` satırları onaylar.

    `draft → pending_approval → approved`; **atlama YOKTUR** (S8), üçüncü çağrı
    **409**. Ödeme damgası bu uçtan basılmaz (`/pay` ayrıdır).

    🔴 "Tümünü" onaylamaz: `uncomputed` (S4) ve taşeron (K2) satırlar ATLANIR ve
    yanıtta **sebebe göre ayrı sayılarla** raporlanır — sessiz atlama yoktur
    (WORKFLOW §3).

    🔴 **KRIT-BORDRO:** dönemde ÖDENEBİLİR satır yoksa **409** — ne ilk ne
    ikinci adım. Boş bir onay `compute` kapısını (S5) o ayın üzerine KALICI
    olarak kapatırdı: dönemi geri alan/silen uç yoktur ve UQ `(year, month)`
    aynı ayı ikinci kez açtırmaz. Gerekçenin tamamı `service/approvals.py`dedir.

    🔴 **MU-3E:** `approved` adımında bordronun TAHAKKUK FİŞİ kesilir
    (`730` gider · `335` personele borç · `360` vergi · `361` SGK). Fiş
    yazılamazsa onay da geri alınır: hedef ayın muhasebe dönemi kapalıysa
    **409**, hesap eşlemesi ya da satır bileşeni eksikse **422**.
    """
    sonuc, detail = await service.approve_period(session, user, period_id)
    await _audit(request, session, user, AuditAction.approve, detail)
    return sonuc


@router.post(
    "/payroll/periods/{period_id}/pay",
    response_model=PayrollPeriodPayResult,
    responses={409: {"description": "Yalnız onaylanmış dönem ödenebilir"}},
    dependencies=[_PAYROLL_APPROVE],
)
async def pay_payroll_period_endpoint(
    request: Request,
    period_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollPeriodPayResult:
    """Ödendi damgası (`paid_at`) — dönem ve ONAYLI satırlar `paid`.

    * **S8:** dönem `approved` değilse **409** — `draft → paid` para çıkışının
      onay zincirini atlardı. İkinci `pay` de **409**: ikinci ödeme demektir.
    * **🔴 K2:** taşeron satırı `paid` OLMAZ ve `paid_net_total`a GİRMEZ.
    * Onaylanmamış ve hesaplanamamış satırlar ödenmez, sayıyla raporlanır.

    Dış entegrasyon YOKTUR (spec §1): EFT talimatı gönderilmez.
    """
    sonuc, detail = await service.pay_period(session, period_id)
    await _audit(request, session, user, AuditAction.approve, detail)
    return sonuc


# --- T5: SGK bildirimi + oran tablosu + Excel (spec §5'in son dört satırı) --
#
# | Uç | Yetki | Mockup |
# |---|---|---|
# | `GET /payroll/periods/{id}/sgk-summary` | `view` | SGK 55-95 |
# | `POST /payroll/periods/{id}/sgk-submit` | `full` | SGK 44 "SGK'ya Gönder" |
# | `GET /payroll/rates` · `PUT /payroll/rates/{year}/{source}` | `view`/`full` | K1 |
# | `GET /payroll/periods/{id}/export` | `view` | BY 55 "Excel" |
#
# 🔴 **SGK'ya GERÇEK GÖNDERİM YOKTUR** (spec §1): `sgk-submit` bir DAMGADIR —
# ne HTTP isteği, ne kuyruk, ne dosya gönderimi. SGK 96-118'in çalışan listesi
# ("SGK No" + 4a/4b rozeti) de AÇILMAZ: spec §5 özeti 55-95'e bağlar ve `sgk_no`
# diye bir kolon İK-1'de yoktur (WORKFLOW §3: icat yasağı).
#
# İki okuma ucu (`sgk-summary`, `export`) `record_audit` ÇAĞIRMAZ; iki yazma ucu
# (`sgk-submit`, `PUT rates`) TEK denetim satırı yazar.


@router.get(
    "/payroll/periods/{period_id}/sgk-summary",
    response_model=PayrollSgkSummaryResponse,
    dependencies=[_VIEW],
)
async def payroll_sgk_summary_endpoint(
    period_id: uuid.UUID,
    session: DbSession,
) -> PayrollSgkSummaryResponse:
    """SGK bildirim ekranının prim hesabı — SGK **55-95**.

    * **🔴 Mockup TUTARLARI beklenti DEĞİLDİR** (spec S1): SGK mockup'ı kendi
      aritmetiğine uymuyor (SGK 82 → 148.800 yazar, kendi oranlarından 174.652
      çıkar). Açık ORAN kazanır; buradaki işveren sayıları mockup'takinden
      BÜYÜKTÜR ve bu kararın kendisidir.
    * **Taban:** taşeron (`excluded`) satır MATRAHA GİRER — SGK bildirimi bir
      ödeme değil BİLDİRİMDİR (SGK 112-113 taşeron satırlarını listeler,
      SGK 55'in "48"i BY tfoot 298'in 48'idir). Gerekçe `sgk.py`de.
    * **Fail-closed:** brütü `null` satır (S4) ve oran seti olmayan tip matraha
      GİRMEZ, ikisi de AYRI SAYILIR (sessiz atlama yok).

    Görünmeyen dönem var olmayanla AYNI 404'ü alır.
    """
    return await service.sgk_summary(session, period_id)


@router.post(
    "/payroll/periods/{period_id}/sgk-submit",
    response_model=PayrollSgkSubmitResult,
    responses={409: {"description": "Bu dönemin SGK bildirimi zaten işaretlenmiş"}},
    dependencies=[_SGK_SUBMIT],
)
async def payroll_sgk_submit_endpoint(
    request: Request,
    period_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollSgkSubmitResult:
    """SGK 44 "SGK'ya Gönder" — YALNIZ `sgk_submitted_at` damgası (spec §1).

    * **Dış sistem entegrasyonu YOKTUR.** Bu uç hiçbir yere istek atmaz.
    * **İkinci damga 409:** damga bir OLAYIN zamanıdır (SGK 46'daki son bildirim
      tarihiyle karşılaştırılır); sessizce yeniden yazılsaydı geç bir bildirim
      ikinci tıklamayla zamanında yapılmış görünürdü.
    * **Dönem durumu ön koşul DEĞİLDİR:** SGK 44-47 bildirimin beklediğini
      söylerken BY 61 aynı dönemin bordrosunun hâlâ onay beklediğini yazar —
      mockup bildirimin ödeme onayından ÖNCE yapılabildiğini gösteriyor.
    """
    sonuc, detail = await service.submit_sgk(session, period_id)
    await _audit(request, session, user, AuditAction.update, detail)
    return sonuc


@router.get("/payroll/rates", response_model=PayrollRateListResponse, dependencies=[_VIEW])
async def list_payroll_rates_endpoint(
    session: DbSession,
    year: int | None = None,
) -> PayrollRateListResponse:
    """Oran setleri (K1) — `(yıl, personel tipi)` anahtarlı, yedi oran + `is_active`.

    Pasif setler de döner: geçmiş bir bordronun hangi oranla hesaplandığı
    okunabilir kalmalıdır. Sayfalama YOKTUR (tablo yılda dört satır büyür,
    gerekçe `schemas.PayrollRateListResponse`).
    """
    return await service.list_rates(session, year)


@router.put(
    "/payroll/rates/{year}/{source}",
    response_model=PayrollRateResponse,
    responses={
        409: {"description": "Bu yılda onaylanmış/ödenmiş dönem var: oranlar değiştirilemez"}
    },
    dependencies=[_FULL],
)
async def upsert_payroll_rate_endpoint(
    request: Request,
    year: Annotated[int, Path(ge=MIN_PAYROLL_YEAR, le=MAX_PAYROLL_YEAR)],
    source: WorkerSource,
    data: PayrollRateUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollRateResponse:
    """Oran seti açar ya da DEĞİŞTİRİR (K1: oranlar VERİDİR, koda gömülmez).

    🔴 **GEÇMİŞ DÖNEM DEĞİŞMEZ (para korkuluğu):** o yılda `approved`/`paid` bir
    dönem varsa **409**. Oran satıra kopyalanmaz (K1) ve dönem toplamları canlı
    setten türer; yazmaya izin verilseydi onaylanmış bir dönemin raporlanmış
    maliyeti ve SGK bildirimi geriye dönük değişirdi. Kapı YILA kapanır — yeni
    tip açmak da aynı sonucu doğururdu. Başka yıl ve taslak dönemli yıl
    SERBESTTİR; kural bordroyu tıkamaz.

    Gövde TAM SETTİR: yedi oranın hepsi zorunludur, kısmi yama yoktur.
    """
    rate, detail = await service.upsert_rate(session, year, source, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return PayrollRateResponse.model_validate(rate)


# --- TB6 T1: gelir vergisi tarifesi (IK3-GV'nin ERTELENMİŞ ucu) -------------
#
# 🔴 Bu iki uç açılmasaydı 2027'nin ilk bordro dönemi K3 gereği `uncomputed`
# dönerdi: tarife YILLIKTIR ve tohum (`b3c4d5e6f7a8`) yalnız 2026'yı basar.
# Ürün sessizce yanlış bordro üretmez ama HESAPLAYAMAZ hâle gelirdi.


@router.get(
    "/payroll/tax-brackets", response_model=PayrollTaxBracketListResponse, dependencies=[_VIEW]
)
async def list_payroll_tax_brackets_endpoint(
    session: DbSession,
    year: int | None = None,
    income_kind: IncomeKind | None = None,
) -> PayrollTaxBracketListResponse:
    """GVK m.103 artan oranlı tarifesi (IK3-GV K2) — `(yıl, gelir türü, sıra)`.

    Pasif setler de döner: geçmiş bir bordronun hangi tarifeyle hesaplandığı
    okunabilir kalmalıdır. Sayfalama YOKTUR (gerekçe `schemas`).

    🔴 Bu uç aynı zamanda PUT'un ÖN KOŞULUDUR: tam küme değiştirmeye açılan bir
    yüzeyin, kümenin TAMAMINI okuyan bir eşi olmak zorundadır — yoksa kullanıcı
    neyin üstüne yazdığını göremeden yazar.
    """
    return await tax_brackets_service.list_tax_brackets(session, year, income_kind)


@router.put(
    "/payroll/tax-brackets/{year}/{income_kind}",
    response_model=PayrollTaxBracketListResponse,
    responses={
        409: {"description": "Bu yılda onaylanmış/ödenmiş dönem var: tarife değiştirilemez"}
    },
    dependencies=[_ADMIN],
)
async def replace_payroll_tax_brackets_endpoint(
    request: Request,
    year: Annotated[int, Path(ge=MIN_PAYROLL_YEAR, le=MAX_PAYROLL_YEAR)],
    income_kind: IncomeKind,
    data: PayrollTaxBracketSetUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PayrollTaxBracketListResponse:
    """Yılın tarifesini **TAM KÜME** olarak değiştirir (K1: mevzuat VERİDİR).

    🔴 **GEÇMİŞ DÖNEM DEĞİŞMEZ (para korkuluğu):** o yılda `approved`/`paid` bir
    dönem varsa **409**. Gerekçe oran ucununkiyle AYNI DEĞİLDİR ve ölçülmüştür —
    vergi satıra SNAPSHOT edilir, ama ayın vergisi `T(önceki+bu ay) − T(önceki)`
    olduğu için yıl ortasında değişen bir tarife, sonraki ilk bordroya ödenmiş
    ayların farkını YÜKLER.

    Gövde yılın TÜM dilimlerini taşır; kısmi güncelleme YOKTUR (tarife birikimli
    okunur, tek dilimi yamalamak setin bütününün anlamını değiştirirdi). Kümenin
    bütünlüğü (boşluk · örtüşme · ortada açık uç · sınırsız SON dilim)
    `income_tax.normalize_brackets` ile doğrulanır → **422**.
    """
    rows, detail = await tax_brackets_service.replace_tax_brackets(session, year, income_kind, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return PayrollTaxBracketListResponse(items=rows, total=len(rows))


@router.get(
    "/payroll/periods/{period_id}/export",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {export.XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def export_payroll_period_endpoint(
    period_id: uuid.UUID,
    session: DbSession,
) -> Response:
    """BY 55 "Excel" — dönem tablosunun çıktısı (puantaj export emsali).

    Dönem detayı EKRAN UCUYLA AYNI çağrıdan gelir (`get_period_detail`): satır
    tutarları, bölüm gruplaması ve toplamlar birebir aynıdır — ikinci bir sorgu
    yazılsaydı dosya ile ekran zamanla ayrışırdı.

    İndirme bir OKUMADIR: kapı `payroll:view`tir ve `record_audit` ÇAĞIRMAZ.
    Görünmeyen dönem var olmayanla AYNI 404'ü alır.
    """
    detail = await service.get_period_detail(session, period_id)
    buffer = export.build_payroll_workbook(detail)
    return Response(
        content=buffer.getvalue(),
        media_type=export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": http.content_disposition(
                export.filename(detail.year, detail.month)
            )
        },
    )
