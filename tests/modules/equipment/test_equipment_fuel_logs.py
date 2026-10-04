"""MK-1 T5 — yakıt kaydı uçları (spec §4 "Yakıt" bloğu).

Kilitlenen kararlar: **`amount` KOLON DEĞİLDİR** (`liters × unit_price`, her
okumada `cost.fuel_amount`ten türer) · **K13** (`unit_price` satır bazlı) ·
**K14** (`entered_by_id` oturum kullanıcısından damgalanır, gövdede YOKTUR) ·
**K19** (para tam sayıya `ROUND_HALF_UP`) · **K20** (görünürlük, iki kapılı) ·
TB3 sayfalama kanonu · izin kapıları.

Yakıt özeti (`fuel-summary`) `test_equipment_fuel_summary.py`dedir.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest

_GUN = date(2026, 8, 5)


def _govde(equipment_id: uuid.UUID, **kwargs) -> dict:
    govde: dict = {
        "equipment_id": str(equipment_id),
        "fuel_date": _GUN.isoformat(),
        "liters": "45",
        "unit_price": "39.70",
    }
    govde.update(kwargs)
    return govde


@pytest.fixture
async def makine(ekipman_fabrikasi, gorunen_santiye):
    return await ekipman_fabrikasi("Tower Crane TC-48", site=gorunen_santiye)


# --- `amount` TÜRETİLİR, KOLON DEĞİLDİR (K19) ---


@pytest.mark.asyncio
async def test_amount_turetilir_ve_dogru_yuvarlanir(client, sef_headers, makine):
    """🔴 M4: `45 × 39,70 = 1.786,5` → **₺1.787** (`ROUND_HALF_UP`, K19)."""
    yanit = await client.post("/equipment/fuel-logs", json=_govde(makine.id), headers=sef_headers)
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["amount"] == "1787"
    assert "unit_price" in govde and "liters" in govde, "amount TÜRETİLİR, kaynak alanlar da döner"


@pytest.mark.asyncio
async def test_amount_yuvarlamasi_asagi_da_calisir(client, sef_headers, makine):
    """🔴 M4: `62 × 39,70 = 2.461,4` → **₺2.461** (aşağı yuvarlama örneği)."""
    yanit = await client.post(
        "/equipment/fuel-logs",
        json=_govde(makine.id, liters="62", unit_price="39.70"),
        headers=sef_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["amount"] == "2461"


@pytest.mark.asyncio
async def test_amount_db_kolonu_degildir(client, sef_headers, makine, seeded_db):
    """`amount` DB'de bir KOLON değildir: ORM nesnesinin `__dict__`inde YOKTUR,
    yalnız `.amount` özelliği (property) olarak hesaplanır."""
    from app.modules.equipment.models import EquipmentFuelLog

    yanit = await client.post("/equipment/fuel-logs", json=_govde(makine.id), headers=sef_headers)
    log_id = uuid.UUID(yanit.json()["id"])
    log = await seeded_db.get(EquipmentFuelLog, log_id)
    assert "amount" not in log.__dict__
    assert log.amount == Decimal("1787")


# --- K14: `entered_by_id` oturum kullanıcısından damgalanır ---


@pytest.mark.asyncio
async def test_k14_entered_by_id_oturum_kullanicisindan_damgalanir(client, sef_headers, makine):
    yanit = await client.post("/equipment/fuel-logs", json=_govde(makine.id), headers=sef_headers)
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["entered_by_id"] is not None


@pytest.mark.asyncio
async def test_k14_govdede_entered_by_id_alani_yoktur(client, sef_headers, makine):
    """İstemci `entered_by_id` göndermeye çalışsa bile şema onu KABUL ETMEZ
    (fazladan alan sessizce yoksayılır, üzerine yazılmaz)."""
    yanit = await client.post(
        "/equipment/fuel-logs",
        json=_govde(makine.id, entered_by_id=str(uuid.uuid4())),
        headers=sef_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["entered_by_id"] is not None


# --- K13: `unit_price` satır bazlıdır ---


@pytest.mark.asyncio
async def test_k13_farkli_satirlar_farkli_birim_fiyat_tasir(client, sef_headers, makine):
    ilk = await client.post(
        "/equipment/fuel-logs", json=_govde(makine.id, unit_price="39.70"), headers=sef_headers
    )
    ikinci = await client.post(
        "/equipment/fuel-logs",
        json=_govde(makine.id, unit_price="42.10", fuel_date="2026-08-06"),
        headers=sef_headers,
    )
    assert Decimal(ilk.json()["unit_price"]) == Decimal("39.70")
    assert Decimal(ikinci.json()["unit_price"]) == Decimal("42.10")


# --- K9 eşi: `site_id` verilmezse makinenin O ANKİ ataması damgalanır ---


@pytest.mark.asyncio
async def test_santiye_verilmezse_makinenin_o_anki_atamasi_damgalanir(
    client, sef_headers, makine, gorunen_santiye
):
    yanit = await client.post("/equipment/fuel-logs", json=_govde(makine.id), headers=sef_headers)
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["site_id"] == str(gorunen_santiye.id)


@pytest.mark.asyncio
async def test_acikca_null_gonderilen_santiye_damgalanmaz(client, sef_headers, makine):
    yanit = await client.post(
        "/equipment/fuel-logs", json=_govde(makine.id, site_id=None), headers=sef_headers
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["site_id"] is None


# --- Sıfır/negatif litre ya da birim fiyat → 422 ---


@pytest.mark.asyncio
async def test_sifir_litre_422(client, sef_headers, makine):
    yanit = await client.post(
        "/equipment/fuel-logs", json=_govde(makine.id, liters="0"), headers=sef_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_negatif_birim_fiyat_422(client, sef_headers, makine):
    yanit = await client.post(
        "/equipment/fuel-logs", json=_govde(makine.id, unit_price="-1"), headers=sef_headers
    )
    assert yanit.status_code == 422, yanit.text


# --- PATCH / DELETE ---


async def _kayit_ac(client, headers, equipment_id, **kwargs) -> dict:
    yanit = await client.post(
        "/equipment/fuel-logs", json=_govde(equipment_id, **kwargs), headers=headers
    )
    assert yanit.status_code == 201, yanit.text
    return yanit.json()


@pytest.mark.asyncio
async def test_patch_litreyi_gunceller_ve_amount_yeniden_hesaplanir(client, sef_headers, makine):
    kayit = await _kayit_ac(client, sef_headers, makine.id, liters="45", unit_price="39.70")
    assert kayit["amount"] == "1787"
    yanit = await client.patch(
        f"/equipment/fuel-logs/{kayit['id']}", json={"liters": "62"}, headers=sef_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["amount"] == "2461"


@pytest.mark.asyncio
async def test_patch_dokunulmamis_alan_ezilmez(client, sef_headers, makine):
    kayit = await _kayit_ac(client, sef_headers, makine.id, note="ilk not")
    yanit = await client.patch(
        f"/equipment/fuel-logs/{kayit['id']}", json={"liters": "50"}, headers=sef_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["note"] == "ilk not"


@pytest.mark.asyncio
async def test_delete_kaydi_siler(client, sef_headers, admin_headers, makine):
    """Yakıt kaydı MALİ İZ DEĞİLDİR: kayıt hatası silinebilir — YALNIZ Sistem Yöneticisi (SIL-B1;
    `full` yazar şef silemez)."""
    kayit = await _kayit_ac(client, sef_headers, makine.id)
    yasak = await client.delete(f"/equipment/fuel-logs/{kayit['id']}", headers=sef_headers)
    assert yasak.status_code == 403, yasak.text
    silme = await client.delete(f"/equipment/fuel-logs/{kayit['id']}", headers=admin_headers)
    assert silme.status_code == 204, silme.text

    okuma = await client.get(f"/equipment/fuel-logs/{kayit['id']}", headers=sef_headers)
    assert okuma.status_code == 404, okuma.text


@pytest.mark.asyncio
async def test_delete_olmayan_kayit_404(client, admin_headers):
    yanit = await client.delete(f"/equipment/fuel-logs/{uuid.uuid4()}", headers=admin_headers)
    assert yanit.status_code == 404, yanit.text


# --- Liste + süzgeçler (TB3) ---


@pytest.mark.asyncio
async def test_liste_suzgecleri_ve_toplam(
    client, sef_headers, makine, ekipman_fabrikasi, gorunen_santiye
):
    diger = await ekipman_fabrikasi("Ekskavatör CAT 320", site=gorunen_santiye)
    await _kayit_ac(client, sef_headers, makine.id, liters="10")
    await _kayit_ac(client, sef_headers, makine.id, liters="5", fuel_date="2026-09-02")
    await _kayit_ac(client, sef_headers, diger.id, liters="8")

    hepsi = await client.get("/equipment/fuel-logs", headers=sef_headers)
    assert hepsi.status_code == 200, hepsi.text
    assert hepsi.json()["total"] == 3

    makineye_gore = await client.get(
        f"/equipment/fuel-logs?equipment_id={makine.id}", headers=sef_headers
    )
    assert makineye_gore.json()["total"] == 2

    tarihe_gore = await client.get(
        "/equipment/fuel-logs?date_from=2026-09-01&date_to=2026-09-30", headers=sef_headers
    )
    assert tarihe_gore.json()["total"] == 1

    santiyeye_gore = await client.get(
        f"/equipment/fuel-logs?site_id={gorunen_santiye.id}", headers=sef_headers
    )
    assert santiyeye_gore.json()["total"] == 3


@pytest.mark.asyncio
async def test_tb3_limit_tavani_422(client, sef_headers):
    yanit = await client.get("/equipment/fuel-logs?limit=201", headers=sef_headers)
    assert yanit.status_code == 422, yanit.text


# --- K20: görünürlük (iki kapılı) ---


@pytest.mark.asyncio
async def test_k20_gorunmeyen_makinenin_kaydina_post_404(
    client, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    yanit = await client.post("/equipment/fuel-logs", json=_govde(gizli.id), headers=sef_headers)
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_k20_gorunmeyen_kayit_detayda_404(
    client, admin_headers, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    kayit = await _kayit_ac(client, admin_headers, gizli.id)
    yanit = await client.get(f"/equipment/fuel-logs/{kayit['id']}", headers=sef_headers)
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_k20_gorunmeyen_kayit_patch_404(
    client, admin_headers, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    kayit = await _kayit_ac(client, admin_headers, gizli.id)
    yanit = await client.patch(
        f"/equipment/fuel-logs/{kayit['id']}", json={"liters": "10"}, headers=sef_headers
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_k20_gorunmeyen_kayit_delete_404(
    client, admin_headers, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye
):
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    kayit = await _kayit_ac(client, admin_headers, gizli.id)
    yanit = await client.delete(f"/equipment/fuel-logs/{kayit['id']}", headers=sef_headers)
    # SIL-B1: kapı handler'dan önce koşar → görünmeyen kayıt için de 403 (varlık sızmaz)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_k20_gorunmeyen_kayit_listede_hic_gorunmez(
    client, admin_headers, sef_headers, ekipman_fabrikasi, gorunmeyen_santiye, makine
):
    gizli = await ekipman_fabrikasi("Gizli Vinç", site=gorunmeyen_santiye)
    await _kayit_ac(client, admin_headers, gizli.id)
    await _kayit_ac(client, sef_headers, makine.id)

    yanit = await client.get("/equipment/fuel-logs", headers=sef_headers)
    assert yanit.json()["total"] == 1


# --- İzin kapıları ---


@pytest.mark.asyncio
async def test_okuma_izni_yazamaz_403(client, muhendis_headers, makine):
    yanit = await client.post(
        "/equipment/fuel-logs", json=_govde(makine.id), headers=muhendis_headers
    )
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_izinsiz_kullanici_okuyamaz_403(client, yetkisiz_headers):
    yanit = await client.get("/equipment/fuel-logs", headers=yetkisiz_headers)
    assert yanit.status_code == 403, yanit.text
