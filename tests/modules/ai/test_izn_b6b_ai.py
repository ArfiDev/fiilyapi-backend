"""IZN-B6b — AI yetki yüzeyi sayfa hücrelerinden türer (R1 `yetkilerim`, R2 `MODUL_ANAHTARLARI`).

(a) `yetkilerim` çıktısı `/auth/me` `pages` + `hidden_fields` tabanlıdır.
(b) Beklenen alan yoksa SESSİZ BOŞ harita dönmez: `yetki_alani_eksik`.
(c) `MODUL_ANAHTARLARI` == `sayfalar.ESKI_MODULLER` (23).
(d) `aktor_baglami.permissions` her modülde `level_from_cells` ile eşit; SisYön `admin`.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.core.access import AccessLevel
from app.core.page_gate import level_from_cells, load_cells
from app.core.sayfalar import ESKI_MODULLER
from app.core.security import create_access_token
from app.modules.ai import audit as ai_audit
from app.modules.ai.actor import MODUL_ANAHTARLARI
from app.modules.ai.registry import ToolRegistry
from app.modules.ai.result import Ok, ToolError
from app.modules.ai.tools.catalog import READ_TOOLS
from app.modules.ai.tools.reads.handlers import yetkilerim


@pytest.fixture(autouse=True)
def _denetim_sussun(monkeypatch):
    """Denetim yazımı ayrı session açar (savepoint dışı); burada ölçülen şey yetki yüzeyi."""

    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


def _bearer(user) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, user.token_version)}"}


def _sahte_ctx(govde: dict, durum: int = 200):
    async def _get(*_a, **_k) -> httpx.Response:
        return httpx.Response(durum, json=govde)

    return SimpleNamespace(get=_get)


# --------------------------------------------------------------------------- #
# (a) `yetkilerim` — gerçek /auth/me ile
# --------------------------------------------------------------------------- #


async def test_yetkilerim_gercek_auth_me_sayfalarindan_turer(
    client, seeded_db, user_factory, transport_factory, actor_factory
) -> None:
    user = await user_factory("yetki-a@fiil.example.com", "Sifre1234!", "procurement")
    seeded_db.expunge(user)  # `client` ile aynı session: lazy="raise" kanonu (bkz. test_ai0b_uc)
    me = (await client.get("/auth/me", headers=_bearer(user))).json()

    sonuc = await ToolRegistry(READ_TOOLS).invoke(
        arac_adi="yetkilerim",
        argumanlar={},
        actor=await actor_factory(user),
        transport=transport_factory(user),
    )

    assert isinstance(sonuc, Ok), sonuc
    veri = sonuc.data
    assert "permissions" not in veri
    assert veri["role_key"] == "procurement"
    beklenen = {
        anahtar: h["level"] + ("+onaylar" if h["approve"] else "")
        for anahtar, h in me["pages"].items()
        if h["level"] != "none" or h["approve"]
    }
    assert veri["sayfalar"] == beklenen
    assert beklenen, "pozitif kontrol: seed rolün erişimi olan sayfa var"
    assert veri["gizli_alanlar"] == sorted(me["hidden_fields"])


async def test_yetkilerim_erisimsiz_sayfa_LISTELENMEZ() -> None:
    govde = {
        "role_key": "x",
        "pages": {
            "genel.panel": {"level": "view", "approve": False},
            "mali.fatura": {"level": "none", "approve": False},
            "mali.onay": {"level": "edit", "approve": True},
        },
        "hidden_fields": ["maas", "banka"],
    }
    sonuc = await yetkilerim(_sahte_ctx(govde), None)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["sayfalar"] == {"genel.panel": "view", "mali.onay": "edit+onaylar"}
    assert sonuc.data["gizli_alanlar"] == ["banka", "maas"]


# --------------------------------------------------------------------------- #
# (b) alan yoksa sessiz boş YOK
# --------------------------------------------------------------------------- #

_TAM = {"role_key": "x", "pages": {}, "hidden_fields": []}


@pytest.mark.parametrize(
    "govde",
    [
        {k: v for k, v in _TAM.items() if k != "pages"},
        {k: v for k, v in _TAM.items() if k != "hidden_fields"},
        {k: v for k, v in _TAM.items() if k != "role_key"},
        {**_TAM, "pages": []},
        {**_TAM, "pages": None},
        {**_TAM, "hidden_fields": {}},
        {**_TAM, "hidden_fields": "maas"},
        {"permissions": {"ai": "view"}, "role_key": "x"},  # eski biçim
        {},
    ],
    ids=lambda g: (
        ",".join(sorted(g)) + f"|{type(g.get('pages')).__name__}"
        f"|{type(g.get('hidden_fields')).__name__}"
    ),
)
async def test_yetki_alani_eksik_ya_da_tipi_yanlis_ise_acik_hata(govde) -> None:
    sonuc = await yetkilerim(_sahte_ctx(govde), None)
    assert isinstance(sonuc, ToolError), sonuc
    assert sonuc.kod == "yetki_alani_eksik"


async def test_POZITIF_KONTROL_bos_ama_dogru_tipli_alanlar_hata_DEGIL() -> None:
    """Hatanın her şeyi reddeden bozuk bir kapıdan gelmediğinin kanıtı."""
    sonuc = await yetkilerim(_sahte_ctx(_TAM), None)
    assert isinstance(sonuc, Ok)
    assert sonuc.data["sayfalar"] == {} and sonuc.data["gizli_alanlar"] == []


# --------------------------------------------------------------------------- #
# (c) modül kümesi bekçisi
# --------------------------------------------------------------------------- #


def test_MODUL_ANAHTARLARI_seed_modulleriyle_birebir_ve_23() -> None:
    assert MODUL_ANAHTARLARI == ESKI_MODULLER, (
        f"eksik: {sorted(ESKI_MODULLER - MODUL_ANAHTARLARI)} "
        f"fazla: {sorted(MODUL_ANAHTARLARI - ESKI_MODULLER)}"
    )
    assert len(MODUL_ANAHTARLARI) == 23


# --------------------------------------------------------------------------- #
# (d) aktör bağlamı
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("rol", ["site_chief", "procurement", "accounting", "patron"])
async def test_aktor_permissions_her_modulde_level_from_cells_ile_esit(
    seeded_db, user_factory, actor_factory, rol
) -> None:
    user = await user_factory(f"akt-{rol}@fiil.example.com", "Sifre1234!", rol)
    hucreler = await load_cells(seeded_db, user.role_id)
    aktor = await actor_factory(user)

    assert set(aktor.permissions) == set(MODUL_ANAHTARLARI)
    for modul in MODUL_ANAHTARLARI:
        assert aktor.permissions[modul] == level_from_cells(hucreler, modul), modul
    assert any(v is not AccessLevel.none for v in aktor.permissions.values())


async def test_aktor_sistem_yoneticisi_her_modulde_admin(user_factory, actor_factory) -> None:
    user = await user_factory("akt-admin@fiil.example.com", "Sifre1234!", "system_admin")
    aktor = await actor_factory(user)
    assert set(aktor.permissions) == set(MODUL_ANAHTARLARI)
    assert set(aktor.permissions.values()) == {AccessLevel.admin}
