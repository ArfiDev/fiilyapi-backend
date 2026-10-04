"""Satınalmanın İKİ durum makinesi ve ₺500K onay eşiği (SA spec §3, §7 S1/S2).

## Tek tablo, tek kapı

Geçerli geçişler AŞAĞIDAKİ İKİ veri yapısındadır (`REQUEST_TRANSITIONS` ·
`ORDER_TRANSITIONS`); uçlar ve servis kendi `if status == …` kontrollerini
YAZMAZ. Tabloda olmayan her çift 409'dur — "tanımlı olanı say, gerisini
reddet" yaklaşımıyla yeni bir durum eklendiğinde varsayılan davranış
REDDETMEKTİR. Desen `progress_payments/transitions.py`ten alınmıştır.

`build_transition_table` oradan İTHAL EDİLMEDİ ve bu bilinçlidir: o fonksiyon
`_TRANSITION_SHAPE`i (hakediş dörtlüsü) `PaymentAction`a bağlar; satınalmanın
işlem kümesi (`select-and-order`) ve hedefleri (`approve → quote_wait`,
`reject → rejected`) FARKLIDIR. Ortaklaştırılsaydı iki iş akışının şekli tek
sabitte birbirine düğümlenir ve birinin değişimi ötekini sessizce bozardı.
Ortak olan ŞEY DESENDİR, veri değil.

## Neyin tabloda OLMADIĞI da bir karardır

* Talep `delivered`e ELLE geçmez: o damgayı stok girişi atar (§7 S4, T4'ün ST
  zinciri). Bir uç açılsaydı hiç mal girmemiş bir talep teslim görünürdü.
* Sipariş `in_transit → delivered` de AYNI sebeple tabloda yoktur.
* `delivered`/`rejected` talep tarafında TERMİNALDİR: hiçbir çiftte KAYNAK
  değillerdir. Reddedilen talep diriltilmez — ihtiyaç sürüyorsa YENİ talep
  açılır, çünkü ret gerekçesi o kaydın kalıcı bir niteliğidir.

## TESLİM tabloları neden AYRIDIR (T4)

`ORDER_DELIVERY_TRANSITIONS` ve `REQUEST_DELIVERY_TRANSITIONS`, yukarıdaki iki
tablodan ayrı durur ve onlara BİRLEŞTİRİLMEZ. Sebep davranışsaldır:
`ORDER_TRANSITIONS` PATCH ucunun tablosudur — oraya `delivered` eklenseydi
kullanıcı hiç mal girmemiş bir siparişi elle teslim edilmiş yapabilirdi, yani
§7 S4'ün tam olarak yasakladığı şey PATCH'te açılırdı. Teslim damgasının TEK
çağıranı `stock_link`tir.

İkinci fark: teslim tablosu dışındaki tek kaynak durum `delivered`in KENDİSİDİR
ve o **409 değil SESSİZ GEÇİŞTİR** (`stock_link` gerekçesi) — stok hareketi bir
olgudur, satınalma damgası yüzünden reddedilemez.

## ₺500K eşiği bir YETKİ kuralıdır, zorunluluk değil

`validation.submit_blockers` "talep eksik mi" sorusunu yanıtlar; eşik ise
"bunu KİM onaylayabilir" sorusunu. Bu yüzden ikisi ayrı modüldedir ve eşik
`submit`te DEĞİL `approve`ta koşar.

🔴 **OK-1C K1 — zinciri OLAN talepte eşiğin YETKİ kapısı İKAME EDİLİR.** Eşiğin
karşılığı orada zincirin `patron` ADIMIDIR; `full` seviyesini AYRICA aramak aynı
kuralı iki kez uygulamak ve zinciri 2. adımda (Proje Müdürü, `approve`) ölü
bırakmaktı. Kural KAYBOLMAZ, YER DEĞİŞTİRİR. Zincirsiz (eski) talepte kapı
AYNEN durur.

**Eşik AYARDAN okunur (OK-1A R6).** Sihirli sayı burada DEĞİL
`company.approval_threshold_try`dedir; onay zincirinin Patron adımı da AYNI
değerden türer. İki eşik bir arada yaşasaydı sessizce ayrışırlardı.

**Eşik ONAY ANINDA, GÜNCEL kalemlerden YENİDEN hesaplanır.** `purchase_requests`
üzerinde donmuş bir tutar kolonu yoktur (T1 kararı) ve olmamalıdır: olsaydı
kalem değişiminde bayatlar, düşük tutarla onaya gönderilip sonra şişirilen bir
talep düşük yetkiyle onaylanabilirdi. PATCH ucunun `pending_approval`da 409
vermesi bu saldırının bir yolunu zaten kapatır — ama eşik ona DAYANMAZ; iki
katman birbirinin yedeğidir ve savunma derinliği testle kilitlidir.
"""

import enum
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, satisfies
from app.core.errors import ApprovalNotAllowedError, ConflictError, ProcurementValidationError
from app.modules.approvals import service as approvals_service
from app.modules.approvals.models import ApprovalDocumentType
from app.modules.procurement import guards, repository, validation
from app.modules.procurement.models import (
    PurchaseOrder,
    PurchaseOrderStatus,
    PurchaseRequest,
    PurchaseRequestStatus,
)
from app.modules.users.models import User

__all__ = [
    "APPROVAL_THRESHOLD_LEVEL",
    "chain_amount",
    "OPEN_REQUEST_STATUSES",
    "ORDER_DELIVERY_TRANSITIONS",
    "ORDER_TRANSITIONS",
    "PENDING_ORDER_STATUSES",
    "REQUEST_DELIVERY_TRANSITIONS",
    "REQUEST_TRANSITIONS",
    "RequestAction",
    "apply_request_transition",
    "assert_order_transition",
    "order_total_from_quote",
]


class RequestAction(str, enum.Enum):
    """Talep geçişleri — değerler UÇ YOLLARIYLA birebir aynıdır
    (`…/select-and-order`), böylece router ile tablo arasında ikinci bir
    eşleme sözlüğü gerekmez (`PaymentAction` deseni)."""

    submit = "submit"
    approve = "approve"
    reject = "reject"
    select_and_order = "select-and-order"


#: Spec §3 talep tablosu — TEK KOPYA. Burada olmayan çift 409'dur.
REQUEST_TRANSITIONS: dict[tuple[PurchaseRequestStatus, RequestAction], PurchaseRequestStatus] = {
    (PurchaseRequestStatus.draft, RequestAction.submit): PurchaseRequestStatus.pending_approval,
    # §3: `approve` ARA bir "onaylandı" durumu üretmez — onaydan sonraki iş
    # teklif toplamaktır ve SAT rozetlerinde de "Teklif Bekleniyor" vardır.
    (PurchaseRequestStatus.pending_approval, RequestAction.approve): (
        PurchaseRequestStatus.quote_wait
    ),
    (PurchaseRequestStatus.pending_approval, RequestAction.reject): PurchaseRequestStatus.rejected,
    (PurchaseRequestStatus.quote_wait, RequestAction.select_and_order): (
        PurchaseRequestStatus.ordered
    ),
}

#: Spec §3 sipariş tablosu — geçiş HEDEF DURUMLA adlandırılır çünkü PATCH
#: gövdesi `status` taşır (ayrı bir aksiyon yolu yoktur, SIP'te düğme de
#: durumun kendisini yazar). `(in_transit, delivered)` KASITLI olarak yoktur.
ORDER_TRANSITIONS: frozenset[tuple[PurchaseOrderStatus, PurchaseOrderStatus]] = frozenset(
    {(PurchaseOrderStatus.approved, PurchaseOrderStatus.in_transit)}
)

#: T4 — TESLİM tablosu (`stock_link`in TEK kaynağı, modül docstring'i).
#: `delivered → delivered` KASITLI olarak yoktur: o çift bir geçiş değil,
#: sessizce yutulan bir tekrardır (ikinci parti girişi).
ORDER_DELIVERY_TRANSITIONS: frozenset[tuple[PurchaseOrderStatus, PurchaseOrderStatus]] = frozenset(
    {
        (PurchaseOrderStatus.approved, PurchaseOrderStatus.delivered),
        (PurchaseOrderStatus.in_transit, PurchaseOrderStatus.delivered),
    }
)

#: T4 — siparişin bağlı TALEBİ. Tek meşru kaynak `ordered`dır: talep o duruma
#: yalnızca `select-and-order` ile gelir ve sipariş de yalnızca oradan doğar.
REQUEST_DELIVERY_TRANSITIONS: frozenset[tuple[PurchaseRequestStatus, PurchaseRequestStatus]] = (
    frozenset({(PurchaseRequestStatus.ordered, PurchaseRequestStatus.delivered)})
)

#: SIP 39 "Aktif Siparişler" + E3 81 "Bekleyen Sipariş" — TEK tanım.
#: "Aktif" = teslim EDİLMEMİŞ. İki ekran aynı sayıyı göstersin diye küme
#: burada durur; ST zarfı ve satınalma özeti onu ORTAK kullanır.
PENDING_ORDER_STATUSES: frozenset[PurchaseOrderStatus] = frozenset(
    {PurchaseOrderStatus.approved, PurchaseOrderStatus.in_transit}
)

#: SAT 71 "Açık Talepler" — TASLAK sayılmaz (kişisel yarım form, sahibinden
#: başkası göremeyeceği bir iş yükü değildir), `delivered`/`rejected` de
#: sayılmaz (kapanmış). Mockup'ın aritmetiği de bunu söyler: 8 = 2 (onay
#: bekleyen) + 5 (teklif bekleyen) + 1 (sipariş verilmiş).
OPEN_REQUEST_STATUSES: frozenset[PurchaseRequestStatus] = frozenset(
    {
        PurchaseRequestStatus.pending_approval,
        PurchaseRequestStatus.quote_wait,
        PurchaseRequestStatus.ordered,
    }
)

#: 🔴 FST 166 "₺500K+ → Patron". ESIK ARTIK BURADA DEGIL, AYARDADIR (OK-1A R6):
#: `company.approval_threshold_try`, tek yazma yolu `PUT /approvals/settings`.
#:
#: Gerekce: OK-1A cok adimli onay zincirini acti ve zincirin Patron adimi AYNI
#: esikten turuyor. Iki esik (buradaki sabit + zincirin ayari) bir arada
#: yasasaydi kacinilmaz olarak AYRISIRLAR ve ayni tutardaki bir talep
#: satinalmada esigin altinda, onay zincirinde ustunde sayilirdi. Sayinin
#: VARSAYILANI `approvals.definitions.DEFAULT_APPROVAL_THRESHOLD_TRY`dir ve
#: kolonun `server_default`i de odur — TEK kaynak.
#:
#: Sabitin geri gelmedigi `test_esik_tek_kaynak_AYARDIR` ile kilitlidir.

#: "Üst seviye rol" = `procurement` modülünde `full`. YENİ bir rol ya da izin
#: İCAT EDİLMEDİ: seed matrisinde (`roles/seed_data.py`) `procurement` satırı
#: sysadmin=`admin` · patron=`full` · satınalma=`full` · PM=`approve` ·
#: şef/saha=`request`tir. FST'nin "Patron"u tam olarak `full`e denk düşer ve
#: normal onaycı PM (`approve`) eşiğin altında kalır. Ladder zaten bu ayrımı
#: taşıdığı için ikinci bir mekanizma (bayrak kolonu, özel rol anahtarı)
#: gereksizdi — ve eklenseydi izin matrisi ile onay kuralı iki ayrı yerden
#: yönetilir, biri ötekinden saparadı.
APPROVAL_THRESHOLD_LEVEL = AccessLevel.full

#: Bu evrak ailesinin onay zincirindeki kimligi (mockup `Onay Kutusu.dc.html:150-178`).
#: 🔴 OK-1C'de YUKARI TASINDI: esik kapisi de zincirin varligini sorar ve
#: sabit fonksiyondan SONRA duruyordu (calisma zamaninda cozuluyordu ama
#: okuyan kisi tanimi arkada aramak zorunda kaliyordu).
_DOCUMENT_TYPE = ApprovalDocumentType.purchase_request


def _next_status(request: PurchaseRequest, action: RequestAction) -> PurchaseRequestStatus:
    new_status = REQUEST_TRANSITIONS.get((request.status, action))
    if new_status is None:
        raise ConflictError(guards.INVALID_REQUEST_TRANSITION)
    return new_status


async def _assert_submittable(session: AsyncSession, request: PurchaseRequest) -> None:
    """Sıkı doğrulama — TEK kaynağı `validation.submit_blockers`tır (T2).

    Engeller TEK 422'de birleşir: kullanıcıya eksikleri birer birer
    keşfettirmek FST gibi uzun bir formda kabul edilemez. Ayraç " · "dır ve
    sıra `submit_blockers`ın sırasıdır (başlık alanları önce, kalemler sonra).
    """
    lines = await repository.load_request_lines(session, request.id)
    blockers = validation.submit_blockers(request, lines)
    if blockers:
        raise ProcurementValidationError(" · ".join(blockers))


async def _assert_approver_level(
    session: AsyncSession, actor: User, request: PurchaseRequest
) -> None:
    """Onay eşiği (modül docstring'indeki gerekçe).

    🔴 EŞİK AYARDAN OKUNUR (OK-1A R6): `company.approval_threshold_try`.
    Buraya bir sayı GÖMÜLMEZ — onay zincirinin Patron adımı da AYNI değerden
    türer ve iki kaynak sessizce ayrışırdı. Varsayılan ₺500.000'dur
    (`approvals.definitions.DEFAULT_APPROVAL_THRESHOLD_TRY`).

    ⚠️ Eşik DEĞİŞTİĞİNDE bu kapı ANINDA yeni değeri okur ve bu bilinçlidir:
    burada donacak bir zincir YOKTUR — karar isteğin kendi anında verilir.
    Snapshot yalnızca `approval_chains` içindir (açık zincirler sonradan
    değişen eşikten etkilenmez).

    Tutar `repository.request_estimated_total` ile O AN hesaplanır — kayıtta
    donmuş bir toplam OKUNMAZ.

    ⚠️ T5 BULGUSU — FİYATSIZ KALEM EŞİĞİN ÜSTÜ SAYILIR (fail-closed). Toplam,
    fiyatsız kalemi atlar (`SUM` NULL'ları yutar); "eksik fiyat" bu yüzden
    "düşük tutar"dan AYIRT EDİLEMEZ. Bilinmeyen küçük sayılsaydı ₺2M'lik bir
    talep, tek bir alan boş bırakılarak toplam 0 gösterir ve DB'ye hiç
    dokunmadan en düşük yetkiliden geçerdi. `submit` bunu zaten engeller
    (`validation.LINE_PRICE_REQUIRED`) — buradaki kontrol İKİNCİ KATMANDIR ve
    ona DAYANMAZ: eski/elle girmiş fiyatsız satır da onaycısını şaşırtmamalıdır.

    🔴 **OK-1C K1 — ZİNCİRİ OLAN EVRAKTA BU KAPI İKAME EDİLİR.** Eşik denetimi
    zincirli bir talepte ZATEN zincirin kendi mekanizmasıdır:
    `approvals.definitions.step_roles` eşik aşılınca zincirin SONUNA bir
    **`patron`** adımı EKLER ve talep o imza atılmadan `quote_wait`e GEÇEMEZ.
    Burada ayrıca `full` aramak, aynı kuralı İKİ KEZ uygulamak ve zinciri fiilen
    ölü bırakmaktır: eşik üstü zincirin 2. adımı Proje Müdürü'nündür ve onun
    seviyesi `approve`tır — yani zincir tam orada, `APPROVAL_THRESHOLD_EXCEEDED`
    403'üne çarpıp KİLİTLENİYORDU (ölçüldü, 2026-08-22).

    🔴 **YETKİ NET OLARAK ZAYIFLAMAZ, YER DEĞİŞTİRİR.** Üst seviye imza koşulu
    kaybolmaz; `full` kapısından zincirin `patron` ADIMINA taşınır ve orada
    GÖREVLER AYRILIĞI ile birlikte uygulanır (aynı kişi iki adımı imzalayamaz),
    yani zincirli yolda koşul DAHA sıkıdır. Bekçisi
    `test_esik_USTU_talep_PATRON_adimi_imzalanmadan_TAMAMLANMAZ`tır.

    🔴 **SINIR: yalnız ZİNCİRİ OLAN evrak.** Zincirsiz (eski) bir talepte tek
    satır değişmez ve `full` aranmaya devam eder — orada `patron` adımı YOKTUR,
    dolayısıyla eşiği koruyan TEK katman budur.

    ⚠️ Sorgu maliyeti ≈ 0: bu dal YALNIZ eşik üstü (ya da tutarı bilinmeyen)
    taleplerde koşar ve zincir bulunduğunda `repository.actor_level` sorgusu
    HİÇ koşmaz — bir sorgu eklenmez, yer değiştirir.
    """
    lines = await repository.load_request_lines(session, request.id)
    total = await repository.request_estimated_total(session, request.id)
    tutar_bilinmiyor = validation.lines_missing_price(lines)
    threshold = await approvals_service.get_threshold(session)
    if total < threshold and not tutar_bilinmiyor:
        return
    if await approvals_service.open_chain(session, _DOCUMENT_TYPE, request.id) is not None:
        return
    level = await repository.actor_level(session, actor)
    if not satisfies(level, APPROVAL_THRESHOLD_LEVEL):
        raise ApprovalNotAllowedError(guards.APPROVAL_THRESHOLD_EXCEEDED)


def _stamp(
    request: PurchaseRequest, action: RequestAction, actor: User, reason: str | None
) -> None:
    """Geçişin damga kolonları.

    `submit` ve `select-and-order` damga BIRAKMAZ: ikisinin de "kim, ne zaman"
    bilgisi denetim günlüğündedir ve `purchase_requests`ta karşılık kolon
    açılmadı (T1). `reject` gerekçesi ise KOLONDUR (`rejection_reason`) çünkü
    SAT ekranı onu kaydın üstünde gösterir — denetim günlüğü ekranda okunmaz.
    """
    now = datetime.now(UTC)
    if action is RequestAction.approve:
        request.approved_at = now
        request.approved_by_user_id = actor.id
    elif action is RequestAction.reject:
        request.rejected_at = now
        request.rejection_reason = reason


async def chain_amount(session: AsyncSession, request: PurchaseRequest) -> Decimal | None:
    """Zincirin esikle karsilastiracagi tutar — `request_estimated_total` (KDV'siz).

    Esigin BUGUNKU kapisi (`_assert_approver_level`) da AYNI sayidan okur; ikinci
    bir taban acilsaydi ayni talep izin kapisinda esigin altinda, zincirde
    ustunde sayilirdi.

    🔴 NULL-ESIK / FAIL-CLOSED (SA kanonu, FIILEN BULUNMUS acik): fiyatsiz kalem
    `SUM`da YUTULUR ve "eksik veri" ile "dusuk tutar" ayni `0`i uretir. Bu
    yuzden `validation.lines_missing_price` dogruysa tutar BELIRLENEMEZ sayilir
    ve `None` doner — motor onu esigin USTU sayar, Patron adimi EKLENIR.
    `submit` fiyatsiz kalemi zaten 422 ile durdurur (BIRINCI katman); bu IKINCI
    katmandir ve ona DAYANMAZ (elle ya da eski bir surumle girmis satir).
    """
    lines = await repository.load_request_lines(session, request.id)
    if validation.lines_missing_price(lines):
        return None
    return await repository.request_estimated_total(session, request.id)


async def _chain_decision(
    session: AsyncSession,
    actor: User,
    request: PurchaseRequest,
    action: RequestAction,
    reason: str | None,
) -> approvals_service.ChainDecision | None:
    """OK-1A T3 — talebin onay ZINCIRIYLE bagi. TEK yer.

    🔴 KILIT SIRASI: talep satiri ZATEN kilitlidir (`service.visible_request_
    locked`); zincir ANCAK ondan sonra kilitlenir. Sira UC evrak ailesinde de
    AYNIDIR (deadlock).

    `select-and-order` zincire DOKUNMAZ: o bir onay adimi degil, onaydan SONRAKI
    tedarik adimidir.
    """
    if action is RequestAction.submit:
        await approvals_service.create_chain(
            session,
            document_type=_DOCUMENT_TYPE,
            document_id=request.id,
            amount=await chain_amount(session, request),
            created_by_user_id=request.created_by_user_id,
        )
        return None
    if action is RequestAction.approve:
        return await approvals_service.approve_next_step(
            session,
            actor=actor,
            document_type=_DOCUMENT_TYPE,
            document_id=request.id,
            require_chain=False,
        )
    if action is RequestAction.reject:
        return await approvals_service.reject_chain(
            session,
            actor=actor,
            document_type=_DOCUMENT_TYPE,
            document_id=request.id,
            reason=reason,
            require_chain=False,
        )
    return None


async def apply_request_transition(
    session: AsyncSession,
    actor: User,
    request: PurchaseRequest,
    action: RequestAction,
    *,
    reason: str | None = None,
) -> approvals_service.ChainDecision | None:
    """Talebin TEK geçiş yolu. Sıra: tablo → korkuluk → ZİNCİR → damga.

    Kapsam süzgeci (404) BURADA DEĞİL çağıranda koşar (`service.visible_request_
    locked`) ve tablo kontrolünden ÖNCEDİR: görünmeyen bir talebin durumu
    hakkında 409 ile bilgi sızdırılmaz.

    Korkuluklar tablodan SONRA koşar: yanlış durumdaki bir talep için önce
    "eksik alan" ya da "yetkin yetmiyor" demek, asıl engeli (kayıt o aşamada
    değil) gizlerdi.

    🔴 **OK-1A T3 — ZİNCİR korkuluklardan SONRA, damgadan ÖNCE.** `approve` artık
    zincirin SIRADAKİ adımını ilerletir ve talep ancak SON adımda `quote_wait`e
    geçer; ara adımlarda `pending_approval`da KALIR ve HİÇBİR damga atılmaz
    (`approved_at`/`approved_by_user_id` boş kalır — yarım bir onay "onaylandı"
    görünmemelidir).

    🔴 **OK-1C K1:** eşiğin izin kapısı (`_assert_approver_level`) zinciri OLAN
    talepte artık İKAME EDİLİR — iki katman birbirinin yedeği DEĞİL, aynı
    kuralın iki kopyasıydı ve zinciri 2. adımda kilitliyordu. Zincirsiz talepte
    kapı AYNEN durur; gerekçe o fonksiyonun docstring'indedir.
    """
    new_status = _next_status(request, action)

    if action is RequestAction.submit:
        await _assert_submittable(session, request)
    elif action is RequestAction.approve:
        await _assert_approver_level(session, actor, request)

    decision = await _chain_decision(session, actor, request, action, reason)
    if action is RequestAction.approve and decision is not None and not decision.is_complete:
        # ARA ADIM: durum ve damga DEĞİŞMEZ. Koşul YALNIZ `approve` içindir —
        # `reject` de `is_complete=False` döner ama zinciri DAMGALAR (silmez) ve talebi
        # `rejected`a taşıması GEREKİR.
        await session.flush()
        return decision

    request.status = new_status
    _stamp(request, action, actor, reason)
    await session.flush()
    return decision


def assert_order_transition(order: PurchaseOrder, target: PurchaseOrderStatus) -> None:
    """Siparişin durum kapısı. Tabloda olmayan her çift 409.

    Hedefin MEVCUT durumla aynı olması da geçiştir ve reddedilir: "değişmedi"
    sessizce başarı sayılsaydı ekran, geçersiz bir düğmeyi çalışıyor sanırdı.
    """
    if (order.status, target) not in ORDER_TRANSITIONS:
        raise ConflictError(guards.INVALID_ORDER_TRANSITION)


def order_total_from_quote(
    unit_price: Decimal,
    quantity_total: Decimal,
    shipping_included: bool,
    shipping_cost: Decimal | None,
) -> Decimal:
    """Sipariş tutarı = teklif × talebin toplam miktarı + (varsa) nakliye.

    Formül TEK KOPYADIR: "en iyi fiyat" rozeti de (`service.quote_total_cost`)
    buradan geçer. İki kopya olsaydı ekranda en ucuz görünen teklif ile
    sipariş edilen tutar farklı tabanlardan gelir ve sessizce ayrışırdı.
    """
    kalem = unit_price * quantity_total
    if shipping_included or shipping_cost is None:
        return kalem
    return kalem + shipping_cost
