"""IZN-B1 — `seed_data.py` ↔ `izn_b1` migration'ı ELLE KOPYA eşitlik bekçisi.

Migration `app`i KASITLI olarak import etmez (uygulanmış migration donmuş olmalıdır), bu yüzden
seed değerleri migration'a elle kopyalanır ve ikisinin eşitliğini garanti eden hiçbir mekanizma
yoktur. Bu test o boşluğu kapatır (`test_seed_migration_matches_seed_data.py` emsali):

* 7 eski rolün `MATRIX` hücreleri (migration'da canlı satırı olmayan çiftin YEDEĞİ),
* 6 yeni rol (`IZN_ROLES`, `IZN_MATRIX`, `IZN_HIDDEN_FIELDS`),
* 100 sayfa → eski modül eşlemesi + onay bayrağı, yedi modülsüz sayfanın başlangıç düzeyi,
* eski → yeni DÖNÜŞÜM fonksiyonu: migration'ın `_page_cells`i ile uygulamanın
  `modul_hucrelerinden_sayfa_matrisi`si tüm roller için AYNI sonucu vermeli.

Eski bekçi (`test_seed_migration_matches_seed_data.py`) eski 8 rolü / 184 hücreyi dondurulmuş
migration'larla eşit tutmaya DEVAM eder; yeni roller `ROLES`/`ROLE_ORDER`/`MATRIX`e GİRMEZ.

DB gerektirmez.
"""

import importlib.util
import random
import sys
from pathlib import Path

import pytest

from app.core.access import AccessLevel, Scope
from app.core.sayfalar import (
    SAYFALAR,
    HiddenCategory,
    PageLevel,
    esik_spec,
    gizli_alanlar,
    sayfa_matrisi,
)
from app.modules.roles import seed_data
from app.modules.roles.models import IZN_ROLE_KEYS

MIGRATION_PATH = next(
    (Path(__file__).parents[2] / "alembic" / "versions").glob("*_izn_b1_sayfa_katalogu.py")
)


@pytest.fixture(scope="module")
def migration():
    spec = importlib.util.spec_from_file_location(
        f"_migration_{MIGRATION_PATH.stem}", MIGRATION_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _app_cells(matrix, order, role_key) -> dict[str, tuple[str, str]]:
    index = order.index(role_key)
    return {m: (cells[index][0].value, cells[index][1].value) for m, cells in matrix.items()}


def test_migration_zinciri_okt_b1in_ustunde() -> None:
    spec_text = MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'down_revision: str | Sequence[str] | None = "b7c3e9a1d5f2"' in spec_text


def test_eski_matris_ve_rol_sirasi_seed_ile_ayni(migration) -> None:
    assert list(migration.ROLE_ORDER) == list(seed_data.ROLE_ORDER)
    assert set(migration.MATRIX) == set(seed_data.MATRIX)
    for module_key, cells in seed_data.MATRIX.items():
        assert migration.MATRIX[module_key] == [(a.value, s.value) for a, s in cells], module_key


def test_yeni_roller_seed_ile_ayni(migration) -> None:
    app_roles = [(r["key"], r["name"], r["emoji"], r["description"]) for r in seed_data.IZN_ROLES]
    mig_roles = [(r["key"], r["name"], r["emoji"], r["description"]) for r in migration.IZN_ROLES]
    assert mig_roles == app_roles
    assert all(r["is_system"] is False for r in seed_data.IZN_ROLES)
    assert list(migration.IZN_ROLE_ORDER) == list(seed_data.IZN_ROLE_ORDER)
    assert set(seed_data.IZN_ROLE_ORDER) == IZN_ROLE_KEYS
    # Yeni roller eski listelere GİRMEZ (dondurulmuş migration bekçisi 8 rol/184 hücre ister).
    assert IZN_ROLE_KEYS.isdisjoint({r["key"] for r in seed_data.ROLES})
    assert IZN_ROLE_KEYS.isdisjoint(seed_data.ROLE_ORDER)


def test_yeni_rol_matrisi_seed_ile_ayni(migration) -> None:
    assert set(migration.IZN_MATRIX) == set(seed_data.IZN_MATRIX) == set(seed_data.MATRIX)
    for module_key, cells in seed_data.IZN_MATRIX.items():
        assert len(cells) == len(seed_data.IZN_ROLE_ORDER)
        assert migration.IZN_MATRIX[module_key] == [(a.value, s.value) for a, s in cells], (
            module_key
        )


def test_gizli_alan_bayraklari_seed_ile_ayni(migration) -> None:
    assert {k: tuple(v) for k, v in migration.IZN_HIDDEN_FIELDS.items()} == {
        k: tuple(c.value for c in v) for k, v in seed_data.IZN_HIDDEN_FIELDS.items()
    }
    assert set(migration.CATEGORIES) == {c.value for c in HiddenCategory}
    assert set(migration.LEVELS) == {level.value for level in PageLevel}


def test_sayfa_eslemesi_ve_esik_tablosu_katalogla_birebir(migration) -> None:
    assert list(migration.PAGES) == [
        (s.key, s.eski_modul, s.onay_var, esik_spec(s.envanter_no, s.eski_modul)) for s in SAYFALAR
    ]
    assert migration.MODULSUZ_VARSAYILAN == {
        k: v.value for k, v in seed_data.MODULSUZ_VARSAYILAN.items()
    }


def test_yeni_rol_sayfa_istisnalari_seed_ile_ayni(migration) -> None:
    assert migration.IZN_SAYFA_ISTISNALARI == {
        role: {k: (level.value, approve) for k, (level, approve) in cells.items()}
        for role, cells in seed_data.IZN_SAYFA_ISTISNALARI.items()
    }


def test_donusum_fonksiyonu_tum_roller_icin_ayni_sonucu_verir(migration) -> None:
    """Migration'ın `_page_cells`i ile uygulamanın dönüşümü 13 rol için AYNI hücreleri üretir."""
    for role_key in seed_data.ROLE_ORDER:
        if role_key == "system_admin":
            continue
        cells = _app_cells(seed_data.MATRIX, seed_data.ROLE_ORDER, role_key)
        assert _tuple_rows(migration._page_cells(cells)) == _app_page_rows(role_key), role_key
        assert set(migration._hidden_categories(cells)) == {
            c.value for c in seed_data.HIDDEN_FIELDS[role_key]
        }, role_key
    for role_key in seed_data.IZN_ROLE_ORDER:
        cells = _app_cells(seed_data.IZN_MATRIX, seed_data.IZN_ROLE_ORDER, role_key)
        rows = _tuple_rows(migration._page_cells(cells))
        rows.update(migration.IZN_SAYFA_ISTISNALARI.get(role_key, {}))
        assert rows == _app_page_rows(role_key), role_key


def test_donusum_rastgele_modul_hucreleriyle_de_AYNI_migration_kopyasi(migration) -> None:
    """Eşikli türetme: 400 rastgele rol için migration kopyası = uygulama."""
    rng = random.Random(20261004)
    seviyeler = list(AccessLevel)
    kapsamlar = [Scope.all, Scope.limited, Scope.finance]
    modules = list(seed_data.MATRIX)
    for _ in range(400):
        cells = {m: (rng.choice(seviyeler), rng.choice(kapsamlar)) for m in modules}
        beklenen = {k: (lv.value, ap) for k, (lv, ap) in sayfa_matrisi(cells).items()}
        mig_cells = {m: (a.value, sc.value) for m, (a, sc) in cells.items()}
        assert _tuple_rows(migration._page_cells(mig_cells)) == beklenen
        assert set(migration._hidden_categories(mig_cells)) == {
            c.value for c in gizli_alanlar(cells)
        }
        # DB CHECK: onay görünmeyen sayfada olamaz.
        assert all(not (lv == "none" and ap) for lv, ap in beklenen.values())


def _tuple_rows(rows) -> dict[str, tuple[str, bool]]:
    return {page_key: (level, approve) for page_key, level, approve in rows}


def _app_page_rows(role_key: str) -> dict[str, tuple[str, bool]]:
    return {
        k: (level.value, approve) for k, (level, approve) in seed_data.PAGE_MATRIX[role_key].items()
    }


def test_sistem_yoneticisi_sayfa_matrisinde_YOK() -> None:
    assert "system_admin" not in seed_data.PAGE_MATRIX
    assert "system_admin" not in seed_data.HIDDEN_FIELDS
    assert len(seed_data.PAGE_MATRIX) == 13  # 7 eski + 6 yeni
    for cells in seed_data.PAGE_MATRIX.values():
        assert set(cells) == {s.key for s in SAYFALAR}


def test_finance_kapsami_duzeyi_degistirmez_limited_rol_bayragi_olur() -> None:
    finance = {"dashboard": (AccessLevel.view, Scope.finance)}
    limited = {"dashboard": (AccessLevel.view, Scope.limited)}
    assert gizli_alanlar(finance) == frozenset()
    assert gizli_alanlar(limited) == frozenset({HiddenCategory.tum_tutarlar})
    # Erişimi olmayan (none) hücredeki kapsam bayrak üretmez.
    assert gizli_alanlar({"dashboard": (AccessLevel.none, Scope.limited)}) == frozenset()


def test_esikli_donusum_ornekleri_ceo_karari() -> None:
    def hucre(key: str, **seviyeler: AccessLevel):
        cells = {m: (AccessLevel.none, Scope.all) for m in seed_data.MATRIX}
        cells.update({m: (lv, Scope.all) for m, lv in seviyeler.items()})
        return sayfa_matrisi(cells)[key]

    # Sade eski kural `full → Düzenler+Onaylar` idi; artık eşik gerçekten karşılanmalı.
    assert hucre("mali.donem_kapanisi", accounting=AccessLevel.full) == (PageLevel.edit, False)
    assert hucre("mali.donem_kapanisi", accounting=AccessLevel.admin) == (PageLevel.edit, True)
    assert hucre("mali.yevmiye", accounting=AccessLevel.full) == (PageLevel.edit, True)
    assert hucre("mali.yevmiye", accounting=AccessLevel.draft) == (PageLevel.view, False)
    # Çok modüllü onay eşiği: dönüştür = contracts full VE projects admin.
    assert hucre(
        "teklif.teklif_hazirlama", contracts=AccessLevel.full, projects=AccessLevel.full
    ) == (
        PageLevel.edit,
        False,
    )
    assert hucre(
        "teklif.teklif_hazirlama", contracts=AccessLevel.full, projects=AccessLevel.admin
    ) == (
        PageLevel.edit,
        True,
    )
    # Onay, Görmez sayfada OLAMAZ (DB CHECK).
    assert hucre(
        "teklif.teklif_hazirlama", contracts=AccessLevel.none, projects=AccessLevel.admin
    ) == (
        PageLevel.none,
        False,
    )
