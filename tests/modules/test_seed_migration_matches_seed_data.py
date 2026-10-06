"""Migration'lardaki elle kopyalanmis izin matrisinin seed_data.py ile birebir
ayni oldugunu dogrular.

app/modules/roles/seed_data.py (testlerin seed ettigi kaynak) ile
alembic/versions/*.py (production'a giden dondurulmus kopyalar) ayni matrisi
iki ayri yerde tutar. Migration'lar app kodunu KASITLI olarak import ETMEZ
(uygulanmis bir migration donmus olmalidir), bu yuzden ikisinin esitligini
garanti eden hicbir mekanizma yoktur. Bu test o boslugu kapatir.

Matris tek bir migration'da degil, uc uca eklenen migration'larda birikir:
  * a477fdf00fdf -> ilk 8 rol x 13 modul (bulk_insert)
  * 2cffc2fcfcf0 -> 14. modul "invoicing" + 8 izin satiri + sort_order kaydirmasi
  * b7fcd67bde1e -> 15. modul "projects", c41a7e2b9d05 -> 16. modul "sites" (ayni desen)
  * e9e8e6a52f96 -> 18. modul "contracts", f2a3b4c5d6e7 -> 19. modul "sales" (ayni desen)
  * b8c9d0e1f2a3 -> 20. modul "documents", d7e8f9a0b1c2 -> 21. modul "equipment"
  * e5f7a9c1b3d4 -> 22. modul "ai" (AI-0b)
  * 46a82e4b271c -> 23. modul "earned_value" (PLN-B1)
Bu yuzden karsilastirma, migration'larin BILESKESI ile seed_data arasinda yapilir.

DB gerektirmez: ilk migration'in upgrade() fonksiyonu, gercek `alembic.op` yerine
satirlari bellekte toplayan sahte bir `op` ile cagrilir (bulk_insert cagrilarini
yakalar). Ikinci migration satirlari calisma aninda DB'den okunan id'lerle
INSERT ettigi icin bulk_insert kullanmaz; onun yerine upgrade()'in SQL uretirken
okudugu modul-duzeyi sabitleri karsilastirilir.

IZN-B6b: `seed_data.MODULES` ve `Scope` uygulamadan kalkti; `seed_data.MATRIX` yalniz DUZEY
tasir. (duzey, kapsam) ciftleri ve modul satirlari migration'larin DONMUS kopyasiyla
(`tests/_donmus_eski_matris.py`, HEAD 3d04f8f) cakilir; CANLI `seed_data.MATRIX`in duzeyleri
ve CANLI kapi modul uzayi (`sayfalar.MODUL_ANAHTARLARI`) ayrica migration bileskesine esitlenir
(sayfa hucreleri izn_b1'de bu duzeylerden turedigi icin canli/migration ayrismasi = sessiz
yetki farki).
"""

import importlib.util
import sys
import uuid
from pathlib import Path
from unittest.mock import patch

from app.core.sayfalar import MODUL_ANAHTARLARI
from app.modules.roles import seed_data as app_seed_data
from tests import _donmus_eski_matris as eski_seed

VERSIONS_DIR = Path(__file__).parents[2] / "alembic" / "versions"
SEED_MIGRATION_PATH = VERSIONS_DIR / "a477fdf00fdf_seed_roller_modul_ve_izinler.py"
INVOICING_MIGRATION_PATH = VERSIONS_DIR / "2cffc2fcfcf0_invoicing_izin_modulu.py"
P1_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_p1_proje_cekirdegi.py"))
P2_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_p2_santiye_bolum.py"))
BOQ_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_boq_izin_modulu.py"))
BOQ_PROCUREMENT_FIX_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_boq_procurement_izin_duzeltmesi.py"))
P5_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_p5_sozlesmeler.py"))
P8_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_p8_unite_satisi.py"))
DOCUMENTS_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_belge_cekirdegi.py"))
EQUIPMENT_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_mk1_makine_cekirdegi.py"))
AI_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_ai0b_izin_modulu_ve_arac_denetimi.py"))
EARNED_VALUE_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_pln_b1_earned_value_butce.py"))
#: Tohum DEGIL, DUZELTICI: uygulanmayan kapsamlari `all`a ceker (2026-09-19).
SCOPE_FIX_MIGRATION_PATH = next(VERSIONS_DIR.glob("*_izin_kapsami_uygulanmayanlar_all.py"))
EXTENSION_MIGRATION_PATHS = [
    INVOICING_MIGRATION_PATH,
    P1_MIGRATION_PATH,
    P2_MIGRATION_PATH,
    BOQ_MIGRATION_PATH,
    BOQ_PROCUREMENT_FIX_MIGRATION_PATH,
    P5_MIGRATION_PATH,
    P8_MIGRATION_PATH,
    DOCUMENTS_MIGRATION_PATH,
    EQUIPMENT_MIGRATION_PATH,
    AI_MIGRATION_PATH,
    EARNED_VALUE_MIGRATION_PATH,
]


def _load_migration_module(path: Path):
    """Dosya adi revizyon hash'i ile basladigi icin nokta-yollu import edilemez."""
    name = f"_migration_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_seed_migration():
    return _load_migration_module(SEED_MIGRATION_PATH)


def _load_invoicing_migration():
    return _load_migration_module(INVOICING_MIGRATION_PATH)


def _value(x) -> str:
    """Enum uyesi veya string oldugu farketmeksizin karsilastirilabilir deger."""
    return x.value if hasattr(x, "value") else str(x)


def _captured_bulk_inserts(migration) -> dict[str, list[dict]]:
    """migration.upgrade()'i sahte bir op ile calistirip bulk_insert satirlarini yakalar.

    Gercek DB baglantisi kurulmaz; op.bulk_insert cagrilari kaydedilir ve
    upgrade() gercek SQL yerine bu kayda yazar.
    """
    captured: dict[str, list[dict]] = {}

    def fake_bulk_insert(table, rows):
        captured[table.name] = rows

    with patch.object(migration.op, "bulk_insert", side_effect=fake_bulk_insert):
        migration.upgrade()

    return captured


def _permission_map_from_app() -> dict[tuple[str, str], tuple[str, str]]:
    """DONMUS kopyanin (duzey, kapsam) ciftleri (IZN-B6b: canli seed kapsam tasimaz)."""
    result: dict[tuple[str, str], tuple[str, str]] = {}
    for module_key, cells in eski_seed.MATRIX.items():
        for role_key, (level, scope) in zip(eski_seed.ROLE_ORDER, cells, strict=True):
            result[(role_key, module_key)] = (_value(level), _value(scope))
    return result


def _level_map_from_live_app() -> dict[tuple[str, str], str]:
    """CANLI `seed_data.MATRIX`in duzeyleri (sayfa hucrelerinin kaynagi)."""
    result: dict[tuple[str, str], str] = {}
    for module_key, cells in app_seed_data.MATRIX.items():
        for role_key, level in zip(app_seed_data.ROLE_ORDER, cells, strict=True):
            result[(role_key, module_key)] = _value(level)
    return result


def _permission_map_from_migrations() -> dict[tuple[str, str], tuple[str, str]]:
    seed_migration = _load_seed_migration()
    captured = _captured_bulk_inserts(seed_migration)
    role_key_by_id = {v: k for k, v in seed_migration.ROLE_IDS.items()}
    module_key_by_id = {v: k for k, v in seed_migration.MODULE_IDS.items()}

    result: dict[tuple[str, str], tuple[str, str]] = {}
    for row in captured["role_permissions"]:
        role_key = role_key_by_id[row["role_id"]]
        module_key = module_key_by_id[row["module_id"]]
        result[(role_key, module_key)] = (_value(row["access_level"]), _value(row["scope"]))

    for path in EXTENSION_MIGRATION_PATHS:
        extension = _load_migration_module(path)
        for module_key, cells in extension.MATRIX.items():
            for role_key, (level, scope) in zip(extension.ROLE_ORDER, cells, strict=True):
                result[(role_key, module_key)] = (_value(level), _value(scope))

    # 🔴 DUZELTICI migration (2026-09-19) — canlinin GERCEKTEN izledigi yol
    #    "tohumlar, SONRA duzeltme"dir. Tohum migration'lari DUZELTILMEZ
    #    (migration yazildigi gunun fotografidir); onun yerine bu bekci ayni
    #    sirayi taklit eder. Dusen kapsamlar migration'in KENDISINDEN okunur,
    #    ikinci bir kopya yazilmaz.
    duzeltici = _load_migration_module(SCOPE_FIX_MIGRATION_PATH)
    dusen = set(duzeltici.DUSEN)
    kapsamsiz = duzeltici.KAPSAMSIZ_MODUL
    result = {
        (role_key, module_key): (
            level,
            "all" if (scope in dusen or module_key == kapsamsiz) else scope,
        )
        for (role_key, module_key), (level, scope) in result.items()
    }
    return result


def _roles_set_from_app() -> set[tuple[str, str, bool, str]]:
    return {
        (row["key"], row["emoji"], row["is_system"], row["description"])
        for row in app_seed_data.ROLES
    }


def _roles_set_from_migration(captured: dict[str, list[dict]]) -> set[tuple[str, str, bool, str]]:
    return {
        (row["key"], row["emoji"], row["is_system"], row["description"])
        for row in captured["roles"]
    }


def _modules_set_from_app() -> set[tuple[str, str, str, int]]:
    return {
        (row["key"], row["name"], _value(row["group"]), row["sort_order"])
        for row in eski_seed.MODULES
    }


def _modules_set_from_migrations() -> set[tuple[str, str, str, int]]:
    """Ilk migration'in modul satirlari, uzanti migration'lari sirayla uygulanmis halde."""
    captured = _captured_bulk_inserts(_load_seed_migration())
    modules: dict[str, tuple[str, str, int]] = {
        row["key"]: (row["name"], _value(row["group"]), row["sort_order"])
        for row in captured["modules"]
    }
    for path in EXTENSION_MIGRATION_PATHS:
        extension = _load_migration_module(path)
        for key, sort_order in extension.SORT_ORDER_UPDATES.items():
            name, group, _ = modules[key]
            modules[key] = (name, group, sort_order)
        modules[extension.MODULE_KEY] = (
            extension.MODULE_NAME,
            extension.MODULE_GROUP,
            extension.MODULE_SORT_ORDER,
        )
    return {(key, name, group, so) for key, (name, group, so) in modules.items()}


def _all_uuids_unique(captured: dict[str, list[dict]]) -> bool:
    ids = [row["id"] for row in captured["roles"]]
    ids += [row["id"] for row in captured["modules"]]
    ids += [row["id"] for row in captured["role_permissions"]]
    assert all(isinstance(i, uuid.UUID) for i in ids)
    return len(ids) == len(set(ids))


def test_migration_permission_matrix_matches_seed_data():
    migration_map = _permission_map_from_migrations()
    assert _permission_map_from_app() == migration_map
    assert _level_map_from_live_app() == {
        key: level for key, (level, _scope) in migration_map.items()
    }


def test_migration_permission_matrix_has_184_cells():
    app_map = _permission_map_from_app()
    migration_map = _permission_map_from_migrations()
    assert len(app_map) == 184
    assert len(migration_map) == 184
    assert len(_level_map_from_live_app()) == 184


def test_migration_role_keys_match_seed_data():
    migration = _load_seed_migration()
    assert set(migration.ROLE_IDS.keys()) == {row["key"] for row in app_seed_data.ROLES}
    assert list(migration.ROLE_ORDER) == list(app_seed_data.ROLE_ORDER)


def test_extension_migration_role_orders_match_seed_data():
    """Sutun sirasi kaymissa izinler yanlis rollere yazilir — sessiz yetki sizintisi."""
    for path in EXTENSION_MIGRATION_PATHS:
        extension = _load_migration_module(path)
        assert list(extension.ROLE_ORDER) == list(app_seed_data.ROLE_ORDER)


def test_migration_module_keys_match_seed_data():
    migration = _load_seed_migration()
    keys = set(migration.MODULE_IDS.keys())
    for path in EXTENSION_MIGRATION_PATHS:
        keys.add(_load_migration_module(path).MODULE_KEY)
    assert keys == {row["key"] for row in eski_seed.MODULES}
    assert keys == set(MODUL_ANAHTARLARI)


def test_migration_role_rows_match_seed_data():
    captured = _captured_bulk_inserts(_load_seed_migration())
    # IZN-B2 (`671436c0b42a`): ilk seed migration'ı DONMUŞ kalır (Patron `is_system=true` yazar);
    # `izn_b2` migration'ı `patron.is_system=false` yapar (plan §8b) ve `seed_data.ROLES` yeni
    # durumu taşır. Bilinen tek fark burada açıkça uygulanır.
    migration_roles = {
        (key, emoji, False if key == "patron" else is_system, description)
        for key, emoji, is_system, description in _roles_set_from_migration(captured)
    }
    assert _roles_set_from_app() == migration_roles


def test_migration_module_rows_match_seed_data():
    assert _modules_set_from_app() == _modules_set_from_migrations()


def test_extension_migration_downgrades_restore_previous_sort_orders():
    """Her uzanti migration'inin PREVIOUS_SORT_ORDERS'i kendinden onceki bileskeye esit olmali."""
    captured = _captured_bulk_inserts(_load_seed_migration())
    current = {row["key"]: row["sort_order"] for row in captured["modules"]}
    for path in EXTENSION_MIGRATION_PATHS:
        extension = _load_migration_module(path)
        assert extension.PREVIOUS_SORT_ORDERS == {
            key: current[key] for key in extension.SORT_ORDER_UPDATES
        }
        current.update(extension.SORT_ORDER_UPDATES)
        current[extension.MODULE_KEY] = extension.MODULE_SORT_ORDER


def test_migration_generated_ids_are_unique():
    """bulk_insert satirlari gercekten benzersiz UUID'ler tasiyor mu (sahte op, gercek
    DB kisitlarini calistirmadigi icin bunu ayrica dogrulamak gerekir)."""
    captured = _captured_bulk_inserts(_load_seed_migration())
    assert _all_uuids_unique(captured)
