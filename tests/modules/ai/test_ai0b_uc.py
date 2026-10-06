"""T6 — `GET /ai/tools` · `GET /ai/context` + 22. izin modülü (`ai`).

`GET /ai/tools` aynı zamanda **canlı doğrulama aracıdır**: iki farklı rolle
çağrılıp listelerin **FARKLI** geldiği ölçülür. Aynı gelseydi Kapı A hiçbir şey
yapmıyor olurdu.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.access import AccessLevel, Scope
from app.core.sayfalar import PageLevel
from app.core.security import create_access_token
from app.modules.roles.models import Module, ModuleGroup, RolePagePermission, RolePermission
from app.modules.roles.seed_data import MATRIX, MODULES, ROLE_ORDER
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

pytestmark = pytest.mark.asyncio


async def _bearer(client, user):
    return {"Authorization": f"Bearer {create_access_token(user.id, user.token_version)}"}


# --------------------------------------------------------------------------- #
# 22. modül — `ai`
# --------------------------------------------------------------------------- #


def test_ai_modulu_MATRISTE_ve_MODULLERDE_var() -> None:
    anahtarlar = [m["key"] for m in MODULES]
    assert "ai" in anahtarlar
    assert len(MODULES) == 23  # PLN-B1: 23. modül earned_value (ai 22. kalır)
    assert set(MATRIX) == set(anahtarlar)


def test_ai_modulu_SISTEM_grubunda_ve_sort_order_22() -> None:
    satir = next(m for m in MODULES if m["key"] == "ai")
    assert satir["group"] is ModuleGroup.SISTEM
    assert satir["sort_order"] == 22
    assert satir["name"] == "FİİL AI"


def test_ai_satiri_KULLANICI_KARARINA_uyar() -> None:
    """Kullanıcı: "AI'ı herkes kendi kapsamında kullanabilsin."

    🔴 `patron` `_F` DEĞİL `_V`: `ai:full` hiçbir şey ifade etmez (yazma kapısı
    `SYSTEM_ADMIN_KEY` rol anahtarındadır), `_F` yazmak İzin Matrisi ekranında
    **var olmayan bir yetki** gösterirdi.
    🔴 `system_admin` `_A`: `test_system_admin_has_admin_level_everywhere`
    bunu ZORUNLU kılar — seçim değil, kısıt.
    """
    hucreler = dict(zip(ROLE_ORDER, MATRIX["ai"], strict=True))
    assert hucreler["system_admin"] == (AccessLevel.admin, Scope.all)
    assert hucreler["patron"] == (AccessLevel.view, Scope.all)
    for rol in ROLE_ORDER[1:]:
        assert hucreler[rol] == (AccessLevel.view, Scope.all), rol
    assert all(seviye is not AccessLevel.none for seviye, _ in MATRIX["ai"])


async def test_ai_izin_satirlari_SEED_ile_iner(seeded_db) -> None:
    modul = (await seeded_db.execute(select(Module).where(Module.key == "ai"))).scalar_one()
    satirlar = (
        (
            await seeded_db.execute(
                select(RolePermission).where(RolePermission.module_id == modul.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(satirlar) == 8


async def test_ai_modulu_AUTH_ME_yanitinda_GORUNUR(client, user_factory, seeded_db) -> None:
    """🔴 Asıl belirti Ayarlar ekranı DEĞİL burasıdır: `get_role_matrix` bir
    INNER JOIN'dir; izin satırı olmayan modülün anahtarı `/auth/me` yanıtında
    HİÇ BULUNMAZ ve frontend varsayılana düşer."""
    user = await user_factory("me@fiil.example.com", "Sifre1234!", "site_chief")
    # 🔴 `populate_existing` kanonu: `client` fixture'ı testle AYNI session'ı
    # paylaşır; `get_current_user`ın `joinedload(User.role)`u kimlik
    # haritasındaki rolsüz nesneye uygulanmaz ve `lazy="raise"` patlar.
    # Üretimde her istek kendi session'ını alır — bu, testin kurgusunun bedeli.
    seeded_db.expunge(user)
    yanit = await client.get("/auth/me", headers=await _bearer(client, user))
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["permissions"]["ai"] == "view"


# --------------------------------------------------------------------------- #
# `GET /ai/tools`
# --------------------------------------------------------------------------- #


async def test_ai_tools_kimliksiz_401(client) -> None:
    assert (await client.get("/ai/tools")).status_code == 401


async def test_ai_tools_ai_izni_YOKSA_403(client, user_factory, seeded_db) -> None:
    """Kapısının gerçekten koştuğunun kanıtı: `ai` hücresi `none`a düşürülür."""
    user = await user_factory("kapali@fiil.example.com", "Sifre1234!", "site_chief")
    await modul_duzeyi_yaz(seeded_db, user.role_id, "ai", AccessLevel.none)

    yanit = await client.get("/ai/tools", headers=await _bearer(client, user))
    assert yanit.status_code == 403, yanit.text


async def test_ai_tools_IKI_ROLDE_FARKLI_liste_doner(client, user_factory) -> None:
    """🔴 Canlı doğrulama ölçütü. Aynı gelseydi Kapı A ölü kod olurdu.

    `site_chief` `timesheet=_F` taşır → `puantaj_haftasi` GÖRÜR.
    `hr_manager` `dashboard=_LIM` (view) ama `projects=_LIM` (view) ve
    `timesheet=_F` — bu yüzden ayrım `procurement` ile kurulur:
    `procurement` matriste `dashboard=_N` · `projects=_N` · `timesheet=_N`dir.
    """
    sef = await user_factory("sef@fiil.example.com", "Sifre1234!", "site_chief")
    satinalma = await user_factory("sat@fiil.example.com", "Sifre1234!", "procurement")

    sef_yanit = await client.get("/ai/tools", headers=await _bearer(client, sef))
    sat_yanit = await client.get("/ai/tools", headers=await _bearer(client, satinalma))
    assert sef_yanit.status_code == sat_yanit.status_code == 200

    sef_adlar = {k["ad"] for k in sef_yanit.json()["items"]}
    sat_adlar = {k["ad"] for k in sat_yanit.json()["items"]}
    assert sef_adlar != sat_adlar, "iki rol AYNI listeyi aldı — Kapı A çalışmıyor"
    assert {"projeleri_listele", "puantaj_haftasi", "gosterge_ozeti"} <= sef_adlar
    assert {"projeleri_listele", "puantaj_haftasi", "gosterge_ozeti"} & sat_adlar == set()
    # …ama kapısız araçlar İKİSİNDE DE var (liste boş değil).
    assert {"navigate_to", "yetkilerim"} <= sat_adlar


async def test_ai_tools_ucler_ve_kapilar_YAYINLANMAZ(client, user_factory) -> None:
    """Uç listesi bir iç detaydır; yayınlamak istemciye 'şu yolu çağır' fikri
    verir ve `navigate_to`nun kapalı-enum kararını dolanırdı."""
    user = await user_factory("gizli@fiil.example.com", "Sifre1234!", "site_chief")
    govde = (await client.get("/ai/tools", headers=await _bearer(client, user))).json()
    for kalem in govde["items"]:
        assert set(kalem) == {"ad", "aciklama", "kapsam", "kume"}


# --------------------------------------------------------------------------- #
# `GET /ai/context` (S14)
# --------------------------------------------------------------------------- #


async def test_ai_context_yetkisiz_modulleri_ADIYLA_bildirir(client, user_factory) -> None:
    user = await user_factory("ctx@fiil.example.com", "Sifre1234!", "procurement")
    yanit = await client.get("/ai/context", headers=await _bearer(client, user))
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["role_key"] == "procurement"
    assert "timesheet" in govde["yetkisiz_moduller"]
    assert "dashboard" in govde["yetkisiz_moduller"]
    assert govde["arac_adlari"], "kapısız araçlar her aktörde görünmeli"


async def test_ai_context_PROJE_KIMLIKLERINI_yayinlamaz(client, user_factory) -> None:
    """🔴 Bu uç `ai` kapısıyla korunuyor, `projects` kapısıyla DEĞİL. Görünür
    proje kimliklerini buraya koymak `projects:none` olan bir role (örn.
    `procurement`) kimlik sızdırırdı."""
    user = await user_factory("sizinti@fiil.example.com", "Sifre1234!", "procurement")
    govde = (await client.get("/ai/context", headers=await _bearer(client, user))).json()
    assert "visible_project_ids" not in govde
    assert "proje_kimlikleri_notu" in govde


async def test_ai_context_ve_tools_AYNI_arac_kumesini_bildirir(client, user_factory) -> None:
    user = await user_factory("tutarli@fiil.example.com", "Sifre1234!", "accounting")
    tools = (await client.get("/ai/tools", headers=await _bearer(client, user))).json()
    ctx = (await client.get("/ai/context", headers=await _bearer(client, user))).json()
    assert {k["ad"] for k in tools["items"]} == set(ctx["arac_adlari"])


async def test_ai_uclari_YAZMA_metodu_TASIMAZ(client, user_factory) -> None:
    user = await user_factory("yazma@fiil.example.com", "Sifre1234!", "system_admin")
    basliklar = await _bearer(client, user)
    for metot, yol in (("post", "/ai/tools"), ("put", "/ai/context"), ("delete", "/ai/tools")):
        yanit = await getattr(client, metot)(yol, headers=basliklar)
        assert yanit.status_code == 405, (metot, yol, yanit.status_code)


async def test_sysadmin_kataloguna_PROPOSE_eklenmez_cunku_liste_BOS(client, user_factory) -> None:
    """`PROPOSE_TOOLS == ()` — ama dallanma yazılı ve testi de burada.

    Sysadmin ile normal rolün listesi `propose_*` yüzünden ayrışmaz; ayrışma
    yalnız okuma kapılarından gelir. Bu, `PROPOSE_TOOLS` dolduğunda kırılacak
    bir iddiadır ve **bilerek** öyledir.
    """
    admin = await user_factory("admin@fiil.example.com", "Sifre1234!", "system_admin")
    govde = (await client.get("/ai/tools", headers=await _bearer(client, admin))).json()
    assert not any(k["ad"].startswith("propose_") for k in govde["items"])
    assert not any(k["kapsam"] == "sistem_yoneticisi" for k in govde["items"])


async def test_KABUL_OLCUTU_4_SAHTE_propose_araci_patronda_GORUNMEZ(seeded_db, user_factory):
    """AI-0 kabul ölçütü 4: `sysadmin_only` işaretli SAHTE bir araçla kapı
    ŞİMDİDEN kanıtlı — `patron` matriste `_V` ve yine de göremiyor.

    🔴 Kapı `AccessLevel.admin` üzerine KURULMAZ: ölçülmüş klon riski var
    (`create_custom_role` + her hücrede `admin` = `is_system=False` süper rol).
    Kapı `role.key` + `is_system` ikilisine bakar.
    """
    from app.modules.ai.actor import aktor_baglami
    from app.modules.ai.registry import ToolKapsami, ToolKumesi, ToolRegistry, ToolSpec
    from app.modules.ai.tools import schemas
    from app.modules.ai.tools.catalog import READ_TOOLS

    sahte_propose = ToolSpec(
        ad="propose_sonda",
        aciklama="sahte",
        kapsam=ToolKapsami.SISTEM_YONETICISI,
        kume=ToolKumesi.KAPSAMSIZ,
        kapilar=frozenset(),
        ucler=(),
        veri_modulleri=frozenset(),
        yol_parametreleri={},
        girdi=schemas.BosGirdi,
        yanit_modeli=schemas.AiYonlendirme,
        calistir=None,  # type: ignore[arg-type]
    )
    kayit = ToolRegistry(READ_TOOLS, (sahte_propose,))

    patron = await user_factory("patron@fiil.example.com", "Sifre1234!", "patron")
    admin = await user_factory("sa@fiil.example.com", "Sifre1234!", "system_admin")

    patron_araclar = {s.ad for s in kayit.katalog(await aktor_baglami(seeded_db, patron))}
    admin_araclar = {s.ad for s in kayit.katalog(await aktor_baglami(seeded_db, admin))}

    assert "propose_sonda" not in patron_araclar
    # 🔴 POZİTİF KONTROL: `PROPOSE_TOOLS=[]` yazan biri bu testi GEÇEMESİN.
    assert "propose_sonda" in admin_araclar


async def test_KLON_ROL_her_hucrede_admin_olsa_bile_propose_GOREMEZ(seeded_db, user_factory):
    """⚠️ Ölçülmüş klon riski: `is_system=False` bir süper rol."""
    from app.modules.ai.registry import (
        ActorContext,
        ToolKapsami,
        ToolKumesi,
        ToolRegistry,
        ToolSpec,
    )
    from app.modules.ai.tools import schemas
    from app.modules.ai.tools.catalog import READ_TOOLS

    sahte_propose = ToolSpec(
        ad="propose_sonda",
        aciklama="sahte",
        kapsam=ToolKapsami.SISTEM_YONETICISI,
        kume=ToolKumesi.KAPSAMSIZ,
        kapilar=frozenset(),
        ucler=(),
        veri_modulleri=frozenset(),
        yol_parametreleri={},
        girdi=schemas.BosGirdi,
        yanit_modeli=schemas.AiYonlendirme,
        calistir=None,  # type: ignore[arg-type]
    )
    kayit = ToolRegistry(READ_TOOLS, (sahte_propose,))

    klon = ActorContext(
        user_id=(await user_factory("klon@fiil.example.com", "Sifre1234!", "patron")).id,
        role_key="sistem_yoneticisi_kopyasi",
        role_is_system=False,
        permissions={m: AccessLevel.admin for m in MATRIX},
    )
    assert "propose_sonda" not in {s.ad for s in kayit.katalog(klon)}

    # Ve anahtarı doğru olup `is_system=False` olan bir klon da geçemez.
    yari_klon = ActorContext(
        user_id=klon.user_id,
        role_key="system_admin",
        role_is_system=False,
        permissions=klon.permissions,
    )
    assert "propose_sonda" not in {s.ad for s in kayit.katalog(yari_klon)}


async def test_ozel_rol_ai_hucresi_UPDATE_edilebilir(seeded_db, user_factory, client) -> None:
    """🔴 Migration SAPMA 1'in uygulama tarafındaki karşılığı.

    Seed yolunda özel rol yoktur (seed 8 rolü yazar), ama `create_custom_role`
    yeni rol açtığında `ai` hücresinin de üretildiğini ölçüyoruz — yoksa Ayarlar
    ekranı o hücreyi hiç değiştiremez (`modul_duzeyi_yaz` geçerli modülde hata atmaz;
    bilinmeyen modülde `NotFoundError` atar).
    """
    from app.modules.roles.schemas import RoleCreate
    from app.modules.roles.service import create_custom_role

    rol = await create_custom_role(
        seeded_db, RoleCreate(key="ai_sonda", name="AI Sonda Rolü", emoji="🧪")
    )
    await seeded_db.flush()

    await modul_duzeyi_yaz(seeded_db, rol.id, "ai", AccessLevel.view)
    hucre = await seeded_db.get(RolePagePermission, (rol.id, "genel.fiil_ai"))
    assert hucre is not None and hucre.level is PageLevel.view


async def test_ozel_rol_olusturma_eski_satir_yazmaz_100_sayfa_hucresi_yazar(seeded_db) -> None:
    """IZN-B2: özel rol eski `role_permissions` satırı taşımaz; sayfa hücreleri (100) vardır."""
    from app.modules.roles.schemas import RoleCreate
    from app.modules.roles.service import create_custom_role

    rol = await create_custom_role(
        seeded_db, RoleCreate(key="ai_sonda_2", name="AI Sonda 2", emoji="🧪")
    )
    await seeded_db.flush()
    satirlar = (
        (await seeded_db.execute(select(RolePermission).where(RolePermission.role_id == rol.id)))
        .scalars()
        .all()
    )
    assert satirlar == []


async def test_roller_ve_moduller_ucu_22_modul_doner(client, user_factory) -> None:
    admin = await user_factory("modul@fiil.example.com", "Sifre1234!", "system_admin")
    yanit = await client.get("/modules", headers=await _bearer(client, admin))
    assert yanit.status_code == 200
    anahtarlar = [m["key"] for m in yanit.json()]
    assert "ai" in anahtarlar
    # PLN-B1: `ai` artık son DEĞİL — 23. modül `earned_value` sona eklendi (kaydırma yok).
    assert anahtarlar[-2:] == ["ai", "earned_value"], "sort_order 22, 23 → listenin SONUNDA"


async def test_izin_matrisi_ekrani_ai_satirini_GORUR(client, user_factory) -> None:
    """A5 kodla cevaplandı: `PermissionMatrix.tsx` `useModules()` TÜM modülleri
    çeker ve modül bazlı gizleme YOKTUR. Yani `ai` satırı ekranda **görünür**.
    Bu bir karar değil, OLGUdur — ve backend tarafı burada kilitlenir."""
    admin = await user_factory("matris@fiil.example.com", "Sifre1234!", "system_admin")
    roller = (await client.get("/roles", headers=await _bearer(client, admin))).json()
    sef = next(r for r in roller if r["key"] == "site_chief")
    matris = (
        await client.get(f"/roles/{sef['id']}/permissions", headers=await _bearer(client, admin))
    ).json()
    anahtarlar = {satir["module_key"] for satir in matris}
    assert "ai" in anahtarlar
