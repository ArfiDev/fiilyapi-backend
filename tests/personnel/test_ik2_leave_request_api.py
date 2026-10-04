"""İK-2 T2 — izin talebi CRUD uçtan uca (HTTP).

Spec: `docs/superpowers/specs/2026-08-12-ik2-izin-yonetimi-design.md` §3, §5 K2/K3.

Fixtures kısmen `conftest.py`de (login başlıkları). Testler `create_all` şemasıyla
koşar (migration DEĞİL) — `leave_types` SEED'i migration'dadır, o yüzden tipler
BURADA elle açılır.

Kapılar (`personnel` modülü, İK-1 emsali): okuma `view`, yazma `full`. SİLME
`full`u DEĞİL, ya `admin`i ya da KAYIT SAHİPLİĞİNİ ister (spec §3 "pending, sahibi
ya da admin") — bu yüzden DELETE'in router kapısı `view`dir ve gerçek karar
serviste verilir.

⚠️ `days` ve `status` İSTEMCİDEN ALINMAZ (spec §5 K2): şema `extra="forbid"`
olduğundan gönderilmeleri sessizce yok sayılmaz, açıkça 422 olur.

⚠️ approve/reject T3'ün işidir — bu dosyada YOKTUR. Çakışma (K3) kuralı da T3'te
409'a çevrilir; T2 yalnız yardımcıyı hazırlar (`test_ik2_leave_service.py`).
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.documents.models import Document
from app.modules.personnel.models import LeaveRequest, LeaveStatus, LeaveType, Personnel
from app.modules.projects.models import Project
from app.modules.site_diary.models import WorkerSource
from app.modules.users.models import User
from tests.conftest import test_engine


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="IK2-T2-A", name="İzin Projesi")


@pytest.fixture
async def diger_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="IK2-T2-B", name="Öteki Proje")


@pytest.fixture
async def personel(seeded_db: AsyncSession, proje: Project) -> Personnel:
    kayit = Personnel(
        full_name="Mehmet Yılmaz",
        trade="Kalıpçı Usta",
        source=WorkerSource.company,
        assigned_project_id=proje.id,
    )
    seeded_db.add(kayit)
    await seeded_db.flush()
    return kayit


@pytest.fixture
async def diger_personel(seeded_db: AsyncSession, diger_proje: Project) -> Personnel:
    kayit = Personnel(
        full_name="Ali Kaya",
        trade="Demir Ustası",
        source=WorkerSource.company,
        assigned_project_id=diger_proje.id,
    )
    seeded_db.add(kayit)
    await seeded_db.flush()
    return kayit


@pytest.fixture
async def yillik(seeded_db: AsyncSession) -> LeaveType:
    tip = LeaveType(name="Yıllık İzin", deducts_from_annual=True, color="#2563eb", sort_order=1)
    seeded_db.add(tip)
    await seeded_db.flush()
    return tip


@pytest.fixture
async def hastalik(seeded_db: AsyncSession) -> LeaveType:
    tip = LeaveType(name="Hastalık İzni", requires_document=True, color="#dc2626", sort_order=2)
    seeded_db.add(tip)
    await seeded_db.flush()
    return tip


@pytest.fixture
async def pasif_tip(seeded_db: AsyncSession) -> LeaveType:
    tip = LeaveType(name="Ücretsiz (kapalı)", is_active=False, sort_order=9)
    seeded_db.add(tip)
    await seeded_db.flush()
    return tip


@pytest.fixture
async def arsiv_belgesi(seeded_db: AsyncSession, proje: Project) -> Document:
    """BC arşivinde `proje` kapsamında bir belge — `hr_manager` bu projeyi GÖRMEZ."""
    belge = Document(
        project_id=proje.id, filename="rapor.pdf", mime_type="application/pdf", size_bytes=10
    )
    seeded_db.add(belge)
    await seeded_db.flush()
    return belge


def _govde(personel: Personnel, tip: LeaveType, **ekstra) -> dict:
    return {
        "personnel_id": str(personel.id),
        "leave_type_id": str(tip.id),
        "start_date": "2026-08-04",
        "end_date": "2026-08-08",
        **ekstra,
    }


async def _talep_olustur(client, headers, personel, tip, **ekstra) -> str:
    yanit = await client.post(
        "/leave-requests", json=_govde(personel, tip, **ekstra), headers=headers
    )
    assert yanit.status_code == 201, yanit.text
    return yanit.json()["id"]


# --- POST: sunucu gün hesabı + istemci alanı reddi ---------------------------


@pytest.mark.asyncio
async def test_post_201_gun_sunucu_hesabi_ve_pending(client, ik_headers, personel, yillik):
    """04-08 Ağustos = 5 TAKVİM günü (mockup İZ satırı); durum HER ZAMAN `pending`."""
    yanit = await client.post(
        "/leave-requests", json=_govde(personel, yillik, note="Aile ziyareti"), headers=ik_headers
    )
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["days"] == 5
    assert govde["status"] == "pending"
    assert govde["decided_by"] is None
    assert govde["decided_at"] is None
    assert govde["note"] == "Aile ziyareti"
    # İZ tablosu künyesi
    assert govde["personnel_name"] == "Mehmet Yılmaz"
    assert govde["personnel_trade"] == "Kalıpçı Usta"
    assert govde["leave_type_name"] == "Yıllık İzin"
    assert govde["leave_type_color"] == "#2563eb"
    assert govde["deducts_from_annual"] is True


@pytest.mark.asyncio
async def test_post_tek_gun_1(client, ik_headers, personel, yillik):
    yanit = await client.post(
        "/leave-requests",
        json=_govde(personel, yillik, start_date="2026-07-31", end_date="2026-07-31"),
        headers=ik_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["days"] == 1


@pytest.mark.asyncio
async def test_post_days_gonderilirse_422(client, ik_headers, personel, yillik):
    """`days` SUNUCU hesabıdır (K2) — istemci gönderemez, SESSİZCE YOK SAYILMAZ."""
    yanit = await client.post(
        "/leave-requests", json=_govde(personel, yillik, days=99), headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_status_gonderilirse_422(client, ik_headers, personel, yillik):
    yanit = await client.post(
        "/leave-requests", json=_govde(personel, yillik, status="approved"), headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_karar_alanlari_gonderilirse_422(client, ik_headers, personel, yillik):
    yanit = await client.post(
        "/leave-requests",
        json=_govde(personel, yillik, reject_reason="olmaz"),
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_bitis_baslangictan_once_422(client, ik_headers, personel, yillik):
    yanit = await client.post(
        "/leave-requests",
        json=_govde(personel, yillik, start_date="2026-08-08", end_date="2026-08-04"),
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


# --- POST: varlık referansları (404/422 kanonu) ------------------------------


@pytest.mark.asyncio
async def test_post_var_olmayan_personel_404(client, ik_headers, yillik):
    yanit = await client.post(
        "/leave-requests",
        json={
            "personnel_id": str(uuid.uuid4()),
            "leave_type_id": str(yillik.id),
            "start_date": "2026-08-04",
            "end_date": "2026-08-08",
        },
        headers=ik_headers,
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_post_var_olmayan_izin_tipi_404(client, ik_headers, personel):
    yanit = await client.post(
        "/leave-requests",
        json={
            "personnel_id": str(personel.id),
            "leave_type_id": str(uuid.uuid4()),
            "start_date": "2026-08-04",
            "end_date": "2026-08-08",
        },
        headers=ik_headers,
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_post_pasif_izin_tipi_422(client, ik_headers, personel, pasif_tip):
    """Pasif tip 404 DEĞİL 422: kayıt vardır, engelleyen düzeltilebilir bir DURUMdur."""
    yanit = await client.post(
        "/leave-requests", json=_govde(personel, pasif_tip), headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_post_gorunmeyen_bc_belge_404_idor(
    client, ik_headers, personel, hastalik, arsiv_belgesi
):
    """`hr_manager` `arsiv_belgesi`nin projesini GÖRMEZ → gövde içi varlık ref 404."""
    yanit = await client.post(
        "/leave-requests",
        json=_govde(personel, hastalik, document_id=str(arsiv_belgesi.id)),
        headers=ik_headers,
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_post_gorunur_bc_belge_201(client, admin_headers, personel, hastalik, arsiv_belgesi):
    yanit = await client.post(
        "/leave-requests",
        json=_govde(personel, hastalik, document_id=str(arsiv_belgesi.id)),
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["document_id"] == str(arsiv_belgesi.id)


# --- Rol kapıları ------------------------------------------------------------


@pytest.mark.asyncio
async def test_post_yetkisiz_403(client, yetkisiz_headers, personel, yillik):
    yanit = await client.post(
        "/leave-requests", json=_govde(personel, yillik), headers=yetkisiz_headers
    )
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_post_sef_403_view_yazamaz(client, sef_headers, personel, yillik):
    yanit = await client.post("/leave-requests", json=_govde(personel, yillik), headers=sef_headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_liste_yetkisiz_403(client, yetkisiz_headers):
    yanit = await client.get("/leave-requests", headers=yetkisiz_headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_liste_sef_200_okur(client, sef_headers):
    yanit = await client.get("/leave-requests", headers=sef_headers)
    assert yanit.status_code == 200, yanit.text


# --- GET liste: süzgeçler + sayfalama (TB3) ----------------------------------


@pytest.mark.asyncio
async def test_liste_bos_zarf(client, ik_headers):
    yanit = await client.get("/leave-requests", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.asyncio
async def test_liste_personnel_id_suzgeci(client, ik_headers, personel, diger_personel, yillik):
    await _talep_olustur(client, ik_headers, personel, yillik)
    await _talep_olustur(client, ik_headers, diger_personel, yillik)
    yanit = await client.get(f"/leave-requests?personnel_id={personel.id}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["total"] == 1
    assert govde["items"][0]["personnel_id"] == str(personel.id)


@pytest.mark.asyncio
async def test_liste_project_id_suzgeci(
    client, ik_headers, personel, diger_personel, yillik, proje
):
    """`project_id` PERSONELİN projesi üzerinden daraltır (personelde proje kolonu var)."""
    await _talep_olustur(client, ik_headers, personel, yillik)
    await _talep_olustur(client, ik_headers, diger_personel, yillik)
    yanit = await client.get(f"/leave-requests?project_id={proje.id}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["total"] == 1
    assert govde["items"][0]["personnel_id"] == str(personel.id)


@pytest.mark.asyncio
async def test_liste_status_suzgeci(client, ik_headers, seeded_db, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    onayli = LeaveRequest(
        personnel_id=personel.id,
        leave_type_id=yillik.id,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
        days=3,
        status=LeaveStatus.approved,
    )
    seeded_db.add(onayli)
    await seeded_db.flush()

    bekleyen = await client.get("/leave-requests?status=pending", headers=ik_headers)
    assert bekleyen.status_code == 200, bekleyen.text
    assert bekleyen.json()["total"] == 1
    assert bekleyen.json()["items"][0]["id"] == talep_id

    hepsi = await client.get("/leave-requests", headers=ik_headers)
    assert hepsi.json()["total"] == 2


@pytest.mark.asyncio
async def test_liste_sayfalama(client, ik_headers, personel, yillik):
    for _ in range(3):
        await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.get("/leave-requests?limit=2&offset=0", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["total"] == 3
    assert len(govde["items"]) == 2
    assert govde["limit"] == 2 and govde["offset"] == 0


@pytest.mark.asyncio
async def test_liste_limit_tavani_422(client, ik_headers):
    """TB3 korkuluğu: tavanı aşan `limit` sessizce kırpılmaz, 422 olur."""
    yanit = await client.get("/leave-requests?limit=5000", headers=ik_headers)
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_liste_n_plus_1_yok(client, ik_headers, personel, yillik):
    """Liste künyeyi (personel + tip) TEK JOIN'le getirir; satır başına ayrı SELECT yok."""
    for _ in range(3):
        await _talep_olustur(client, ik_headers, personel, yillik)

    standalone: list[str] = []

    def _yakala(conn, cursor, statement, parameters, context, executemany):
        dusuk = statement.lower()
        if "from leave_types" in dusuk or "from personnel " in dusuk:
            standalone.append(statement)

    event.listen(test_engine.sync_engine, "before_cursor_execute", _yakala)
    try:
        yanit = await client.get("/leave-requests", headers=ik_headers)
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", _yakala)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["total"] == 3
    assert standalone == [], f"N+1: künye tablolarına standalone SELECT gitti: {standalone}"


# --- GET tek kayıt -----------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tek_kayit_200(client, ik_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.get(f"/leave-requests/{talep_id}", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["personnel_name"] == "Mehmet Yılmaz"


@pytest.mark.asyncio
async def test_get_tek_kayit_yok_404(client, ik_headers):
    yanit = await client.get(f"/leave-requests/{uuid.uuid4()}", headers=ik_headers)
    assert yanit.status_code == 404, yanit.text


# --- PATCH: yalnız `pending` -------------------------------------------------


@pytest.mark.asyncio
async def test_patch_tarih_degisince_days_yeniden_hesap(client, ik_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(
        f"/leave-requests/{talep_id}", json={"end_date": "2026-08-06"}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["days"] == 3


@pytest.mark.asyncio
async def test_patch_yalniz_not_days_degismez(client, ik_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(
        f"/leave-requests/{talep_id}", json={"note": "güncel"}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["days"] == 5
    assert yanit.json()["note"] == "güncel"


@pytest.mark.asyncio
async def test_patch_days_gonderilirse_422(client, ik_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(f"/leave-requests/{talep_id}", json={"days": 1}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_status_gonderilirse_422(client, ik_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(
        f"/leave-requests/{talep_id}", json={"status": "approved"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_ters_tarih_422(client, ik_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(
        f"/leave-requests/{talep_id}", json={"end_date": "2026-08-01"}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
@pytest.mark.parametrize("alan", ["start_date", "end_date", "leave_type_id"])
async def test_patch_zorunlu_alan_null_422(client, ik_headers, personel, yillik, alan):
    """NOT NULL kolonlara AÇIK `null` gönderilirse 422 — 500 DEĞİL.

    Şema `date | None = None` olduğundan `{"start_date": null}` geçerdi;
    `exclude_unset=True` açık null'ı KORUR, servisteki `or` deseni onu
    "gönderilmedi" sanıp tarih sırasını ESKİ değerle denetlerdi, ardından
    `setattr` NOT NULL kolona None yazıp `calculate_leave_days(None, date)`
    çağrılırdı → yakalayıcısı olmayan `TypeError` (flush'tan ÖNCE, yani DB
    NOT NULL kısıtı HİÇ devreye girmezdi).

    Kayıt DEĞİŞMEMİŞ olmalı: doğrulamaların hepsi yazmadan önce koşar.
    """
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(f"/leave-requests/{talep_id}", json={alan: None}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text

    sonra = await client.get(f"/leave-requests/{talep_id}", headers=ik_headers)
    assert sonra.status_code == 200, sonra.text
    govde = sonra.json()
    assert govde["start_date"] == "2026-08-04"
    assert govde["end_date"] == "2026-08-08"
    assert govde["days"] == 5
    assert govde["leave_type_id"] == str(yillik.id)


@pytest.mark.asyncio
async def test_patch_onaylanmis_409(client, ik_headers, seeded_db, personel, yillik):
    """`approved`/`rejected` kayıt DÜZENLENEMEZ (spec §3) — 409 (durum çakışması)."""
    onayli = LeaveRequest(
        personnel_id=personel.id,
        leave_type_id=yillik.id,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
        days=3,
        status=LeaveStatus.approved,
    )
    seeded_db.add(onayli)
    await seeded_db.flush()
    yanit = await client.patch(
        f"/leave-requests/{onayli.id}", json={"note": "x"}, headers=ik_headers
    )
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_patch_yok_404(client, ik_headers):
    yanit = await client.patch(
        f"/leave-requests/{uuid.uuid4()}", json={"note": "x"}, headers=ik_headers
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_patch_gorunmeyen_bc_belge_404_idor(
    client, ik_headers, personel, yillik, arsiv_belgesi
):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.patch(
        f"/leave-requests/{talep_id}",
        json={"document_id": str(arsiv_belgesi.id)},
        headers=ik_headers,
    )
    assert yanit.status_code == 404, yanit.text


# --- DELETE: pending + (admin YA DA sahibi) ----------------------------------


@pytest.mark.asyncio
async def test_delete_admin_204(client, ik_headers, admin_headers, personel, yillik):
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.delete(f"/leave-requests/{talep_id}", headers=admin_headers)
    assert yanit.status_code == 204, yanit.text
    kontrol = await client.get(f"/leave-requests/{talep_id}", headers=ik_headers)
    assert kontrol.status_code == 404


@pytest.mark.asyncio
async def test_delete_full_yetki_403(client, ik_headers, personel, yillik):
    """`hr_manager` `full`dur ama SAHİP değildir → silemez (spec §3 "sahibi ya da
    admin"; `app/core/access.py`: full silmeyi KAPSAMAZ — İK-1 belge silme emsali).
    Yanlış açılan talebi İK yine de PATCH'leyebilir."""
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.delete(f"/leave-requests/{talep_id}", headers=ik_headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_delete_onaylanmis_409(client, admin_headers, seeded_db, personel, yillik):
    onayli = LeaveRequest(
        personnel_id=personel.id,
        leave_type_id=yillik.id,
        start_date=date(2026, 9, 1),
        end_date=date(2026, 9, 3),
        days=3,
        status=LeaveStatus.approved,
    )
    seeded_db.add(onayli)
    await seeded_db.flush()
    yanit = await client.delete(f"/leave-requests/{onayli.id}", headers=admin_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_delete_yabanci_view_kullanicisi_403(
    client, ik_headers, sef_headers, personel, yillik
):
    """`site_chief` (`view`) Sistem Yöneticisi değil → silemez (403)."""
    talep_id = await _talep_olustur(client, ik_headers, personel, yillik)
    yanit = await client.delete(f"/leave-requests/{talep_id}", headers=sef_headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_delete_sahibi_de_silemez_istisna_yok_geri_cekebilir(
    client, seeded_db, ik_headers, sef_headers, yillik
):
    """SIL-B1 (K4): talebin SAHİBİ de silemez (403) — silme YALNIZ Sistem Yöneticisi'nindir.
    Vazgeçmenin yolu silme değil GERİ ÇEKMEdir (`withdraw`, durum geçişi)."""
    sef = (
        await seeded_db.execute(select(User).where(User.email == "sef@personnel.co"))
    ).scalar_one()
    kendi = Personnel(full_name="Şef Kendisi", source=WorkerSource.company, user_id=sef.id)
    seeded_db.add(kendi)
    await seeded_db.flush()
    talep_id = await _talep_olustur(client, ik_headers, kendi, yillik)

    yanit = await client.delete(f"/leave-requests/{talep_id}", headers=sef_headers)
    assert yanit.status_code == 403, yanit.text
    geri = await client.post(f"/leave-requests/{talep_id}/withdraw", headers=sef_headers)
    assert geri.status_code == 200, geri.text


@pytest.mark.asyncio
async def test_delete_yok_404(client, admin_headers):
    yanit = await client.delete(f"/leave-requests/{uuid.uuid4()}", headers=admin_headers)
    assert yanit.status_code == 404, yanit.text


# --- GET /leave-types (SALT OKUMA — katalog CRUD'u AÇILMAZ) ------------------


@pytest.mark.asyncio
async def test_leave_types_yalniz_aktif_sirali(client, ik_headers, yillik, hastalik, pasif_tip):
    yanit = await client.get("/leave-types", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    adlar = [t["name"] for t in yanit.json()]
    assert adlar == ["Yıllık İzin", "Hastalık İzni"]
    assert yanit.json()[0]["deducts_from_annual"] is True


@pytest.mark.asyncio
async def test_leave_types_yazma_ucu_yok(client, admin_headers):
    """Katalog CRUD'u AÇILMAZ (spec §1) — POST diye bir uç YOKTUR (405)."""
    yanit = await client.post("/leave-types", json={"name": "Ücretsiz"}, headers=admin_headers)
    assert yanit.status_code == 405, yanit.text
