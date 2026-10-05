"""IZN-B3 ONARIM — çürütücü bulgularının ÖZEL senaryo testleri.

KRİTİK (onay/ret), Orta-1 (günlük yeniden aç), Orta-2 (panel), Orta-3 (alan kapısı `can_read`
PROJE BAŞINA), Orta-4 (slug bağlamı), Orta-5 (liste uçlarında proje başına süzme), Orta-6
(`PUT /users/{id}/access` yetki yükseltme) ve düşük bulgu (Sistem Yöneticisi ekip satırı).
Genel rota taraması `test_izn_b3_onarim_idor.py`dedir.
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import SAYFALAR, PageLevel
from app.modules.progress_payments.models import ProgressPayment, ProgressPaymentStatus
from app.modules.projects import context as proje_baglami
from app.modules.roles import seed_data
from app.modules.roles.models import Role, RolePagePermission
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
)
from app.modules.users.models import ProjectMember, User
from tests._ekip_dunyasi import proje_kur, rol_kur
from tests._proje_ekibi import disiplin_ata, ekibe_ekle, tum_projeler

PASSWORD = "parola1234"


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _rol(session: AsyncSession, key: str) -> Role:
    return (await session.execute(select(Role).where(Role.key == key))).scalar_one()


@pytest.fixture
async def iki_proje(seeded_db: AsyncSession, user_factory, project_factory):
    await seed_data.seed_izn_reference_data(seeded_db)
    olusturan = await user_factory(email="olusturan@izn.co", password=PASSWORD, role_key="patron")
    kule = await proje_kur(seeded_db, project_factory, "KULE", "Kule", olusturan.id)
    kopru = await proje_kur(seeded_db, project_factory, "KOPRU", "Köprü", olusturan.id)
    return kule, kopru


async def _durum(session: AsyncSession, model, kimlik: uuid.UUID):
    session.expire_all()
    return (await session.execute(select(model.status).where(model.id == kimlik))).scalar_one()


# --- KRİTİK: onay / ret uçları -------------------------------------------------------------


@pytest.mark.parametrize("cozucu", ["acik", "kapali"])
async def test_KRITIK_ana_rol_ve_baska_projedeki_ekip_rolu_kopru_hakedisini_onaylayamaz(
    client: AsyncClient, seeded_db, user_factory, iki_proje, monkeypatch, cozucu: str
) -> None:
    """Çürütücü kanıtı: ana rol `hr_manager`, Kule'de Proje Müdürü, Köprü'de Görüntüleyici →
    `POST /progress-payments/{Köprü}/approve` 200 alıp hakedişi onaylıyordu (`approve`/`reject`
    uçları ve taşeron `approve` da). Kök: onay kapısı kaydı yazmıyordu."""
    if cozucu == "kapali":
        # Varlığı çözülemeyen uç gibi: kapı KAYDI + `visible_projects` tek başına korur (kayıt
        # kapı içinden kalkarsa bu varyant KIRMIZI olur: onay 200/409 ya da bağlam hatası).
        monkeypatch.setattr(proje_baglami, "RESOLVERS", {})
        monkeypatch.setattr(proje_baglami, "BODY_RESOLVERS", {})
    kule, kopru = iki_proje
    kisi = await user_factory(email="karma@izn.co", password=PASSWORD, role_key="hr_manager")
    await ekibe_ekle(
        seeded_db, kisi, kule.project.id, (await _rol(seeded_db, "project_manager")).id
    )
    await ekibe_ekle(seeded_db, kisi, kopru.project.id, (await _rol(seeded_db, "viewer")).id)
    baslik = await _giris(client, "karma@izn.co")
    kopru_odeme, kopru_tas = kopru.payment.id, kopru.sub_payment.id
    kule_odeme, kule_tas = kule.payment.id, kule.sub_payment.id
    seeded_db.expunge_all()

    for yol in (
        f"/progress-payments/{kopru_odeme}/approve",
        f"/progress-payments/{kopru_odeme}/reject",
        f"/subcontractor-progress-payments/{kopru_tas}/approve",
        f"/subcontractor-progress-payments/{kopru_tas}/reject",
    ):
        yanit = await client.post(yol, headers=baslik, json={"reason": "x"})
        assert yanit.status_code in (403, 404), (yol, yanit.status_code, yanit.text)
    assert await _durum(seeded_db, ProgressPayment, kopru_odeme) is (
        ProgressPaymentStatus.pending_approval
    )
    assert await _durum(seeded_db, SubcontractorProgressPayment, kopru_tas) is (
        SubcontractorPaymentStatus.pending_approval
    )
    if cozucu == "kapali":
        return  # çözücü kapalıyken ekip rolü kapıyı açmaz (pozitif kontrol yalnız "acik" varyantı)
    # Pozitif kontrol: Kule'de aynı kişi Proje Müdürü → kapı GEÇER (403 değil).
    for yol in (
        f"/progress-payments/{kule_odeme}/approve",
        f"/subcontractor-progress-payments/{kule_tas}/approve",
    ):
        yanit = await client.post(yol, headers=baslik, json={})
        assert yanit.status_code != 403, (yol, yanit.text)


async def test_ana_rolunde_onaylar_olan_kisi_kopru_gunlugunu_acamaz_kule_gunlugunu_acar(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    """Orta-1: `POST /diary/{id}/reopen` kapısı yalnız şirket geneli kök sayfaya bakıyordu; proje
    içi ikizleri (73/88) de kapıda olmalı: Köprü'de rolü Görüntüleyici olan kişi Köprü günlüğünü
    açamaz."""
    kule, kopru = iki_proje
    guclu = await rol_kur(seeded_db, "izn_guclu", PageLevel.edit, approve=True)
    kisi = await user_factory(email="gunluk@izn.co", password=PASSWORD, role_key="izn_guclu")
    await ekibe_ekle(seeded_db, kisi, kule.project.id, guclu.id)
    await ekibe_ekle(seeded_db, kisi, kopru.project.id, (await _rol(seeded_db, "viewer")).id)
    baslik = await _giris(client, "gunluk@izn.co")
    kule_g, kopru_g = kule.entry.id, kopru.entry.id
    seeded_db.expunge_all()

    yanit = await client.post(f"/diary/{kopru_g}/reopen", headers=baslik)
    assert yanit.status_code in (403, 404), yanit.text
    assert await _durum(seeded_db, SiteDiaryEntry, kopru_g) is DiaryStatus.submitted
    acik = await client.post(f"/diary/{kule_g}/reopen", headers=baslik)
    assert acik.status_code == 200, acik.text
    assert await _durum(seeded_db, SiteDiaryEntry, kule_g) is DiaryStatus.draft


# --- Orta-2: gösterge paneli ---------------------------------------------------------------


async def test_panel_kopruda_rolu_depocu_olan_patron_icin_kopru_kartini_gostermez(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    """Ana rol Patron, Köprü'de Depo Sorumlusu (projeler/hakediş sayfaları Görmez) → panelde KOPRU
    YOK, KULE var. Proje kartı izni PROJE BAŞINA (eskiden ana rol: Köprü kartı da görünürdü)."""
    kule, kopru = iki_proje
    kisi = await user_factory(email="panel@izn.co", password=PASSWORD, role_key="patron")
    await ekibe_ekle(seeded_db, kisi, kule.project.id)  # ana rolle (Patron)
    await ekibe_ekle(
        seeded_db, kisi, kopru.project.id, (await _rol(seeded_db, "warehouse_keeper")).id
    )
    baslik = await _giris(client, "panel@izn.co")
    seeded_db.expunge_all()

    govde = (await client.get("/dashboard/summary", headers=baslik)).json()
    kodlar = {p["code"] for p in govde["projects"]}
    assert "KULE" in kodlar and "KOPRU" not in kodlar, kodlar
    assert govde["active_project_count"] == 1


# --- Orta-3: alan kapısı proje başına ------------------------------------------------------


async def test_kopruda_hakedis_gormez_olan_kisinin_kopru_kartinda_mali_ilerleme_restricted(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    """`can_read` PROJE BAŞINA: ana rol Patron (hakedişi görür) ama Köprü'deki rolü hakediş
    sayfalarını Görmez → Köprü kartında `financial_progress` "restricted"; Kule'de açık."""
    kule, kopru = iki_proje
    rol = await rol_kur(seeded_db, "izn_hakedissiz", PageLevel.view)
    hakedis_sayfalari = [s.key for s in SAYFALAR if s.eski_modul == "progress_payments"]
    await seeded_db.execute(
        update(RolePagePermission)
        .where(
            RolePagePermission.role_id == rol.id, RolePagePermission.page_key.in_(hakedis_sayfalari)
        )
        .values(level=PageLevel.none)
    )
    kisi = await user_factory(email="kart@izn.co", password=PASSWORD, role_key="patron")
    await ekibe_ekle(seeded_db, kisi, kule.project.id)
    await ekibe_ekle(seeded_db, kisi, kopru.project.id, rol.id)
    baslik = await _giris(client, "kart@izn.co")
    kule_id, kopru_id = kule.project.id, kopru.project.id
    seeded_db.expunge_all()

    liste = (await client.get("/projects", headers=baslik)).json()["items"]
    kartlar = {p["id"]: p["contracting"]["financial_progress"] for p in liste}
    # İzinli ama hakediş verisi YOK: `metric(None, modül)` → `pending_module` modülü adlandırır;
    # İZİNSİZ: `restricted()` → `pending_module=None` (iki FARKLI durum, karar 2026-08-27).
    assert kartlar[str(kule_id)]["pending_module"] == "progress_payments", kartlar
    assert kartlar[str(kopru_id)]["pending_module"] is None, kartlar
    assert kartlar[str(kopru_id)]["available"] is False
    detay = (await client.get(f"/projects/{kopru_id}", headers=baslik)).json()
    assert detay["contracting"]["financial_progress"]["pending_module"] is None


# --- Orta-4: slug ---------------------------------------------------------------------------


async def test_slug_ve_uuid_ayni_sonucu_verir_ve_slug_proje_baglamini_cozer(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    """Slug yolları UUID ile AYNI yanıtı verir; kapı slug'ı projeye çözer: ana rolü hiçbir şey
    olmayan ama Kule'de güçlü kişi Kule slug'ıyla yazar (403 değil), Köprü slug'ıyla 403 alır."""
    kule, kopru = iki_proje
    guclu = await rol_kur(seeded_db, "izn_guclu", PageLevel.edit, approve=True)
    hicbiri = await rol_kur(seeded_db, "izn_hicbiri", PageLevel.none)
    for ep in (kule, kopru):
        ad = ep.project.name.lower().replace("ö", "o")
        ep.project.slug = f"{ad}-projesi"
        ep.site.slug = f"{ad}-santiye"
        ep.section.slug = f"{ad}-bolum"
        ep.payment.slug = f"{ad}-projesi-1"
        ep.sub_payment.slug = f"{ad}-tsz-1"
    kisi = await user_factory(email="slug@izn.co", password=PASSWORD, role_key="izn_hicbiri")
    await ekibe_ekle(seeded_db, kisi, kule.project.id, guclu.id)
    await seeded_db.flush()
    assert hicbiri.id
    baslik = await _giris(client, "slug@izn.co")
    adaylar = []
    for ep in (kule, kopru):
        adaylar.append(
            {
                "proje": (str(ep.project.id), ep.project.slug),
                "santiye": (str(ep.site.id), ep.site.slug),
                "bolum": (str(ep.section.id), ep.section.slug),
                "hakedis": (str(ep.payment.id), ep.payment.slug),
                "tas": (str(ep.sub_payment.id), ep.sub_payment.slug),
            }
        )
    seeded_db.expunge_all()
    kule_k, kopru_k = adaylar

    sablonlar = {
        "proje": "/projects/{}",
        "santiye": "/sites/{}",
        "bolum": "/sections/{}",
        "hakedis": "/progress-payments/{}",
        "tas": "/subcontractor-progress-payments/{}",
    }
    for ad, sablon in sablonlar.items():
        uuid_yanit = await client.get(sablon.format(kule_k[ad][0]), headers=baslik)
        slug_yanit = await client.get(sablon.format(kule_k[ad][1]), headers=baslik)
        assert uuid_yanit.status_code == slug_yanit.status_code, (ad, uuid_yanit.status_code)
        assert uuid_yanit.json() == slug_yanit.json(), ad
    # `GET /projects/{slug}/contract/distribution` (UUID ve slug aynı durum)
    d_uuid = await client.get(
        f"/projects/{kule_k['proje'][0]}/contract/distribution", headers=baslik
    )
    d_slug = await client.get(
        f"/projects/{kule_k['proje'][1]}/contract/distribution", headers=baslik
    )
    assert d_uuid.status_code == d_slug.status_code, (d_uuid.text, d_slug.text)

    # Slug kapıda PROJEYE çözülür: ana rol yetersiz, Kule rolü güçlü.
    kule_yaz = await client.patch(f"/sites/{kule_k['santiye'][1]}", headers=baslik, json={})
    assert kule_yaz.status_code != 403, kule_yaz.text
    kopru_yaz = await client.patch(f"/sites/{kopru_k['santiye'][1]}", headers=baslik, json={})
    assert kopru_yaz.status_code == 403, kopru_yaz.text
    for yol in (
        f"/progress-payments/{kopru_k['hakedis'][1]}/approve",
        f"/subcontractor-progress-payments/{kopru_k['tas'][1]}/approve",
    ):
        assert (await client.post(yol, headers=baslik, json={})).status_code in (403, 404), yol


# --- Orta-5: liste uçlarında proje başına süzme ---------------------------------------------


async def test_P1de_disiplinli_P2de_kisitsiz_kisi_listede_P2_satirlarini_gorur_403_almaz(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    kule, kopru = iki_proje
    from app.modules.catalog.models import EvDiscipline
    from app.modules.earned_value.models import ContractorType

    disiplin = EvDiscipline(
        code="LST", name="Liste", color="#2563EB", default_contractor_type=ContractorType.OWN
    )
    seeded_db.add(disiplin)
    await seeded_db.flush()
    guclu = await rol_kur(seeded_db, "izn_guclu", PageLevel.edit, approve=True)
    kisi = await user_factory(email="liste@izn.co", password=PASSWORD, role_key="izn_guclu")
    await ekibe_ekle(seeded_db, kisi, kule.project.id, guclu.id)
    await ekibe_ekle(seeded_db, kisi, kopru.project.id, guclu.id)
    await disiplin_ata(seeded_db, kisi, kule.project.id, disiplin.id)  # Kule'de kısıtlı
    baslik = await _giris(client, "liste@izn.co")
    kule_odeme, kopru_odeme = kule.payment.id, kopru.payment.id
    kule_tas, kopru_tas = kule.sub_payment.id, kopru.sub_payment.id
    kopru_id, kule_id = kopru.project.id, kule.project.id
    seeded_db.expunge_all()

    liste = await client.get("/progress-payments", headers=baslik)
    assert liste.status_code == 200, liste.text
    assert {i["id"] for i in liste.json()["items"]} == {str(kopru_odeme)}  # Kule (kısıtlı) dışarıda
    tas = await client.get("/subcontractor-progress-payments", headers=baslik)
    assert tas.status_code == 200, tas.text
    assert {i["id"] for i in tas.json()["items"]} == {str(kopru_tas)}
    ozet = await client.get("/subcontractor-progress-payments/summary", headers=baslik)
    assert ozet.status_code == 200, ozet.text
    # Tek-proje ucu hâlâ 403 (kısıtlı projede hakediş kapalı); kısıtsız projede açık.
    assert (await client.get(f"/progress-payments/{kule_odeme}", headers=baslik)).status_code == 403
    assert (
        await client.get(f"/progress-payments/{kopru_odeme}", headers=baslik)
    ).status_code == 200
    assert kule_tas and kule_id and kopru_id


# --- Orta-6: PUT /users/{id}/access yetki yükseltme -----------------------------------------


async def _yonetici(client, session, user_factory, email: str):
    """`Kullanıcılar Düzenler` + her sayfa Görür (Sistem Yöneticisi DEĞİL) aktör."""
    rol = await rol_kur(session, f"yon_{uuid.uuid4().hex[:6]}", PageLevel.edit, approve=True)
    kisi = await user_factory(email=email, password=PASSWORD, role_key=rol.key)
    return kisi, await _giris(client, email)


def _govde(rol: Role, *, tum: bool = False, projeler: list | None = None) -> dict:
    return {"role_id": str(rol.id), "all_projects": tum, "projects": projeler or []}


async def test_sistem_yoneticisi_olmayan_aktor_kendi_erisimini_degistiremez(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    kule, _ = iki_proje
    kisi, baslik = await _yonetici(client, seeded_db, user_factory, "ben@izn.co")
    rol_id = kisi.role_id
    yeni = await _rol(seeded_db, "viewer")
    kisi_id = kisi.id
    seeded_db.expunge_all()
    yanit = await client.put(
        f"/users/{kisi_id}/access",
        json={
            "role_id": str(rol_id),
            "all_projects": False,
            "projects": [{"project_id": str(kule.project.id), "role_id": str(yeni.id)}],
        },
        headers=baslik,
    )
    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == (
        "Kendi erişiminizi değiştiremezsiniz; yalnızca Sistem Yöneticisi değiştirebilir"
    )


async def test_tum_projeler_isaretini_yalniz_sistem_yoneticisi_verir_ve_kaldirir(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    _kule, _ = iki_proje
    _yon, yonetici_baslik = await _yonetici(client, seeded_db, user_factory, "yon@izn.co")
    admin = await user_factory(email="admin@izn.co", password=PASSWORD, role_key="system_admin")
    hedef = await user_factory(email="hedef@izn.co", password=PASSWORD, role_key="project_manager")
    tumlu = await user_factory(email="tumlu@izn.co", password=PASSWORD, role_key="project_manager")
    await tum_projeler(seeded_db, tumlu)
    rol = await _rol(seeded_db, "project_manager")
    admin_baslik = await _giris(client, "admin@izn.co")
    hedef_id, tumlu_id, rol_id = hedef.id, tumlu.id, rol.id
    assert admin.id
    seeded_db.expunge_all()

    govde = {"role_id": str(rol_id), "all_projects": True, "projects": []}
    ver = await client.put(f"/users/{hedef_id}/access", json=govde, headers=yonetici_baslik)
    assert ver.status_code == 403, ver.text
    assert ver.json()["detail"] == (
        "'Tüm projeler' işaretini yalnızca Sistem Yöneticisi verebilir ya da kaldırabilir"
    )
    kaldir = await client.put(
        f"/users/{tumlu_id}/access",
        json={**govde, "all_projects": False},
        headers=yonetici_baslik,
    )
    assert kaldir.status_code == 403, kaldir.text
    # Sistem Yöneticisi ikisini de yapar (pozitif kontrol).
    assert (
        await client.put(f"/users/{hedef_id}/access", json=govde, headers=admin_baslik)
    ).status_code == 200
    assert (
        await client.put(
            f"/users/{tumlu_id}/access", json={**govde, "all_projects": False}, headers=admin_baslik
        )
    ).status_code == 200


async def test_hedef_sistem_yoneticisiyse_ana_rolunu_yalniz_sistem_yoneticisi_degistirir(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    _yon, yonetici_baslik = await _yonetici(client, seeded_db, user_factory, "yon2@izn.co")
    await user_factory(email="admin1@izn.co", password=PASSWORD, role_key="system_admin")
    ikinci = await user_factory(email="admin2@izn.co", password=PASSWORD, role_key="system_admin")
    sef = await _rol(seeded_db, "site_chief")
    ikinci_id, sef_id = ikinci.id, sef.id
    seeded_db.expunge_all()
    mesaj = "Sistem Yöneticisi'nin ana rolünü yalnızca Sistem Yöneticisi değiştirebilir"

    via_access = await client.put(
        f"/users/{ikinci_id}/access", json=_govde_id(sef_id), headers=yonetici_baslik
    )
    assert via_access.status_code == 403, via_access.text
    assert via_access.json()["detail"] == mesaj
    via_patch = await client.patch(
        f"/users/{ikinci_id}", json={"role_id": str(sef_id)}, headers=yonetici_baslik
    )
    assert via_patch.status_code == 403, via_patch.text
    assert via_patch.json()["detail"] == mesaj


def _govde_id(rol_id: uuid.UUID) -> dict:
    return {"role_id": str(rol_id), "all_projects": False, "projects": []}


async def test_sistem_yoneticisine_ekip_satiri_yazilmaz_ve_me_bos_doner(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    kule, _ = iki_proje
    admin = await user_factory(email="a1@izn.co", password=PASSWORD, role_key="system_admin")
    ikinci = await user_factory(email="a2@izn.co", password=PASSWORD, role_key="system_admin")
    sisyon = await _rol(seeded_db, "system_admin")
    sef = await _rol(seeded_db, "site_chief")
    admin_baslik = await _giris(client, "a1@izn.co")
    ikinci_baslik = await _giris(client, "a2@izn.co")
    ikinci_id, sisyon_id, sef_id, kule_id = ikinci.id, sisyon.id, sef.id, kule.project.id
    assert admin.id
    seeded_db.expunge_all()

    yanit = await client.put(
        f"/users/{ikinci_id}/access",
        json={
            "role_id": str(sisyon_id),
            "all_projects": False,
            "projects": [{"project_id": str(kule_id), "role_id": str(sef_id)}],
        },
        headers=admin_baslik,
    )
    assert yanit.status_code == 422, yanit.text
    assert (
        yanit.json()["detail"]
        == "Sistem Yöneticisi her projeyi görür; proje ekibi satırı eklenemez"
    )
    # Bayat ekip satırı olsa bile /auth/me Sistem Yöneticisi için boş döner.
    seeded_db.add(ProjectMember(user_id=ikinci_id, project_id=kule_id, role_id=sef_id))
    await seeded_db.flush()
    seeded_db.expunge_all()
    me = (await client.get("/auth/me", headers=ikinci_baslik)).json()
    assert me["projects"] == [] and me["role_pages"] == {}
    assert isinstance(await seeded_db.get(User, ikinci_id), User)


# --- bağlam çözümü: gövde + visible_projects bekçisi -----------------------------------------


async def test_govdedeki_site_id_proje_baglamini_cozer_ekip_rolu_yalniz_o_projede_gecerli(
    client: AsyncClient, seeded_db, user_factory, iki_proje
) -> None:
    """Oluşturma uçlarında proje yol parametresinde değil GÖVDEDEDİR (`site_id`): ana rolü yetersiz
    ama Kule'de güçlü kişi Kule şantiyesi için kapıyı geçer (422 = gövde doğrulaması), Köprü
    şantiyesi için 403 alır."""
    kule, kopru = iki_proje
    guclu = await rol_kur(seeded_db, "izn_guclu", PageLevel.edit, approve=True)
    await rol_kur(seeded_db, "izn_hicbiri", PageLevel.none)
    kisi = await user_factory(email="govde@izn.co", password=PASSWORD, role_key="izn_hicbiri")
    await ekibe_ekle(seeded_db, kisi, kule.project.id, guclu.id)
    baslik = await _giris(client, "govde@izn.co")
    kule_site, kopru_site = str(kule.site.id), str(kopru.site.id)
    seeded_db.expunge_all()

    kule_yanit = await client.post("/stock/entries", json={"site_id": kule_site}, headers=baslik)
    assert kule_yanit.status_code != 403, kule_yanit.text
    kopru_yanit = await client.post("/stock/entries", json={"site_id": kopru_site}, headers=baslik)
    assert kopru_yanit.status_code == 403, kopru_yanit.text
    bos = await client.post("/stock/entries", json={}, headers=baslik)
    assert bos.status_code == 403, bos.text  # bağlam yok → yalnız ana rol (hiçbir şey)


async def test_visible_projects_http_icinde_kapisiz_cagrida_hata_atar_uretimde_fail_closed(
    seeded_db, user_factory, project_factory, monkeypatch
) -> None:
    """Üyelik tek başına yetki DEĞİLDİR: HTTP isteği (bağlam açık) içinde kapı geçilmeden ve `pairs`
    verilmeden `visible_projects` test/dev'de HATA atar, üretimde BOŞ döner; `pairs` verilince ya da
    kapı geçince çalışır; HTTP DIŞI doğrudan çağrıda yalnız üyelik (test/betik)."""
    from app.core.access import AccessLevel
    from app.core.config import settings
    from app.core.gate_context import (
        GATE_KEY,
        GateContext,
        MissingGateContextError,
        record_gate,
    )
    from app.core.page_gate import gate_flags
    from app.modules.projects.service import visible_projects

    proje = await project_factory("BKC")
    kisi = await user_factory(email="bekci@izn.co", password=PASSWORD, role_key="site_chief")
    await ekibe_ekle(seeded_db, kisi, proje.id)
    # HTTP DIŞI: bağlam yok → yalnız üyelik
    assert [p.id for p in await visible_projects(seeded_db, kisi)] == [proje.id]
    seeded_db.info[GATE_KEY] = GateContext()  # HTTP isteği başladı, kapı geçilmedi
    try:
        with pytest.raises(MissingGateContextError):
            await visible_projects(seeded_db, kisi)
        monkeypatch.setattr(settings, "environment", "production")
        assert await visible_projects(seeded_db, kisi) == []  # üretim: fail-closed
        monkeypatch.undo()
        pairs = gate_flags("progress_payments", AccessLevel.view)
        assert [p.id for p in await visible_projects(seeded_db, kisi, pairs=pairs)] == [proje.id]
        record_gate(seeded_db, ())  # şirket geneli kapı geçti → üyelik yeterli
        assert [p.id for p in await visible_projects(seeded_db, kisi)] == [proje.id]
    finally:
        seeded_db.info.pop(GATE_KEY, None)
