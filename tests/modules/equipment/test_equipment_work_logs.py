"""MK-1 T4 — çalışma kaydı uçları (spec §4 "Çalışma kaydı" bloğu).

Kilitlenen kararlar: **K11** (`hours` SUNUCU hesabıdır) · **K12** (aynı ekipman +
aynı gün saat toplamı ≤ 24, tek istekli yüzü) · **K9** (kaydın KENDİ şantiyesi) ·
**K10** (arıza ayrı kayıt tipi, operatörsüz) · TB3 sayfalama kanonu · izin kapıları.

K12'nin ASIL kanıtı burada DEĞİL `test_mk1_work_log_concurrency.py`dedir: tek
istekli bir test kilidi ASLA ölçmez (İK-2 dersi). Bu dosya yalnız kuralın tek
istekte doğru cevabı verdiğini çiviler.

Görünürlük (K20) `test_equipment_idor.py`de, özet ucu `test_equipment_work_summary.py`de.
"""

import uuid
from datetime import date

import pytest

from app.modules.equipment.models import WorkLogType

_GUN = date(2026, 7, 17)


def _govde(equipment_id: uuid.UUID, **kwargs) -> dict:
    """M3:261 satırının gövdesi: `06:00–15:00` → sunucu 9 saat hesaplar."""
    govde: dict = {
        "equipment_id": str(equipment_id),
        "work_date": _GUN.isoformat(),
        "start_time": "06:00:00",
        "end_time": "15:00:00",
    }
    govde.update(kwargs)
    return govde


@pytest.fixture
async def makine(ekipman_fabrikasi, gorunen_santiye):
    return await ekipman_fabrikasi("Tower Crane TC-48", site=gorunen_santiye)


# --- K11: `hours` SUNUCU HESABIDIR ---


@pytest.mark.asyncio
async def test_k11_aralik_saate_cevrilir(client, sef_headers, makine):
    """🔴 K11 · M3:261 — `06:00–15:00` = 9 saat. Mockup'ta doğrulanmış sayı."""
    yanit = await client.post("/equipment/work-logs", json=_govde(makine.id), headers=sef_headers)
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["hours"] == "9.00"


@pytest.mark.asyncio
async def test_k11_araliksiz_kayitta_saat_dogrudan_alinir(client, sef_headers, makine):
    """M3:283 arıza kaydı aralık BASMAZ; saat doğrudan gövdeden gelir (K10+K11)."""
    yanit = await client.post(
        "/equipment/work-logs",
        json={
            "equipment_id": str(makine.id),
            "work_date": _GUN.isoformat(),
            "record_type": WorkLogType.breakdown.value,
            "hours": "8",
            "note": "Pompa arızası",
        },
        headers=sef_headers,
    )
    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["hours"] == "8.00"
    assert govde["record_type"] == "breakdown"
    assert govde["operator_id"] is None


@pytest.mark.asyncio
async def test_k11_aralikla_birlikte_gonderilen_saat_422(client, sef_headers, makine):
    """🔴 K11: aralık verilmişken `hours` göndermek REDDEDİLİR — sessizce
    yoksayılsaydı istemci kendi hesabının tutulduğunu sanırdı."""
    yanit = await client.post(
        "/equipment/work-logs", json=_govde(makine.id, hours="12"), headers=sef_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_k11_tek_basina_baslangic_422(client, sef_headers, makine):
    """İki alan BİRLİKTE ya hiç ya ikisi de (K11)."""
    govde = _govde(makine.id)
    del govde["end_time"]
    yanit = await client.post("/equipment/work-logs", json=govde, headers=sef_headers)
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_k11_bitis_baslangictan_kucukse_422(client, sef_headers, makine):
    """Gece vardiyası bu dilimde DESTEKLENMEZ — sessiz negatif saatten iyidir."""
    yanit = await client.post(
        "/equipment/work-logs",
        json=_govde(makine.id, start_time="22:00:00", end_time="06:00:00"),
        headers=sef_headers,
    )
    assert yanit.status_code == 422, yanit.text
    assert "gece" in yanit.json()["detail"].lower()


@pytest.mark.asyncio
async def test_k11_ne_aralik_ne_saat_422(client, sef_headers, makine):
    """Aralık da saat de yoksa kaydın hiçbir süresi yoktur."""
    yanit = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat()},
        headers=sef_headers,
    )
    assert yanit.status_code == 422, yanit.text


# --- K12: günlük 24 saat tavanı (tek istekli yüz) ---


@pytest.mark.asyncio
async def test_k12_gunluk_tavan_asilirsa_422(client, sef_headers, makine):
    """Aynı ekipman + aynı gün toplamı 24'ü AŞAMAZ."""
    ilk = await client.post(
        "/equipment/work-logs",
        json={
            "equipment_id": str(makine.id),
            "work_date": _GUN.isoformat(),
            "hours": "20",
        },
        headers=sef_headers,
    )
    assert ilk.status_code == 201, ilk.text

    ikinci = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "5"},
        headers=sef_headers,
    )
    assert ikinci.status_code == 422, ikinci.text
    assert "24" in ikinci.json()["detail"]


@pytest.mark.asyncio
async def test_k12_tam_24_saat_gecerlidir(client, sef_headers, makine):
    """Tavan AŞILAMAZ ama tam 24 saat kuralın İÇİNDEDİR (`>` denetimi, `>=` değil)."""
    await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "16"},
        headers=sef_headers,
    )
    yanit = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "8"},
        headers=sef_headers,
    )
    assert yanit.status_code == 201, yanit.text


@pytest.mark.asyncio
async def test_k12_farkli_gun_tavani_paylasmaz(client, sef_headers, makine):
    """Tavan GÜNLÜKTÜR: ertesi gün sıfırdan başlar."""
    await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "20"},
        headers=sef_headers,
    )
    yanit = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": "2026-07-18", "hours": "20"},
        headers=sef_headers,
    )
    assert yanit.status_code == 201, yanit.text


@pytest.mark.asyncio
async def test_k12_farkli_ekipman_tavani_paylasmaz(
    client, sef_headers, makine, ekipman_fabrikasi, gorunen_santiye
):
    """Tavan EKİPMAN başınadır: ikinci makine birincinin saatinden etkilenmez."""
    ikinci_makine = await ekipman_fabrikasi("Ekskavatör CAT 320", site=gorunen_santiye)
    await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "20"},
        headers=sef_headers,
    )
    yanit = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(ikinci_makine.id), "work_date": _GUN.isoformat(), "hours": "20"},
        headers=sef_headers,
    )
    assert yanit.status_code == 201, yanit.text


@pytest.mark.asyncio
async def test_k12_ariza_kaydi_da_tavana_girer(client, sef_headers, makine):
    """K10 arızayı ayrı TİP yapar ama gün 24 saattir: makine arızalıyken de
    dünyanın saati uzamaz."""
    await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "20"},
        headers=sef_headers,
    )
    yanit = await client.post(
        "/equipment/work-logs",
        json={
            "equipment_id": str(makine.id),
            "work_date": _GUN.isoformat(),
            "record_type": WorkLogType.breakdown.value,
            "hours": "6",
        },
        headers=sef_headers,
    )
    assert yanit.status_code == 422, yanit.text


# --- PATCH ---


async def _kayit_ac(client, headers, equipment_id, **kwargs) -> dict:
    govde = {"equipment_id": str(equipment_id), "work_date": _GUN.isoformat(), "hours": "8"}
    govde.update(kwargs)
    yanit = await client.post("/equipment/work-logs", json=govde, headers=headers)
    assert yanit.status_code == 201, yanit.text
    return yanit.json()


@pytest.mark.asyncio
async def test_patch_tavan_denetimi_kaydin_KENDI_saatini_haric_tutar(client, sef_headers, makine):
    """🔴 PATCH'te kayıt KENDİSİYLE çakışmamalıdır.

    20 saatlik tek kaydı 22 saate çekmek geçerlidir; kendi 20 saati toplama
    ikinci kez katılsaydı (20 + 22 = 42) düzeltme İMKÂNSIZ olurdu.
    """
    kayit = await _kayit_ac(client, sef_headers, makine.id, hours="20")
    yanit = await client.patch(
        f"/equipment/work-logs/{kayit['id']}", json={"hours": "22"}, headers=sef_headers
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["hours"] == "22.00"


@pytest.mark.asyncio
async def test_patch_tavani_asan_duzeltme_422(client, sef_headers, makine):
    """Aynı günde 10 + 10 varken birini 20'ye çekmek toplamı 30 yapar."""
    await _kayit_ac(client, sef_headers, makine.id, hours="10")
    ikinci = await _kayit_ac(client, sef_headers, makine.id, hours="10")
    yanit = await client.patch(
        f"/equipment/work-logs/{ikinci['id']}", json={"hours": "20"}, headers=sef_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_tarih_degisiminde_HEDEF_gun_denetlenir(client, sef_headers, makine):
    """Kayıt taşındığında tavan HEDEF günde ölçülür — kaynak gün yalnız boşalır."""
    await _kayit_ac(client, sef_headers, makine.id, hours="20", work_date="2026-07-18")
    tasinan = await _kayit_ac(client, sef_headers, makine.id, hours="10")
    yanit = await client.patch(
        f"/equipment/work-logs/{tasinan['id']}",
        json={"work_date": "2026-07-18"},
        headers=sef_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_ekipman_degisiminde_HEDEF_makine_denetlenir(
    client, sef_headers, makine, ekipman_fabrikasi, gorunen_santiye
):
    """Kayıt başka makineye taşınırsa tavan O makinenin gününde ölçülür."""
    hedef = await ekipman_fabrikasi("Beton Pompası BP-36", site=gorunen_santiye)
    await _kayit_ac(client, sef_headers, hedef.id, hours="20")
    tasinan = await _kayit_ac(client, sef_headers, makine.id, hours="10")
    yanit = await client.patch(
        f"/equipment/work-logs/{tasinan['id']}",
        json={"equipment_id": str(hedef.id)},
        headers=sef_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_aralik_saati_YENIDEN_hesaplar(client, sef_headers, makine):
    """K11 PATCH'te de sunucudadır: yeni aralık yeni saati üretir."""
    kayit = await _kayit_ac(
        client, sef_headers, makine.id, hours=None, start_time="06:00:00", end_time="15:00:00"
    )
    assert kayit["hours"] == "9.00"
    yanit = await client.patch(
        f"/equipment/work-logs/{kayit['id']}",
        json={"start_time": "07:00:00", "end_time": "15:00:00"},
        headers=sef_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["hours"] == "8.00"


@pytest.mark.asyncio
async def test_patch_aralikli_kayitta_saat_gondermek_422(client, sef_headers, makine):
    """Aralığı duran kayda `hours` yazmak K11'i PATCH üzerinden delerdi."""
    kayit = await _kayit_ac(
        client, sef_headers, makine.id, hours=None, start_time="06:00:00", end_time="15:00:00"
    )
    yanit = await client.patch(
        f"/equipment/work-logs/{kayit['id']}", json={"hours": "12"}, headers=sef_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_patch_aralik_bosaltilirsa_saat_dogrudan_yazilir(client, sef_headers, makine):
    """Aralıktan vazgeçmenin YOLU: iki zaman alanını `null`layıp saati vermek."""
    kayit = await _kayit_ac(
        client, sef_headers, makine.id, hours=None, start_time="06:00:00", end_time="15:00:00"
    )
    yanit = await client.patch(
        f"/equipment/work-logs/{kayit['id']}",
        json={"start_time": None, "end_time": None, "hours": "5"},
        headers=sef_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["hours"] == "5.00"
    assert govde["start_time"] is None


# --- DELETE ---


@pytest.mark.asyncio
async def test_delete_kaydi_siler_ve_tavani_bosaltir(client, sef_headers, admin_headers, makine):
    """Çalışma kaydı mali iz DEĞİLDİR (türev maliyet): kayıt hatası silinebilir — YALNIZ Sistem
    Yöneticisi (SIL-B1; `full` yazar şef silemez)."""
    kayit = await _kayit_ac(client, sef_headers, makine.id, hours="20")
    yasak = await client.delete(f"/equipment/work-logs/{kayit['id']}", headers=sef_headers)
    assert yasak.status_code == 403, yasak.text
    silme = await client.delete(f"/equipment/work-logs/{kayit['id']}", headers=admin_headers)
    assert silme.status_code == 204, silme.text

    yeniden = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "20"},
        headers=sef_headers,
    )
    assert yeniden.status_code == 201, yeniden.text


@pytest.mark.asyncio
async def test_delete_olmayan_kayit_404(client, admin_headers):
    yanit = await client.delete(f"/equipment/work-logs/{uuid.uuid4()}", headers=admin_headers)
    assert yanit.status_code == 404, yanit.text


# --- Liste + süzgeçler (TB3) ---


@pytest.mark.asyncio
async def test_liste_suzgecleri_ve_toplam(
    client, sef_headers, makine, ekipman_fabrikasi, gorunen_santiye
):
    diger = await ekipman_fabrikasi("Kompresör SC-200", site=gorunen_santiye)
    await _kayit_ac(client, sef_headers, makine.id, hours="8")
    await _kayit_ac(client, sef_headers, makine.id, hours="6", work_date="2026-08-02")
    await _kayit_ac(
        client, sef_headers, diger.id, hours="4", record_type=WorkLogType.breakdown.value
    )

    hepsi = await client.get("/equipment/work-logs", headers=sef_headers)
    assert hepsi.status_code == 200, hepsi.text
    assert hepsi.json()["total"] == 3

    makineye_gore = await client.get(
        f"/equipment/work-logs?equipment_id={makine.id}", headers=sef_headers
    )
    assert makineye_gore.json()["total"] == 2

    tarihe_gore = await client.get(
        "/equipment/work-logs?date_from=2026-08-01&date_to=2026-08-31", headers=sef_headers
    )
    assert tarihe_gore.json()["total"] == 1

    tipe_gore = await client.get("/equipment/work-logs?record_type=breakdown", headers=sef_headers)
    assert tipe_gore.json()["total"] == 1

    santiyeye_gore = await client.get(
        f"/equipment/work-logs?site_id={gorunen_santiye.id}", headers=sef_headers
    )
    assert santiyeye_gore.json()["total"] == 3


@pytest.mark.asyncio
async def test_tb3_limit_tavani_422(client, sef_headers):
    """TB3 kanonu: `limit ≤ 200`, aşımı 422."""
    yanit = await client.get("/equipment/work-logs?limit=201", headers=sef_headers)
    assert yanit.status_code == 422, yanit.text


# --- K9: kaydın KENDİ şantiyesi ---


@pytest.mark.asyncio
async def test_k9_kayit_kendi_santiyesini_tasir(
    client, sef_headers, makine, gorunen_santiye, seeded_db, gorunen_proje
):
    """🔴 K9: makine bugün başka şantiyede olsa da kayıt YAPILDIĞI yere aittir."""
    from app.modules.sites.models import Site

    ikinci_santiye = Site(project_id=gorunen_proje.id, code="MK-C", name="C-Blok")
    seeded_db.add(ikinci_santiye)
    await seeded_db.flush()

    kayit = await _kayit_ac(client, sef_headers, makine.id, site_id=str(ikinci_santiye.id))
    assert kayit["site_id"] == str(ikinci_santiye.id)
    assert makine.site_id == gorunen_santiye.id


@pytest.mark.asyncio
async def test_k9_santiye_verilmezse_makinenin_o_anki_atamasi_damgalanir(
    client, sef_headers, makine, gorunen_santiye
):
    """🔴 K9 SNAPSHOT: `site_id` gövdede YOKSA makinenin O ANKİ ataması damgalanır.

    `NULL` bırakılsaydı K9 kâğıt üzerinde kalırdı: her kayıt şantiyesiz doğar,
    "hangi şantiye ne kadar makine yaktı" sorusu hiçbir zaman cevaplanamazdı.
    """
    kayit = await _kayit_ac(client, sef_headers, makine.id)
    assert kayit["site_id"] == str(gorunen_santiye.id)


@pytest.mark.asyncio
async def test_acikca_null_gonderilen_santiye_damgalanmaz(client, sef_headers, makine):
    """Gönderilmemek ile `null` göndermek FARKLIDIR (F-İK "touched" dersi):
    depoda yapılan iş açıkça şantiyesiz kaydedilebilmelidir."""
    kayit = await _kayit_ac(client, sef_headers, makine.id, site_id=None)
    assert kayit["site_id"] is None


# --- İzin kapıları ---


@pytest.mark.asyncio
async def test_okuma_izni_yazamaz_403(client, muhendis_headers, makine):
    yanit = await client.post(
        "/equipment/work-logs",
        json={"equipment_id": str(makine.id), "work_date": _GUN.isoformat(), "hours": "8"},
        headers=muhendis_headers,
    )
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_izinsiz_kullanici_okuyamaz_403(client, yetkisiz_headers):
    yanit = await client.get("/equipment/work-logs", headers=yetkisiz_headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_patch_yalniz_notu_degistiren_istek_aralikSIZ_kaydi_KILITLEMEZ(
    client, sef_headers, makine
):
    """🔴 FINAL REVIEW bulgusu — K11 kuralının PATCH'te kaydı kilitlemesi.

    Arıza kaydında (M3:283) saat aralığı YOKTUR, saat doğrudan girilir. Böyle bir
    kaydın YALNIZ notunu düzeltmek isteyen bir istek `hours` göndermez ve gönderemez
    de (K11: sunucu hesabı). `_resolve_hours` gövdedeki `hours`u ve satırdaki
    aralığı birlikte okuduğu için ikisi de boş kalıyor ve kayıt "saat zorunlu"
    diye 422'ye takılıyordu: aralıksız HER kayıt, açıldıktan sonra bir daha hiç
    düzeltilemez oluyordu (T3'ün K2'de bulduğu kilitlenmenin aynısı).

    Doğru davranış: gövde saatin hiçbir girdisine DOKUNMUYORSA satırdaki saat
    korunur; kural yalnız saate dokunan isteklerde koşar.
    """
    kayit = await _kayit_ac(
        client,
        sef_headers,
        makine.id,
        record_type=WorkLogType.breakdown.value,
        hours="6",
        note="Hidrolik hortum patladı",
    )
    yanit = await client.patch(
        f"/equipment/work-logs/{kayit['id']}",
        json={"note": "Hidrolik hortum değişti, servis bekleniyor"},
        headers=sef_headers,
    )
    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["hours"] == "6.00"
    assert yanit.json()["note"] == "Hidrolik hortum değişti, servis bekleniyor"
