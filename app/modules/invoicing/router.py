"""Fatura uçları (FAT-1 T3) — spec §7'nin 1, 3, 4, 5, 6, 7 numaralı yolları.

Kapı `invoicing` iznidir (spec §6, seed'de HAZIR — matris DEĞİŞMEDİ). Seviye
sırası `none < view < draft < request < approve < full < admin`
(`app/core/access.py`) ve üç kapı buradan çıkar:

| Uç | Yetki |
|---|---|
| `GET /invoices` | `view` |
| `POST /invoices` | `full` |
| `GET /invoices/{id}` | `view` |
| `PATCH /invoices/{id}` | `full` |
| `PUT /invoices/{id}/lines` | `full` |
| `DELETE /invoices/{id}` | **`admin`** |

**Neden `DELETE` düz `admin`:** `full` silmeyi KAPSAMAZ (repo kanonu) ve
`procurement`in `can_delete` istisnası burada GEÇERSİZDİR — faturanın "sahibi"
onu kesen kullanıcı değil ŞİRKETTİR; muhasebeci kendi kestiği taslağı bile
tek başına düşürememelidir (mali belge, `documents`/`inventory` deseni).

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez);
yazma uçlarının hepsi TEK denetim satırı yazar ve metin servis katmanında,
kayıt değişmeden/yok olmadan ÖNCE kurulur.

## AÇILMAYAN uçlar (spec §1/§7, icat yasağı)

`GİB'den Çek` (FY:23) · muhasebe fişi (FGE:197-241) · tahsilat KAYDI
(FGI:220-247; burada yalnız DURUM damgası olacaktır, T4) · `Kısmi Onayla`
(FGE:140) · toplu seçim/onay · `e-Arşiv` ve `İtiraz/İade` sekmeleri. Durum
uçları (`send`/`mark-collected`/`approve`/`dispute`) ve `GET /invoices/summary`
**T4'ündür**.

## 🔴 ROTA SIRASI (spec §9, MK-2 dersi — `main.py:94-104`)

`/invoices/summary` İKİ SEGMENTLİDİR ve `/invoices/{invoice_id}` (UUID) ile
AYNI şekli taşır; FastAPI yolları KAYIT SIRASINA göre eşler. Sonra
kaydedilseydi `summary` bir UUID sanılıp 422'ye düşerdi. Yeri aşağıda
AYRILMIŞTIR ve kural bir bekçi testiyle kilitlidir
(`test_rota_sirasi_iki_segmentli_literal_yollar_UUID_rotasindan_ONCE`).
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.mask_route import MaskeRotasi
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.page_gate import gate_flags
from app.core.permissions import require_pages, require_permission, require_system_admin
from app.core.ratelimit import client_ip
from app.core.slug import parse_ref
from app.core.tasima import assert_tasima_yetkisi
from app.modules.audit.models import AuditAction
from app.modules.audit.service import record_audit
from app.modules.invoicing import guards, service, state_service, summary
from app.modules.invoicing.models import InvoiceDirection, InvoiceStatus
from app.modules.invoicing.schemas import (
    InvoiceCreate,
    InvoiceDetailResponse,
    InvoiceLinesReplace,
    InvoiceListResponse,
    InvoiceSummaryResponse,
    InvoiceUpdate,
)
from app.modules.invoicing.transitions import InvoiceAction
from app.modules.silme import service as silme_service
from app.modules.silme.params import DELETE_WITH_PREVIEW_RESPONSES, PreviewTokenQuery
from app.modules.treasury import payments_service
from app.modules.treasury.schemas import PaymentCreate, PaymentListResponse, PaymentResponse
from app.modules.users.models import User

router = APIRouter(tags=["invoicing"], responses=COMMON_ERROR_RESPONSES, route_class=MaskeRotasi)

_VIEW = require_permission(service.PERMISSION_MODULE, AccessLevel.view)
_FULL = require_permission(service.PERMISSION_MODULE, AccessLevel.full)
#: IZN-B2: Gelen faturada Onayla / Tahsil Edildi = Fatura Yönetimi sayfası ONAYLAR (eski `full`).
_INVOICE_APPROVE = require_pages(("mali.fatura",), "approve")

# TB3 sayfalama standardı: varsayılan 50, tavan 200 — tavan aşımı sessizce
# KIRPILMAZ, 422 döner (ST/SA ile birebir).
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


# --- Uç 1: liste ---


@router.get("/invoices", response_model=InvoiceListResponse, dependencies=[_VIEW])
async def list_invoices_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    direction: InvoiceDirection | None = None,
    status_filter: Annotated[InvoiceStatus | None, Query(alias="status")] = None,
    project_id: uuid.UUID | None = None,
    site_id: uuid.UUID | None = None,
    q: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> InvoiceListResponse:
    """FY tablosunun veri kaynağı — süzgeçler AND'lidir.

    Kapsam süzgeci HER ZAMAN uygulanır: görünmeyen projenin faturası listede
    YOKTUR ve `total`a da girmez. `project_id` NULL fatura (şirket geneli)
    modül izniyle görünür (§6).

    `q` FATURA NUMARASI ve TARAF ADI üzerinde kısmi arar (FY:94). `status`
    süzgeci ÜÇ giden değerini de alır; ekranın "Vadeli" seçeneği `sent`e eşlenir
    (K1 — "Vadeli" ayrı bir durum DEĞİLDİR).

    `limit` varsayılan 50, tavan 200 — aşım **422**.
    """
    return await service.list_invoices(
        session,
        user,
        direction=direction,
        status=status_filter,
        project_id=project_id,
        site_id=site_id,
        q=q,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )


# --- Uç 3: oluştur ---


@router.post(
    "/invoices",
    response_model=InvoiceDetailResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        404: {"description": "Seçilen proje/şantiye/cari/kaynak kayıt bulunamadı"},
        409: {"description": "Bu fatura numarası bu yönde zaten kayıtlı"},
        422: {"description": "Gövde kuralı ihlali (numara sahibi · tek taraf · oran toplamı)"},
    },
    dependencies=[_FULL],
)
async def create_invoice_endpoint(
    request: Request,
    data: InvoiceCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """FK formunun kaydı: başlık + kalemler TEK gövde, ATOMİK.

    * giden → `draft`, gelen → `pending` (K2); gövde `status` GÖNDEREMEZ
    * giden numarayı SUNUCU üretir (`FIL…`), gelen numarayı İSTEMCİ verir (S5)
    * `line_total`/`sort_order` ve hesaplanmış para alanları gövdeden GELEMEZ
      (**422**) — oranlar (`*_rate`) GELİR
    * görünmeyen ya da olmayan proje/şantiye/cari/kaynak → **404**

    Bozuk bir kalem varsa HİÇBİR ŞEY yazılmaz — ne başlık ne satır. Denetime
    FATURA BAŞINA TEK satır düşer.
    """
    invoice, detail = await service.create_invoice(session, user, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return await service.build_detail(session, invoice)


# --------------------------------------------------------------------------- #
# 🔴 AYRILMIŞ YER — iki segmentli LİTERAL yollar (`/invoices/summary`).
#
# Aşağıdaki `/invoices/{invoice_id}` (UUID) rotasıyla ÇAKIŞIR: FastAPI yolları
# KAYIT SIRASINA göre eşler, sonra kaydedilirse `summary` bir UUID sanılıp
# 422'ye düşer (MK-2 dersi, `main.py:94-104`). Yeni iki segmentli LİTERAL
# yolların hepsi bu satırın ÜSTÜNE eklenir; bekçi testi
# `test_rota_sirasi_iki_segmentli_literal_yollar_UUID_rotasindan_ONCE`.
# --------------------------------------------------------------------------- #


# --- Uç 2: özet ---


@router.get("/invoices/summary", response_model=InvoiceSummaryResponse, dependencies=[_VIEW])
async def invoices_summary_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceSummaryResponse:
    """FY:69-75 KPI şeridi — beş kart.

    Kapsam süzgeci liste ucundakiyle AYNIDIR: görünmeyen projenin faturası
    hiçbir toplama girmez (IDOR'un sayısal hâli). `pending_approval` ADETTİR,
    tutar değil. Ay penceresi `DISPLAY_TIMEZONE`dedir — ayrıntı `summary.py`
    modül docstring'indedir.
    """
    return await summary.build_summary(session, user)


# --- Uç 4: detay ---


@router.get(
    "/invoices/{invoice_id}",
    response_model=InvoiceDetailResponse,
    responses={
        404: {"description": "Fatura bulunamadı"},
        409: {"description": "Fatura numarası hem gelen hem giden faturada kayıtlı"},
    },
    dependencies=[_VIEW],
)
async def get_invoice_endpoint(
    invoice_id: str,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """FGI/FGE detayı: başlık + kalemler + SAKLANAN toplamlar.

    Görünmeyen fatura var olmayanla AYNI 404'ü alır. Toplamlar okuma anında
    yeniden HESAPLANMAZ (K7): fatura donmuş bir belgedir.

    URL-4 — yol parametresi UUID **ya da** `invoice_no` kabul eder (yol adı
    `invoice_id` KALIR, URL-2 kararı 1). Numara iki yönde de kayıtlıysa ve
    ikisi de KULLANICIYA GÖRÜNÜYORSA **409** döner: sessizce biri seçilmez.

    🔴 **KARDEŞ UÇLAR `uuid` KALIR** — sözleşme bu yol parametresi adı altında
    BİLEREK ASİMETRİKTİR: `GET /invoices/{invoice_id}` `string`, ama
    `…/{invoice_id}/payments` · `/approve` · `/send` · `/dispute` ·
    `/mark-collected` `uuid` bekler. Anahtar YALNIZCA **link üretimi ve detay
    okuması** içindir; istemci kardeş çağrılarda gövdeden dönen `id`yi
    kullanır (URL-2 kararı 3). Detay yanıtındaki `slug`ı bir eylem ucuna
    geçirmek 422 verir — bu bir kusur değil, yazma yüzeyini tahmin edilebilir
    bir anahtara açmama kararının görünen yüzüdür.
    """
    invoice = await service.visible_invoice(session, user, parse_ref(invoice_id))
    return await service.build_detail(session, invoice)


# --- Uç 5: PATCH ---


@router.patch(
    "/invoices/{invoice_id}",
    response_model=InvoiceDetailResponse,
    responses={
        404: {"description": "Fatura ya da seçilen proje/şantiye/cari/kaynak bulunamadı"},
        409: {"description": "Fatura bu durumda düzenlenemez"},
        422: {"description": "Gövde kuralı ihlali ya da gelen faturada kapsam dışı alan"},
    },
    dependencies=[_FULL],
)
async def update_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    data: InvoiceUpdate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """**Giden faturada yalnız `draft`, gelen faturada yalnız `pending`** —
    aksi **409** (yetki değil DURUM engeli).

    Gelen faturada yalnız `note`/`due_date`/`payment_method` düzeltilebilir
    (**422** aksi hâlde): gelen fatura SATICININ belgesidir.

    Kayıt kilitlenerek okunur ve durum kapısı KİLİTLİ satır üzerinde koşar
    (spec §8, TOCTOU). Oran değişirse başlık toplamları `amounts`tan YENİDEN
    hesaplanır; kalemler değişmez (onların yolu `PUT lines`).
    """
    await assert_tasima_yetkisi(
        session,
        request,
        user,
        gate_flags(service.PERMISSION_MODULE, AccessLevel.full),
        data,
        proje_yok=guards.INVOICE_PROJECT_INVALID,
        santiye_yok=guards.INVOICE_SITE_INVALID,
    )
    invoice = await service.visible_invoice(session, user, invoice_id, for_update=True)
    invoice, detail = await service.update_invoice(session, user, invoice, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_detail(session, invoice)


# --- Uç 6: DELETE ---


@router.delete(
    "/invoices/{invoice_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_WITH_PREVIEW_RESPONSES,
        404: {"description": "Fatura bulunamadı"},
    },
    dependencies=[require_system_admin()],
)
async def delete_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Faturayı bağlı kayıtlarıyla birlikte siler. Yalnız Sistem Yöneticisi.

    ÖNİZLEME ZORUNLU.
    Durumdan bağımsız (taslak olmayan fatura da silinir). Kalemleri, ödemeleri, faturanın ve
    ödemelerinin muhasebe fişleri ile stornoları birlikte gider (K2; kapalı dönem durdurmaz).
    Önce `GET /admin/silme/invoice/{id}/onizleme`, sonra bu uç `preview_token` ile: eksikse 428
    `preview_required`; ağaç değiştiyse 409 `preview_stale`. Yanıt `204`, gövdesiz.
    """
    detail = await silme_service.sil(session, "invoice", invoice_id, preview_token)
    await _audit(request, session, user, AuditAction.delete, detail)


# --- Uç 7: PUT lines ---


@router.put(
    "/invoices/{invoice_id}/lines",
    response_model=InvoiceDetailResponse,
    responses={
        404: {"description": "Fatura bulunamadı"},
        409: {"description": "Kalemler yalnızca taslak faturada değiştirilebilir"},
    },
    dependencies=[_FULL],
)
async def replace_invoice_lines_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    data: InvoiceLinesReplace,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """Kalem kümesini TOPTAN yazar (hakediş/puantaj emsali) — yalnız `draft`.

    `sort_order` gövdedeki dizinin İNDEKSİDİR, `line_total` sunucunun hesabıdır;
    ikisi de gövdeden GELEMEZ (**422**). Başlık toplamları aynı hesapla
    güncellenir. Boş liste hepsini SİLER (kalemsiz taslak meşrudur; K6 kapısı
    `send`/`approve` anındadır, T4).

    Kilit sırası SABİT: fatura → kalemler.
    """
    invoice = await service.visible_invoice(session, user, invoice_id, for_update=True)
    invoice, detail = await service.replace_lines(session, invoice, data)
    await _audit(request, session, user, AuditAction.update, detail)
    return await service.build_detail(session, invoice)


# --------------------------------------------------------------------------- #
# Uçlar 8-11: DURUM GEÇİŞLERİ (spec §3, §7, §8)
#
# Dördü de TEK gövdeyi (`_gecis`) paylaşır; aralarındaki tek fark `InvoiceAction`
# parametresidir ve geçerliliği `transitions.py`nin matrisinden okunur. Uçların
# hiçbirinde `if invoice.status == …` YOKTUR (§3): dört ayrı gövde yazılsaydı
# kilidi ya da K6 kapısını birinde unutmak mümkün olurdu.
#
# 🔴 Ortak sözleşme: **409** yön dışı ya da matris dışı geçiş · **422** kalemsiz
# `send`/`approve` (K6) ya da ödemesiz `mark-collected` (MU-3E İŞ 2) · **404**
# görünmeyen fatura · **403** `full` altı yetki.
# Geçiş yalnız `status` damgalar — para alanları YENİDEN HESAPLANMAZ (K7).
# --------------------------------------------------------------------------- #

_GECIS_YANITLARI = {
    404: {"description": "Fatura bulunamadı"},
    409: {"description": "İşlem faturanın yönüne ya da durumuna uygulanamaz"},
}

_KAPILI_GECIS_YANITLARI = {
    **_GECIS_YANITLARI,
    422: {"description": "Kalemsiz fatura gönderilemez / onaylanamaz (K6)"},
}

#: 🔴 MU-3E İŞ 2 — `mark-collected` de artık 422 verebilir, ama SEBEBİ K6 DEĞİL.
#: `_KAPILI_GECIS_YANITLARI` yeniden kullanılsaydı OpenAPI, kalemsiz bir
#: faturanın tahsil edilemediğini söyler ve istemci kullanıcıya YANLIŞ İŞİ
#: yaptırırdı (kalem eklemek yerine ödeme girmesi gerekir).
_TAHSILAT_GECIS_YANITLARI = {
    **_GECIS_YANITLARI,
    422: {"description": "Faturanın toplamını karşılayan ödeme kaydı yok (MU-3E İŞ 2)"},
}


async def _gecis(
    request: Request,
    session: AsyncSession,
    user: User,
    invoice_id: uuid.UUID,
    action: InvoiceAction,
) -> InvoiceDetailResponse:
    """Dört geçiş ucunun ORTAK gövdesi — kilit/matris/K6 sırası servistedir."""
    sonuc = await state_service.perform_transition(session, user, invoice_id, action)
    await _audit(request, session, user, sonuc.audit_action, sonuc.detail)
    return await service.build_detail(session, sonuc.invoice)


@router.post(
    "/invoices/{invoice_id}/send",
    response_model=InvoiceDetailResponse,
    responses=_KAPILI_GECIS_YANITLARI,
    dependencies=[_FULL],
)
async def send_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """`draft → sent` (FK:25 `GİB'e Gönder`) — YALNIZ giden fatura.

    **GİB'e gerçek bir gönderim YAPILMAZ** (spec §1: entegratör bağı FAT-3'ün
    işidir); bu uç yalnızca DURUM damgalar — `progress_payments.mark-paid`
    emsali. Gelen faturaya çağrılırsa **409** (yön dışı).

    🔴 **K6:** kalemsiz fatura **422**. Kalemsiz fatura 0,00₺ olarak kusursuz
    hesaplanır — hesap doğrudur, FATURA yanlıştır.
    """
    return await _gecis(request, session, user, invoice_id, InvoiceAction.send)


@router.post(
    "/invoices/{invoice_id}/mark-collected",
    response_model=InvoiceDetailResponse,
    responses=_TAHSILAT_GECIS_YANITLARI,
    dependencies=[_INVOICE_APPROVE],
)
async def mark_collected_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """`sent → collected` (FY:130 `Tahsil Edildi`) — YALNIZ giden fatura.

    **Tahsilat KAYDI değildir** (FGI:220-247 formu Hazine diliminindir:
    `bank_accounts` tablosu henüz YOK). Burada yalnız damga vardır; tutar,
    hesap ve tarih alanları AÇILMAZ — yazma yolu olmayan kolon her zaman NULL
    döner ve uydurma alan olur.

    K6 kapısı UYGULANMAZ: `sent` bir fatura zaten kapıdan geçmiştir.

    🔴 **MU-3E İŞ 2 — ÖDEME ARANIR (kullanıcı kararı 2026-08-26).** Faturanın
    toplamını karşılayan ödeme kaydı yoksa **422**. Ödemesiz damga muhasebede
    `120 Alıcılar`ı AÇIK bırakıyor ve mizan alıcıları fazla gösteriyordu:
    nakit bacağı `payments` satırından doğar (MU-3C), ödeme yoksa fiş de
    yoktur. Doğru yol `POST /payments`tir — tahsilat girildiğinde bu damga
    KENDİLİĞİNDEN basılır (K5) ve nakit fişi de aynı işlemde yazılır.
    """
    return await _gecis(request, session, user, invoice_id, InvoiceAction.mark_collected)


@router.post(
    "/invoices/{invoice_id}/approve",
    response_model=InvoiceDetailResponse,
    responses=_KAPILI_GECIS_YANITLARI,
    dependencies=[_INVOICE_APPROVE],
)
async def approve_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """`pending → approved` (FGE:25 `Onayla & Muhasebeleştir`) — YALNIZ gelen.

    **Muhasebe fişi ÜRETİLMEZ** (FGE:197-241 önizlemesi Muhasebe diliminindir:
    hesap planı tablosu YOK). `Kısmi Onayla` (FGE:140) da AÇILMADI — etkisi
    hiçbir mockup'ta çizilmemiş, FAT-2'nin işi.

    🔴 **K6:** kalemsiz fatura **422**. Giden faturaya çağrılırsa **409**.
    """
    return await _gecis(request, session, user, invoice_id, InvoiceAction.approve)


@router.post(
    "/invoices/{invoice_id}/dispute",
    response_model=InvoiceDetailResponse,
    responses=_GECIS_YANITLARI,
    dependencies=[_FULL],
)
async def dispute_invoice_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> InvoiceDetailResponse:
    """`pending → disputed` (FGE:24 `İtiraz Et`) — YALNIZ gelen fatura.

    İtiraz GEREKÇESİ alanı AÇILMADI: FGE:24 tek bir düğmedir, gerekçe formu
    hiçbir mockup'ta çizilmemiştir (icat yasağı). `İtiraz/İade` sekmesinin
    (FY:65) içeriği de çizilmemiştir — bu uç yalnız durumu damgalar.

    K6 kapısı UYGULANMAZ: itiraz bir REDDETMEDİR ve eksik kalem, itirazı
    engellemek için sebep değildir.
    """
    return await _gecis(request, session, user, invoice_id, InvoiceAction.dispute)


# --------------------------------------------------------------------------- #
# HZ-1 T4 — ÖDEME UÇLARI (6, 7, 8) · FGI:220-247 `Tahsilat Kaydı` formu
#
# 🔴 **NEDEN BU DOSYADA** (HZ-1 spec §5, MK-2'nin `main.py:94-104` dersi):
# `/invoices/{id}/payments` yolları `invoicing` router'ının İÇİNDE tanımlanır.
# Ayrı bir router'da tutulup `main.py`de `invoicing_router`dan SONRA
# kaydedilselerdi, `/invoices/{invoice_id}` rotasının önce eşleşme riski taşıyan
# bir kayıt sırası doğardı; yolun sahibi router'la aynı yerde durması bu soruyu
# tamamen ortadan kaldırır.
#
# `DELETE /payments/{id}` de BURADADIR (`treasury` router'ında değil) ve gerekçe
# ROTA ÇAKIŞMASI DEĞİLDİR — `/payments` kökü hiçbir yolla çakışmaz. Gerekçe
# İZİN ve TAG birliğidir: üç ucun kapısı da **`invoicing`**tir (spec §4) ve
# `treasury` router'ı yalnız `treasury` izinli yolları barındırır. Uç oraya
# konsaydı `treasury` etiketli bir yol `invoicing` izniyle korunur, hem openapi
# gruplaması hem BFF kökü yanıltıcı olurdu.
#
# 🔴 **İŞ MANTIĞI BURADA DEĞİL** `treasury/payments_service.py`dedir: kilit
# (K7), eşik (K6) ve durum türetimi (K5) tek dosyada durur. Bu router yalnız
# yolu, yetkiyi ve denetim satırını taşır.
# --------------------------------------------------------------------------- #

_ODEME_YANITLARI = {
    404: {"description": "Fatura ya da seçilen banka hesabı bulunamadı"},
    422: {"description": "Toplam tahsilat fatura tutarını aşamaz (K6)"},
}


@router.get(
    "/invoices/{invoice_id}/payments",
    response_model=PaymentListResponse,
    responses={404: {"description": "Fatura bulunamadı"}},
    dependencies=[_VIEW],
)
async def list_invoice_payments_endpoint(
    invoice_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    limit: _LIMIT = 50,
    offset: _OFFSET = 0,
) -> PaymentListResponse:
    """Faturanın tahsilat/ödeme satırları + **`paid_total`** + **`remaining`**.

    🔴 K5: `invoices` üzerinde `paid_amount` kolonu YOKTUR; iki toplam da
    `Σ payments`ten TÜRETİLİR ve **TÜM satırlardan** gelir — sayfadan DEĞİL.

    `limit` varsayılan 50, tavan 200 — aşım **422** (kırpma DEĞİL). Görünmeyen
    fatura var olmayanla AYNI 404'ü alır.
    """
    return await payments_service.list_payments(
        session, user, invoice_id, limit=limit, offset=offset
    )


@router.post(
    "/invoices/{invoice_id}/payments",
    response_model=PaymentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=_ODEME_YANITLARI,
    dependencies=[_FULL],
)
async def create_invoice_payment_endpoint(
    request: Request,
    invoice_id: uuid.UUID,
    data: PaymentCreate,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
) -> PaymentResponse:
    """FGI:220-247 formunun kaydı — tahsilat DA ödeme DE aynı uçtur (K4).

    * 🔴 **K7:** fatura satırı DENETİMLERDEN ÖNCE kilitlenir (`FOR UPDATE`);
      kilit sırası SABİT: fatura → ödemeler → hesap.
    * 🔴 **K6:** `Σ payments + yeni > total` → **422**, kuruş bazında TAM
      karşılaştırma, tolerans YOK. `= total` GEÇER.
    * 🔴 **K5:** başarıda fatura durumu `Σ`dan TÜRETİLEREK damgalanır ve damga
      yalnız matrisin TANIDIĞI geçişle konur; gelen faturada durum DEĞİŞMEZ.
    * `bank_account_id` yoksa **404**; pasif hesap **422**.
    * Yön gövdeden GELMEZ, bağlı faturanın `direction`'ından okunur (K4).
    """
    payment, detail = await payments_service.create_payment(session, user, invoice_id, data)
    await _audit(request, session, user, AuditAction.create, detail)
    return PaymentResponse.model_validate(payment)


@router.delete(
    "/payments/{payment_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    responses={
        **DELETE_WITH_PREVIEW_RESPONSES,
        404: {"description": "Ödeme kaydı bulunamadı"},
    },
    dependencies=[require_system_admin()],
)
async def delete_payment_endpoint(
    request: Request,
    payment_id: uuid.UUID,
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    preview_token: PreviewTokenQuery = None,
) -> None:
    """Ödeme/tahsilat kaydını bağlı kayıtlarıyla birlikte siler. Yalnız Sistem Yöneticisi.

    ÖNİZLEME ZORUNLU.
    Ödemenin muhasebe fişi ve stornosu birlikte silinir (yeni storno YAZILMAZ; kapalı dönem
    durdurmaz). Bağlı çek/senet portföy dışıysa o da fişiyle silinir; portföydeki çek aynen kalır.
    Faturanın durumu kalan ödemelerden yeniden türetilir. Önce
    `GET /admin/silme/payment/{id}/onizleme`, sonra bu uç `preview_token` ile: eksikse 428
    `preview_required`; ağaç değiştiyse 409 `preview_stale`. Yanıt `204`.
    """
    detail = await silme_service.sil(session, "payment", payment_id, preview_token)
    await _audit(request, session, user, AuditAction.delete, detail)
