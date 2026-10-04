"""IK-2 T3 — izin ONAY/RED + bakiye turevleri (spec §2, §3, §5 K3/K4/K5).

🔴 ESIK = KILIT burada yasar: `_lock_decision_scope` denetimlerden ONCE
serilestirir, `_assert_approvable` esikleri ondan SONRA okur."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import timezone
from app.core.errors import (
    ApprovalNotAllowedError,
    ConflictError,
    NotFoundError,
)
from app.modules.audit import messages
from app.modules.personnel import guards, leave, repository
from app.modules.personnel.models import (
    LeaveBalance,
    LeaveRequest,
    LeaveStatus,
    LeaveType,
    Personnel,
)
from app.modules.personnel.schemas import (
    LeaveBalanceResponse,
    LeaveBalanceUpdate,
    LeaveRejectRequest,
    LeaveRequestResponse,
)
from app.modules.personnel.service.core import (
    get_personnel,
    has_personnel_admin,
    is_own_personnel_record,
)
from app.modules.personnel.service.leave_requests import (
    _leave_response,
    find_overlapping_approved_leave,
    get_leave_request_row,
)
from app.modules.users.models import User


async def _leave_balance_parts(
    session: AsyncSession, personnel: Personnel, year: int, today: date
) -> tuple[int | None, Decimal, int]:
    """(hak, devreden, kullanılan) üçlüsü — bakiye ucunun ve onay kapısının ORTAK
    tabanı.

    İki yol da AYNI üçlüden beslenir ki ekranda görülen "kalan" ile onayı
    engelleyen "kalan" ASLA ayrışmasın: ayrı hesaplanan bir eşik, kullanıcının
    ekranda 4 gün görüp onayda 409 yediği (ya da tersi) bir dünya üretirdi.

    Bakiye satırı YOKSA `carried_over` sıfırdır — satır MANUEL devreden içindir
    (İZ 137) ve yokluğu "devreden yok" demektir, veri eksikliği değil.
    """
    reference = leave.balance_reference_date(year, today)
    entitlement = leave.annual_entitlement(personnel.hire_date, reference)
    balance = await repository.get_leave_balance(session, personnel.id, year)
    carried_over = balance.carried_over if balance is not None else Decimal("0")
    used = await repository.sum_deductible_approved_days(session, personnel.id, year)
    return entitlement, carried_over, used


def _balance_response(
    personnel: Personnel,
    year: int,
    today: date,
    entitlement: int | None,
    carried_over: Decimal,
    used: int,
) -> LeaveBalanceResponse:
    """Üçlü → İZ bakiye satırı. TÜM türevler `leave.py` tek kaynağından gelir —
    formül burada TEKRARLANMAZ."""
    reference = leave.balance_reference_date(year, today)
    months = leave.completed_service_months(personnel.hire_date, reference)
    return LeaveBalanceResponse(
        personnel_id=personnel.id,
        personnel_name=personnel.full_name,
        year=year,
        hire_date=personnel.hire_date,
        seniority_years=None if months is None else months // 12,
        seniority_months=None if months is None else months % 12,
        annual_entitlement=entitlement,
        carried_over=carried_over,
        used=used,
        remaining=leave.remaining_leave(entitlement, carried_over, used),
        usage_pct=leave.usage_pct(entitlement, carried_over, used),
    )


async def get_leave_balance(
    session: AsyncSession, personnel_id: uuid.UUID, year: int, *, today: date | None = None
) -> LeaveBalanceResponse:
    """Bakiye görünümü. Personel yok → 404; BAKİYE SATIRI yoksa 404 DEĞİL —
    türevler yine hesaplanabilir (devreden 0)."""
    today = today or timezone.today()
    personnel = await get_personnel(session, personnel_id)
    entitlement, carried_over, used = await _leave_balance_parts(session, personnel, year, today)
    return _balance_response(personnel, year, today, entitlement, carried_over, used)


async def upsert_leave_balance(
    session: AsyncSession,
    personnel_id: uuid.UUID,
    year: int,
    data: LeaveBalanceUpdate,
    *,
    today: date | None = None,
) -> tuple[LeaveBalanceResponse, str]:
    """Devreden günü yazar (UPSERT) — YALNIZ `carried_over` (spec §3, §5 K1).

    PUT'tur çünkü kaynak (personel, yıl) çiftiyle ADRESLENİR ve tek alanlıdır:
    aynı isteği iki kez göndermek aynı sonucu verir, ikinci satır AÇILMAZ
    (`uq_leave_balances_personnel_year` yarış emniyet ağı olarak kalır).

    **Kilit neden burada da var:** UQ tek başına yalnız ikinci SATIRI engeller —
    iki eşzamanlı PUT ikisi de "satır yok" görüp INSERT ederse ikincisi
    `IntegrityError`a düşer ve kullanıcı 409 alır; oysa PUT'un sözleşmesi
    "gönderdiğin değer yazılır"dır, ikinci istek UPDATE'e DÜŞMELİDİR. Ayrıca
    `carried_over` onay eşiğinin (K5) girdisidir: kilit `approve` ile AYNI
    personel satırında olduğundan devreden gün, onay hesabının ortasında
    kayamaz. Sıra `_lock_decision_scope` ile aynıdır (personel önce).

    Türev alanlar gövdede KABUL EDİLMEZ (`extra="forbid"`) — `annual_entitlement`
    kolon değildir (K1) ve gönderilmesi sessizce yutulsaydı istemci hakkı
    değiştirdiğini sanırdı.
    """
    today = today or timezone.today()
    personnel = await get_personnel(session, personnel_id)
    await repository.lock_personnel_for_update(session, personnel.id)

    balance = await repository.get_leave_balance(session, personnel.id, year)
    if balance is None:
        balance = LeaveBalance(personnel_id=personnel.id, year=year, carried_over=data.carried_over)
        await repository.add_leave_balance(session, balance)
    else:
        balance.carried_over = data.carried_over
        await session.flush()
        await session.refresh(balance)

    entitlement, carried_over, used = await _leave_balance_parts(session, personnel, year, today)
    detail = messages.leave_balance_updated(personnel.full_name, year, balance.carried_over)
    return _balance_response(personnel, year, today, entitlement, carried_over, used), detail


async def _lock_decision_scope(
    session: AsyncSession, request: LeaveRequest, personnel: Personnel
) -> None:
    """Karar yolunun SERİLEŞTİRME kilidi — TÜM denetimlerden ÖNCE (spec §5 K3/K5).

    **Sıra sabittir: önce `personnel`, sonra `leave_requests`.** Kilit alan üç yol
    (`approve`, `reject`, `upsert_leave_balance`) AYNI sırayı izler; ters sırada
    kilitleyen bir yol eklenirse karşılıklı kilitlenme (deadlock) doğar.

    * **Personel satırı** eşiğin ortak kaynağıdır: çakışma (K3) ve kalan hak (K5)
      denetimleri o personelin TÜM onaylı izinleri üzerinden okunur. Kilit
      alındıktan sonra yapılan okumalar (READ COMMITTED) rakip transaction'ın
      COMMIT'ini görür — "ikisi de eşiği geçti" yarışı burada kapanır.
    * **Talep satırı** çift-karar yarışına karşıdır: `populate_existing` ile durum
      kilit ALTINDA yeniden okunur, yoksa `_assert_decidable` atlatılabilirdi.

    Talep kilit alınana kadar SİLİNMİŞ olabilir (`delete_leave_request` yalnız
    `pending` satırı siler) — o hâlde 404, kayıt yokmuş gibi.
    """
    await repository.lock_personnel_for_update(session, personnel.id)
    if await repository.get_leave_request_locked(session, request.id) is None:
        raise NotFoundError(guards.LEAVE_REQUEST_MISSING)


def _assert_decidable(request: LeaveRequest) -> None:
    """Karara YALNIZ `pending` talep açıktır → aksi 409 (spec §5 K4: onay TEK adım).

    Onaylanmış talebi yeniden onaylamak bakiyeyi ikinci kez tüketmez ama karar
    damgasını (kim, ne zaman) sessizce EZERDİ; reddedilmişi onaylamak ise
    reddin denetim izini yok ederdi. Düzeltme yolu yeni talep açmaktır.
    """
    if request.status is not LeaveStatus.pending:
        raise ConflictError(guards.LEAVE_DECISION_NOT_PENDING)


def _assert_withdrawable(request: LeaveRequest) -> None:
    """Geri çekme YALNIZ `pending` talepte → aksi 409 (İK-2.2).

    Karara bağlanmış talebi geri çekmek onayı ya da reddi SESSİZCE İPTAL ederdi;
    ikinci kez geri çekmek ise ilk geri çekmenin damgasını EZERDİ. Metin
    `LEAVE_DECISION_NOT_PENDING`ten AYRIDIR (guards.py gerekçesi).
    """
    if request.status is not LeaveStatus.pending:
        raise ConflictError(guards.LEAVE_WITHDRAW_NOT_PENDING)


async def _assert_approver_is_not_owner(
    session: AsyncSession, actor: User, personnel: Personnel
) -> bool:
    """🔴 OK-1A T5 — kimse KENDİ izin talebini ONAYLAYAMAZ. Döner: "vekâleten" mi.

    Kullanıcı kararı (2026-08-21). **TEK İSTİSNA `admin`dir** ve o da denetim
    günlüğüne `messages.APPROVAL_ON_BEHALF_MARK` işaretiyle geçer — istisna
    GÖRÜNÜR olmadan verilirse denetim onu bir daha bulamaz.

    ## Kapsam — bu bir KAPI, onay ZİNCİRİ değil

    İzin talebi `approvals` motoruna BAĞLANMADI (K4): zincirin adımlarını ₺ tutarı
    ve eşik belirler, izin talebinde ikisi de YOKTUR. Onay hâlâ TEK ADIMDIR
    (İK-2 spec §5 K4); buraya konan yalnızca bir 403'tür.

    ## Sıra: `_assert_decidable`ten SONRA, `_assert_approvable`ten ÖNCE

    Durum (409) önce koşar çünkü karara bağlanmış bir talep kimse için açık
    değildir — sahibine "onaylayamazsın" demek, asıl olguyu (karar zaten
    verilmiş) gizlerdi. İş kuralı kapıları (çakışma/hak aşımı, 409) ise SONRA
    koşar: yetkisi olmayana önce "hak aşımı" demek, gerçek engeli gizlerdi
    (`approvals/service.py::reject_chain` ile aynı gerekçe).

    ## RED bu kapıdan GEÇMEZ

    Kullanıcı kararı yalnız "onaylayamaz" der. Kendi talebini reddetmek bir yetki
    YÜKSELTMESİ değildir (kişi zaten `pending` talebini SİLEBİLİR) ve ret'e kapı
    konsaydı, kendi talebini geri çevirmek isteyen kullanıcı kilitlenirdi.
    """
    if not is_own_personnel_record(personnel, actor):
        return False
    if not await has_personnel_admin(session, actor):
        raise ApprovalNotAllowedError(guards.LEAVE_APPROVE_OWN_REQUEST)
    return True


async def _assert_approvable(
    session: AsyncSession,
    request: LeaveRequest,
    personnel: Personnel,
    leave_type: LeaveType,
    today: date,
) -> None:
    """`approve`ın İKİ iş kuralı kapısı (spec §5 K3, K5) — ikisi de 409.

    **Sıra bilinçlidir:** önce ÇAKIŞMA (K3), sonra hak aşımı (K5). Çakışma kaydın
    kendisiyle ilgili mutlak bir engeldir (bir gün iki izne birden ait olamaz) ve
    tipten bağımsızdır; hak aşımı ise yalnız `deducts_from_annual` tiplerde
    anlamlıdır. Ters sırada, çakışan bir hastalık izni için önce hesap yapılıp
    sonra çakışmaya düşülürdü — kullanıcı da daha az bilgilendirici hatayı görürdü.

    **RED bu kapılardan GEÇMEZ** (İZ 98-99: hak aşan satırda ✓ pasif, ✗ aktif).
    """
    overlapping = await find_overlapping_approved_leave(
        session,
        personnel.id,
        request.start_date,
        request.end_date,
        exclude_id=request.id,
    )
    if overlapping is not None:
        raise ConflictError(guards.LEAVE_OVERLAPPING_APPROVED)

    # Yıllık haktan DÜŞMEYEN tip (hastalık/mazeret) eşiğe HİÇ girmez — kıdemsiz
    # personel de rapor izni alabilmelidir (İZ 87).
    if not leave_type.deducts_from_annual:
        return

    year = leave.leave_year(request.start_date, request.end_date)
    entitlement, carried_over, used = await _leave_balance_parts(session, personnel, year, today)
    remaining = leave.remaining_leave(entitlement, carried_over, used)
    # 🔴 NULL-EŞİK KANONU (fail-closed): kalan HESAPLANAMIYORSA onay ENGELLİDİR.
    # "Bilinmeyen = küçük" varsayımı burada `used=0` + `hak=0` üretip tam hakkı
    # açardı; bilinmeyen BÜYÜK/engelleyici sayılır ve ayrı bir metinle söylenir
    # (kullanıcı "hak aşımı" değil "kıdem/işe giriş eksik" olduğunu görsün).
    if remaining is None:
        raise ConflictError(guards.LEAVE_ENTITLEMENT_UNKNOWN)
    if Decimal(request.days) > remaining:
        raise ConflictError(guards.LEAVE_ENTITLEMENT_EXCEEDED)


def _stamp_decision(
    request: LeaveRequest, actor: User, status: LeaveStatus, reason: str | None
) -> None:
    """Karar damgası SUNUCUDANDIR (istemci gönderemez, şema `extra="forbid"`).

    `reject_reason` onayda AÇIKÇA temizlenir: bir talep yalnız `pending`ken karara
    açıldığından bugün dolu gelemez, ama alan boş bırakılırsa ileride bir "karara
    geri döndürme" yolu açıldığında eski red gerekçesi onaylı kayıtta ASILI KALIRDI.
    """
    request.status = status
    request.decided_by = actor.id
    request.decided_at = datetime.now(UTC)
    request.reject_reason = reason


async def approve_leave_request(
    session: AsyncSession, actor: User, request_id: uuid.UUID, *, today: date | None = None
) -> tuple[LeaveRequestResponse, str]:
    """Talebi onaylar — TEK adım (spec §5 K4), kapı `personnel` **full+**.

    Sıra: kayıt (404) → **satır kilidi** → durum (409) → **KENDİ TALEBİ (403,
    OK-1A T5)** → çakışma (409) → hak aşımı / fail-closed (409) → damga. TÜM
    denetimler yazmadan ÖNCE koşar: yarı onaylanmış bir kayıt bırakılmaz.

    403 kapısının `admin` istisnası kararı "vekâleten" yapar ve bu, denetim
    metnine `on_behalf` ile taşınır (`_assert_approver_is_not_owner`).

    Kilit denetimlerden ÖNCE ve AYNI transaction içinde alınır
    (`_lock_decision_scope`): kilitsiz hâlde iki eşzamanlı onay aynı `used`
    toplamını okuyup ikisi de K5 eşiğini geçerdi.
    """
    today = today or timezone.today()
    request, personnel, leave_type = await get_leave_request_row(session, request_id)
    await _lock_decision_scope(session, request, personnel)
    _assert_decidable(request)
    on_behalf = await _assert_approver_is_not_owner(session, actor, personnel)
    await _assert_approvable(session, request, personnel, leave_type, today)

    _stamp_decision(request, actor, LeaveStatus.approved, None)
    await session.flush()
    await session.refresh(request)

    detail = messages.leave_request_approved(
        personnel.full_name,
        leave_type.name,
        request.start_date,
        request.end_date,
        on_behalf=on_behalf,
    )
    return _leave_response(request, personnel, leave_type), detail


async def reject_leave_request(
    session: AsyncSession, actor: User, request_id: uuid.UUID, data: LeaveRejectRequest
) -> tuple[LeaveRequestResponse, str]:
    """Talebi reddeder — gerekçe ZORUNLU; **red HER ZAMAN serbesttir**.

    Hak aşımı ve çakışma kapıları BİLİNÇLİ olarak çağrılmaz (İZ 98-99: onaylanamaz
    satırın ✗ butonu aktiftir). Onaylanamayan bir talebin reddedilememesi onu
    sonsuza dek `pending` bırakır ve İZ'in "Bekleyen" sayacını kalıcı kirletirdi.

    Sıra: kayıt (404) → **satır kilidi** → durum (409) → damga. Gerekçe boşluk
    denetimi şemadadır (422).

    Red iş kuralı kapılarından geçmese de karar damgası bir DURUM GEÇİŞİDİR:
    kilitsiz hâlde eşzamanlı bir `approve` ile aynı talep hem onaylanıp hem
    reddedilebilir, ikinci damga birincinin izini EZERDİ. Kilit `approve` ile
    AYNI sırayı (personel → talep) izler.
    """
    request, personnel, leave_type = await get_leave_request_row(session, request_id)
    await _lock_decision_scope(session, request, personnel)
    _assert_decidable(request)

    reason = data.reason.strip()
    _stamp_decision(request, actor, LeaveStatus.rejected, reason)
    await session.flush()
    await session.refresh(request)

    detail = messages.leave_request_rejected(
        personnel.full_name, leave_type.name, request.start_date, request.end_date, reason
    )
    return _leave_response(request, personnel, leave_type), detail


# --- İK-2.2: talebi GERİ ÇEKME (kullanıcı kararı 2026-08-22) ---------------
#
# 🔴 Bu bir KARAR DEĞİL, sahibin VAZGEÇMESİdir — ama `approve`/`reject` ile AYNI
# satırın AYNI durum alanını yazar, bu yüzden onlarla AYNI kilit disiplinine
# tabidir ve bu dosyada yaşar (`_lock_decision_scope` tek yazımı burada).


async def withdraw_leave_request(
    session: AsyncSession, actor: User, request_id: uuid.UUID
) -> tuple[LeaveRequestResponse, str]:
    """Talebi SAHİBİ geri çeker — kapı YALNIZ SAHİPLİKTİR.

    Sıra: kayıt (404) → **SAHİPLİK (404)** → satır kilidi → durum (409) → damga.

    ## 🔴 Neden 403 değil 404

    Bu ucun modül kapısı YOKTUR (`/leave-requests/self` emsali): `personnel=none`
    seviyeli her kullanıcı buraya ULAŞIR. 403 dönseydi cevap *"bu id'de bir talep
    VAR"* derdi ve yabancı bir aktör başkasının taleplerinin varlığını sayabilirdi.
    Görünmeyen kayıt var olmayandan AYIRT EDİLEMEZ (repo kanonu) — metin de
    AYNIDIR (`LEAVE_REQUEST_MISSING`).

    ## 🔴 Neden `admin` istisnası YOK

    `delete_leave_request` iki yoldan açılır (`admin` YA DA sahibi); burada
    YALNIZ sahiplik vardır. Geri çekme bir VAZGEÇMEdir, yetki yükseltmesi değil:
    admin'in başkası adına "vazgeçmesi" anlamsızdır ve görevler ayrılığını
    bulandırır — admin zaten `reject` edebilir (gerekçesi denetime düşer) ya da
    `DELETE` edebilir (SIL-B1'den beri DELETE yalnız Sistem Yöneticisi'nindir). Bu yüzden
    sahiplik kuralı ayrı yazılır; `is_own_personnel_record` (core'un TEK yazımı) doğrudan okunur.

    ## 🔴 Neden sahiplik kapısı KİLİTTEN ÖNCE

    EŞİK = KİLİT kanonu **durum/eşik** denetimleri içindir (onlar TOCTOU'ya
    açıktır ve kilit ALTINDA okunmalıdır). Sahiplik bir eşik DEĞİLDİR: eşzamanlı
    izin yollarının hiçbiri `Personnel.user_id`yi yazmaz, dolayısıyla kilit
    altında yeniden okumak hiçbir şey kazandırmaz. Buna karşılık kapıyı kilitten
    SONRA koymak İKİ zarar üretirdi:
      * yabancı bir aktör, göremediği bir personelin satırında `FOR UPDATE`
        alabilirdi — İK'nın onay akışını bloke edebilecek bir yüzey;
      * 404 cevabı kilit beklemesi kadar GECİKİRDİ ve kaydın varlığı
        ZAMANLAMAYLA sızardı (tam da 403'ten kaçınma gerekçesinin delinmesi).

    ## Belirsizlik BU YÖNDE yok

    `resolve_self_personnel` (kullanıcı → personel) çoklu kayıtta 409 verir;
    burada yön TERStir (talep → personel → `user_id`) ve bir kaydın TEK
    `user_id`si vardır — tahmin yürütülmez, 409 hâli doğmaz.
    """
    request, personnel, leave_type = await get_leave_request_row(session, request_id)
    if not is_own_personnel_record(personnel, actor):
        raise NotFoundError(guards.LEAVE_REQUEST_MISSING)

    await _lock_decision_scope(session, request, personnel)
    _assert_withdrawable(request)

    # Damga TEK yazımdan geçer: `withdrawn` de terminal bir durumdur ve bugün
    # `status != pending` olan HER satırın `decided_at`i DOLUdur. NULL bırakmak
    # o değişmezi kırar ve ekranda tarihsiz bir terminal satır üretirdi.
    # `decided_by` burada GERİ ÇEKENdir (= sahibi); ayrım denetim metnindedir.
    _stamp_decision(request, actor, LeaveStatus.withdrawn, None)
    await session.flush()
    await session.refresh(request)

    detail = messages.leave_request_withdrawn(
        personnel.full_name, leave_type.name, request.start_date, request.end_date
    )
    return _leave_response(request, personnel, leave_type), detail
