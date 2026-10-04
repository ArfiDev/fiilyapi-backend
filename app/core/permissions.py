from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import AccessLevel, Scope
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.page_gate import Flag, gate_ok, page_ok, pages_ok
from app.core.scoped_route import kapsam_bagimligi_kur
from app.modules.roles.repository import get_permission, role_mask_basis
from app.modules.users.models import User

_DENIED = "Bu işlem için yetkiniz yok"


def require_permission(module_key: str, min_level: AccessLevel):
    """Uç için asgari yetki kapısı — IZN-B2'den itibaren KARAR SAYFA HÜCRELERİNDEN çıkar.

    Kullanımı:
        @router.post(
            "/x", dependencies=[require_permission("progress_payments", AccessLevel.draft)]
        )

    İmza ve 403 gövdesi DEĞİŞMEDİ. Karar kuralı `core/page_gate.py`dedir: (modül, düzey) ile eşit
    eşikli sayfa bayraklarının VEYA'sı; Sistem Yöneticisi her yerde geçer; `admin` düzeyi ve
    eşleşen bayrağı olmayan çift yalnız Sistem Yöneticisi'ne açıktır (fail-closed). Hücre yoksa
    reddedilir (varsayılan kapalı). Kapanış değişkenleri (`module_key`, `min_level`) YAPISAL
    bekçilerce okunur: yeniden adlandırma.
    """

    async def _check(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        if not await gate_ok(session, user, module_key, min_level):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check)


def require_any_permission(*gates: tuple[str, AccessLevel]):
    """Uç için "HERHANGİ BİRİ YETER" kapısı: `gates` içinden en az biri sağlanırsa geçer.

    Reddedişte gövde `require_permission` ile BİREBİR aynıdır (403, "Bu işlem için yetkiniz
    yok"). Hücre yoksa o kapı sağlanmamış sayılır (varsayılan kapalı). Her kapı `gate_ok`
    kuralıyla (sayfa hücreleri) değerlendirilir.
    """

    async def _check_any(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        for module_key, min_level in gates:
            if await gate_ok(session, user, module_key, min_level):
                return
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check_any)


def require_page(page_key: str, flag: Flag):
    """§2.4 admin anlamlarının YENİ yeri: kapı doğrudan SAYFANIN bayrağına bağlanır.

    `flag`: `view` (≥ Görür) · `edit` (Düzenler) · `approve` (Onaylar). Sistem Yöneticisi her
    yerde geçer. 403 gövdesi `require_permission` ile aynıdır. Bayrağın başlangıç eşiği eski
    kapıyla AYNIDIR (katalog; parite testi çakar), yani geçişte kimsenin yetkisi değişmez.
    """

    async def _check_page(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        if not await page_ok(session, user, page_key, flag):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check_page)


def require_pages(page_keys: tuple[str, ...], flag: Flag):
    """Onay eylemi uçlarının kapısı: sayfa VE ikizlerinin bayrağından HERHANGİ BİRİ yeter.

    Kullanım: `require_pages(("mali.hakedis_isveren", "proje.isveren_hakedis"), "approve")`.
    Sistem Yöneticisi her yerde geçer; 403 gövdesi `require_permission` ile aynıdır.
    """

    async def _check_pages(
        user: Annotated[User, Depends(get_current_user)],
        session: DbSession,
    ) -> None:
        if not await pages_ok(session, user, page_keys, flag):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=_DENIED)

    return Depends(_check_pages)


async def can_read(session: AsyncSession, user: User, module_key: str) -> bool:
    """ILR-1/2 — bir ROLUN o modulu OKUYUP okuyamadigi (uc kapisi DEGIL, ALAN kapisi).

    🔴 `require_permission` bir UCU kapatir; bu ise TUREV BIR ALANI kapatir.
    Ikisi ayri sorunlardir: `boq` ucunu okuyabilen `procurement`, gunlukten
    turemis bir yuzdeyi gormemelidir — yoksa `site_diary`nin kapisi hic
    calismadan o veri BOQ ekranindan sizar (K4).

    Varsayilan KAPALI: hucre yoksa `False`. Karar `gate_ok(modül, view)` (sayfa hücreleri).
    """
    return await gate_ok(session, user, module_key, AccessLevel.view)


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
