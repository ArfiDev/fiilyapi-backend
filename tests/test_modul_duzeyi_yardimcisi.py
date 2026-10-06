"""Yeni yardımcı (`_modul_duzeyi_yardimcisi`) eski yolla (`_legacy_...`) BİREBİR eşdeğer mi (B6b-T).

Aynı çağrı dizisi iki yoldan koşulur (her biri SAVEPOINT'te, sonra geri alınır):
ESKİ = `update_role_permission` (eski satır + `sync_page_cells`), YENİ = `modul_duzeyi_yaz`.
Karşılaştırılan: `role_page_permissions` (100 hücre) + `role_hidden_fields` (eski tablo hariç).

BİLİNÇLİ FARKLAR (açıkça ayrı testlerde sabitlenir):
1. Eski yol DB'deki eski satırı okur; yeni yol okumaz (ORM ile elle değiştirilen eski satır).
2. Seed rolün başlangıç `limited` hücresi eski yolda ilk sync'te `tum_tutarlar`ı açar (site_chief);
   yeni yol açık `tum_tutarlar=` ister. Bu yüzden seed roller `tum_tutarlar`sız karşılaştırılır.
"""

import random
import zlib

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.core.errors import NotFoundError, PermissionLockedError
from app.core.sayfalar import ESKI_MODULLER, HiddenCategory
from app.modules.roles.models import (
    Module,
    Role,
    RoleHiddenField,
    RolePagePermission,
    RolePermission,
)
from app.modules.roles.schemas import RoleCreate
from app.modules.roles.seed_data import (
    IZN_MATRIX,
    IZN_ROLE_ORDER,
    ROLE_ORDER,
    seed_izn_reference_data,
)
from app.modules.roles.service import create_custom_role
from tests._legacy_permission_yardimcisi import sync_page_cells, update_role_permission
from tests._modul_duzeyi_yardimcisi import (
    modul_duzeyi_yaz,
    modul_duzeyleri_yaz,
    set_permission_uyumlu,
    update_role_permission_uyumlu,
)

_MODULLER = sorted(ESKI_MODULLER)
_DUZEYLER = list(AccessLevel)
_ESKI_ROLLER = [k for k in ROLE_ORDER if k != "system_admin"]


async def _snap(session, role_id):
    session.expire_all()
    hucreler = {
        r.page_key: (r.level, r.can_approve)
        for r in (
            await session.execute(
                select(RolePagePermission).where(RolePagePermission.role_id == role_id)
            )
        )
        .scalars()
        .all()
    }
    gizli = {
        r.category
        for r in (
            await session.execute(select(RoleHiddenField).where(RoleHiddenField.role_id == role_id))
        )
        .scalars()
        .all()
    }
    return hucreler, gizli


async def _rol(session, key):
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


async def _iki_yol(session, role_id, eski, yeni):
    """`eski`/`yeni` koşar; her biri SAVEPOINT içinde, sonuç anlık görüntüsü döner."""
    sonuclar = []
    for kos in (eski, yeni):
        session.info.pop("modul_duzeyi_haritalari", None)
        sp = await session.begin_nested()
        await kos()
        sonuclar.append(await _snap(session, role_id))
        await sp.rollback()
        session.expire_all()
    session.info.pop("modul_duzeyi_haritalari", None)
    return sonuclar


def _dizi(tohum: int, adet: int = 14):
    rng = random.Random(tohum)
    return [(rng.choice(_MODULLER), rng.choice(_DUZEYLER)) for _ in range(adet)]


async def _eski_satir_ekle_izn(session, role_id, modul_duzeyleri):
    """IZN rolünde eski yol `update_role_permission` ile reddedilir; satırı elle açıp sync'ler."""
    modules = {m.key: m for m in (await session.execute(select(Module))).scalars().all()}
    for modul, level in modul_duzeyleri:
        mevcut = (
            await session.execute(
                select(RolePermission).where(
                    RolePermission.role_id == role_id,
                    RolePermission.module_id == modules[modul].id,
                )
            )
        ).scalar_one_or_none()
        if mevcut is None:
            session.add(
                RolePermission(
                    role_id=role_id,
                    module_id=modules[modul].id,
                    access_level=level,
                    scope=Scope.all,
                )
            )
        else:
            mevcut.access_level = level
        await session.flush()
    await sync_page_cells(session, role_id)


@pytest.mark.parametrize("role_key", _ESKI_ROLLER)
async def test_seed_eski_rol_rastgele_dizi_esdeger(seeded_db, role_key):
    rol = await _rol(seeded_db, role_key)
    rid = rol.id
    dizi = _dizi(zlib.crc32(role_key.encode()) % 1000 + 7)

    async def eski():
        for modul, level in dizi:
            await update_role_permission(seeded_db, rid, modul, level, Scope.all)

    async def yeni():
        for modul, level in dizi:
            await modul_duzeyi_yaz(seeded_db, rid, modul, level)

    (e_h, e_g), (y_h, y_g) = await _iki_yol(seeded_db, rid, eski, yeni)
    assert len(e_h) == 100
    assert y_h == e_h
    # Fark 2: tum_tutarlar eski yolda seed `limited` hücreden gelir → o kategori hariç eşit.
    assert y_g - {HiddenCategory.tum_tutarlar} == e_g - {HiddenCategory.tum_tutarlar}


@pytest.mark.parametrize("role_key", IZN_ROLE_ORDER)
async def test_yeni_izn_rolu_rastgele_dizi_esdeger(seeded_db, role_key):
    await seed_izn_reference_data(seeded_db)
    rol = await _rol(seeded_db, role_key)
    rid = rol.id
    dizi = _dizi(zlib.crc32(role_key.encode()) % 1000 + 11)
    index = IZN_ROLE_ORDER.index(role_key)

    async def eski():
        # Eski satırlar IZN_MATRIX'ten (migration'ın türettiği gibi) açılır, sonra dizi.
        await _eski_satir_ekle_izn(
            seeded_db, rid, [(m, cells[index][0]) for m, cells in IZN_MATRIX.items()]
        )
        await _eski_satir_ekle_izn(seeded_db, rid, dizi)

    async def yeni():
        for modul, level in dizi:
            await modul_duzeyi_yaz(seeded_db, rid, modul, level)

    (e_h, e_g), (y_h, y_g) = await _iki_yol(seeded_db, rid, eski, yeni)
    assert y_h == e_h
    assert y_g - {HiddenCategory.tum_tutarlar} == e_g - {HiddenCategory.tum_tutarlar}


@pytest.mark.parametrize("tohum", [1, 2, 3, 4, 5])
async def test_ozel_rol_rastgele_dizi_ve_tum_tutarlar_esdeger(seeded_db, tohum):
    rol = await create_custom_role(
        seeded_db, RoleCreate(key=f"ozel_{tohum}", name="Özel", emoji="", description="")
    )
    rid = rol.id
    dizi = _dizi(tohum, 20)
    rng = random.Random(tohum)
    # Her adım: (modül, düzey, limited mi). Eski yol Scope.limited, yeni yol tum_tutarlar=…
    adimlar = [(m, lv, rng.random() < 0.3) for m, lv in dizi]

    async def eski():
        for modul, level, limited in adimlar:
            scope = Scope.limited if limited else Scope.all
            if scope is Scope.limited:
                # B4'ten beri `update_role_permission` limited'i reddeder (eski satırı elle yaz).
                await _eski_satir_ekle_limited(seeded_db, rid, modul, level)
            else:
                await update_role_permission(seeded_db, rid, modul, level, Scope.all)

    async def yeni():
        son: dict[str, AccessLevel] = {}
        limited_mi: dict[str, bool] = {}
        for modul, level, limited in adimlar:
            son[modul] = level
            limited_mi[modul] = limited
            await modul_duzeyi_yaz(seeded_db, rid, modul, level)
        # Eski yol bayrağı SON matristen türetir: erişimi olan herhangi bir limited hücre var mı.
        wanted = any(son[m] is not AccessLevel.none and limited_mi[m] for m in son)
        modul, level, _ = adimlar[-1]
        await modul_duzeyi_yaz(seeded_db, rid, modul, level, tum_tutarlar=wanted)

    (e_h, e_g), (y_h, y_g) = await _iki_yol(seeded_db, rid, eski, yeni)
    assert y_h == e_h
    assert y_g == e_g


async def _eski_satir_ekle_limited(session, role_id, modul, level):
    module = (await session.execute(select(Module).where(Module.key == modul))).scalar_one()
    mevcut = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module.id
            )
        )
    ).scalar_one_or_none()
    if mevcut is None:
        mevcut = RolePermission(role_id=role_id, module_id=module.id)
        session.add(mevcut)
    mevcut.access_level = level
    mevcut.scope = Scope.limited
    await session.flush()
    await sync_page_cells(session, role_id)


async def test_tum_tutarlar_ac_kapat_esdeger_ozel_rol(seeded_db):
    rol = await create_custom_role(
        seeded_db, RoleCreate(key="ozel_tt", name="Özel", emoji="", description="")
    )
    rid = rol.id

    async def eski():
        await _eski_satir_ekle_limited(seeded_db, rid, "projects", AccessLevel.view)
        await update_role_permission(seeded_db, rid, "projects", AccessLevel.view, Scope.all)

    async def yeni():
        await modul_duzeyi_yaz(seeded_db, rid, "projects", AccessLevel.view, tum_tutarlar=True)
        await modul_duzeyi_yaz(seeded_db, rid, "projects", AccessLevel.view, tum_tutarlar=False)

    (e_h, e_g), (y_h, y_g) = await _iki_yol(seeded_db, rid, eski, yeni)
    assert (y_h, y_g) == (e_h, e_g)
    assert e_g == set()

    # Açık hâl de eşit: eski limited satır / yeni tum_tutarlar=True.
    async def eski2():
        await _eski_satir_ekle_limited(seeded_db, rid, "projects", AccessLevel.view)

    async def yeni2():
        await modul_duzeyi_yaz(seeded_db, rid, "projects", AccessLevel.view, tum_tutarlar=True)

    (e_h, e_g), (y_h, y_g) = await _iki_yol(seeded_db, rid, eski2, yeni2)
    assert (y_h, y_g) == (e_h, e_g)
    assert e_g == {HiddenCategory.tum_tutarlar}


async def test_cok_modul_tek_seferde_esdeger_tek_tek(seeded_db):
    rol = await _rol(seeded_db, "patron")
    rid = rol.id
    dizi = _dizi(99, 6)
    yeni_harita = dict(dizi)

    async def tek_tek():
        for modul, level in dizi:
            await modul_duzeyi_yaz(seeded_db, rid, modul, level)

    async def toplu():
        await modul_duzeyleri_yaz(seeded_db, rid, yeni_harita)

    a, b = await _iki_yol(seeded_db, rid, tek_tek, toplu)
    assert a == b


async def test_uyumlu_sarmalayicilar_ayni_sonucu_uretir(seeded_db):
    rol = await _rol(seeded_db, "site_chief")
    rid = rol.id

    async def a():
        await set_permission_uyumlu(seeded_db, "site_chief", "boq", AccessLevel.full)

    async def b():
        await update_role_permission_uyumlu(seeded_db, rid, "boq", AccessLevel.full)

    x, y = await _iki_yol(seeded_db, rid, a, b)
    assert x == y


async def test_eski_tabloya_yazmaz_ve_okumaz(seeded_db):
    rol = await _rol(seeded_db, "site_chief")
    rid = rol.id
    onceki = (await seeded_db.execute(select(RolePermission.id))).scalars().all()
    await modul_duzeyi_yaz(seeded_db, rid, "boq", AccessLevel.full)
    sonraki = (await seeded_db.execute(select(RolePermission.id))).scalars().all()
    assert sorted(onceki) == sorted(sonraki)

    # FARK 1 (belgeli): eski satır elle değişse de hücre yeni yoldan türer, satırı okumaz.
    modul = (await seeded_db.execute(select(Module).where(Module.key == "payroll"))).scalar_one()
    satir = (
        await seeded_db.execute(
            select(RolePermission).where(
                RolePermission.role_id == rid, RolePermission.module_id == modul.id
            )
        )
    ).scalar_one()
    satir.access_level = AccessLevel.admin
    await seeded_db.flush()
    await modul_duzeyi_yaz(seeded_db, rid, "boq", AccessLevel.full)
    hucreler, _ = await _snap(seeded_db, rid)
    assert hucreler["mali.bordro"][0].value == "none"


async def test_seed_limited_bayrak_farki_sabitlenir(seeded_db):
    """FARK 2: site_chief seed `limited` hücreleri eski yolda `tum_tutarlar`ı açar, yenide açmaz."""
    rol = await _rol(seeded_db, "site_chief")
    rid = rol.id

    async def eski():
        await update_role_permission(seeded_db, rid, "boq", AccessLevel.full, Scope.all)

    async def yeni():
        await modul_duzeyi_yaz(seeded_db, rid, "boq", AccessLevel.full)

    (_, e_g), (_, y_g) = await _iki_yol(seeded_db, rid, eski, yeni)
    assert HiddenCategory.tum_tutarlar in e_g
    assert HiddenCategory.tum_tutarlar not in y_g


async def test_sistem_yoneticisi_ve_bilinmeyen_modul_hatasi(seeded_db):
    with pytest.raises(PermissionLockedError):
        await modul_duzeyi_yaz(seeded_db, "system_admin", "settings", AccessLevel.none)
    with pytest.raises(NotFoundError):
        await modul_duzeyi_yaz(seeded_db, "patron", "yok_modul", AccessLevel.view)
    with pytest.raises(NotFoundError):
        await modul_duzeyi_yaz(seeded_db, "yok_rol", "boq", AccessLevel.view)
    with pytest.raises(PermissionLockedError):
        await set_permission_uyumlu(seeded_db, "patron", "boq", AccessLevel.view, Scope.own)
