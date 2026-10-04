"""IZN-B2 KAPI KÖPRÜSÜ — uç/servis kapılarının kararını SAYFA HÜCRELERİNDEN verir.

Kapı imzası değişmez: `require_permission(modül, düzey)` / `can_read` / servis içi düzey okumaları
aynen kalır; KARAR artık `role_page_permissions` hücrelerinden çıkar (eski `role_permissions`
donduruldu). Kural (IZN-PLAN §2.2, "eylem başına eşik"; CEO onarım kararı 2026-10-04):

    kapı (modül M, düzey L) ≡ VEYA( sayfa P'nin bayrağı F ), F ve P şöyle seçilir:
      * L = view            → P'nin GÖRME eşiği tam `(M, view)` olan sayfalarda Görür (≥ Görür).
      * L = draft/request/full → P'nin YAZMA eşiği tam `(M, L)` olan sayfalarda Düzenler.
      * L = approve / admin → HİÇ bayrak: ONAYLAR bayrağı HİÇBİR modül kapısını açmaz.

Neden: eski kural "eşiği (M, L)'ye eşit HER bayrak" Görür ya da Onaylar bitinin yazma kapısını
açmasına yol açıyordu (Şef'e yalnız `ik.izin_yonetimi` Onaylar → `POST /personnel` geçiyordu).
Onay eylemi uçları artık `require_page(sayfalar, "approve")` ile (§2.4) kendi sayfa Onaylar
bitine bağlıdır; hücre, eşiği karşılayan eski düzeyden türetildiği için seed rollerinde karar
BİREBİR eski kapıyla aynıdır (parite testi). Sayfa ekranından değiştirilen hücre kapıya anında
yansır.

İKİ BİLİNÇLİ KURAL:

* **`admin` düzeyi** (silme ve "Süper" anlamları) genel kapıda sayfa bayrağına BAĞLANMAZ → yalnız
  Sistem Yöneticisi (IZN-PLAN §2.2: "silme → Sistem Yöneticisi"). Silme DIŞI admin anlamları
  (proje oluştur, dönemi/günlüğü yeniden aç, vergi dilimi, onay eşiği, dönüştür, rol/sayfa izni
  yönetimi) `require_page` ile AÇIKÇA kendi sayfa bayrağına bağlanır. Hakediş "Onayı Geri Al"
  ONAYLAR'A BAĞLANMAZ (CEO): yalnız Sistem Yöneticisi.
* **Eşleşen bayrağı olmayan** (modül, düzey) çifti FAIL-CLOSED: yalnız Sistem Yöneticisi geçer.
"""

import uuid
from functools import cache
from typing import Final, Literal

from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.access import SYSTEM_ADMIN_ROLE_KEY, AccessLevel
from app.core.sayfalar import SAYFA_BY_KEY, SAYFALAR, PageLevel
from app.modules.roles.models import Role, RolePagePermission

Flag = Literal["view", "edit", "approve"]
_FLAG_OF_ESIK: Final[tuple[tuple[str, Flag], ...]] = (
    ("gorme", "view"),
    ("yazma", "edit"),
    ("onay", "approve"),
)

#: Düzeyler yüksekten alçağa (`effective_level` ilk sağlananı döndürür). `admin` listede YOK:
#: yalnız Sistem Yöneticisi (genel kapıda sayfa bayrağı yok).
_DESCENDING: Final[tuple[AccessLevel, ...]] = (
    AccessLevel.full,
    AccessLevel.approve,
    AccessLevel.request,
    AccessLevel.draft,
    AccessLevel.view,
)


@cache
def gate_flags(module_key: str, level: AccessLevel) -> tuple[tuple[str, Flag], ...]:
    """(modül, düzey) kapısının sayfa bayrakları (modül docstring'indeki kural)."""
    if level is AccessLevel.view:
        attr, flag = "gorme", "view"
    elif level in (AccessLevel.draft, AccessLevel.request, AccessLevel.full):
        attr, flag = "yazma", "edit"
    else:  # approve / admin / none: kapıyı hiçbir sayfa bayrağı açmaz
        return ()
    found: list[tuple[str, Flag]] = []
    for sayfa in SAYFALAR:
        esik = getattr(sayfa, attr)
        if esik is not None and len(esik) == 1 and esik[0] == (module_key, level):
            found.append((sayfa.key, flag))
    return tuple(found)


@cache
def display_flags(module_key: str, level: AccessLevel) -> tuple[tuple[str, Flag], ...]:
    """YALNIZ GÖSTERGE (`/auth/me.permissions`): kapı bayrakları + eşiği `(M, L)` olan Onaylar.

    Onaylar bayrağı kapıyı AÇMAZ ama eski modül düzeyini (örn. `progress_payments=approve`)
    ekrana yansıtmak için gösterge türetimine girer.
    """
    found = list(gate_flags(module_key, level))
    for sayfa in SAYFALAR:
        esik = sayfa.onay
        if esik is not None and len(esik) == 1 and esik[0] == (module_key, level):
            found.append((sayfa.key, "approve"))
    return tuple(found)


@cache
def module_page_keys(module_key: str) -> tuple[str, ...]:
    """Bir modülün kapı bayrağı veren sayfaları (`effective_level` sorgusu için)."""
    keys = {page_key for level in _DESCENDING for page_key, _flag in gate_flags(module_key, level)}
    return tuple(sorted(keys))


@cache
def display_page_keys(module_key: str) -> tuple[str, ...]:
    keys = {page_key for level in _DESCENDING for page_key, _f in display_flags(module_key, level)}
    return tuple(sorted(keys))


def flag_true(grant: tuple[PageLevel, bool], flag: Flag) -> bool:
    level, approve = grant
    if flag == "view":
        return level is not PageLevel.none
    if flag == "edit":
        return level is PageLevel.edit
    return approve


Cells = dict[str, tuple[PageLevel, bool]]


def gate_ok_from_cells(cells: Cells, module_key: str, level: AccessLevel) -> bool:
    """Hücre haritasından kapı kararı (Sistem Yöneticisi DIŞI roller)."""
    return any(
        page_key in cells and flag_true(cells[page_key], flag)
        for page_key, flag in gate_flags(module_key, level)
    )


def level_from_cells(cells: Cells, module_key: str) -> AccessLevel:
    """Rolün modüldeki EN YÜKSEK geçilen düzeyi (Sistem Yöneticisi dışı; `admin` döndürmez)."""
    for level in _DESCENDING:
        if gate_ok_from_cells(cells, module_key, level):
            return level
    return AccessLevel.none


async def load_cells(
    session: AsyncSession, role_id: uuid.UUID, page_keys: tuple[str, ...] | None = None
) -> Cells:
    """Rolün sayfa hücreleri (TEK sorgu). `page_keys` verilirse yalnız onlar."""
    stmt = select(
        RolePagePermission.page_key, RolePagePermission.level, RolePagePermission.can_approve
    ).where(RolePagePermission.role_id == role_id)
    if page_keys is not None:
        if not page_keys:
            return {}
        stmt = stmt.where(RolePagePermission.page_key.in_(page_keys))
    rows = (await session.execute(stmt)).all()
    return {key: (level, approve) for key, level, approve in rows if key in SAYFA_BY_KEY}


async def is_admin_role(session: AsyncSession, user: object) -> bool:
    """Kullanıcı Sistem Yöneticisi mi? Rol yüklüyse sorgu YOK (`get_current_user` yükler)."""
    loaded = inspect(user).dict.get("role")
    if loaded is not None:
        return loaded.key == SYSTEM_ADMIN_ROLE_KEY
    # `session.get` kimlik haritasını kullanır: rol bir kez okunduktan sonra ek sorgu KOŞMAZ.
    role = await session.get(Role, user.role_id)  # type: ignore[attr-defined]
    return role is not None and role.key == SYSTEM_ADMIN_ROLE_KEY


async def gate_ok(session: AsyncSession, user: object, module_key: str, level: AccessLevel) -> bool:
    """`require_permission(modül, düzey)`in kararı. Sistem Yöneticisi her yerde geçer."""
    if await is_admin_role(session, user):
        return True
    flags = gate_flags(module_key, level)
    if not flags:
        return False  # eşleşen bayrak yok (admin/silme dahil) → yalnız Sistem Yöneticisi
    cells = await load_cells(session, user.role_id, tuple(k for k, _ in flags))  # type: ignore[attr-defined]
    return gate_ok_from_cells(cells, module_key, level)


async def pages_ok(
    session: AsyncSession, user: object, page_keys: tuple[str, ...], flag: Flag
) -> bool:
    """`require_pages(sayfalar, bayrak)`ın kararı: sayfalardan HERHANGİ BİRİNDE bayrak doğruysa."""
    if await is_admin_role(session, user):
        return True
    cells = await load_cells(session, user.role_id, tuple(page_keys))  # type: ignore[attr-defined]
    return any(key in cells and flag_true(cells[key], flag) for key in page_keys)


async def page_ok(session: AsyncSession, user: object, page_key: str, flag: Flag) -> bool:
    """`require_page(sayfa, bayrak)`ın kararı (§2.4 admin anlamlarının yeni yeri)."""
    return await pages_ok(session, user, (page_key,), flag)


async def effective_level(session: AsyncSession, user: object, module_key: str) -> AccessLevel:
    """Servis içi `permission.access_level` okumalarının karşılığı (TEK sorgu).

    Sistem Yöneticisi → `admin`. Diğer roller → `gate_ok`un geçtiği en yüksek düzey
    (`full`, `approve`, `request`, `draft`, `view`) ya da `none`.
    """
    if await is_admin_role(session, user):
        return AccessLevel.admin
    cells = await load_cells(session, user.role_id, module_page_keys(module_key))  # type: ignore[attr-defined]
    return level_from_cells(cells, module_key)


@cache
def _module_pages(module_key: str) -> tuple[str, ...]:
    return tuple(s.key for s in SAYFALAR if s.eski_modul == module_key)


def display_level(cells: Cells, module_key: str) -> AccessLevel:
    """`/auth/me.permissions` ve `GET /roles/{id}/permissions` için TÜRETİLMİŞ salt-okur düzey.

    Kapı bayrakları + Onaylar bayrakları (`display_flags`) üzerinden en yüksek sağlanan düzey;
    `view` kapısına bayrak vermeyen ama sayfası görünen modül (`approvals`: Onay Kutusu herkese
    açık) `view` gösterir. GÖSTERGEDİR, kapı değildir: kapılar `gate_ok`tur ve frontend F5'te
    `pages`e geçince bu alan kalkar. Ara düzeyler (örn. Patron `dashboard=full`) sayfa hücresinde
    ayrışmadığı için görünen düzey daha düşük olabilir; frontend'in okuduğu her (modül, eşik)
    kararı seed rollerinde eskiyle AYNIDIR (`test_izn_b2_fe_esik_paritesi`).
    """
    level = AccessLevel.none
    for candidate in _DESCENDING:
        if any(
            key in cells and flag_true(cells[key], flag)
            for key, flag in display_flags(module_key, candidate)
        ):
            level = candidate
            break
    if (
        level is AccessLevel.none
        and not gate_flags(module_key, AccessLevel.view)
        and any(
            key in cells and cells[key][0] is not PageLevel.none
            for key in _module_pages(module_key)
        )
    ):
        return AccessLevel.view
    return level
