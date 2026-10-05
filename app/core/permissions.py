import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, Scope
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.gate_context import Flag
from app.core.openapi import SYSTEM_ADMIN_ONLY_DETAIL
from app.core.page_gate import decide, gate_flags, gate_ok, is_admin_role, page_ok, pages_ok
from app.core.scoped_route import kapsam_bagimligi_kur
from app.modules.projects.context import request_project
from app.modules.roles.repository import get_permission, role_mask_basis
from app.modules.users.models import User

_DENIED = "Bu işlem için yetkiniz yok"


async def _project_of(session: AsyncSession, request: Request | None) -> uuid.UUID | None:
    """İsteğin proje bağlamı (yol / gövde); istek yoksa (doğrudan çağrı) `None`."""
    return None if request is None else await request_project(session, request)


def require_permission(module_key: str, min_level: AccessLevel, *, multi_project: bool = False):
    """Uç için asgari yetki kapısı — IZN-B2'den itibaren KARAR SAYFA HÜCRELERİNDEN çıkar.

    Kullanımı:
        @router.post(
            "/x", dependencies=[require_permission("progress_payments", AccessLevel.draft)]
        )

    İmza ve 403 gövdesi DEĞİŞMEDİ. Karar kuralı `core/page_gate.decide`dedir: (modül, düzey) ile
    eşit eşikli sayfa bayraklarının VEYA'sı; Sistem Yöneticisi her yerde geçer; `admin` düzeyi ve
    eşleşen bayrağı olmayan çift yalnız Sistem Yöneticisi'ne açıktır (fail-closed). IZN-B3: isteğin
    PROJESİ çözülürse (yol/gövde) ve kişi o ekipteyse O PROJEDEKİ rolle, aksi hâlde yalnız ANA
    rolle karar verilir; `multi_project=True` çok proje LİSTE uçlarıdır (ana rol VEYA ekip rolü).
    Geçen kapı bağlama KENDİ İÇİNDE yazılır. Kapanış değişkenleri (`module_key`, `min_level`)
    YAPISAL bekçilerce okunur: yeniden adlandırma.
    """

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
        request: Request = None,  # type: ignore[assignment]
    ) -> None:
        project_id = await _project_of(session, request)
        if not await gate_ok(
            session, user, module_key, min_level, project_id=project_id, multi_project=multi_project
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check)


def require_system_admin():
    """Silme kapısı (SIL-B1, KARARLAR §1.7 K4): YALNIZ Sistem Yöneticisi, HER KOŞULDA.

    `require_permission`dan farkı: modül seviyesine DEĞİL rol ANAHTARINA bakar
    (`page_gate.is_admin_role`). `admin` seviyesi matrisle başka role de verilebilir; silme
    yetkisi verilemez. İstisna YOKTUR: kendi taslağı, AI sohbeti, izin talebi dahil.

    Kullanımı: `@router.delete("/x/{id}", dependencies=[require_system_admin()])`.
    Kapı handler'dan ÖNCE koşar: yetkisiz aktör kaydın var olup olmadığını (404) ya da bağlı
    kaydı olup olmadığını (409) öğrenemez. `tests/core/test_silme_kapi_bekcisi.py` her DELETE
    ucunun bu kapıyı taşıdığını router taramasıyla çakar.
    """

    async def _check(user: Annotated[User, Depends(get_current_user)], session: DbSession) -> None:
        # Rol ANAHTARI == system_admin: `page_gate.is_admin_role` TEK kaynaktır (IZN-B2'nin
        # "her yerde geçer" kuralıyla aynı yardımcı); yüklenmemiş rol için anahtar okunur.
        if not await is_admin_role(session, user):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=SYSTEM_ADMIN_ONLY_DETAIL
            )

    return Depends(_check)


def require_any_permission(*gates: tuple[str, AccessLevel], multi_project: bool = False):
    """Uç için "HERHANGİ BİRİ YETER" kapısı: `gates` içinden en az biri sağlanırsa geçer.

    Reddedişte gövde `require_permission` ile BİREBİR aynıdır (403, "Bu işlem için yetkiniz
    yok"). Hücre yoksa o kapı sağlanmamış sayılır (varsayılan kapalı). Karar `page_gate.decide`:
    kapıların bayrakları TEK VEYA grubunda birleşir (proje bağlamı ve kayıt `require_permission`
    gibi).
    """

    async def _check_any(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
        request: Request = None,  # type: ignore[assignment]
    ) -> None:
        project_id = await _project_of(session, request)
        pairs = tuple(pair for key, level in gates for pair in gate_flags(key, level))
        if not await decide(
            session, user, pairs, project_id=project_id, multi_project=multi_project
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check_any)


def require_page(page_key: str, flag: Flag, *, multi_project: bool = False):
    """§2.4 admin anlamlarının YENİ yeri: kapı doğrudan SAYFANIN bayrağına bağlanır.

    `flag`: `view` (≥ Görür) · `edit` (Düzenler) · `approve` (Onaylar). Sistem Yöneticisi her
    yerde geçer. 403 gövdesi `require_permission` ile aynıdır. Proje bağlamı ve kayıt
    `require_permission` gibidir (`page_gate.decide`).
    """

    async def _check_page(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
        request: Request = None,  # type: ignore[assignment]
    ) -> None:
        project_id = await _project_of(session, request)
        if not await page_ok(
            session, user, page_key, flag, project_id=project_id, multi_project=multi_project
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check_page)


def require_pages(page_keys: tuple[str, ...], flag: Flag, *, multi_project: bool = False):
    """Onay eylemi uçlarının kapısı: sayfa VE ikizlerinin bayrağından HERHANGİ BİRİ yeter.

    Kullanım: `require_pages(("mali.hakedis_isveren", "proje.isveren_hakedis"), "approve")`.
    Sistem Yöneticisi her yerde geçer; 403 gövdesi `require_permission` ile aynıdır.
    """

    async def _check_pages(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
        request: Request = None,  # type: ignore[assignment]
    ) -> None:
        project_id = await _project_of(session, request)
        if not await pages_ok(
            session, user, page_keys, flag, project_id=project_id, multi_project=multi_project
        ):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check_pages)


async def can_read(
    session: AsyncSession, user: User, module_key: str, project_id: uuid.UUID | None = None
) -> bool:
    """ILR-1/2 — bir ROLUN o modulu OKUYUP okuyamadigi (uc kapisi DEGIL, ALAN kapisi).

    🔴 `require_permission` bir UCU kapatir; bu ise TUREV BIR ALANI kapatir.
    Ikisi ayri sorunlardir: `boq` ucunu okuyabilen `procurement`, gunlukten
    turemis bir yuzdeyi gormemelidir — yoksa `site_diary`nin kapisi hic
    calismadan o veri BOQ ekranindan sizar (K4).

    IZN-B3: `project_id` verilirse O PROJEDEKI rolle (ekipteyse) karar verilir; verilmezse ANA rol.
    Çok projeli yerlerde proje başına `can_read_projects`. Bağlama YAZMAZ (`record=False`): alan
    kapısı bir proje süzgeci DEĞİLDİR.

    Varsayilan KAPALI: hucre yoksa `False`. Karar `gate_ok(modül, view)` (sayfa hücreleri).
    """
    return await gate_ok(
        session, user, module_key, AccessLevel.view, project_id=project_id, record=False
    )


async def can_read_projects(
    session: AsyncSession, user: User, module_key: str, project_ids: list[uuid.UUID]
) -> dict[uuid.UUID, bool]:
    """`can_read`in proje başına TOPLU hâli (N+1 yok): her proje için o projedeki rolün kararı.

    Ana rol kararı bir kez alınır; ekipte olunan projelerde o projedeki rol (proje içi sayfa
    çiftleriyle) kararı yeniden belirler. Ekipte olmayan / "Tüm projeler" / Sistem Yöneticisi →
    ana rol (`page_gate.decide` ile AYNI kural).
    """
    if not project_ids:
        return {}
    from app.core.gate_context import project_pairs
    from app.core.page_gate import cells_of_roles, cells_satisfy, load_cells, team_roles

    pairs = gate_flags(module_key, AccessLevel.view)
    if await is_admin_role(session, user):
        return {pid: True for pid in project_ids}
    if not pairs:
        return {pid: False for pid in project_ids}
    main = cells_satisfy(await load_cells(session, user.role_id, tuple(k for k, _ in pairs)), pairs)
    scoped = project_pairs(pairs)
    team = await team_roles(session, user) if scoped else {}
    cells = (
        await cells_of_roles(session, set(team.values()), tuple(k for k, _ in scoped))
        if team
        else {}
    )
    return {
        pid: cells_satisfy(cells[team[pid]], scoped) if pid in team else main for pid in project_ids
    }


async def actor_scope(session: AsyncSession, user: User, module_key: str) -> Scope:
    """Aktörün O MODÜLDEKİ veri kapsamı (`RolePermission.scope`).

    🔴 `require_permission` bir UCU, `can_read` bir ALANI kapatır; bu ise alan
    kapısını KAPSAMA bağlar (`core.field_scope` üç kovayı anlatır).

    İzin satırı yoksa `Scope.all` döner ve bu bilinçli bir fail-OPEN'dır: satır
    yoksa `require_permission` ucu ZATEN 403 ile kapatmıştır, yani buraya
    ulaşılmışsa erişim vardır. Burada `limited` varsaymak, kapsamı yapılandırılmamış
    her rolün ekranını sessizce boşaltırdı.
    """
    permission = await get_permission(session, user.role_id, module_key)
    if permission is not None:
        return permission.scope
    return await role_default_scope(session, user.role_id)


async def role_default_scope(session: AsyncSession, role_id) -> Scope:
    """Eski satırı OLMAYAN modül için kapsam (IZN-B2, CEO kararı HİBRİT).

    * Rolün HİÇ eski `role_permissions` satırı yoksa (yeni 6 rol, B2 sonrası açılan özel/kopya
      roller): `tum_tutarlar` gizliyse `limited`, değilse `all`. FAIL-CLOSED: yeni "Görüntüleyici"
      B4'e kadar tutarları görmez.
    * Rolün eski satırları varsa (8 seed rol + eski özel roller) ve bu modülün satırı yoksa:
      eski davranış (`all`, belgelenmiş fail-OPEN) AYNEN. Bu rollerde kaydedilen gizli alanlar
      B4'e kadar maskeyi DEĞİŞTİRMEZ (`RolePagesResponse.hidden_fields_effective=false`).
    """
    basis = await role_mask_basis(session, role_id)
    return Scope.limited if (not basis.has_legacy_rows and basis.hides_all_amounts) else Scope.all


def kapsam_kapisi(module_key: str):
    """Routerın `dependencies=[...]`ine eklenen KÖPRÜ.

    Bunu kuran router, `route_class=kapsam_rotasi(module_key, kapsamdan_oku)`
    ile birlikte kullanılmalıdır; ikisinden biri eksikse maske sessizce
    `all` görür. `tests/core/test_kapsam_baglantisi.py` bu çifti çakar.
    """

    async def _cozucu(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> Scope:
        return await actor_scope(session, user, module_key)

    # Kapsam bağımlılığı async generator'dır; function kapsamlı `get_db`ye
    # bağlanabilmesi için kendisinin de function olması şart (yoksa import'ta
    # DependencyScopeError). Maske ve serileştirme function_stack kapanmadan önce
    # koştuğundan davranış değişmez.
    return Depends(kapsam_bagimligi_kur(_cozucu), scope="function")
