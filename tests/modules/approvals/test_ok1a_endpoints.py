"""OK-1A T1/T2 — motorun BEŞ ucu (sözleşme Y5) + yetki kapıları + liste kanonu.

```
GET  /approvals                    — onay kutusu (satır zenginleştirmesi T4'te)
GET  /approvals/settings           — eşiği oku
PUT  /approvals/settings           — eşiği yaz     [approvals: admin]
GET  /approvals/roles              — tüm atamalar  [approvals: admin]
PUT  /approvals/roles/{user_id}    — atama yaz     [approvals: admin]
```

YENİ izin modülü AÇILMADI: `approvals` seed'de ZATEN vardır
(`roles/seed_data.py:74,176`) ve `admin` seviyesinden yalnız `system_admin` geçer.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.audit import messages
from app.modules.audit.models import AuditLog
from app.modules.company.schemas import CompanyUpdate

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_BEKLENEN_YOLLAR = {
    "/approvals",
    "/approvals/history",
    "/approvals/settings",
    "/approvals/roles",
    "/approvals/roles/{user_id}",
}


@pytest.fixture
async def admin_basliklari(aktor_fabrikasi, giris):
    await aktor_fabrikasi("uc-admin@ok1a.co", role_key="system_admin")
    return await giris("uc-admin@ok1a.co")


@pytest.fixture
async def muhasebe_basliklari(aktor_fabrikasi, giris):
    """`accounting` → `approvals` = `_FIN` (view). `admin` kapısını GEÇEMEZ."""
    await aktor_fabrikasi("uc-muhasebe@ok1a.co", role_key="accounting")
    return await giris("uc-muhasebe@ok1a.co")


# --- Rota kümesi: `/approvals/{uuid}` YOKTUR, sabit yollar yutulmaz ---


def test_modulun_ROTA_KUMESI_tam_olarak_alti_yoldur() -> None:
    """MK-2 rota sırası tuzağı: `/approvals/{id}` açılsaydı `/approvals/settings`
    bir UUID sanılıp 422'ye düşerdi. Bugün böyle bir rota YOKTUR ve bu kilitlidir.

    OKT-B1: `GET /approvals/history` ALTINCI yoldur (sabit yol, parametreli kardeşi yok)."""
    from app.main import app

    yollar = {yol for yol in app.openapi()["paths"] if yol.startswith("/approvals")}
    assert yollar == _BEKLENEN_YOLLAR


# --- Eşik ayarı ---


async def test_esik_varsayilani_500K(client, admin_basliklari):
    yanit = await client.get("/approvals/settings", headers=admin_basliklari)

    assert yanit.status_code == 200, yanit.text
    assert Decimal(yanit.json()["approval_threshold_try"]) == Decimal("500000.00")


async def test_esigi_ADMIN_yazar_ve_denetim_satiri_duser(client, seeded_db, admin_basliklari):
    yanit = await client.put(
        "/approvals/settings",
        json={"approval_threshold_try": "750000.00"},
        headers=admin_basliklari,
    )

    assert yanit.status_code == 200, yanit.text
    assert Decimal(yanit.json()["approval_threshold_try"]) == Decimal("750000.00")
    assert await service.get_threshold(seeded_db) == Decimal("750000.00")

    detaylar = [satir.detail for satir in (await seeded_db.execute(select(AuditLog))).scalars()]
    assert messages.APPROVAL_THRESHOLD_UPDATED in detaylar


async def test_esigi_YETKISIZ_yazamaz_403(client, muhasebe_basliklari, seeded_db):
    yanit = await client.put(
        "/approvals/settings",
        json={"approval_threshold_try": "1.00"},
        headers=muhasebe_basliklari,
    )

    assert yanit.status_code == 403, yanit.text
    assert await service.get_threshold(seeded_db) == Decimal("500000.00")


async def test_negatif_esik_422(client, admin_basliklari):
    yanit = await client.put(
        "/approvals/settings", json={"approval_threshold_try": "-1.00"}, headers=admin_basliklari
    )

    assert yanit.status_code == 422, yanit.text


# --- 🔴 R7: `PUT /company` eşiğe DOKUNAMAZ ---


def test_CompanyUpdate_semasi_esigi_TASIMAZ() -> None:
    assert "approval_threshold_try" not in CompanyUpdate.model_fields


async def test_PUT_company_esigi_DEGISTIREMEZ(client, admin_basliklari):
    """İKİ katman AYNI testte: (a) alan gönderilirse `extra="forbid"` 422 verir,
    (b) MEŞRU bir `PUT /company` çağrısı eşiği KIMILDATMAZ.

    Tek başına (a), kolon HİÇ OLMASAYDI da yeşil geçerdi — (b) o sahte-yeşili
    kapatır: eşik önce gerçekten yazılır, sonra hâlâ orada olduğu ölçülür.
    """
    yaz = await client.put(
        "/approvals/settings",
        json={"approval_threshold_try": "750000.00"},
        headers=admin_basliklari,
    )
    assert yaz.status_code == 200, yaz.text

    yasak = await client.put(
        "/company", json={"approval_threshold_try": "1.00"}, headers=admin_basliklari
    )
    assert yasak.status_code == 422, yasak.text

    mesru = await client.put("/company", json={"name": "FİİL Yapı A.Ş."}, headers=admin_basliklari)
    assert mesru.status_code == 200, mesru.text

    oku = await client.get("/approvals/settings", headers=admin_basliklari)
    assert Decimal(oku.json()["approval_threshold_try"]) == Decimal("750000.00")


# --- Onay rolü atamaları ---


async def test_rol_atamasi_TAM_KUME_degistirir(
    client, seeded_db, admin_basliklari, aktor_fabrikasi
):
    """Atama TAM KÜME yazar: gönderilmeyen rol KALKAR (kısmi ekleme değil)."""
    hedef = await aktor_fabrikasi(
        "atama-hedef@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.site_chief],
    )

    yanit = await client.put(
        f"/approvals/roles/{hedef.id}",
        json={"approval_roles": ["project_manager", "accounting"]},
        headers=admin_basliklari,
    )

    assert yanit.status_code == 200, yanit.text
    assert set(yanit.json()["approval_roles"]) == {"project_manager", "accounting"}
    assert set(await service.user_approval_roles(seeded_db, hedef.id)) == {
        ApprovalRole.project_manager,
        ApprovalRole.accounting,
    }


async def test_rol_atamasi_BOS_kume_ile_tum_rolleri_kaldirir(
    client, seeded_db, admin_basliklari, aktor_fabrikasi
):
    hedef = await aktor_fabrikasi(
        "atama-bos@ok1a.co", approval_roles=[ApprovalRole.accounting, ApprovalRole.patron]
    )

    yanit = await client.put(
        f"/approvals/roles/{hedef.id}", json={"approval_roles": []}, headers=admin_basliklari
    )

    assert yanit.status_code == 200, yanit.text
    assert await service.user_approval_roles(seeded_db, hedef.id) == []


async def test_rol_atamasi_YETKISIZE_403(client, muhasebe_basliklari, aktor_fabrikasi):
    hedef = await aktor_fabrikasi("atama-yetkisiz@ok1a.co")

    yanit = await client.put(
        f"/approvals/roles/{hedef.id}",
        json={"approval_roles": ["patron"]},
        headers=muhasebe_basliklari,
    )

    assert yanit.status_code == 403, yanit.text


async def test_olmayan_kullaniciya_atama_404(client, admin_basliklari):
    yanit = await client.put(
        f"/approvals/roles/{uuid.uuid4()}",
        json={"approval_roles": ["patron"]},
        headers=admin_basliklari,
    )

    assert yanit.status_code == 404, yanit.text


async def test_gecersiz_onay_rolu_422(client, admin_basliklari, aktor_fabrikasi):
    hedef = await aktor_fabrikasi("atama-gecersiz@ok1a.co")

    yanit = await client.put(
        f"/approvals/roles/{hedef.id}",
        json={"approval_roles": ["system_admin"]},
        headers=admin_basliklari,
    )

    assert yanit.status_code == 422, yanit.text


async def test_atama_listesi_YALNIZ_ADMINE(
    client, admin_basliklari, muhasebe_basliklari, aktor_fabrikasi
):
    await aktor_fabrikasi("liste-rol@ok1a.co", approval_roles=[ApprovalRole.patron])

    yetkisiz = await client.get("/approvals/roles", headers=muhasebe_basliklari)
    assert yetkisiz.status_code == 403, yetkisiz.text

    yanit = await client.get("/approvals/roles", headers=admin_basliklari)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert set(govde) >= {"items", "total", "limit", "offset"}
    kayitlar = {satir["full_name"]: satir["approval_roles"] for satir in govde["items"]}
    assert "Onay Aktörü" in kayitlar
    assert kayitlar["Onay Aktörü"] == ["patron"]


# --- 🔴 Liste ucu kanonu (TB3/T2): tavan aşımı 422, KIRPMA DEĞİL ---


@pytest.mark.parametrize("yol", ["/approvals", "/approvals/roles"])
@pytest.mark.parametrize("sorgu", ["limit=201", "limit=0", "limit=-1", "offset=-1"])
async def test_liste_sinirlari_422(client, admin_basliklari, yol, sorgu):
    yanit = await client.get(f"{yol}?{sorgu}", headers=admin_basliklari)

    assert yanit.status_code == 422, (yol, sorgu, yanit.text)


async def test_onay_kutusu_ZARFI_ve_VARSAYILANLARI(client, admin_basliklari):
    yanit = await client.get("/approvals", headers=admin_basliklari)

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert set(govde) == {"items", "total", "limit", "offset", "my_approval_roles"}
    assert govde["limit"] == 50
    assert govde["offset"] == 0


# --- Onay kutusu: yalnız KULLANICIYA DÜŞEN sıradaki adımlar ---


async def test_onay_kutusu_YALNIZ_siradaki_adimi_ve_KENDI_rollerini_doner(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Üç ayrı süzgeç TEK turda: rol · sıra · bekçi 5 (kendi evrakı).

    ⚠️ T4 UYARLAMASI: zincirler artık GERÇEK evraklara bağlanıyor. Uydurma bir
    `document_id` projeye çözülemediği için görünürlük süzgecine (fail-closed)
    takılır ve kutuda HİÇ görünmezdi — iddia sessizce boş kümeye kayardı.
    """
    yaratan = await aktor_fabrikasi("kutu-yaratan@ok1a.co")
    sef = await aktor_fabrikasi(
        "kutu-sef@ok1a.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("kutu-sef@ok1a.co")

    dusen, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=dusen,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )
    # (a) Şefin KENDİ evrakı — bekçi 5 yüzünden kutuda GÖRÜNMEZ.
    kendi, _ = await evrak_fabrikasi(_TASERON, creator=sef)
    await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=kendi,
        amount=Decimal("100.00"),
        created_by_user_id=sef.id,
    )
    # (b) Sıradaki adımı `accounting` olan evrak — şefin rolü DEĞİL.
    baskasinin, _ = await evrak_fabrikasi(ApprovalDocumentType.progress_payment, creator=yaratan)
    await service.create_chain(
        seeded_db,
        document_type=ApprovalDocumentType.progress_payment,
        document_id=baskasinin,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )

    yanit = await client.get("/approvals", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["my_approval_roles"] == ["site_chief"]
    assert [satir["document_id"] for satir in govde["items"]] == [str(dusen)]
    assert govde["total"] == 1
    satir = govde["items"][0]
    assert satir["document_type"] == "subcontractor_progress_payment"
    assert satir["created_by_name"] == "Onay Aktörü"
    assert satir["current_step_no"] == 1
    assert [adim["approval_role"] for adim in satir["steps"]] == [
        "site_chief",
        "project_manager",
        "accounting",
    ]
    # 🔴 KANON E: cevap OLGUYU taşır, KARARI değil.
    assert "can_approve" not in satir


async def test_onay_kutusu_GOREVLER_AYRILIGINA_takilan_satiri_GIZLER(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("kutu-ayrilik-yaratan@ok1a.co")
    cift_rollu = await aktor_fabrikasi(
        "kutu-ayrilik@ok1a.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.site_chief, ApprovalRole.project_manager],
    )
    basliklar = await giris("kutu-ayrilik@ok1a.co")
    document_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=Decimal("100.00"),
        created_by_user_id=yaratan.id,
    )

    once = await client.get("/approvals", headers=basliklar)
    assert [s["document_id"] for s in once.json()["items"]] == [str(document_id)]

    await service.approve_next_step(
        seeded_db, actor=cift_rollu, document_type=_TASERON, document_id=document_id
    )

    sonra = await client.get("/approvals", headers=basliklar)
    assert sonra.json()["items"] == [], "2. adım aynı aktöre kapalıdır, kutuda görünmemeliydi"
    assert sonra.json()["total"] == 0
