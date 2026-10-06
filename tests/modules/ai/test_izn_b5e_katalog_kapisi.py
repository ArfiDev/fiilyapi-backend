"""IZN-B5e — AI katalog görünürlüğü GERÇEK kapı mantığıyla (`page_gate`), gösterge düzeyiyle DEĞİL.

Ölçüldü (B5c): `display_level` `gate_flags`ten biraz geniştir; katalog kapısı eskiden
`actor.permissions` (= `display_level`) okuyordu. Tek hücreli `mali.satis_toplu_uretim` Düzenler
rolü `projects=full` gösteriyor, `proje_detayi` listede görünüyor ama
çağrıda `Restricted` dönüyordu.
Kural: katalogda listelenen araç, ucun kendi kapısından (ana rol, proje bağlamsız) geçer.
"""

from __future__ import annotations

import pytest

from app.core.access import AccessLevel, satisfies
from app.core.page_gate import gate_ok
from app.core.sayfalar import PageLevel
from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry, kapilar_gecti
from app.modules.ai.result import NotFound
from app.modules.ai.tools.catalog import READ_TOOLS
from app.modules.roles.models import Role, RolePagePermission
from app.modules.roles.seed_data import PAGE_MATRIX
from tests._proje_ekibi import ekibe_ekle


@pytest.fixture(autouse=True)
def _denetimsiz(monkeypatch):
    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


@pytest.fixture
def tek_hucreli(seeded_db, user_factory):
    async def _yap(sayfa: str, duzey: PageLevel, etiket: str):
        rol = Role(key=f"b5e_{etiket}", name=etiket, emoji="", description="", is_system=False)
        seeded_db.add(rol)
        await seeded_db.flush()
        seeded_db.add(RolePagePermission(role_id=rol.id, page_key=sayfa, level=duzey))
        await seeded_db.flush()
        kullanici = await user_factory(f"b5e_{etiket}@fiil.example.com", "Sifre1234!", rol.key)
        seeded_db.expunge(kullanici)  # okuma düzlemi aynı session'ı kullanır
        return kullanici

    return _yap


async def _katalog(actor_factory, kullanici) -> set[str]:
    return {s.ad for s in ToolRegistry(READ_TOOLS).katalog(await actor_factory(kullanici))}


async def test_gosterge_gercek_kapidan_genis_rol_araci_GORMEZ(tek_hucreli, actor_factory) -> None:
    """`mali.satis_toplu_uretim` Düzenler → gösterge `projects=full`; GERÇEK kapı kapalı."""
    kullanici = await tek_hucreli("mali.satis_toplu_uretim", PageLevel.edit, "toplu")
    katalog = await _katalog(actor_factory, kullanici)
    assert not katalog & {"proje_detayi", "projeleri_listele", "arsa_payi"}, katalog


async def test_gercek_kapisi_acik_rol_araci_GORUR_ve_cagri_Restricted_degil(
    seeded_db, tek_hucreli, actor_factory, project_factory, transport_factory
) -> None:
    """Pozitif kontrol: `genel.projeler` Görür → listede, çağrı kapıdan geçer (ekipte değil)
    → 404."""
    proje = await project_factory(code="B5E-P", name="Pozitif")
    kullanici = await tek_hucreli("genel.projeler", PageLevel.view, "gp")
    aktor = await actor_factory(kullanici)
    kayit = ToolRegistry(READ_TOOLS)
    assert "proje_detayi" in {s.ad for s in kayit.katalog(aktor)}
    sonuc = await kayit.invoke(
        arac_adi="proje_detayi",
        argumanlar={"project_id": str(proje.id)},
        actor=aktor,
        transport=transport_factory(kullanici),
    )
    assert isinstance(sonuc, NotFound), type(sonuc).__name__


async def test_ekip_rolu_katalogu_GENISLETMEZ(
    seeded_db, tek_hucreli, actor_factory, project_factory
) -> None:
    """Ana rolü kapıyı açmayan kişi, ekip rolü açsa da katalogda aracı görmez (genişleme YOK):
    ekip rolü ASLA tek başına kapıyı açmaz (`decide`), projesiz katalogda ana rol belirleyicidir."""
    proje = await project_factory(code="B5E-E", name="Ekip")
    zayif = await tek_hucreli("mali.satis_toplu_uretim", PageLevel.edit, "zayif")
    guclu = await tek_hucreli("genel.projeler", PageLevel.view, "guclu")
    rol_guclu = await seeded_db.get(Role, guclu.role_id)
    await ekibe_ekle(seeded_db, zayif, proje.id, rol_guclu.id)
    assert "proje_detayi" not in await _katalog(actor_factory, zayif)


async def _seed_rolu_kur(seeded_db, user_factory, role_key: str):
    """Seed başlangıç sayfa matrisinden (`PAGE_MATRIX`, 13 rol) taze rol + kullanıcı."""
    rol = Role(key=f"b5e_s_{role_key}", name=role_key, emoji="", description="", is_system=False)
    seeded_db.add(rol)
    await seeded_db.flush()
    for sayfa, (duzey, onay) in PAGE_MATRIX[role_key].items():
        seeded_db.add(
            RolePagePermission(role_id=rol.id, page_key=sayfa, level=duzey, can_approve=onay)
        )
    await seeded_db.flush()
    kullanici = await user_factory(f"b5e_s_{role_key}@fiil.example.com", "Sifre1234!", rol.key)
    seeded_db.expunge(kullanici)
    return kullanici


async def test_seed_13_rol_katalog_gercek_kapiyla_esit_ve_eskinin_alt_kumesi(
    seeded_db, user_factory, actor_factory
) -> None:
    """13 seed rolü: yeni katalog ⊆ eski (genişleme 0) ve her araç `gate_ok` kararıyla birebir.

    Parite bekçisi, mutasyon bekçisi DEĞİL (seed'de gösterge = kapı; fark üretmez).
    """
    assert len(PAGE_MATRIX) == 13
    kayit = ToolRegistry(READ_TOOLS)
    for rol_anahtari in PAGE_MATRIX:
        kullanici = await _seed_rolu_kur(seeded_db, user_factory, rol_anahtari)
        aktor = await actor_factory(kullanici)
        yeni = {s.ad for s in kayit.katalog(aktor)}
        eski = {
            s.ad
            for s in READ_TOOLS
            if not (aktor.disiplin_kisitli and s.disiplin_kisitliya_kapali)
            and all(
                satisfies(aktor.permissions.get(m, AccessLevel.none), lv) for m, lv in s.kapilar
            )
        }
        assert yeni <= eski, (rol_anahtari, sorted(yeni - eski))
        for s in READ_TOOLS:
            gercek = not (aktor.disiplin_kisitli and s.disiplin_kisitliya_kapali)
            for m, lv in s.kapilar:
                gercek = gercek and await gate_ok(seeded_db, kullanici, m, lv, record=False)
            assert (s.ad in yeni) == gercek, (rol_anahtari, s.ad)


def test_kapilar_gecti_gecen_kapilar_verilince_gostergeye_BAKMAZ() -> None:
    """Birim: `gecen_kapilar` verilirse `permissions` yok sayılır (mutasyon sabiti)."""
    spec = next(s for s in READ_TOOLS if s.kapilar)
    (modul, seviye) = next(iter(spec.kapilar))
    tam = {modul: AccessLevel.admin}
    assert kapilar_gecti(spec, tam, frozenset()) is False


async def test_system_admin_katalogu_TUM_okuma_araclari_kapisizlar_dahil(
    seeded_db, user_factory, actor_factory
) -> None:
    """`aktor_baglami`daki Sistem Yöneticisi dalı: hücresiz admin her kapıdan geçer."""
    from app.modules.roles.models import SYSTEM_ADMIN_KEY

    admin = await user_factory("b5e_sa@fiil.example.com", "Sifre1234!", SYSTEM_ADMIN_KEY)
    aktor = await actor_factory(admin)
    assert {s.ad for s in ToolRegistry(READ_TOOLS).katalog(aktor)} == {s.ad for s in READ_TOOLS}


async def test_aktor_baglami_gecen_kapilari_HER_ZAMAN_doldurur(
    seeded_db, user_factory, actor_factory
) -> None:
    kullanici = await user_factory("b5e_dolu@fiil.example.com", "Sifre1234!", "patron")
    assert (await actor_factory(kullanici)).gecen_kapilar is not None


def test_ActorContext_YALNIZ_actor_py_icinde_kurulur() -> None:
    """Başka kurucu `gecen_kapilar=None` (gösterge, daha geniş) yoluna sessizce düşebilir."""
    import ast
    from pathlib import Path

    kok = Path(__file__).parents[3] / "app"
    kurucular = []
    for yol in kok.rglob("*.py"):
        for dugum in ast.walk(ast.parse(yol.read_text(encoding="utf-8"))):
            if (
                isinstance(dugum, ast.Call)
                and getattr(dugum.func, "id", getattr(dugum.func, "attr", None)) == "ActorContext"
            ):
                kurucular.append(yol.relative_to(kok).as_posix())
    assert set(kurucular) == {"modules/ai/actor.py"}, kurucular
