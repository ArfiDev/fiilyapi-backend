"""İK-2 T3 — izin onayı/reddi + bakiye uçları (HTTP, uçtan uca).

Spec: `docs/superpowers/specs/2026-08-12-ik2-izin-yonetimi-design.md` §2, §3,
§5 K3/K4/K5/K6.

**Onay TEK ADIMDIR** (K4): çok-aşamalı onay motoru AÇILMAZ; `personnel` **full+**
tek ✓ ile kararı verir. Karar alanları (`decided_by`/`decided_at`/`reject_reason`)
SUNUCU damgasıdır — istemci gönderemez (`extra="forbid"`).

İki 409 kapısı `approve`tadır ve İKİSİ DE reddi ENGELLEMEZ:
* **K3 çakışma** — aynı personelin çakışan ONAYLI izni,
* **K5 hak aşımı** — talebin günü kalan haktan büyük, YALNIZ `deducts_from_annual`
  tiplerde; **kalan bilinmiyorsa (kıdem<1, `hire_date` NULL) onay ENGELLİ**
  (🔴 NULL-eşik kanonu, fail-closed).

Tarihler BUGÜNE GÖRE türetilir (`_yil()`): sabit takvim yılı yazmak testi bir yıl
sonra sessizce başka bir kıdem/bakiye penceresine kaydırırdı.
"""

import uuid
from datetime import date

import pytest
from sqlalchemy import select

from app.modules.audit.models import AuditLog
from app.modules.personnel.models import (
    LeaveBalance,
    LeaveStatus,
)
from app.modules.users.models import User
from tests.personnel._ik2_leave_decision import (
    _gun,
    _post_talep,
    _talep,
    _yeni_denetim_metinleri,
    _yil,
    arsiv_belgesi_fixture,
    hastalik_fixture,
    kidemsiz_personel_fixture,
    personel_fixture,
    proje_fixture,
    tarihsiz_personel_fixture,
    yillik_fixture,
)

__all__ = [
    "arsiv_belgesi_fixture",
    "hastalik_fixture",
    "kidemsiz_personel_fixture",
    "personel_fixture",
    "proje_fixture",
    "tarihsiz_personel_fixture",
    "yillik_fixture",
]


# --- approve: mutlu yol + sunucu damgası ------------------------------------


@pytest.mark.asyncio
async def test_approve_200_sunucu_damgasi(client, ik_headers, seeded_db, personel, yillik):
    """Onay TEK adımdır (K4): durum `approved`, karar alanları SUNUCU damgası."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["status"] == "approved"
    assert govde["decided_at"] is not None
    assert govde["reject_reason"] is None
    aktor = (
        await seeded_db.execute(select(User).where(User.email == "ik@personnel.co"))
    ).scalar_one()
    assert govde["decided_by"] == str(aktor.id)


@pytest.mark.asyncio
async def test_approve_ikinci_kez_409(client, ik_headers, personel, yillik):
    """YALNIZ `pending` karara açıktır — onaylı talep yeniden onaylanamaz."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    assert (
        await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    ).status_code == 200
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_reddedilmis_talep_approve_409(client, ik_headers, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    assert (
        await client.post(
            f"/leave-requests/{talep_id}/reject", json={"reason": "Yoğunluk"}, headers=ik_headers
        )
    ).status_code == 200
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_var_olmayan_talep_404(client, ik_headers):
    yanit = await client.post(f"/leave-requests/{uuid.uuid4()}/approve", headers=ik_headers)
    assert yanit.status_code == 404, yanit.text


# --- K3: çakışan ONAYLI izin → approve 409 ----------------------------------


@pytest.mark.asyncio
async def test_approve_cakisan_onayli_izin_409(client, ik_headers, personel, yillik):
    """Aynı personelin çakışan ONAYLI izni varsa ikinci onay 409 (çift izin engeli)."""
    birinci = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    ikinci = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 5), _gun(3, 7))
    assert (
        await client.post(f"/leave-requests/{birinci}/approve", headers=ik_headers)
    ).status_code == 200
    yanit = await client.post(f"/leave-requests/{ikinci}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_cakismayan_ikinci_izin_200(client, ik_headers, personel, yillik):
    """Sınır: 06'da biten iznin ardından 07'de başlayan izin ÇAKIŞMAZ."""
    birinci = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    ikinci = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 7), _gun(3, 8))
    assert (
        await client.post(f"/leave-requests/{birinci}/approve", headers=ik_headers)
    ).status_code == 200
    yanit = await client.post(f"/leave-requests/{ikinci}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_approve_baska_personelin_izni_cakismaz(
    client, ik_headers, personel, kidemsiz_personel, yillik, seeded_db
):
    """Çakışma PERSONEL BAZLIDIR — başkasının aynı tarihli onaylı izni engel değildir."""
    await _talep(
        seeded_db,
        kidemsiz_personel,
        yillik,
        date(_yil(), 3, 2),
        date(_yil(), 3, 6),
        LeaveStatus.approved,
    )
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


# --- K5: hak aşımı → approve 409 --------------------------------------------


@pytest.mark.asyncio
async def test_approve_hak_asimi_409(client, ik_headers, personel, yillik):
    """Hak 14; 20 günlük talep onaylanamaz (İZ 98-99 onay engeli)."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 20))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_hakkin_tam_sinirinda_200(client, ik_headers, personel, yillik):
    """SINIR: `days == kalan` GEÇER (hak tam kullanılabilir), `days == kalan + 1` GEÇMEZ.

    Tek yandan bakmak `>` ile `>=` arasındaki kaymayı yakalamaz ve o kayma ya bir
    günü haksız yere engeller ya da bir gün fazla verdirir.
    """
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 14))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_approve_haktan_bir_gun_fazla_409(client, ik_headers, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 15))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_devreden_hakki_buyutur(client, ik_headers, seeded_db, personel, yillik):
    """Devreden (İZ 137) kalana EKLENİR: 14 + 3 = 17 → 15 günlük talep GEÇER."""
    seeded_db.add(LeaveBalance(personnel_id=personel.id, year=_yil(), carried_over=3))
    await seeded_db.flush()
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 15))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_approve_onceki_onaylar_kalani_dusurur(client, ik_headers, personel, yillik):
    """Kullanılan = o yılın ONAYLI günleri: 10 gün onaylandıysa kalan 4, 5 gün 409."""
    ilk = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 10))
    assert (
        await client.post(f"/leave-requests/{ilk}/approve", headers=ik_headers)
    ).status_code == 200
    ikinci = await _post_talep(client, ik_headers, personel, yillik, _gun(6, 1), _gun(6, 5))
    yanit = await client.post(f"/leave-requests/{ikinci}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_bekleyen_talepler_kalani_dusurmez(client, ik_headers, personel, yillik):
    """BEKLEYEN talep henüz taahhüt DEĞİLDİR — kalanı düşürmez (yalnız `approved`)."""
    await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 10))
    ikinci = await _post_talep(client, ik_headers, personel, yillik, _gun(6, 1), _gun(6, 5))
    yanit = await client.post(f"/leave-requests/{ikinci}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_approve_hastalik_izni_haktan_dusmez(client, ik_headers, personel, yillik, hastalik):
    """`deducts_from_annual=false` tip hak aşımı kapısına HİÇ girmez (İZ 87)."""
    tuketen = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 14))
    assert (
        await client.post(f"/leave-requests/{tuketen}/approve", headers=ik_headers)
    ).status_code == 200
    rapor = await _post_talep(client, ik_headers, personel, hastalik, _gun(6, 1), _gun(6, 20))
    yanit = await client.post(f"/leave-requests/{rapor}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_onayli_hastalik_izni_kullanilana_girmez(
    client, ik_headers, personel, yillik, hastalik
):
    """Onaylı hastalık izni bakiyedeki `used`a GİRMEZ — 14 günlük yıllık hâlâ geçer."""
    rapor = await _post_talep(client, ik_headers, personel, hastalik, _gun(6, 1), _gun(6, 20))
    assert (
        await client.post(f"/leave-requests/{rapor}/approve", headers=ik_headers)
    ).status_code == 200
    yillik_talep = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 14))
    yanit = await client.post(f"/leave-requests/{yillik_talep}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


# --- 🔴 NULL-EŞİK KANONU (fail-closed) --------------------------------------


@pytest.mark.asyncio
async def test_approve_hire_date_null_409_fail_closed(
    client, ik_headers, tarihsiz_personel, yillik
):
    """🔴 `hire_date` NULL → kalan BİLİNMEZ → onay ENGELLİ (409).

    "Bilinmeyen = küçük" varsayımı burada 0 kullanılmış gün üretir ve TAM HAKKI
    açardı; kanon bilinmeyeni BÜYÜK/engelleyici sayar.
    """
    talep_id = await _post_talep(
        client, ik_headers, tarihsiz_personel, yillik, _gun(4, 1), _gun(4, 2)
    )
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_kidem_bir_yildan_az_409_fail_closed(
    client, ik_headers, kidemsiz_personel, yillik
):
    """🔴 Kıdem 1 yılı doldurmadı → hak YOK → TEK GÜNLÜK izin bile onaylanamaz."""
    talep_id = await _post_talep(
        client, ik_headers, kidemsiz_personel, yillik, _gun(4, 1), _gun(4, 1)
    )
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_approve_kidemsiz_personel_hastalik_izni_200(
    client, ik_headers, kidemsiz_personel, hastalik
):
    """Fail-closed engel YALNIZ yıllık haktan düşen tiplerdedir — rapor serbesttir."""
    talep_id = await _post_talep(
        client, ik_headers, kidemsiz_personel, hastalik, _gun(4, 1), _gun(4, 3)
    )
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_approve_devreden_tek_basina_hak_acmaz(
    client, ik_headers, seeded_db, kidemsiz_personel, yillik
):
    """🔴 Kıdem yoksa DEVREDEN de kapıyı AÇMAZ: `None + 3` iyimserliği yasaktır."""
    seeded_db.add(LeaveBalance(personnel_id=kidemsiz_personel.id, year=_yil(), carried_over=5))
    await seeded_db.flush()
    talep_id = await _post_talep(
        client, ik_headers, kidemsiz_personel, yillik, _gun(4, 1), _gun(4, 1)
    )
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    assert yanit.status_code == 409, yanit.text


# --- Yıl ataması (yıl sınırını aşan talep) ----------------------------------


@pytest.mark.asyncio
async def test_yil_sinirini_asan_talep_baslangic_yilina_sayilir(
    client, ik_headers, seeded_db, personel, yillik
):
    """31 Ara → 2 Oca talebi TAMAMEN başladığı yıla sayılır (gün BÖLÜNMEZ).

    Kanıt: aynı yılda 10 gün onaylıyken 4 günlük yıl-aşan talep kalanı (4) tam
    doldurur ve GEÇER; 5 günlük olsaydı geçmezdi (bir sonraki test).
    """
    ilk = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 10))
    assert (
        await client.post(f"/leave-requests/{ilk}/approve", headers=ik_headers)
    ).status_code == 200
    asan = await _post_talep(
        client,
        ik_headers,
        personel,
        yillik,
        date(_yil(), 12, 30).isoformat(),
        date(_yil() + 1, 1, 2).isoformat(),
    )
    yanit = await client.post(f"/leave-requests/{asan}/approve", headers=ik_headers)
    assert yanit.status_code == 200, yanit.text

    bakiye = await client.get(f"/leave-balances/{personel.id}/{_yil()}", headers=ik_headers)
    assert bakiye.status_code == 200, bakiye.text
    # 10 + 4 = 14 — dört günün TAMAMI başlangıç yılına yazıldı.
    assert bakiye.json()["used"] == 14
    ertesi = await client.get(f"/leave-balances/{personel.id}/{_yil() + 1}", headers=ik_headers)
    assert ertesi.json()["used"] == 0


# --- reject: `reason` zorunlu, red HER ZAMAN serbest ------------------------


@pytest.mark.asyncio
async def test_reject_200_gerekce_damgalanir(client, ik_headers, seeded_db, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject",
        json={"reason": "Şantiye yoğunluğu"},
        headers=ik_headers,
    )
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["status"] == "rejected"
    assert govde["reject_reason"] == "Şantiye yoğunluğu"
    assert govde["decided_at"] is not None
    aktor = (
        await seeded_db.execute(select(User).where(User.email == "ik@personnel.co"))
    ).scalar_one()
    assert govde["decided_by"] == str(aktor.id)


@pytest.mark.asyncio
async def test_reject_gerekcesiz_422(client, ik_headers, personel, yillik):
    """`reason` ZORUNLUdur (TH emsali) — gövdesiz red kabul edilmez."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(f"/leave-requests/{talep_id}/reject", json={}, headers=ik_headers)
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_reject_bos_gerekce_422(client, ik_headers, personel, yillik):
    """Yalnız BOŞLUKTAN oluşan gerekçe de reddedilir — `min_length` tek başına
    boşluk dizisini geçirirdi."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "   "}, headers=ik_headers
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_reject_hak_asiminda_da_serbest(client, ik_headers, personel, yillik):
    """Red HER ZAMAN serbesttir: onayı 409 ile engellenen talep REDDEDİLEBİLİR
    (İZ 98-99: ✓ pasif, ✗ aktif)."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(4, 1), _gun(4, 20))
    assert (
        await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    ).status_code == 409
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "Hak aşımı"}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_reject_cakismada_da_serbest(client, ik_headers, personel, yillik):
    birinci = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    ikinci = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 5), _gun(3, 7))
    await client.post(f"/leave-requests/{birinci}/approve", headers=ik_headers)
    yanit = await client.post(
        f"/leave-requests/{ikinci}/reject", json={"reason": "Çakışma"}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_reject_kidemsiz_personelde_de_serbest(client, ik_headers, kidemsiz_personel, yillik):
    talep_id = await _post_talep(
        client, ik_headers, kidemsiz_personel, yillik, _gun(4, 1), _gun(4, 1)
    )
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "Kıdem yok"}, headers=ik_headers
    )
    assert yanit.status_code == 200, yanit.text


@pytest.mark.asyncio
async def test_reject_ikinci_kez_409(client, ik_headers, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "Yoğunluk"}, headers=ik_headers
    )
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "Tekrar"}, headers=ik_headers
    )
    assert yanit.status_code == 409, yanit.text


@pytest.mark.asyncio
async def test_reject_var_olmayan_talep_404(client, ik_headers):
    yanit = await client.post(
        f"/leave-requests/{uuid.uuid4()}/reject", json={"reason": "yok"}, headers=ik_headers
    )
    assert yanit.status_code == 404, yanit.text


@pytest.mark.asyncio
async def test_karara_baglanan_talep_patch_edilemez(client, ik_headers, personel, yillik):
    """T2 kuralı KORUNUR: onaylanan talep artık düzenlenemez (409) — bakiyeyi
    geriye dönük kaydırmasın."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    yanit = await client.patch(
        f"/leave-requests/{talep_id}", json={"end_date": _gun(3, 9)}, headers=ik_headers
    )
    assert yanit.status_code == 409, yanit.text


# --- Karar alanları / BC bağı istemciden GELMEZ (K6 sızıntı kapısı) ---------


@pytest.mark.asyncio
async def test_approve_govdesinde_karar_alani_422(client, ik_headers, personel, yillik):
    """`decided_by` SUNUCU damgasıdır — istemci başkasının adına imza atamaz."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(
        f"/leave-requests/{talep_id}/approve",
        json={"decided_by": str(uuid.uuid4())},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_approve_govdesinde_document_id_422_bc_sizintisi_yok(
    client, ik_headers, personel, yillik, arsiv_belgesi
):
    """K6: onay yolu BC bağı KURDURMAZ — görünmeyen belge buradan bağlanamaz."""
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(
        f"/leave-requests/{talep_id}/approve",
        json={"document_id": str(arsiv_belgesi.id)},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


@pytest.mark.asyncio
async def test_reject_govdesinde_document_id_422(
    client, ik_headers, personel, yillik, arsiv_belgesi
):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject",
        json={"reason": "yok", "document_id": str(arsiv_belgesi.id)},
        headers=ik_headers,
    )
    assert yanit.status_code == 422, yanit.text


# --- Rol kapıları (K4: `personnel` full+) -----------------------------------


@pytest.mark.asyncio
async def test_approve_sef_403_view_karar_veremez(
    client, ik_headers, sef_headers, personel, yillik
):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=sef_headers)
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_reject_sef_403(client, ik_headers, sef_headers, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "yok"}, headers=sef_headers
    )
    assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_approve_yetkisiz_403(client, ik_headers, yetkisiz_headers, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    yanit = await client.post(f"/leave-requests/{talep_id}/approve", headers=yetkisiz_headers)
    assert yanit.status_code == 403, yanit.text


# --- Denetim günlüğü ---------------------------------------------------------


@pytest.mark.asyncio
async def test_approve_denetime_yazilir(client, ik_headers, seeded_db, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))
    await client.post(f"/leave-requests/{talep_id}/approve", headers=ik_headers)
    metinler = await _yeni_denetim_metinleri(seeded_db, onceki)
    assert len(metinler) == 1
    assert "onaylandı" in metinler[0]
    assert "Ayşe Demir" in metinler[0]


@pytest.mark.asyncio
async def test_reject_denetime_gerekceyle_yazilir(client, ik_headers, seeded_db, personel, yillik):
    talep_id = await _post_talep(client, ik_headers, personel, yillik, _gun(3, 2), _gun(3, 6))
    onceki = set(await seeded_db.scalars(select(AuditLog.id)))
    await client.post(
        f"/leave-requests/{talep_id}/reject", json={"reason": "Yoğunluk"}, headers=ik_headers
    )
    metinler = await _yeni_denetim_metinleri(seeded_db, onceki)
    assert len(metinler) == 1
    assert "reddedildi" in metinler[0]
    assert "Yoğunluk" in metinler[0]
