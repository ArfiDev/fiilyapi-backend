"""KAT-B2.4 (K5/K8) — taseron hakedis satirinda Bakanlik poz no SNAPSHOT'i.

Taseron tarafinda IKI BAGIMSIZ satir kurucu vardir: olusturma (`service._build_lines`, tum kalemler
otomatik) ve PUT (`lines._new_line`). IKISI de `source_code` kopyalar; biri atlanirsa olusturma ile
PUT ayrisir — bu dosya iki yolu AYRI AYRI sabitler. Snapshot degismezligi, refresh (K8), istemci
422'si ve para etkisizligi de burada.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

pytestmark = pytest.mark.asyncio

KOD = "25.200.2001"
YENI_KOD = "25.200.9009"


def _kalemler(contract):
    return sorted(contract.items, key=lambda item: item.sort_order)


async def _kodlu_sozlesme(seeded_db: AsyncSession, taseron_sozlesmesi):
    contract, _, _ = taseron_sozlesmesi
    birinci, ikinci = _kalemler(contract)
    birinci.source_code = KOD  # ikinci kalem kodsuz (NULL) kalir
    await seeded_db.flush()
    return contract, birinci, ikinci


async def _olustur(client: AsyncClient, headers, contract_id) -> dict:
    yanit = await client.post(
        f"/subcontractor-contracts/{contract_id}/progress-payments", json={}, headers=headers
    )
    assert yanit.status_code == 201, yanit.text
    return yanit.json()


async def _put(client: AsyncClient, headers, payment_id, satirlar: list[dict]):
    return await client.put(
        f"/subcontractor-progress-payments/{payment_id}/lines",
        json={"lines": satirlar},
        headers=headers,
    )


def _satir(kalem, quantity="3", **ekstra) -> dict:
    return {"contract_item_id": str(kalem.id), "quantity": quantity} | ekstra


def _bul(govde: dict, kalem) -> dict:
    return next(s for s in govde["lines"] if s["contract_item_id"] == str(kalem.id))


async def test_olusturma_build_lines_kodu_kopyalar_kodsuz_null(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    """Yol 1: `service._build_lines` (tum kalemler otomatik)."""
    contract, birinci, ikinci = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    hakedis = await _olustur(client, admin_headers, contract.id)
    assert _bul(hakedis, birinci)["source_code"] == KOD
    assert _bul(hakedis, ikinci)["source_code"] is None  # bagli ama kodsuz kalem


async def test_put_new_line_kodu_kopyalar(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    """Yol 2: `lines._new_line` (PUT ile yeniden eklenen satir) — yol 1'den BAGIMSIZ kurucu."""
    contract, birinci, ikinci = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    hakedis = await _olustur(client, admin_headers, contract.id)
    assert (await _put(client, admin_headers, hakedis["id"], [])).status_code == 200
    govde = (
        await _put(client, admin_headers, hakedis["id"], [_satir(birinci), _satir(ikinci)])
    ).json()
    assert _bul(govde, birinci)["source_code"] == KOD
    assert _bul(govde, ikinci)["source_code"] is None


async def test_iki_yol_ayni_sonucu_verir_olusturma_put_ayrismaz(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    contract, birinci, ikinci = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    olusan = await _olustur(client, admin_headers, contract.id)
    await _put(client, admin_headers, olusan["id"], [])
    yeniden = (
        await _put(client, admin_headers, olusan["id"], [_satir(birinci), _satir(ikinci)])
    ).json()
    for kalem in (birinci, ikinci):
        assert _bul(yeniden, kalem)["source_code"] == (KOD if kalem is birinci else None)


async def test_kalem_kodu_sonradan_degisince_mevcut_satir_eski_kodu_korur(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    contract, birinci, _ = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    hakedis = await _olustur(client, admin_headers, contract.id)
    satir_id = _bul(hakedis, birinci)["id"]

    birinci.source_code = YENI_KOD  # DB'de dogrudan degisim
    await seeded_db.flush()

    ikinci = (await _put(client, admin_headers, hakedis["id"], [_satir(birinci, "7")])).json()
    satir = _bul(ikinci, birinci)
    assert satir["id"] == satir_id
    assert satir["source_code"] == KOD  # PUT snapshot'i TAZELEMEZ


async def test_refresh_kodu_tazeler_tek_basina_degisti_sayilir_no_op_sifir(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    """K8: altili karsilastirma. Baska hicbir alan degismeden YALNIZ kod degisince satir sayilir."""
    contract, birinci, _ = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    hakedis = await _olustur(client, admin_headers, contract.id)
    url = f"/subcontractor-progress-payments/{hakedis['id']}/refresh-prices"

    assert (await client.post(url, headers=admin_headers)).json()["refreshed_count"] == 0

    birinci.source_code = YENI_KOD
    await seeded_db.flush()
    assert (await client.post(url, headers=admin_headers)).json()["refreshed_count"] == 1
    detay = (
        await client.get(f"/subcontractor-progress-payments/{hakedis['id']}", headers=admin_headers)
    ).json()
    assert _bul(detay, birinci)["source_code"] == YENI_KOD

    assert (await client.post(url, headers=admin_headers)).json()["refreshed_count"] == 0


async def test_istemci_govdesinde_source_code_422(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    contract, birinci, _ = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    hakedis = await _olustur(client, admin_headers, contract.id)
    for deger in ("99.999", None):
        yanit = await _put(
            client, admin_headers, hakedis["id"], [_satir(birinci, source_code=deger)]
        )
        assert yanit.status_code == 422, yanit.text


async def test_kod_para_alanlarini_etkilemez_hakedis_code_degismez(
    client: AsyncClient, admin_headers, taseron_sozlesmesi, seeded_db: AsyncSession
) -> None:
    contract, birinci, _ = await _kodlu_sozlesme(seeded_db, taseron_sozlesmesi)
    hakedis = await _olustur(client, admin_headers, contract.id)
    satir = _bul(
        (await _put(client, admin_headers, hakedis["id"], [_satir(birinci, "2")])).json(), birinci
    )
    assert satir["code"] == birinci.code
    assert Decimal(satir["contract_unit_price"]) == birinci.unit_price
    assert Decimal(satir["line_total"]) == birinci.unit_price * 2
