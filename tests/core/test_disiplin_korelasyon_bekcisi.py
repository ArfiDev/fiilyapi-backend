"""🔴 KORELASYON BEKÇİSİ (DSC-B1) — `ItemVisible` + AST bekçisi. DB'siz (yalnız derleme).

`item_visible_clause` kalem FROM'u kapsayan sorguda yoksa korelasyonun SESSİZCE düşüp kısıtın
FAIL-OPEN olmasını `ItemVisible`in derleme hatasına çevirir. Kısıtsızda da sarmaladığı için
(SQL'e yalnız `true` yazılır) bekçi atamasız aktörün testinde de çalışır.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from sqlalchemy import exists, select, union_all
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import aliased

import app.main  # noqa: F401 — EV saglayicisini porta kaydeder
from app.core import discipline_scope as port
from app.core.discipline_scope import UNRESTRICTED, DisciplineScope
from app.modules.boq.models import BoqItem
from app.modules.site_diary.models import SiteDiaryEntry, SiteDiaryLine

DIALECT = postgresql.dialect()
KISITLI = DisciplineScope(frozenset({uuid.UUID(int=1)}))
DIGER = DisciplineScope(frozenset({uuid.UUID(int=2)}))
KAPSAMLAR = pytest.mark.parametrize("scope", [KISITLI, UNRESTRICTED], ids=["kisitli", "kisitsiz"])


def _derle(stmt) -> str:
    return str(stmt.compile(dialect=DIALECT))


def _mesru_kaliplar(scope: DisciplineScope) -> dict[str, object]:
    vis = port.item_visible_clause
    return {
        "exists_ic_from": select(SiteDiaryEntry.id).where(
            exists().where(
                SiteDiaryLine.entry_id == SiteDiaryEntry.id,
                BoqItem.id == SiteDiaryLine.boq_item_id,
                vis(scope, BoqItem),
            )
        ),
        "exists_dis_kaleme_korele": select(BoqItem.id).where(
            exists().where(SiteDiaryLine.boq_item_id == BoqItem.id, vis(scope, BoqItem))
        ),
        "kolon_olarak": select(BoqItem.id, vis(scope, BoqItem)),
        "orm_varlik": select(BoqItem).where(vis(scope, BoqItem)),
        "visible_item_ids_kalibi": select(SiteDiaryLine.code).where(
            SiteDiaryLine.boq_item_id.in_(port.visible_item_ids(scope, BoqItem))
        ),
        "subquery_c": select(select(BoqItem.id).where(vis(scope, BoqItem)).subquery().c.id),
        "item_fk_conditions": select(SiteDiaryLine.code).where(
            *port.item_fk_conditions(scope, SiteDiaryLine.boq_item_id, BoqItem)
        ),
    }


@KAPSAMLAR
@pytest.mark.parametrize(
    "ad",
    [
        "exists_ic_from",
        "exists_dis_kaleme_korele",
        "kolon_olarak",
        "orm_varlik",
        "visible_item_ids_kalibi",
        "subquery_c",
        "item_fk_conditions",
    ],
)
def test_mesru_kalip_derlenir(scope: DisciplineScope, ad: str) -> None:
    assert _derle(_mesru_kaliplar(scope)[ad])


def _hatali_kaliplar(scope: DisciplineScope) -> dict[str, object]:
    vis = port.item_visible_clause
    yanlis = aliased(BoqItem)
    return {
        "kalem_from_da_yok": select(SiteDiaryLine.code).where(vis(scope, BoqItem)),
        "union_all_kalemsiz_dal": union_all(
            select(BoqItem.id).where(vis(scope, BoqItem)),
            select(SiteDiaryLine.id).where(vis(scope, BoqItem)),
        ),
        "yanlis_alias": select(BoqItem.id).where(vis(scope, yanlis)),
        "exists_icinde_kalem_yok": select(SiteDiaryEntry.id).where(
            exists().where(SiteDiaryLine.entry_id == SiteDiaryEntry.id, vis(scope, BoqItem))
        ),
    }


@KAPSAMLAR
@pytest.mark.parametrize(
    "ad", ["kalem_from_da_yok", "union_all_kalemsiz_dal", "yanlis_alias", "exists_icinde_kalem_yok"]
)
def test_hatali_kalip_derleme_hatasi_verir(scope: DisciplineScope, ad: str) -> None:
    with pytest.raises(RuntimeError, match="ItemVisible"):
        _derle(_hatali_kaliplar(scope)[ad])


def test_kisitsiz_sql_yalniz_true_uretir() -> None:
    """Atamasız yanıt değişmez: sarmalayıcı SQL'e yalnız `true` yazar, alt sorgu YOK."""
    sql = _derle(select(BoqItem.id).where(port.item_visible_clause(UNRESTRICTED, BoqItem)))
    assert sql.split("WHERE")[1].strip() == "true"
    assert "ev_group_disciplines" not in sql


def test_kisitli_sql_in_uretir() -> None:
    sql = _derle(select(BoqItem.id).where(port.item_visible_clause(KISITLI, BoqItem)))
    assert " IN (" in sql and "ev_group_disciplines" in sql


def test_item_fk_conditions_kisitsizda_bos_kisitlida_tek_madde() -> None:
    assert port.item_fk_conditions(UNRESTRICTED, SiteDiaryLine.boq_item_id, BoqItem) == []
    assert len(port.item_fk_conditions(KISITLI, SiteDiaryLine.boq_item_id, BoqItem)) == 1


def test_ust_duzey_str_hata_degildir() -> None:
    """Kapsayan SELECT'siz gösterim amaçlı `str()` patlamaz (B0 testleri buna dayanır)."""
    assert str(port.item_visible_clause(UNRESTRICTED, BoqItem)) == "true"


# --- önbellek anahtarı ------------------------------------------------------------------


def test_onbellek_anahtari_kapsam_parametresini_ve_kalem_kaynagini_ayirir() -> None:
    """Aynı yapı + farklı kapsam → AYNI anahtar, FARKLI parametre (önbellek paylaşılır ama
    değerler karışmaz); BoqItem ile alias → FARKLI anahtar (yanlış SQL yeniden kullanılmaz)."""
    alias = aliased(BoqItem)
    k1 = select(BoqItem.id).where(port.item_visible_clause(KISITLI, BoqItem))._generate_cache_key()
    k2 = select(BoqItem.id).where(port.item_visible_clause(DIGER, BoqItem))._generate_cache_key()
    k3 = select(alias.id).where(port.item_visible_clause(KISITLI, alias))._generate_cache_key()
    assert k1 is not None and k2 is not None and k3 is not None
    assert k1.key == k2.key
    assert [b.value for b in k1.bindparams] != [b.value for b in k2.bindparams]
    assert k1.key != k3.key


def test_onbellekten_gelen_ikinci_derleme_de_denetlenir() -> None:
    """Derleme önbelleği bekçiyi ATLAMAZ: aynı hatalı yapı iki kez de RuntimeError verir."""
    for _ in range(2):
        with pytest.raises(RuntimeError, match="ItemVisible"):
            _derle(select(SiteDiaryLine.code).where(port.item_visible_clause(KISITLI, BoqItem)))


# --- AST bekçisi -------------------------------------------------------------------------

_APP = Path(__file__).resolve().parents[2] / "app"
_IZINLI = {
    _APP / "core" / "discipline_scope.py",
    _APP / "modules" / "earned_value" / "discipline_adapter.py",
}
_YASAK_ADLAR = ("item_discipline_expr", "user_discipline_ids_subquery")


def _ad(dugum: ast.AST) -> str | None:
    if isinstance(dugum, ast.Name):
        return dugum.id
    if isinstance(dugum, ast.Attribute):
        return dugum.attr
    return None


def _ihlaller(kaynak: str, yol: str) -> list[str]:
    bulunan = []
    for dugum in ast.walk(ast.parse(kaynak)):
        if isinstance(dugum, ast.Call) and _ad(dugum.func) in _YASAK_ADLAR:
            bulunan.append(f"{yol}:{dugum.lineno} çağrı")
        if isinstance(dugum, ast.ImportFrom) and any(a.name in _YASAK_ADLAR for a in dugum.names):
            bulunan.append(f"{yol}:{dugum.lineno} import")
    return bulunan


def test_item_discipline_expr_port_disindan_cagrilamaz() -> None:
    """Sonucu `.in_`/`.not_in`/karşılaştırma ile kullanma yolu YALNIZ port ve EV adaptörüdür;
    her diğer uç `item_visible_clause` / `visible_item_ids` / `item_fk_conditions` kullanır
    (NOT IN fail-open'ı ve korelasyon düşmesi tek yerde çözülür)."""
    ihlal: list[str] = []
    for yol in sorted(_APP.rglob("*.py")):
        if yol in _IZINLI:
            continue
        ihlal += _ihlaller(yol.read_text(encoding="utf-8"), str(yol.relative_to(_APP.parent)))
    assert not ihlal, f"port dışında item_discipline_expr kullanımı: {ihlal}"


@pytest.mark.parametrize(
    "kaynak",
    [
        "x = item_discipline_expr(BoqItem).in_(ids)",
        "x = port.item_discipline_expr(BoqItem).not_in(ids)",
        "x = discipline_adapter.item_discipline_expr(BoqItem) == d",
        "from app.core.discipline_scope import item_discipline_expr",
        "x = port.user_discipline_ids_subquery(uid)",
        "from app.core.discipline_scope import user_discipline_ids_subquery",
    ],
)
def test_ast_bekcisi_ihlali_yakalar(kaynak: str) -> None:
    """Bekçinin kendisi sahte-yeşil olmasın: ihlal kalıpları GERÇEKTEN yakalanır."""
    assert _ihlaller(kaynak, "x.py")


def test_izinli_dosyalar_gercekten_var() -> None:
    assert all(yol.exists() for yol in _IZINLI)
