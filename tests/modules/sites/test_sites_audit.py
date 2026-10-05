"""T12 — santiye/bolum denetim gunlugu (spec §10).

## Neden ayri bir test dosyasi

Denetim gunlugunun iki kolay kacirilan ayrintisi vardir ve ikisi de SESSIZDIR:

1. **Silme metni satir yok olmadan ONCE kurulmalidir** (`units/service.py:327`
   dersi). Sonra kurulursa `name` alanlari okunamaz ve denetim satiri BOS ADLA
   yazilir — yani silinen kaydin NE OLDUGU tamamen kaybolur. Testte bunu yakalayan
   tek sey, metnin silinen santiyenin adini GERCEKTEN tasimasidir.
2. **Reddedilen istek denetim yazmaz.** Denetim gerceklesen olayi kaydeder,
   denemeyi degil; 409/422 sonrasi gunluge satir dusmesi, olmamis bir islemi
   olmus gibi gosterir.

Bolumlu oluşturmada bolum basina AYRI satir yazilmaz: 5 bolumlu bir form 6 satir
degil 2 satir uretir (`units_bulk_created` deseni).
"""

import uuid

from sqlalchemy import func, select

from app.modules.audit.messages import (
    section_created,
    section_deleted,
    section_updated,
    site_created,
    site_deleted,
    site_draft_created,
    site_published,
    site_sections_created,
    site_updated,
)
from app.modules.audit.models import AuditAction, AuditLog
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.sites.models import Section, Site
from tests._silme_yardimci import onizle, sil_aile
from tests.modules.silme import _dunya as d

_IP = "203.0.113.42"
_IP_HEADER = {"x-forwarded-for": _IP}


async def _admin(client, session, user_factory) -> dict[str, str]:
    """`system_admin`: silme kapisi (`sites:admin`) YALNIZ bu rolde acik."""
    address = f"admin-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key="system_admin")
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}", **_IP_HEADER}


async def _rows(session, action: AuditAction) -> list[AuditLog]:
    stmt = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.occurred_at)
    return list((await session.execute(stmt)).scalars().all())


async def _details(session, action: AuditAction) -> list[str]:
    return [row.detail for row in await _rows(session, action)]


async def _total(session) -> int:
    """Giris satiri DAHIL tum denetim satirlari (okuma uclari icin taban olcum)."""
    return int((await session.execute(select(func.count()).select_from(AuditLog))).scalar_one())


async def _site(session, project, code: str = "A-BLOK", **kwargs) -> Site:
    site = Site(project_id=project.id, code=code, name=kwargs.pop("name", "A-Blok Şantiyesi"))
    for field, value in kwargs.items():
        setattr(site, field, value)
    session.add(site)
    await session.flush()
    return site


def _publishable(**overrides) -> dict:
    body = {
        "name": "A-Blok Şantiyesi",
        "site_manager_name": "Selim Öz",
        "city": "Ankara",
        "construction_area_m2": "12000.00",
        "start_date": "2026-03-01",
        "end_date": "2027-03-01",
    }
    body.update(overrides)
    return body


# --- Olusturma ---


async def test_site_create_writes_site_created(client, db_session, user_factory, project_factory):
    """Mevcut metin KORUNUR: yayin olusturma "Yeni şantiye oluşturuldu"."""
    project = await project_factory("AU-1")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.post(f"/projects/{project.id}/sites", json=_publishable(), headers=headers)

    assert resp.status_code == 201, resp.text
    assert await _details(db_session, AuditAction.create) == [site_created("A-Blok Şantiyesi")]
    assert site_created("X") == "Yeni şantiye oluşturuldu: X"


async def test_draft_create_writes_site_draft_created(
    client, db_session, user_factory, project_factory
):
    """Taslak ile yayin AYRI metinlerdir: denetim ekraninda "gercekten santiye
    acildi mi" sorusu metinden cevaplanabilmelidir (spec §10)."""
    project = await project_factory("AU-2")
    headers = await _admin(client, db_session, user_factory)

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json={"name": "Taslak Şantiye", "is_draft": True},
        headers=headers,
    )

    assert resp.status_code == 201, resp.text
    details = await _details(db_session, AuditAction.create)
    assert details == [site_draft_created("Taslak Şantiye")]
    assert site_draft_created("X") == "Yeni şantiye taslağı oluşturuldu: X"
    assert site_created("Taslak Şantiye") not in details


async def test_sections_create_writes_one_summary_row(
    client, db_session, user_factory, project_factory
):
    """5 bolumlu form 6 satir DEGIL 2 satir uretir (`units_bulk_created` deseni).

    🔴 TB7 T1 (K2-yeni, yonetim karari): iddia SAYI + ICERIKTIR, SIRA degil. Bu
    iki satir AYNI istekte, AYNI transaction'da yazilir; `occurred_at` transaction
    BASLANGICIdir (`func.now()`), yani ikisi BIREBIR ayni damgayi tasir ve
    Postgres esit anahtarlar icin sira GARANTI ETMEZ (olculdu). Hangi satirin
    once goruntulendigi kullaniciya hicbir sey ifade etmiyor — urunun kendisi de
    bu ikisini tanimsiz sirada gosteriyor. Bu yuzden `sorted(...)` ile SIRA
    serbest, SAYI ve ICERIK sabit tutulur. `set()` KULLANILMAZ: kume "ayni metin
    iki kez yazildi" kusurunu sessizce yutar, oysa testin asil iddiasi tam da
    satir SAYISIdir.
    """
    project = await project_factory("AU-3")
    headers = await _admin(client, db_session, user_factory)
    sections = [{"name": f"Faz {index}"} for index in range(1, 6)]

    resp = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable(sections=sections),
        headers=headers,
    )

    assert resp.status_code == 201, resp.text
    details = await _details(db_session, AuditAction.create)
    assert len(details) == 2
    assert sorted(details) == sorted(
        [
            site_created("A-Blok Şantiyesi"),
            site_sections_created("A-Blok Şantiyesi", 5),
        ]
    )
    assert site_sections_created("X", 5) == "Şantiye bölümleri oluşturuldu: X · 5 bölüm"


# --- Guncelleme / yayina alma ---


async def test_patch_writes_site_updated(client, db_session, user_factory, project_factory):
    project = await project_factory("AU-4")
    site = await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)

    resp = await client.patch(f"/sites/{site.id}", json={"name": "Yeni Ad"}, headers=headers)

    assert resp.status_code == 200, resp.text
    assert await _details(db_session, AuditAction.update) == [site_updated("Yeni Ad")]


async def test_publish_writes_site_published(client, db_session, user_factory, project_factory):
    project = await project_factory("AU-5")
    site = await _site(db_session, project, is_draft=True)
    headers = await _admin(client, db_session, user_factory)

    resp = await client.patch(
        f"/sites/{site.id}",
        json={
            "is_draft": False,
            "site_manager_name": "Selim Öz",
            "city": "Ankara",
            "construction_area_m2": "12000.00",
            "start_date": "2026-03-01",
            "end_date": "2027-03-01",
        },
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    details = await _details(db_session, AuditAction.update)
    assert details == [site_published("A-Blok Şantiyesi")]
    assert site_published("X") == "Şantiye taslaktan yayına alındı: X"
    assert site_updated("A-Blok Şantiyesi") not in details


# --- Silme ---


async def test_delete_site_writes_site_deleted(client, db_session, user_factory, project_factory):
    project = await project_factory("AU-6", name="Güneşkent")
    site = await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)

    resp = await sil_aile(client, headers, "site", site.id)

    assert resp.status_code == 204
    assert await _details(db_session, AuditAction.delete) == [
        site_deleted("Güneşkent", "A-Blok Şantiyesi")
    ]
    assert site_deleted("P", "S") == "Şantiye silindi: P · S"


async def test_delete_site_audit_text_contains_deleted_site_name(
    client, db_session, user_factory, project_factory
):
    """S6 — metnin SILMEDEN ONCE kuruldugunun kanti.

    Metin `session.delete` sonrasi kurulsaydi ad alanlari okunamaz olur ve satir
    bos adla yazilirdi; asagidaki iki assert tam olarak o durumu yakalar.
    """
    project = await project_factory("AU-7", name="Güneşkent")
    site = await _site(db_session, project, name="Kuzey Şantiyesi")
    headers = await _admin(client, db_session, user_factory)

    resp = await sil_aile(client, headers, "site", site.id)

    assert resp.status_code == 204
    detail = (await _details(db_session, AuditAction.delete))[0]
    assert "Kuzey Şantiyesi" in detail
    assert "Güneşkent" in detail
    assert not detail.endswith("· ")


async def test_delete_section_writes_section_deleted(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("AU-8")
    site = await _site(db_session, project)
    section = Section(site_id=site.id, name="Kaba İnşaat")
    db_session.add(section)
    await db_session.flush()
    headers = await _admin(client, db_session, user_factory)

    resp = await sil_aile(client, headers, "section", section.id)

    assert resp.status_code == 204
    assert await _details(db_session, AuditAction.delete) == [
        section_deleted("A-Blok Şantiyesi", "Kaba İnşaat")
    ]
    assert section_deleted("S", "B") == "Bölüm silindi: S · B"


# --- Reddedilen istekler ve okuma uclari YAZMAZ ---


async def test_failed_delete_writes_no_audit(client, db_session, user_factory, project_factory):
    """S7 — reddedilen silme (bayat önizleme → 409 `preview_stale`) günlüğe HİÇBİR ŞEY yazmaz.

    SIL-B2: mali bağlı kayıt artık silmeyi engellemez; reddi doğuran tek önkoşul önizlemedir.
    """
    project = await project_factory("AU-9")
    site = await _site(db_session, project)
    olusturan = await user_factory(
        email="olusturan@au9.co", password="parola1234", role_key="site_chief"
    )
    await d.hakedis_satiri(
        db_session, project, site, olusturan, durum=ProgressPaymentStatus.approved
    )
    headers = await _admin(client, db_session, user_factory)
    eski = (await onizle(client, headers, "site", site.id)).json()["preview_token"]
    await d.section(db_session, site, "Sonradan eklenen")  # ağaç değişti

    resp = await client.delete(f"/sites/{site.id}", params={"preview_token": eski}, headers=headers)

    assert resp.status_code == 409
    assert resp.json()["code"] == "preview_stale"
    assert await _rows(db_session, AuditAction.delete) == []


async def test_failed_create_writes_no_audit(client, db_session, user_factory, project_factory):
    """422 (eksik zorunlu alan) ve 409 (kod cakismasi) ile reddedilen POST'lar."""
    project = await project_factory("AU-10")
    await _site(db_session, project, "SNT-KOD")
    headers = await _admin(client, db_session, user_factory)

    rejected_422 = await client.post(
        f"/projects/{project.id}/sites", json={"name": "Eksik Şantiye"}, headers=headers
    )
    rejected_409 = await client.post(
        f"/projects/{project.id}/sites",
        json=_publishable(code="SNT-KOD"),
        headers=headers,
    )

    assert rejected_422.status_code == 422
    assert rejected_409.status_code == 409
    assert await _rows(db_session, AuditAction.create) == []


async def test_read_endpoints_write_no_audit(client, db_session, user_factory, project_factory):
    project = await project_factory("AU-11")
    site = await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)
    before = await _total(db_session)

    assert (await client.get(f"/projects/{project.id}/sites", headers=headers)).status_code == 200
    assert (await client.get(f"/sites/{site.id}", headers=headers)).status_code == 200
    assert (await client.get(f"/sites/{site.id}/sections", headers=headers)).status_code == 200

    assert await _total(db_session) == before


async def test_audit_rows_carry_actor_and_ip(client, db_session, user_factory, project_factory):
    project = await project_factory("AU-12")
    site = await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)

    resp = await sil_aile(client, headers, "site", site.id)

    assert resp.status_code == 204
    rows = await _rows(db_session, AuditAction.delete)
    assert len(rows) == 1
    assert rows[0].actor_user_id is not None
    # INET sutunu `ipaddress` nesnesi dondurur; karsilastirma metne cevrilerek yapilir.
    assert str(rows[0].ip_address) == _IP


# --- Mevcut metinler DEGISMEDI ---


async def test_section_created_updated_messages_unchanged(
    client, db_session, user_factory, project_factory
):
    project = await project_factory("AU-13")
    site = await _site(db_session, project)
    headers = await _admin(client, db_session, user_factory)

    created = await client.post(
        # `is_draft` P6 T3'te eklendi: denetim METNI taslak/yayin ayrimi TASIMAZ
        # (T3 karari — taslak icin ayri `AuditAction` acilmaz), dolayisiyla bu
        # test icin taslak govdesi yeterlidir ve `Form - Bolum Ekle`in zorunlu
        # alanlarini buraya tasimaz.
        f"/sites/{site.id}/sections",
        json={"name": "Kaba İnşaat", "is_draft": True},
        headers=headers,
    )
    section_id = created.json()["id"]
    updated = await client.patch(
        f"/sections/{section_id}", json={"name": "İnce İşler"}, headers=headers
    )

    assert created.status_code == 201, created.text
    assert updated.status_code == 200, updated.text
    assert await _details(db_session, AuditAction.create) == [
        section_created("A-Blok Şantiyesi", "Kaba İnşaat")
    ]
    assert await _details(db_session, AuditAction.update) == [
        section_updated("A-Blok Şantiyesi", "İnce İşler")
    ]
    assert section_created("S", "B") == "Yeni bölüm oluşturuldu: S · B"
    assert section_updated("S", "B") == "Bölüm güncellendi: S · B"
