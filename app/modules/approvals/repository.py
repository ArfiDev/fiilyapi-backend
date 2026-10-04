"""Onay motorunun veri erisimi. Is kurali YOKTUR — o `service.py`dedir."""

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, and_, case, distinct, func, literal, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.modules.approvals import documents
from app.modules.approvals.definitions import HistoryFilter
from app.modules.approvals.models import (
    ApprovalChain,
    ApprovalDocumentType,
    ApprovalRole,
    ApprovalStep,
    UserApprovalRole,
)
from app.modules.users.models import User

__all__ = [
    "ChainGateFacts",
    "assignment_page",
    "chain_gate_facts",
    "chain_steps",
    "get_chain",
    "get_chain_for_update",
    "history_page",
    "pending_page",
    "replace_user_approval_roles",
    "steps_of_chains",
    "user_approval_roles",
    "user_names",
]


async def get_chain(
    session: AsyncSession, document_type: ApprovalDocumentType, document_id: uuid.UUID
) -> ApprovalChain | None:
    """Evragin ACIK zinciri. 🔴 REDDEDILMIS zincir (OKT-B1) "acik" DEGILDIR:
    `rejected_at IS NULL` suzgeci kismi unique indeksin kosuluyla BIREBIR aynidir
    ve yoksa ayni evragin birden cok kaydi `scalar`i patlatirdi."""
    return await session.scalar(
        select(ApprovalChain).where(
            ApprovalChain.document_type == document_type,
            ApprovalChain.document_id == document_id,
            ApprovalChain.rejected_at.is_(None),
        )
    )


async def get_chain_for_update(
    session: AsyncSession, document_type: ApprovalDocumentType, document_id: uuid.UUID
) -> ApprovalChain | None:
    """🔴 EŞİK = KİLİT. Zincir satiri TUM DENETIMLERDEN ONCE kilitlenir.

    `populate_existing=True` sarttir: satir session'da ZATEN yuklüyse
    `with_for_update` tek basina TAZE degeri geri yazmaz ve kilit alinmis olmasina
    ragmen BAYAT alanlarla karar verilir.

    Kilit SIRASI tum uclarda SABITTIR: sozlesme -> evrak -> zincir. Cagiran (T3)
    evrak satirini KENDI kilitler; motor yalniz zinciri kilitler ve sirayi bozmaz
    (deadlock).
    """
    return await session.scalar(
        select(ApprovalChain)
        .where(
            ApprovalChain.document_type == document_type,
            ApprovalChain.document_id == document_id,
            ApprovalChain.rejected_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )


@dataclass(frozen=True)
class ChainGateFacts:
    """Ikame kapisinin OLGULARI — karari kapinin kendisi kurar (kanon E).

    Uc olgu TEK sorgudan gelir; repository burada bir POLITIKA yazmaz, cunku
    "kapi acilir mi" sorusu uc katmanindadir ve degisirse tek yerde degismelidir.
    """

    document_exists: bool
    actor_is_candidate: bool
    holds_next_step_role: bool


async def chain_gate_facts(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    document_type: ApprovalDocumentType,
    document_id: uuid.UUID,
) -> ChainGateFacts:
    """Ikame kapisinin (OK-1C, `approvals/gate.py`) TEK sorgusu — UC olgu, TEK gidis.

    1. `document_exists` — evrak satiri VAR MI (aile tablosunda).
    2. `actor_is_candidate` — aktor EN AZ BIR onay rolu tasiyor mu (ADAY IMZACI).
    3. `holds_next_step_role` — evragin ACIK zincirinin SIRADAKI adiminin onay
       rolu aktorun kumesinde mi.

    Ucu de ayri sorgu olsaydi soguk yolda uc gidis-donus olurdu; ustelik
    "siradaki adim" karari Python'a tasinirdi — ayni karar `_pending_filter`da
    ZATEN SQL'dedir ve iki kopya sessizce ayrisirdi.

    🔴 **K2 — YALNIZ SIRADAKI ADIM.** `step_no`, o zincirin karara baglanmamis
    adimlarinin EN KUCUGUNE esit olmak zorundadir; gecmis ya da gelecek bir
    adimin rolunu tasimak hicbir kapi acmaz.

    🔴 **FAIL-CLOSED.** Zincir yoksa, acik adim yoksa ya da rol eslesmiyorsa
    satir DONMEZ ve olgu `False` cikar — ikame YOKTUR, bugunku modul kapisi
    gecerlidir. Istisna YUTULMAZ: `try/except -> False` yazilsaydi gercek bir
    veritabani arizasi kullaniciya "yetkiniz yok" diye gorunur ve arizanin
    kendisi denetim yuzeyinden kaybolurdu.

    🔴 **KILITSIZDIR ve bu bilinclidir.** Kapi bir KARAR DEGIL, yalnizca
    genisletici bir OR'dur. Otorite hâlâ kilit altindaki
    `service._assert_can_decide`tir (`get_chain_for_update` zincir satirini
    kilitledikten SONRA kosar). Kapi ile karar arasinda zincir ilerlerse karar
    katmani 403/409 verir; yetki genislemesi YOKTUR.
    """
    id_kolonu, _proje_kolonu = documents.DOCUMENT_PROJECT_COLUMNS[document_type]
    belge_var = select(id_kolonu).where(id_kolonu == document_id).exists()
    aday_imzaci = select(UserApprovalRole.id).where(UserApprovalRole.user_id == actor_id).exists()
    onceki = aliased(ApprovalStep)
    siradaki_step_no = (
        select(func.min(onceki.step_no))
        .where(onceki.chain_id == ApprovalChain.id, onceki.decided_at.is_(None))
        .scalar_subquery()
    )
    siradaki_adim_bende = (
        select(literal(1))
        .select_from(ApprovalChain)
        .join(
            ApprovalStep,
            and_(
                ApprovalStep.chain_id == ApprovalChain.id,
                ApprovalStep.decided_at.is_(None),
                ApprovalStep.step_no == siradaki_step_no,
            ),
        )
        .join(
            UserApprovalRole,
            and_(
                UserApprovalRole.user_id == actor_id,
                UserApprovalRole.approval_role == ApprovalStep.approval_role,
            ),
        )
        .where(
            ApprovalChain.document_type == document_type,
            ApprovalChain.document_id == document_id,
            # OKT-B1: reddedilmis zincirin karara baglanmamis adimi KAPI ACMAZ.
            ApprovalChain.rejected_at.is_(None),
        )
        .exists()
    )
    satir = (
        await session.execute(
            select(
                belge_var.label("document_exists"),
                aday_imzaci.label("actor_is_candidate"),
                siradaki_adim_bende.label("holds_next_step_role"),
            )
        )
    ).one()
    return ChainGateFacts(
        document_exists=satir.document_exists,
        actor_is_candidate=satir.actor_is_candidate,
        holds_next_step_role=satir.holds_next_step_role,
    )


async def chain_steps(session: AsyncSession, chain_id: uuid.UUID) -> list[ApprovalStep]:
    return list(
        (
            await session.execute(
                select(ApprovalStep)
                .where(ApprovalStep.chain_id == chain_id)
                .order_by(ApprovalStep.step_no)
            )
        )
        .scalars()
        .all()
    )


async def steps_of_chains(session: AsyncSession, chain_ids: list[uuid.UUID]) -> list[ApprovalStep]:
    """Sayfadaki TUM zincirlerin adimlari TEK sorguda (N+1 yok)."""
    if not chain_ids:
        return []
    return list(
        (
            await session.execute(
                select(ApprovalStep)
                .where(ApprovalStep.chain_id.in_(chain_ids))
                .order_by(ApprovalStep.chain_id, ApprovalStep.step_no)
            )
        )
        .scalars()
        .all()
    )


async def user_names(session: AsyncSession, user_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Ad cozumlemesi TEK sorguda: satir basina kullanici cekmek N+1'in ta kendisi."""
    if not user_ids:
        return {}
    rows = await session.execute(select(User.id, User.full_name).where(User.id.in_(user_ids)))
    return {satir.id: satir.full_name for satir in rows}


async def user_approval_roles(session: AsyncSession, user_id: uuid.UUID) -> list[ApprovalRole]:
    rows = await session.execute(
        select(UserApprovalRole.approval_role)
        .where(UserApprovalRole.user_id == user_id)
        .order_by(UserApprovalRole.approval_role)
    )
    return list(rows.scalars().all())


async def replace_user_approval_roles(
    session: AsyncSession, user_id: uuid.UUID, roles: list[ApprovalRole]
) -> None:
    """TAM KUME degistirir (kanon): once hepsi silinir, sonra verilenler yazilir.

    Kismi ekleme/cikarma ucu ACILMADI — iki ayri ucun birlestirilmesi gereken
    "son durum" ekranda kurulur ve iki istek arasinda kalan yari hâl gorunurdu.
    """
    mevcut = (
        await session.execute(select(UserApprovalRole).where(UserApprovalRole.user_id == user_id))
    ).scalars()
    for satir in mevcut:
        await session.delete(satir)
    await session.flush()
    for rol in roles:
        session.add(UserApprovalRole(user_id=user_id, approval_role=rol))
    await session.flush()


async def assignment_page(
    session: AsyncSession, *, limit: int, offset: int
) -> tuple[list[User], int, dict[uuid.UUID, list[ApprovalRole]]]:
    """EN AZ BIR onay rolu tasiyan kullanicilar + atamalari (UC sabit sorgu).

    Onay rolu OLMAYAN kullanicilar burada DONMEZ: bu uc "atamalar" listesidir,
    kullanici katalogu degil (o `GET /users`tir).
    """
    total = await session.scalar(select(func.count(distinct(UserApprovalRole.user_id))))
    users = list(
        (
            await session.execute(
                select(User)
                .join(UserApprovalRole, UserApprovalRole.user_id == User.id)
                .distinct()
                .order_by(User.full_name, User.id)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    atamalar: dict[uuid.UUID, list[ApprovalRole]] = {user.id: [] for user in users}
    if users:
        rows = await session.execute(
            select(UserApprovalRole)
            .where(UserApprovalRole.user_id.in_(list(atamalar)))
            .order_by(UserApprovalRole.approval_role)
        )
        for satir in rows.scalars():
            atamalar[satir.user_id].append(satir.approval_role)
    return users, total or 0, atamalar


def _pending_filter(
    actor_id: uuid.UUID,
    roles: list[ApprovalRole],
    admin_document_types: list[ApprovalDocumentType],
    visible_project_ids: list[uuid.UUID],
) -> tuple[Select, Select]:
    """Kullaniciya DUSEN siradaki adimlarin ortak suzgeci.

    Bekci 5 ve 6 SQL'e cevrilir; ekranda gosterilen kume ile ucta gecen kume
    AYNI kuraldan turemek zorundadir, yoksa kutuda gorunen bir satir tiklaninca
    403 verirdi.

    🔴 DORDUNCU KOSUL PROJE KAPSAMIDIR (T4, IDOR). Gövde ile SAYIM ayni
    `kosullar` demetinden turer: `total` suzgecin DISINDA kalsaydi kullanici
    GOREMEDIGI kayitlari sayardi (BOR-TEMIZ kanonu) — "items bos ama total > 0"
    hâli sahte-yesildir ve sayfalayici bos sayfalar uretirdi.
    """
    siradaki = (
        select(
            ApprovalStep.chain_id.label("chain_id"),
            func.min(ApprovalStep.step_no).label("step_no"),
        )
        .where(ApprovalStep.decided_at.is_(None))
        .group_by(ApprovalStep.chain_id)
        .subquery()
    )
    benim_kararim = select(ApprovalStep.chain_id).where(ApprovalStep.decided_by_user_id == actor_id)
    kosullar = (
        # 🔴 OKT-B1 — REDDEDILMIS zincir bekleyen DEGILDIR: karara baglanmamis
        # adimi DB'de durur ama zincir terminaldir.
        ApprovalChain.rejected_at.is_(None),
        ApprovalStep.approval_role.in_(roles),
        # Bekci 5 — kendi evraki (admin istisnasiyla).
        or_(
            ApprovalChain.created_by_user_id.is_distinct_from(actor_id),
            ApprovalChain.document_type.in_(admin_document_types),
        ),
        # Bekci 6 — gorevler ayriligi (admin ISTISNASI YOK).
        ApprovalChain.id.not_in(benim_kararim),
        # 🔴 IDOR — evragin PROJESI aktorun gordukleri arasinda mi (T4).
        documents.visible_document_clause(visible_project_ids),
    )
    govde = (
        select(ApprovalChain, ApprovalStep)
        .join(siradaki, siradaki.c.chain_id == ApprovalChain.id)
        .join(
            ApprovalStep,
            and_(
                ApprovalStep.chain_id == ApprovalChain.id,
                ApprovalStep.step_no == siradaki.c.step_no,
            ),
        )
        .where(*kosullar)
    )
    sayim = (
        select(func.count())
        .select_from(ApprovalChain)
        .join(siradaki, siradaki.c.chain_id == ApprovalChain.id)
        .join(
            ApprovalStep,
            and_(
                ApprovalStep.chain_id == ApprovalChain.id,
                ApprovalStep.step_no == siradaki.c.step_no,
            ),
        )
        .where(*kosullar)
    )
    return govde, sayim


async def pending_page(
    session: AsyncSession,
    *,
    actor_id: uuid.UUID,
    roles: list[ApprovalRole],
    admin_document_types: list[ApprovalDocumentType],
    visible_project_ids: list[uuid.UUID],
    limit: int,
    offset: int,
) -> tuple[list[tuple[ApprovalChain, ApprovalStep]], int]:
    govde, sayim = _pending_filter(actor_id, roles, admin_document_types, visible_project_ids)
    total = await session.scalar(sayim)
    rows = await session.execute(
        govde.order_by(ApprovalChain.created_at, ApprovalChain.id).limit(limit).offset(offset)
    )
    return [(chain, step) for chain, step in rows.all()], total or 0


async def history_page(
    session: AsyncSession,
    *,
    roles: list[ApprovalRole],
    visible_project_ids: list[uuid.UUID],
    decision: HistoryFilter,
    limit: int,
    offset: int,
) -> tuple[list[ApprovalChain], int]:
    """Onay GECMISI: gorunur zincirler — `decision` zincirin SON DURUMUNA gore suzer.

    Gorunurluk IKI kosuldur ve bekleyen kutusunun suzgeciyle AYNI iki ilkeye
    dayanir (rol + proje kapsami):

    * adimlarindan HERHANGI BIRININ onay rolu aktorun rollerinden biri (`bool_or`);
    * 🔴 IDOR: evragin projesi aktorun gordukleri arasinda —
      `documents.visible_document_clause`, `_pending_filter`in kullandigi AYNI
      yardimci. Govde ile SAYIM ayni `kosullar` demetinden turer.

    Bekci 5/6 (kendi evraki · gorevler ayriligi) BURADA YOKTUR: onlar "bu adimi
    ben imzalayabilir miyim" sorusunun kurallari; gecmis ise okunur bir kayittir.

    Son durum: `rejected` => `rejected_at IS NOT NULL`; `approved` => ret YOK ve
    TUM adimlar karara baglanmis; `all` => suzgec YOK (suren zincirler dahil,
    kartta `pending`). Siralama: karar zamani (ret ani ya da son imza ani)
    azalan, suren zincirler (karar zamani NULL) SONDA, sonra olusturulma zamani
    azalan, esitlikte `id`.
    """
    adimlar = (
        select(
            ApprovalStep.chain_id.label("chain_id"),
            func.count().label("adim_sayisi"),
            func.count(ApprovalStep.decided_at).label("karara_baglanan"),
            func.max(ApprovalStep.decided_at).label("son_imza"),
            func.bool_or(ApprovalStep.approval_role.in_(roles)).label("rolum_var"),
        )
        .group_by(ApprovalStep.chain_id)
        .subquery()
    )
    reddedildi = ApprovalChain.rejected_at.is_not(None)
    onaylandi = and_(
        ApprovalChain.rejected_at.is_(None), adimlar.c.karara_baglanan == adimlar.c.adim_sayisi
    )
    kosullar = [
        adimlar.c.rolum_var.is_(True),
        # 🔴 IDOR — bekleyen kutusuyla ORTAK yardimci.
        documents.visible_document_clause(visible_project_ids),
    ]
    if decision is HistoryFilter.approved:
        kosullar.append(onaylandi)
    elif decision is HistoryFilter.rejected:
        kosullar.append(reddedildi)
    govde = select(ApprovalChain).join(adimlar, adimlar.c.chain_id == ApprovalChain.id)
    sayim = (
        select(func.count())
        .select_from(ApprovalChain)
        .join(adimlar, adimlar.c.chain_id == ApprovalChain.id)
    )
    karar_zamani = case(
        (reddedildi, ApprovalChain.rejected_at),
        (onaylandi, adimlar.c.son_imza),
        else_=None,
    )
    total = await session.scalar(sayim.where(*kosullar))
    rows = await session.execute(
        govde.where(*kosullar)
        .order_by(
            karar_zamani.desc().nulls_last(), ApprovalChain.created_at.desc(), ApprovalChain.id
        )
        .limit(limit)
        .offset(offset)
    )
    return list(rows.scalars().all()), total or 0
