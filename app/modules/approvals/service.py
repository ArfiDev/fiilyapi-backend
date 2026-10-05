"""Onay zinciri MOTORU — kurulum, onay, ret ve ayar (sozlesme Y1-Y3, Y5).

## BEKCI SIRASI (Y2) — baglayicidir

1. evrak satiri `FOR UPDATE`  — **CAGIRANIN isi** (T3). Kilit sirasi tum uclarda
   SABITTIR: sozlesme -> evrak -> zincir (deadlock).
2. zincir satiri `FOR UPDATE` — `repository.get_chain_for_update`.
3. zincir acik mi / adim SIRADAKI adim mi  -> **409**
4. aktor adimin SAHIBI mi (projedeki rolu)  -> **403** (YALNIZ RET: Sistem Yoneticisi sahip
   olmasa da gecer, vekaleten isaretiyle)
5. 🔴 KENDI EVRAKI                          -> **403**, TEK ISTISNA: aktorun
   EVRAGIN izin modulunde `AccessLevel.admin` seviyesi varsa GECER ve denetim
   metni "vekaleten" isareti tasir.
6. 🔴 GOREVLER AYRILIGI                     -> **403**. Burada admin ISTISNASI
   YOKTUR: K1 istisnayi yalniz "kendi evraki"na verdi. Bekci 5 ile 6 ayni anda
   gecerliyse **5 ONCE** atesler.

## Denetim satirini KIM yazar

Motor satiri YAZMAZ; hazir METNI dondurur ve `record_audit` cagrisini ROUTER
yapar (`units/service.py` · B5 deseni). Gerekce: (a) metin, zincir silinmeden
(rette) ve adim damgalanirken ancak MOTORDA kurulabilir; (b) uc ayri evrak
router'i ayni metni uc kez kurmak zorunda kalmamalidir; (c) `ip_address` istekle
gelir ve motora tasinmasi katman yonunu tersine cevirirdi.
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel
from app.core.errors import ApprovalNotAllowedError, ApprovalValidationError, ConflictError
from app.core.page_gate import gate_flags, gate_ok, is_admin_role
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.approvals import definitions, documents, guards, inbox, repository
from app.modules.approvals.definitions import HistoryDecision, HistoryFilter
from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
)
from app.modules.audit import messages
from app.modules.company import repository as company_repository
from app.modules.projects.service import visible_projects
from app.modules.roles.models import Role
from app.modules.users.models import ProjectMember, User, UserStatus

__all__ = [
    "ChainDecision",
    "ChainRewind",
    "HistoryChainView",
    "PendingChainView",
    "PendingStepView",
    "approve_next_step",
    "audit_detail",
    "chain_step_substitutes_permission",
    "clean_reject_reason",
    "create_chain",
    "get_threshold",
    "history_for_user",
    "open_chain",
    "pending_for_user",
    "reject_chain",
    "rejection_audit_detail",
    "rewind_audit_detail",
    "rewind_last_step",
    "set_threshold",
]


@dataclass(frozen=True)
class ChainDecision:
    """Bir adim kararinin SONUCU — hem onay hem ret bunu doner.

    🔴 KANON E: cevap OLGUYU tasir, KARARI degil. `audit_detail` bir olgudur
    (ne oldu), `is_complete` de oyle (zincir bitti mi) — cagiran evrak ailesi
    kendi durum makinesini bu olgulardan KENDISI turetir.
    """

    chain_id: uuid.UUID
    step_no: int
    approval_role: ApprovalRole
    is_complete: bool
    on_behalf: bool
    audit_detail: str


@dataclass(frozen=True)
class PendingStepView:
    step_no: int
    approval_role: ApprovalRole
    decided_at: datetime | None
    decided_by_name: str | None


@dataclass(frozen=True)
class PendingChainView:
    """Onay kutusu satiri: ZINCIRIN olgulari + EVRAGIN olgulari (T4).

    🔴 `amount_snapshot` ile `gross_amount` AYNI SEY DEGILDIR ve karistirilirsa
    ikisi de yalan soyler: birincisi zincir kurulurken DONMUS esik carpanidir
    (MK-2 kanonu, "neden bu adimlar?" sorusunu yanitlar), ikincisi evragin
    BUGUNKU brut tutaridir (mockup `:138` `:173` `:227`).
    """

    chain_id: uuid.UUID
    document_type: ApprovalDocumentType
    document_id: uuid.UUID
    created_by_name: str | None
    created_at: datetime
    threshold_snapshot: Decimal
    amount_snapshot: Decimal | None
    current_step_no: int
    steps: list[PendingStepView]
    title: str | None
    subtitle: str | None
    gross_amount: Decimal | None
    net_amount: Decimal | None
    can_decide: bool


@dataclass(frozen=True)
class HistoryChainView(PendingChainView):
    """Onay GECMISI satiri (OKT-B1): bekleyen satirin TUM alanlari + son durum.

    `current_step_no`: reddedilmis zincirde REDDEDILEN adim, suren zincirde
    SIRADAKI adim, onaylanmis zincirde SON adim. `decided_by_name`/`decided_at`:
    zincirin SON kararini veren (retse reddeden + ret zamani, onaysa son imzayi
    atan + zamani; kullanici silinmisse ad `None`); zincir SURUYORSA ikisi de
    `None`. `reason` yalniz retse dolu.
    """

    decision: HistoryDecision
    decided_by_name: str | None
    decided_at: datetime | None
    reason: str | None


# --------------------------------------------------------------------------- #
# Esik ayari (K3)
# --------------------------------------------------------------------------- #


async def get_threshold(session: AsyncSession) -> Decimal:
    """Esigin TEK okuma yolu — `procurement` de buradan okur (R6)."""
    company = await company_repository.get_or_create_singleton(session)
    return company.approval_threshold_try


async def set_threshold(session: AsyncSession, value: Decimal) -> Decimal:
    company = await company_repository.get_or_create_singleton(session)
    company.approval_threshold_try = value
    await session.flush()
    return company.approval_threshold_try


# --------------------------------------------------------------------------- #
# Zincir kurulumu (Y1)
# --------------------------------------------------------------------------- #


async def _assert_step_roles_have_owners(
    session: AsyncSession,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
    roles: tuple[ApprovalRole, ...],
    opener_id: uuid.UUID,
) -> None:
    """Zincirin HER adim rolunun belgenin projesinde en az bir (AKTIF) sahibi var mi?

    Sahip = o projenin ekibinde o rolle yer alan aktif kullanici (`all_projects` kisinin ekip
    satiri yok sayilir, `page_gate.team_roles` ile ayni), ya da "Tum projeler" isaretli ve ANA
    rolu o rol olan aktif kullanici. Eksik varsa 409 (`guards.step_roles_unassigned`): zincir
    kurulmaz, cagiran evrak ailesi durum degistirmeden doner. Pasif / izinli kullanici giris
    yapamaz (`core.deps`) — onunla "sahipli" sayilan bir zincir TAKILIRDI.

    🔴 EVRAGI ACAN KISI SAHIP SAYILMAZ: kendi evragini onaylayamaz (`_assert_can_decide`
    bekci 5) ve tek sahip oysa zincir kimsenin kutusuna dusmez, TAKILIRDI. Tek istisna
    `_has_document_admin` kuralidir (kendi evragini onaylamasina izin verilen kisi) — karar
    `_assert_can_decide` ile AYNI fonksiyondan gecer.
    """
    id_kolonu, proje_kolonu = documents.DOCUMENT_PROJECT_COLUMNS[document_type]
    proje_id = await session.scalar(select(proje_kolonu).where(id_kolonu == document_id))
    anahtarlar = list(dict.fromkeys(rol.value for rol in roles))
    acan = await session.get(User, opener_id)
    acan_sayilir = acan is not None and await _has_document_admin(session, acan, document_type)
    acan_haric = [] if acan_sayilir else [User.id != opener_id]
    ekipte = (
        select(literal(1))
        .select_from(ProjectMember)
        .join(User, User.id == ProjectMember.user_id)
        .where(
            ProjectMember.role_id == Role.id,
            ProjectMember.project_id == proje_id,
            User.status == UserStatus.active,
            User.all_projects.is_(False),
            *acan_haric,
        )
        .exists()
    )
    ana_rol = (
        select(literal(1))
        .select_from(User)
        .where(
            User.role_id == Role.id,
            User.all_projects.is_(True),
            User.status == UserStatus.active,
            *acan_haric,
        )
        .exists()
    )
    sahipli = set(
        (
            await session.execute(
                select(Role.key).where(Role.key.in_(anahtarlar), or_(ekipte, ana_rol))
            )
        ).scalars()
    )
    eksik = [anahtar for anahtar in anahtarlar if anahtar not in sahipli]
    if not eksik:
        return
    adlar = dict(
        (await session.execute(select(Role.key, Role.name).where(Role.key.in_(eksik)))).all()
    )
    raise ConflictError(guards.step_roles_unassigned([adlar.get(k, k) for k in eksik]))


async def create_chain(
    session: AsyncSession,
    *,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
    amount: Decimal | None,
    created_by_user_id: uuid.UUID,
) -> ApprovalChain:
    """Evrak onaya gonderilirken zinciri kurar ve IKI CARPANI da DONDURUR.

    `amount` **BRUT** tutardir (R5) ve `None` "belirlenemedi" demektir; ikisinin
    de anlamini `definitions.step_roles` tasir. Cagiran (T3) tutari kendi evrak
    ailesinden hesaplar — motor evrak modullerini ITHAL ETMEZ, yoksa uc ayri
    aile bu modulde birbirine dugumlenirdi.
    """
    if await repository.get_chain(session, document_type, document_id) is not None:
        raise ConflictError(guards.CHAIN_ALREADY_EXISTS)

    threshold = await get_threshold(session)
    adim_rolleri = definitions.step_roles(document_type, amount, threshold)
    # 🔴 IZN-B3b (KARAR, kullanici 2026-10-04): adim rollerinden biri belgenin projesinde
    # (ya da "Tum projeler" + ana rolde) kimsede yoksa zincir AÇILMAZ, evrak durumu degismez.
    await _assert_step_roles_have_owners(
        session, document_type, document_id, adim_rolleri, created_by_user_id
    )
    chain = ApprovalChain(
        document_type=document_type,
        document_id=document_id,
        threshold_snapshot=threshold,
        amount_snapshot=amount,
        created_by_user_id=created_by_user_id,
        created_at=datetime.now(UTC),
    )
    session.add(chain)
    await session.flush()
    for sira, rol in enumerate(adim_rolleri, start=1):
        session.add(ApprovalStep(chain_id=chain.id, step_no=sira, approval_role=rol))
    await session.flush()
    return chain


# --------------------------------------------------------------------------- #
# Bekciler (Y2)
# --------------------------------------------------------------------------- #


async def _load_locked_chain(
    session: AsyncSession,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
    *,
    required: bool,
) -> ApprovalChain | None:
    """Zincir satirini `FOR UPDATE` ile yukler (bekci 2).

    🔴 `required=False` T3'un ESKI KAYIT yoludur ve YALNIZ evrak uclari kullanir.
    Gerekce OLCULMUS bir zorunluluktur: bu dilim canlida uctan UCA acilirken
    `pending_approval` durumunda BEKLEYEN evraklar vardir ve onlarin zinciri
    YOKTUR. Zincir kosulsuz zorunlu kilinsaydi o evraklar ne onaylanabilir ne
    reddedilebilirdi (ikisi de zincirden gecer) — yani her ucus hâlindeki evrak
    KILITLENIRDI. Geri doldurma ikinci bir migration ister ve bu dilimde
    ACILMADI.

    Yolun DARLIGI yapisaldir, sozle degil: `pending_approval`a goturen TEK uc
    `submit`tir ve o HER ZAMAN zincir acar, dolayisiyla zincirsiz bir evrak API
    ile URETILEMEZ (`test_SUBMIT_HER_ZAMAN_zincir_acar_eski_yol_URETILEMEZ`).
    """
    chain = await repository.get_chain_for_update(session, document_type, document_id)
    if chain is None and required:
        raise ConflictError(guards.NO_OPEN_CHAIN)
    return chain


async def open_chain(
    session: AsyncSession, document_type: ApprovalDocumentType, document_id: uuid.UUID
) -> ApprovalChain | None:
    """Evragin ACIK zinciri (kilitsiz OKUMA) — cagiran ekranlar icin."""
    return await repository.get_chain(session, document_type, document_id)


async def chain_step_substitutes_permission(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
) -> bool:
    """OK-1C — ikame kapisi acilir mi (katman yonu: gate -> service -> repository).

    Kural IKI dallidir ve ikincisi bir SIZINTI KAPATIR:

    * **aktor SIRADAKI adimin SAHIBI** (IZN-B3b: evragin projesinde o role atanmis
      ekip uyesi ya da "Tum projeler" + ana rol) -> kapi acilir. Karar degil,
      yalnizca kapi: otorite hâlâ kilit altindaki `_assert_can_decide`tir.
    * **aktor ADAY IMZACI (bir projede ya da ana rolunde adim rolu var) ve evrak HIC
      YOK** -> kapi yine acilir ve istek evrak ucunda 404 olur. Gercek davranis:
      aday imzaciya BASKA bir projede VAR olan evrak 403, HIC var olmayan evrak 404
      doner; yani iki cevap AYNILASMAZ ve aday imzaci bir kimligin var olup
      olmadigini ogrenebilir (varlik bilgisi sizintisi IZN-B3b ile GENISLEDI, eski
      "iki cevap aynilasir" iddiasi artik gecerli DEGIL). Kapinin acilmasi HICBIR
      yetki vermez; otorite `_assert_can_decide`tir.

    🔴 Ikinci dal ADAY IMZACIYLA SINIRLIDIR ve bu bilinclidir: kosulsuz
    acilsaydi hicbir adim rolu OLMAYAN her kullanici da var olan (403) ile var
    olmayan (404) kimligi ayirt edebilir, yani bugun HIC OLMAYAN bir varlik
    kesif yuzeyi butun kullanicilara acilirdi.

    Olgularin kendisi ve SQL gerekcesi `repository.chain_gate_facts`tedir.
    """
    olgular = await repository.chain_gate_facts(
        session, actor_id=actor_id, document_type=document_type, document_id=document_id
    )
    if olgular.holds_next_step_role:
        return True
    return olgular.actor_is_candidate and not olgular.document_exists


def _current_step(steps: list[ApprovalStep], step_no: int | None) -> ApprovalStep:
    """Bekci 3 — zincir acik mi, istenen adim SIRADAKI adim mi.

    `step_no` ISTEGE BAGLIDIR ama verildiginde bir IYIMSER KILITTIR: ekranin
    gordugu adim artik siradaki degilse (baskasi ilerletmis) istek 409 alir ve
    kullanici yanlis adimi onaylamis olmaz.
    """
    bekleyen = [adim for adim in steps if adim.decided_at is None]
    if not bekleyen:
        raise ConflictError(guards.CHAIN_COMPLETED)
    siradaki = bekleyen[0]
    if step_no is not None and step_no != siradaki.step_no:
        raise ConflictError(guards.STEP_NOT_CURRENT)
    return siradaki


async def _has_document_admin(
    session: AsyncSession, actor: User, document_type: ApprovalDocumentType
) -> bool:
    """`admin` seviyesi EVRAGIN izin modulunde aranir (`definitions`).

    `full` YETMEZ (`access.satisfies`): `patron` sistem rolü `full`dur ve
    istisna ona acilsaydi "tek kisilik ekipte kilitlenmeyi onle" gerekcesi,
    kendi evragini onaylayan ikinci bir sinifa donusurdu.
    """
    return await gate_ok(
        session,
        actor,
        definitions.DOCUMENT_PERMISSION_MODULE[document_type],
        AccessLevel.admin,
        record=False,
    )


async def _assert_can_decide(
    session: AsyncSession,
    actor: User,
    chain: ApprovalChain,
    steps: list[ApprovalStep],
    current: ApprovalStep,
    *,
    admin_escape: bool = False,
) -> bool:
    """Bekci 4-5-6. Doner: karar "vekaleten" mi verildi.

    Ret de bir KARARDIR ve AYNI huniden gecer: ayri birakilsaydi evragin sahibi
    kendi evragini REDDEDEREK zinciri silebilir ve onay izini yok edebilirdi.

    🔴 `admin_escape=True` YALNIZ RET yoludur (GECE KARARI, IZN-B3b): adim sahipsiz kalmissa
    (tek sahip ekipten cikti / pasiflesti / izne cikti) kimse karar veremez ve zincir takilir.
    Sistem Yoneticisi sahip OLMASA da zinciri REDDEDEBILIR (evrak taslaga doner, rol yeniden
    atanip yeniden gonderilir); ONAYLAYAMAZ — gorevler ayriligi ve sahiplik onayda korunur.
    Denetim metni "vekaleten" isareti tasir.
    """
    # IZN-B3b: adimin sahibi mi — kapi ve gelen kutusuyla AYNI `step_owner_clause`.
    if not await repository.actor_owns_step(session, actor.id, current.id):
        if admin_escape and await is_admin_role(session, actor):
            return True
        raise ApprovalNotAllowedError(guards.APPROVAL_ROLE_MISSING)

    on_behalf = False
    if chain.created_by_user_id == actor.id:
        if not await _has_document_admin(session, actor, chain.document_type):
            raise ApprovalNotAllowedError(guards.OWN_DOCUMENT)
        on_behalf = True

    if any(adim.decided_by_user_id == actor.id for adim in steps):
        raise ApprovalNotAllowedError(guards.SEPARATION_OF_DUTIES)
    return on_behalf


# --------------------------------------------------------------------------- #
# Onay / ret
# --------------------------------------------------------------------------- #


async def approve_next_step(
    session: AsyncSession,
    *,
    actor: User,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
    step_no: int | None = None,
    require_chain: bool = True,
) -> ChainDecision | None:
    """Zincirin SIRADAKI adimini karara baglar.

    `require_chain=False` ise ZINCIRSIZ evrakta `None` doner (eski kayit yolu,
    `_load_locked_chain` docstring'i) ve cagiran evrak ailesi BUGUNKU tek adimli
    davranisini surdurur.
    """
    chain = await _load_locked_chain(session, document_type, document_id, required=require_chain)
    if chain is None:
        return None
    steps = await repository.chain_steps(session, chain.id)
    current = _current_step(steps, step_no)
    on_behalf = await _assert_can_decide(session, actor, chain, steps, current)

    current.decided_by_user_id = actor.id
    current.decided_at = datetime.now(UTC)
    await session.flush()

    return ChainDecision(
        chain_id=chain.id,
        step_no=current.step_no,
        approval_role=current.approval_role,
        is_complete=all(adim.decided_at is not None for adim in steps),
        on_behalf=on_behalf,
        audit_detail=messages.approval_step_approved(
            document_type.value,
            current.step_no,
            len(steps),
            current.approval_role.value,
            on_behalf=on_behalf,
        ),
    )


def clean_reject_reason(reason: str | None) -> str:
    """Gerekce ZORUNLU metindir (K2); tavan PAYLASILAN sabittendir.

    Module ayri bir sayi yazilsaydi alanin bir giris noktasi kapiyi atlatirdi
    (BC dersi) — tavan `core/text.py::FREE_TEXT_MAX_LENGTH`tir.
    """
    temiz = (reason or "").strip()
    if not temiz:
        raise ApprovalValidationError(guards.REJECT_REASON_REQUIRED)
    if len(temiz) > FREE_TEXT_MAX_LENGTH:
        raise ApprovalValidationError(guards.REJECT_REASON_TOO_LONG)
    return temiz


async def reject_chain(
    session: AsyncSession,
    *,
    actor: User,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
    reason: str | None,
    require_chain: bool = True,
) -> ChainDecision | None:
    """RET TERMINALDIR ama ZINCIR SILINMEZ (OKT-B1, KARARLAR 62ae58a).

    Zincir `rejected_at` / `rejected_by_user_id` / `rejection_reason` ile
    DAMGALANIR ve adimlari oldugu gibi DURUR (reddedilen adim karara
    baglanmamis kalir). Damgali zincir "acik" sayilmaz — evragin yeniden onaya
    gonderilmesi YENI bir zincir acar (`uq_approval_chains_open_document` kismi
    indeksi) ve eski kayit yalniz `GET /approvals/history`de gorunur. Eski K2'nin
    ("tum onaylar silinir") yerine gecer; zaten silinmis retler geri gelmez.

    Gerekce dogrulamasi bekcilerden SONRA kosar: yetkisi olmayan birine once
    "gerekce yaz" demek, asil engeli (bu adim ona kapali) gizlerdi. ⚠️ Evrak
    aileleri gerekceyi KENDI korkuluklarinda (semada ya da `guards`ta) ZATEN
    dogrular ve o dogrulama daha ONCE kosar — bu, zincirsiz ESKI kayitlarda da
    gerekcenin zorunlu kalmasi icin gereklidir (K2 kolonu degil ZORUNLULUGU
    baglar).

    `require_chain=False` ise zincirsiz evrakta `None` doner (eski kayit yolu).
    """
    chain = await _load_locked_chain(session, document_type, document_id, required=require_chain)
    if chain is None:
        return None
    steps = await repository.chain_steps(session, chain.id)
    current = _current_step(steps, None)
    on_behalf = await _assert_can_decide(session, actor, chain, steps, current, admin_escape=True)
    temiz = clean_reject_reason(reason)

    detail = messages.approval_chain_rejected(
        document_type.value,
        current.step_no,
        len(steps),
        current.approval_role.value,
        temiz,
        on_behalf=on_behalf,
    )
    sonuc = ChainDecision(
        chain_id=chain.id,
        step_no=current.step_no,
        approval_role=current.approval_role,
        is_complete=False,
        on_behalf=on_behalf,
        audit_detail=detail,
    )
    chain.rejected_at = datetime.now(UTC)
    chain.rejected_by_user_id = actor.id
    chain.rejection_reason = temiz
    await session.flush()
    return sonuc


# --------------------------------------------------------------------------- #
# Geri sarma (Y4) — `/unapprove`in zincir karsiligi
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ChainRewind:
    """Geri sarilan adimin KIMLIGI — denetim metni bunu tasir (Y4)."""

    chain_id: uuid.UUID
    step_no: int
    approval_role: ApprovalRole


async def rewind_last_step(
    session: AsyncSession,
    *,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
) -> ChainRewind | None:
    """`/unapprove` — SON karara baglanmis adimi geri sarar. ZINCIR SILINMEZ.

    🔴 RETTEN FARKI BUDUR ve dilimin en kolay karistirlan iki islemi bunlardir:
    ret zinciri DAMGALAR ve bitirir (OKT-B1; eskiden siliyordu), geri alma
    yalnizca SON imzayi kaldirir. Ikisi de evragi geriye tasidigi icin sadece DURUMA bakan bir test
    farki GOREMEZ — bu yuzden zincirin varligi ayrica iddia edilir.

    🔴 Zincire HIC DOKUNMAYAN bir geri alma evragi KILITLERDI: tamamlanmis
    zincirli bir evrak `pending_approval`a doner, sonraki `/approve` ise
    `CHAIN_COMPLETED` 409'u alirdi. Yani bu fonksiyon bir sussleme degil,
    Y4'un zorunlu parcasidir.

    Zincirsiz (eski) kayitta `None` doner; karara baglanmis adim yoksa da `None`
    doner — geri sarilacak imza YOKTUR ve evragin durum gecisi yine de kendi
    tablosundan kosar (bu fonksiyon durum makinesini YONETMEZ).
    """
    chain = await _load_locked_chain(session, document_type, document_id, required=False)
    if chain is None:
        return None
    steps = await repository.chain_steps(session, chain.id)
    kararlilar = [adim for adim in steps if adim.decided_at is not None]
    if not kararlilar:
        return None
    son = kararlilar[-1]
    sonuc = ChainRewind(chain_id=chain.id, step_no=son.step_no, approval_role=son.approval_role)
    son.decided_by_user_id = None
    son.decided_at = None
    await session.flush()
    return sonuc


# --------------------------------------------------------------------------- #
# Denetim metninin BIRLESTIRILMESI (T3)
# --------------------------------------------------------------------------- #


def audit_detail(
    document_detail: str, decision: ChainDecision | None, *, document_label: str
) -> str:
    """Evragin kendi denetim metni + zincir adiminin metni — TEK satirda.

    Uc kural, uc evrak ailesinde TEK kopya:
      * zincir YOK (eski kayit) -> evragin BUGUNKU metni aynen;
      * ARA adim -> adim metni + evragin KIMLIGI (`document_label`). Evragin
        durumu DEGISMEDI, dolayisiyla "Hakedis onaylandi" yazmak gunluge OLMAMIS
        bir olguyu yazmak olurdu; ama kimliksiz bir "adim 1/3 onaylandi" satiri
        da HANGI evrak sorusunu yanitsiz birakirdi (bu, `test_her_gecis_denetim_
        satiri_yazar` uyarlanirken OLCULDU: satir talep numarasini KAYBETMISTI);
      * SON adim (ya da ret) -> IKISI DE. Evrak durum degistirdi VE son imza
        atildi; birini atlamak "hangi imza" ya da "ne oldu" sorusundan birini
        yanitsiz birakirdi.
    """
    if decision is None:
        return document_detail
    if not decision.is_complete:
        return f"{decision.audit_detail} · {document_label}"
    return f"{document_detail} · {decision.audit_detail}"


def rejection_audit_detail(document_detail: str, decision: ChainDecision | None) -> str:
    """Ret metni: evragin kendi cumlesi + hangi ADIMDA reddedildigi.

    `audit_detail`ten AYRIDIR cunku ret `is_complete=False` doner (zincir
    tamamlanmadi, REDDEDILDI) — ortak fonksiyona sokulsaydi evragin kendi metni
    (ve gerekcesi) gunlukten DUSERDI.
    """
    if decision is None:
        return document_detail
    return f"{document_detail} · {decision.audit_detail}"


def rewind_audit_detail(document_detail: str, rewind: ChainRewind | None) -> str:
    """Geri alma metni: ESKI onaylayani tasiyan mevcut iz KORUNUR, ustune geri
    sarilan ADIMIN ROLU eklenir (Y4)."""
    if rewind is None:
        return document_detail
    ek = messages.approval_step_rewound(rewind.step_no, rewind.approval_role.value)
    return f"{document_detail} · {ek}"


# --------------------------------------------------------------------------- #
# Onay kutusu (Y7 — satir zenginlestirmesi T4'te)
# --------------------------------------------------------------------------- #


async def _admin_document_types(session: AsyncSession, actor: User) -> list[ApprovalDocumentType]:
    """Bekci 5'in istisnasinin SQL karsiligi. Sorgu sayisi IZIN MODULU sayisi
    kadardir (bugun iki) — SATIR SAYISINDAN bagimsizdir."""
    seviyeler: dict[str, bool] = {}
    tipler: list[ApprovalDocumentType] = []
    for tip, modul in definitions.DOCUMENT_PERMISSION_MODULE.items():
        if modul not in seviyeler:
            seviyeler[modul] = await gate_ok(session, actor, modul, AccessLevel.admin, record=False)
        if seviyeler[modul]:
            tipler.append(tip)
    return tipler


async def _visible_project_ids(session: AsyncSession, actor: User) -> list[uuid.UUID]:
    """Aktorun GORDUGU projeler — bekleyen kutusu ile GECMIS AYNI yardimciyi
    kullanir (IDOR suzgecinin tek kaynagi; SQL tarafi `documents.visible_
    document_clause`tir)."""
    # IZN-B3: gelen kutusu KAPISIZ bir uçtur; proje, evrak izin modüllerinden HERHANGİ BİRİNİN
    # Görür sayfalarını O PROJEDEKİ rolün açtığı projeler olarak görünür (çiftler AÇIKÇA verilir).
    gorunen: dict[uuid.UUID, uuid.UUID] = {}
    for modul in sorted(set(definitions.DOCUMENT_PERMISSION_MODULE.values())):
        pairs = gate_flags(modul, AccessLevel.view)
        for proje in await visible_projects(session, actor, pairs=pairs):
            gorunen[proje.id] = proje.id
    return list(gorunen)


@dataclass(frozen=True)
class _ChainContext:
    """Bir sayfadaki zincirlerin ortak zenginlestirmesi (adimlar · evrak olgulari · adlar)."""

    steps_by_chain: dict[uuid.UUID, list[ApprovalStep]]
    step_map: dict[uuid.UUID, list[PendingStepView]]
    facts: dict[tuple[ApprovalDocumentType, uuid.UUID], inbox.DocumentFacts]
    names: dict[uuid.UUID, str]


async def _chain_context(
    session: AsyncSession,
    chains: list[ApprovalChain],
    *,
    extra_user_ids: set[uuid.UUID] | None = None,
) -> _ChainContext:
    """Sayfadaki zincirler icin SABIT sayida sorgu (adimlar · aile basina evrak
    olgulari · adlar) — satir sayisindan BAGIMSIZ, N+1 yok."""
    steps = await repository.steps_of_chains(session, [chain.id for chain in chains])
    olgular = await inbox.load_facts(
        session, [(chain.document_type, chain.document_id) for chain in chains]
    )

    kimlikler: set[uuid.UUID] = set(extra_user_ids or ())
    for chain in chains:
        if chain.created_by_user_id is not None:
            kimlikler.add(chain.created_by_user_id)
    for adim in steps:
        if adim.decided_by_user_id is not None:
            kimlikler.add(adim.decided_by_user_id)
    adlar = await repository.user_names(session, kimlikler)

    adim_haritasi: dict[uuid.UUID, list[PendingStepView]] = {}
    for adim in steps:
        adim_haritasi.setdefault(adim.chain_id, []).append(
            PendingStepView(
                step_no=adim.step_no,
                approval_role=adim.approval_role,
                decided_at=adim.decided_at,
                decided_by_name=adlar.get(adim.decided_by_user_id)
                if adim.decided_by_user_id
                else None,
            )
        )
    zincir_adimlari: dict[uuid.UUID, list[ApprovalStep]] = {}
    for adim in steps:  # `steps_of_chains` zaten (chain_id, step_no) sirali doner
        zincir_adimlari.setdefault(adim.chain_id, []).append(adim)
    return _ChainContext(
        steps_by_chain=zincir_adimlari, step_map=adim_haritasi, facts=olgular, names=adlar
    )


async def pending_for_user(
    session: AsyncSession, actor: User, *, limit: int, offset: int
) -> tuple[list[PendingChainView], int]:
    """Kullaniciya DUSEN siradaki adimlar.

    🔴 KANON E: yanit adimin rolunu, sirasini ve durumunu verir; satir basina `can_decide`
    OLGUSU da doner (IZN-B3b: eski `my_approval_roles` kalkti — rol artik PROJEYE baglidir ve tek
    bir liste yanlis olurdu). Kutunun suzgeci "siradaki adim SANA dustu" kuralinin kendisidir
    (`repository._pending_filter`), dolayisiyla buradaki her satirda `can_decide` DOGRUDUR; ayri
    bir hesap kosmaz.

    🔴 N+1 YOK: sorgu sayisi SATIR SAYISINDAN bagimsizdir (sayim · sayfa ·
    adimlar · adlar + sabit sayida izin/kapsam sorgusu + AILE BASINA sabit
    sayida evrak sorgusu). Sayfada bulunmayan evrak ailesi HIC sorgulanmaz.

    🔴 IDOR (T4): kapsam `projects.service.visible_projects` uzerinden gelir ve
    suzgec SQL'dedir — `total` da ondan turer (`repository._pending_filter`).
    Kapsam BELLEKTE suzulseydi `total` gorunmeyeni de sayardi.

    🔴 OKT-B1: REDDEDILMIS zincir bekleyen SAYILMAZ (`_pending_filter`).

    ⚠️ Kapsam sorgusu ADAY IMZACI sorgusundan SONRA kosar: hicbir projede adim rolu olmayan aktor
    icin (cogunluk) ikinci bir sorgu hic acilmaz.
    """
    if not await repository.actor_is_candidate(session, actor.id):
        return [], 0

    admin_tipleri = await _admin_document_types(session, actor)
    rows, total = await repository.pending_page(
        session,
        actor_id=actor.id,
        admin_document_types=admin_tipleri,
        visible_project_ids=await _visible_project_ids(session, actor),
        limit=limit,
        offset=offset,
    )
    baglam = await _chain_context(session, [chain for chain, _ in rows])
    return (
        [_pending_view(chain, current, baglam, can_decide=True) for chain, current in rows],
        total,
    )


def _view_fields(
    chain: ApprovalChain, current_step_no: int, baglam: _ChainContext, *, can_decide: bool
) -> dict:
    """`PendingChainView` alanlari — bekleyen kutusu ile gecmis AYNI kaynaktan."""
    olgu = baglam.facts.get((chain.document_type, chain.document_id), inbox.EMPTY_FACTS)
    return {
        "chain_id": chain.id,
        "document_type": chain.document_type,
        "document_id": chain.document_id,
        "created_by_name": baglam.names.get(chain.created_by_user_id)
        if chain.created_by_user_id
        else None,
        "created_at": chain.created_at,
        "threshold_snapshot": chain.threshold_snapshot,
        "amount_snapshot": chain.amount_snapshot,
        "current_step_no": current_step_no,
        "steps": baglam.step_map.get(chain.id, []),
        "title": olgu.title,
        "subtitle": olgu.subtitle,
        "gross_amount": olgu.gross_amount,
        "net_amount": olgu.net_amount,
        "can_decide": can_decide,
    }


def _pending_view(
    chain: ApprovalChain, current: ApprovalStep, baglam: _ChainContext, *, can_decide: bool
) -> PendingChainView:
    return PendingChainView(**_view_fields(chain, current.step_no, baglam, can_decide=can_decide))


# --------------------------------------------------------------------------- #
# Onay gecmisi (OKT-B1)
# --------------------------------------------------------------------------- #


async def history_for_user(
    session: AsyncSession,
    actor: User,
    *,
    decision: HistoryFilter,
    limit: int,
    offset: int,
) -> tuple[list[HistoryChainView], int]:
    """Gorunur zincirler ve SON DURUMLARI — `GET /approvals/history`.

    Karar ZINCIR duzeyindedir (CEO karari): `approved` = zincirin son durumu
    onayli (tum adimlar imzali), `rejected` = zincir reddedildi, `all` = suzgec
    yok (suren zincirler kartta `pending`).

    Gorunurluk: adimlarindan HERHANGI birinin SAHIBI aktor (IZN-B3b: evragin projesindeki proje
    rolu / "Tum projeler" + ana rol) + aktorun gordugu projeler (bekleyen kutusuyla AYNI IDOR
    yardimcisi). Hicbir projede adim rolu olmayan aktor icin sorgu acilmaz. N+1 yok: bekleyen
    kutusuyla ayni sabit sorgu kumesi (`_chain_context`).

    Satir basina `can_decide`: aktor o zincirin SIRADAKI adimini SIMDI karara baglayabilir mi
    (`repository.decidable_chain_ids` — kutunun suzgeciyle ayni kosullar). Sayfada suren zincir
    yoksa ek sorgu KOSMAZ.
    """
    if not await repository.actor_is_candidate(session, actor.id):
        return [], 0
    gorunen = await _visible_project_ids(session, actor)
    chains, total = await repository.history_page(
        session,
        actor_id=actor.id,
        visible_project_ids=gorunen,
        decision=decision,
        limit=limit,
        offset=offset,
    )
    reddeden = {c.rejected_by_user_id for c in chains if c.rejected_by_user_id is not None}
    baglam = await _chain_context(session, chains, extra_user_ids=reddeden)
    suren = [chain.id for chain in chains if _is_open(chain, baglam)]
    karar_verebilir = await repository.decidable_chain_ids(
        session,
        actor_id=actor.id,
        admin_document_types=await _admin_document_types(session, actor) if suren else [],
        visible_project_ids=gorunen,
        chain_ids=suren,
    )
    return [
        _history_view(chain, baglam, can_decide=chain.id in karar_verebilir) for chain in chains
    ], total


def _is_open(chain: ApprovalChain, baglam: _ChainContext) -> bool:
    """Zincir SURUYOR mu: reddedilmedi ve karara baglanmamis adim var (yalniz bunlar `can_decide`
    olabilir)."""
    if chain.rejected_at is not None:
        return False
    return any(adim.decided_at is None for adim in baglam.steps_by_chain.get(chain.id, []))


def _history_view(
    chain: ApprovalChain, baglam: _ChainContext, *, can_decide: bool
) -> HistoryChainView:
    adimlar = baglam.steps_by_chain[chain.id]  # join sayesinde EN AZ bir adim vardir
    bekleyen = next((adim for adim in adimlar if adim.decided_at is None), None)
    if chain.rejected_at is not None:
        # Reddedilen adim = karara baglanmamis adimlarin en kucugu (ret terminaldir).
        reddedilen = bekleyen or adimlar[-1]
        return HistoryChainView(
            **_view_fields(chain, reddedilen.step_no, baglam, can_decide=can_decide),
            decision=HistoryDecision.rejected,
            decided_by_name=baglam.names.get(chain.rejected_by_user_id)
            if chain.rejected_by_user_id
            else None,
            decided_at=chain.rejected_at,
            reason=chain.rejection_reason,
        )
    if bekleyen is not None:
        return HistoryChainView(
            **_view_fields(chain, bekleyen.step_no, baglam, can_decide=can_decide),
            decision=HistoryDecision.pending,
            decided_by_name=None,
            decided_at=None,
            reason=None,
        )
    son = adimlar[-1]
    return HistoryChainView(
        **_view_fields(chain, son.step_no, baglam, can_decide=can_decide),
        decision=HistoryDecision.approved,
        decided_by_name=baglam.names.get(son.decided_by_user_id)
        if son.decided_by_user_id
        else None,
        decided_at=max(adim.decided_at for adim in adimlar if adim.decided_at is not None),
        reason=None,
    )
