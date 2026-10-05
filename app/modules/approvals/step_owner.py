"""IZN-B3b (K1) — zincir ADIMININ SAHİBİ: TEK ortak koşul.

Adım rolü (`approval_steps.approval_role`) bir ROL ANAHTARIDIR (`ApprovalRole` değerleri
`roles.key` ile birebir aynıdır, R1). Bir adımı, **belgenin projesinde** o role atanmış kişi
onaylar:

* `project_members(kişi, belgenin projesi).role.key == adım rolü`, **ya da**
* `users.all_projects` ve kişinin ANA rolünün anahtarı == adım rolü ("Tüm projeler" kişi her
  projede ana rolüyle çalışır).

Eskiden bu bir kullanıcı başına, projesiz `user_approval_roles` tablosuydu; ayrı bir "onay rolü"
kavramı kalktı.

🔴 TEK KOPYA: kapı (`repository.chain_gate_facts`), gelen kutusu (`_pending_filter`), geçmiş
(`history_page`) ve kilitli karar (`service._assert_can_decide`) AYNI `step_owner_clause`ı
kullanır. Üç yer ayrışırsa kutuda görünen satır tıklanınca 403 verirdi.

🔴 SQL'DEDİR, BELLEKTE DEĞİL: koşul `ApprovalChain` ve `ApprovalStep` satırlarına BAĞLIDIR
(belgenin projesi zincirin `document_type`/`document_id`sinden çözülür); çağıran sorgu ikisini
de FROM'unda taşımalıdır. Projesi çözülemeyen belge (silinmiş / hiç var olmamış) hiçbir proje
üyeliğiyle eşleşmez (fail-closed); yalnız "Tüm projeler" dalı kalır ve o dal da belge
görünürlüğü süzgecinden geçer.
"""

import uuid

from sqlalchemy import ColumnElement, Text, case, cast, exists, literal, null, or_, select
from sqlalchemy.orm import aliased

from app.modules.approvals.documents import DOCUMENT_PROJECT_COLUMNS
from app.modules.approvals.models import ApprovalChain, ApprovalRole, ApprovalStep
from app.modules.roles.models import Role
from app.modules.users.models import ProjectMember, User

__all__ = [
    "STEP_ROLE_KEYS",
    "actor_is_candidate_clause",
    "document_project_expr",
    "step_owner_clause",
]

#: Zincirde adım olabilen rol anahtarları.
STEP_ROLE_KEYS: tuple[str, ...] = tuple(rol.value for rol in ApprovalRole)


def document_project_expr() -> ColumnElement:
    """`ApprovalChain`in evrağının `project_id`si (korelasyonlu skaler alt sorgu).

    Çağıran sorgunun FROM'unda `ApprovalChain` bulunmalıdır. Üç evrak ailesinin üçünde de
    `project_id` doğrudan evrak satırındadır (`documents.DOCUMENT_PROJECT_COLUMNS`).
    """
    kollar = [
        (
            ApprovalChain.document_type == document_type,
            select(project_column)
            .where(id_column == ApprovalChain.document_id)
            .correlate(ApprovalChain)
            .scalar_subquery(),
        )
        for document_type, (id_column, project_column) in DOCUMENT_PROJECT_COLUMNS.items()
    ]
    return case(*kollar, else_=null())


def step_owner_clause(actor_id: uuid.UUID) -> ColumnElement[bool]:
    """`ApprovalStep`in sahibi `actor_id` mi? (proje rolü VEYA "Tüm projeler" + ana rol)

    Çağıran sorgu `ApprovalChain` ve `ApprovalStep`i FROM'unda taşımalıdır.
    """
    adim_rolu = cast(ApprovalStep.approval_role, Text)
    # "Tüm projeler" kişide ekip satırı YOK SAYILIR (`page_gate.team_roles` ile hizalı): bayat
    # bir satır o kişiye ana rolünden ayrı bir proje rolü kazandırmaz.
    uye = aliased(User)
    proje_rolu = exists(
        select(literal(1))
        .select_from(ProjectMember)
        .join(Role, Role.id == ProjectMember.role_id)
        .join(uye, uye.id == ProjectMember.user_id)
        .where(
            ProjectMember.user_id == actor_id,
            uye.all_projects.is_(False),
            ProjectMember.project_id == document_project_expr(),
            Role.key == adim_rolu,
        )
    )
    ana_rol = exists(
        select(literal(1))
        .select_from(User)
        .join(Role, Role.id == User.role_id)
        .where(
            User.id == actor_id,
            User.all_projects.is_(True),
            Role.key == adim_rolu,
        )
    )
    return or_(proje_rolu, ana_rol)


def actor_is_candidate_clause(actor_id: uuid.UUID) -> ColumnElement[bool]:
    """ADAY İMZACI: aktör, HERHANGİ BİR projede (ya da "Tüm projeler" ile ana rolünde) bir adım
    rolünü taşıyor mu? Belgeye bağlı DEĞİLDİR.

    Kapının 403/404 eşitlemesi (`service.chain_step_substitutes_permission`) ve gelen kutusunun
    ucuz erken çıkışı bunu kullanır.
    """
    ekipte = exists(
        select(literal(1))
        .select_from(ProjectMember)
        .join(Role, Role.id == ProjectMember.role_id)
        .where(ProjectMember.user_id == actor_id, Role.key.in_(STEP_ROLE_KEYS))
    )
    ana_rol = exists(
        select(literal(1))
        .select_from(User)
        .join(Role, Role.id == User.role_id)
        .where(User.id == actor_id, User.all_projects.is_(True), Role.key.in_(STEP_ROLE_KEYS))
    )
    return or_(ekipte, ana_rol)
