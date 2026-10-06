# ruff: noqa: F811  (admin/kab fiksturleri test_catalog_items_api.py'den ithal edilir)
"""KAT-B1 — katalog kalemine Bakanlik kaynak kodu (`source_code`) + fiyat tarihi (`ref_price_date`).

Tekil uclar (`POST/PATCH /catalog/items`) + liste aramasi + alan maskesi + Excel + DB kismi UQ.
Semantik madde harfleri gorev metnindeki 2a–2f'dir (her test adinda).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from io import BytesIO

import openpyxl
import pytest
from sqlalchemy.exc import IntegrityError

from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.catalog.export import COLUMN_HEADERS
from app.modules.catalog.models import ContractorType, EvCatalogItem
from tests._hassas_alan import rol_gizle
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from .._boq import _auth, _login_with_access
from .test_catalog_items_api import (  # noqa: F401  (fikstur + yardimcilar yeniden kullanilir)
    URL,
    _disiplin,
    _giris,
    _govde,
    admin,
    kab,
)

pytestmark = pytest.mark.asyncio

XLSX_URL = f"{URL}/export"


async def _ekle(client, admin, kab, **over) -> dict:
    resp = await client.post(URL, json=_govde(kab, **over), headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def _yama(client, admin, item_id, **govde):
    return await client.patch(f"{URL}/{item_id}", json=govde, headers=admin)


# ------------------------------------------------------------ olusturma


async def test_olustur_kaynak_kodu_kirpilir_ve_fiyat_tarihi_doner(client, admin, kab) -> None:
    body = await _ekle(
        client,
        admin,
        kab,
        source_code="  15.100.1001  ",
        ref_price="250.00",
        ref_price_date="2026-01-01",
    )
    assert body["source_code"] == "15.100.1001"
    assert body["ref_price_date"] == "2026-01-01"
    assert body["price_updated_at"] is not None
    assert body["poz_no"] == "KAB-0001"  # bizim numara AYNEN uretilir, kaynak kodu ayri


async def test_bos_ve_yalniz_bosluk_kaynak_kodu_NULL_olur_iki_kalem_serbest(
    client, admin, kab
) -> None:
    a = await _ekle(client, admin, kab, name="A", source_code="   ")
    b = await _ekle(client, admin, kab, name="B", source_code="")
    c = await _ekle(client, admin, kab, name="C")
    assert a["source_code"] is None and b["source_code"] is None and c["source_code"] is None


async def test_kaynak_kodu_33_karakter_422(client, admin, kab) -> None:
    resp = await client.post(URL, json=_govde(kab, source_code="1" * 33), headers=admin)
    assert resp.status_code == 422
    assert "source_code" in str(resp.json()["detail"])
    ok = await client.post(URL, json=_govde(kab, source_code="1" * 32), headers=admin)
    assert ok.status_code == 201


async def test_2a_olusturmada_fiyatsiz_fiyat_tarihi_422_alan_adli(client, admin, kab) -> None:
    resp = await client.post(URL, json=_govde(kab, ref_price_date="2026-01-01"), headers=admin)
    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert "Fiyat tarihi" in body["detail"]
    assert body["errors"][0]["loc"] == ["body", "ref_price_date"]
    # hicbir sey yazilmadi
    assert (await client.get(URL, headers=admin)).json()["items"] == []


# ------------------------------------------------------------ PATCH semantigi


async def test_2a_patchte_fiyati_olmayan_kaleme_tarih_422(client, admin, kab) -> None:
    item = await _ekle(client, admin, kab)
    resp = await _yama(client, admin, item["id"], ref_price_date="2026-01-01")
    assert resp.status_code == 422, resp.text
    assert resp.json()["errors"][0]["loc"] == ["body", "ref_price_date"]


async def test_2a_ayni_istekte_fiyat_NULL_yapilirken_tarih_verilirse_422(
    client, admin, kab
) -> None:
    item = await _ekle(client, admin, kab, ref_price="10.00", ref_price_date="2026-01-01")
    resp = await _yama(client, admin, item["id"], ref_price=None, ref_price_date="2026-02-01")
    assert resp.status_code == 422, resp.text
    # reddedilen istek hicbir seyi degistirmedi
    kalan = (await client.get(URL, headers=admin)).json()["items"][0]
    assert kalan["ref_price"] == "10.00" and kalan["ref_price_date"] == "2026-01-01"


async def test_2a_fiyat_NULL_yapilinca_tarih_de_temizlenir(client, admin, kab) -> None:
    item = await _ekle(client, admin, kab, ref_price="10.00", ref_price_date="2026-01-01")
    resp = await _yama(client, admin, item["id"], ref_price=None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["ref_price"] is None and resp.json()["ref_price_date"] is None


async def test_2b_fiyat_degisir_tarih_verilir_tarih_verilen_damga_ilerler(
    client, admin, kab
) -> None:
    item = await _ekle(client, admin, kab, ref_price="10.00", ref_price_date="2025-01-01")
    onceki = datetime.fromisoformat(item["price_updated_at"])
    resp = await _yama(client, admin, item["id"], ref_price="12.00", ref_price_date="2026-01-01")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ref_price_date"] == "2026-01-01"
    assert datetime.fromisoformat(resp.json()["price_updated_at"]) > onceki


async def test_2c_fiyat_degisir_tarih_verilmez_tarih_NULL_olur_damga_ilerler(
    client, admin, kab
) -> None:
    item = await _ekle(client, admin, kab, ref_price="10.00", ref_price_date="2025-01-01")
    onceki = datetime.fromisoformat(item["price_updated_at"])
    resp = await _yama(client, admin, item["id"], ref_price="12.00")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ref_price_date"] is None  # bayat tarih birakilmaz
    assert datetime.fromisoformat(resp.json()["price_updated_at"]) > onceki


async def test_2d_yalniz_tarih_degisir_damga_DEGISMEZ(client, admin, kab) -> None:
    item = await _ekle(client, admin, kab, ref_price="10.00", ref_price_date="2025-01-01")
    resp = await _yama(client, admin, item["id"], ref_price_date="2026-01-01")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ref_price_date"] == "2026-01-01"
    assert resp.json()["price_updated_at"] == item["price_updated_at"]
    assert resp.json()["ref_price"] == "10.00"


async def test_2d_ayni_fiyat_farkli_yazimla_tarih_verilmezse_tarih_KORUNUR(
    client, admin, kab
) -> None:
    item = await _ekle(client, admin, kab, ref_price="10.00", ref_price_date="2025-01-01")
    resp = await _yama(client, admin, item["id"], ref_price="10.0")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ref_price_date"] == "2025-01-01"
    assert resp.json()["price_updated_at"] == item["price_updated_at"]


async def test_2d_fiyatsiz_kaleme_fiyat_ve_tarih_birlikte_verilebilir(client, admin, kab) -> None:
    item = await _ekle(client, admin, kab)
    resp = await _yama(client, admin, item["id"], ref_price="5.00", ref_price_date="2026-01-01")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ref_price_date"] == "2026-01-01"
    assert resp.json()["price_updated_at"] is not None


async def test_2f_acik_null_kaynak_kodu_ve_tarihi_temizler_zorunlu_alan_null_hala_422(
    client, admin, kab
) -> None:
    item = await _ekle(
        client, admin, kab, source_code="15.100.1001", ref_price="1.00", ref_price_date="2026-01-01"
    )
    resp = await _yama(client, admin, item["id"], source_code=None, ref_price_date=None)
    assert resp.status_code == 200, resp.text
    assert resp.json()["source_code"] is None and resp.json()["ref_price_date"] is None
    assert resp.json()["ref_price"] == "1.00"
    bad = await _yama(client, admin, item["id"], name=None)
    assert bad.status_code == 422
    assert "name" in str(bad.json())


# ------------------------------------------------------------ 2e: tekillik


async def test_2e_tekrar_eden_kaynak_kodu_POST_409(client, admin, kab) -> None:
    await _ekle(client, admin, kab, name="A", source_code="15.100.1001")
    resp = await client.post(
        URL, json=_govde(kab, name="B", source_code="15.100.1001"), headers=admin
    )
    assert resp.status_code == 409, resp.text
    assert "Kaynak poz no" in resp.json()["detail"] and "KAB-0001" in resp.json()["detail"]
    assert len((await client.get(URL, headers=admin)).json()["items"]) == 1


async def test_2e_kaynak_kodu_PATCH_baska_kalemin_koduna_409_kendi_koduna_200(
    client, admin, kab, db_session
) -> None:
    a = await _ekle(client, admin, kab, name="A", source_code="15.100.1001")
    b = await _ekle(client, admin, kab, name="B", source_code="15.100.1002")
    assert (await _yama(client, admin, b["id"], source_code="15.100.1001")).status_code == 409
    assert (await _yama(client, admin, a["id"], source_code="15.100.1001")).status_code == 200
    # kod tum sirket genelinde tekil: baska disiplinde de 409
    elk = await _disiplin(db_session, "ELK", "Elektrik")
    resp = await client.post(
        URL, json=_govde(elk, name="C", source_code="15.100.1001"), headers=admin
    )
    assert resp.status_code == 409


# ------------------------------------------------------------ DB kismi UQ


async def test_DB_kismi_UQ_iki_NULL_serbest_iki_ayni_deger_cakisir(db_session, seeded_db) -> None:
    disiplin = await _disiplin(db_session, "UQ1")

    def _satir(no: int, kod: str | None) -> EvCatalogItem:
        return EvCatalogItem(
            discipline_id=disiplin.id,
            name=f"k{no}",
            uom="m",
            standard_unit_mhr=Decimal("1"),
            default_contractor_type=ContractorType.OWN,
            poz_no=f"UQ1-{no:04d}",
            source_code=kod,
        )

    db_session.add_all([_satir(1, None), _satir(2, None), _satir(3, "X.1")])
    await db_session.flush()  # iki NULL + bir dolu: serbest
    with pytest.raises(IntegrityError) as exc:
        async with db_session.begin_nested():
            db_session.add(_satir(4, "X.1"))
            await db_session.flush()
    assert "uq_ev_catalog_items_source_code" in str(exc.value.orig)


# ------------------------------------------------------------ arama


async def test_arama_q_kaynak_kodunu_ONEKLE_bulur_ortadan_bulmaz(client, admin, kab) -> None:
    await _ekle(client, admin, kab, name="Beton", source_code="15.100.1001")
    await _ekle(client, admin, kab, name="Kalıp", source_code="16.200.1001")
    onek = (await client.get(URL, params={"q": "15.100"}, headers=admin)).json()["items"]
    assert [i["name"] for i in onek] == ["Beton"]
    tam = (await client.get(URL, params={"q": "16.200.1001"}, headers=admin)).json()["items"]
    assert [i["name"] for i in tam] == ["Kalıp"]
    orta = (await client.get(URL, params={"q": "100.1001"}, headers=admin)).json()["items"]
    assert orta == []  # kaynak kodunda onek eslesmesi (infix degil)
    # mevcut ad / poz no aramasi bozulmadi
    assert len((await client.get(URL, params={"q": "kalıp"}, headers=admin)).json()["items"]) == 1
    assert (
        len((await client.get(URL, params={"q": "KAB-0002"}, headers=admin)).json()["items"]) == 1
    )


async def test_arama_kaynak_kodunda_LIKE_jokeri_kacirilir(client, admin, kab) -> None:
    await _ekle(client, admin, kab, name="Beton", source_code="15.100.1001")
    assert (await client.get(URL, params={"q": "1%"}, headers=admin)).json()["items"] == []
    assert (await client.get(URL, params={"q": "1_.100"}, headers=admin)).json()["items"] == []


# ------------------------------------------------------------ maske + Excel


async def test_limited_kapsamda_fiyat_tarihi_gizli_kaynak_kodu_gorunur(
    client, admin, kab, db_session, user_factory
) -> None:
    await _ekle(
        client, admin, kab, source_code="15.100.1001", ref_price="9.90", ref_price_date="2026-01-01"
    )
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await rol_gizle(db_session, "accounting", HiddenCategory.sozlesme_fiyat)
    sinirli = await _giris(client, db_session, user_factory, "accounting")
    gizli = (await client.get(URL, headers=sinirli)).json()["items"][0]
    assert gizli["ref_price"] is None and gizli["ref_price_date"] is None
    assert gizli["source_code"] == "15.100.1001"
    acik = (await client.get(URL, headers=admin)).json()["items"][0]
    assert acik["ref_price_date"] == "2026-01-01"  # pozitif kontrol


def _satirlar(resp) -> list[tuple]:
    assert resp.status_code == 200, resp.text
    sayfa = openpyxl.load_workbook(BytesIO(resp.content)).active
    return [tuple(c.value for c in satir) for satir in sayfa.iter_rows()]


async def test_excel_yeni_iki_sutun_SONDA_eski_sutunlar_ayni(client, admin, kab) -> None:
    await _ekle(
        client, admin, kab, source_code="15.100.1001", ref_price="9.90", ref_price_date="2026-01-01"
    )
    await _ekle(client, admin, kab, name="Kodsuz")
    satirlar = _satirlar(await client.get(XLSX_URL, headers=admin))
    assert satirlar[0][-2:] == ("Kaynak Poz No", "Fiyat Tarihi")
    assert satirlar[0][:12] == COLUMN_HEADERS[:12]
    assert satirlar[0][0] == "Poz No" and satirlar[0][11] == "Varsayılan Yüklenici"
    assert satirlar[1][-2:] == ("15.100.1001", "01.01.2026")
    assert satirlar[1][4] == "9.90"
    assert satirlar[2][-2:] == (None, None)  # kodsuz / tarihsiz kalem: BOS hucre
    assert len(satirlar[1]) == 14


async def test_excel_limited_rolde_fiyat_tarihi_bos_kaynak_poz_no_gorunur(
    client, admin, kab, db_session, user_factory
) -> None:
    await _ekle(
        client, admin, kab, source_code="15.100.1001", ref_price="9.90", ref_price_date="2026-01-01"
    )
    await modul_duzeyi_yaz(db_session, "accounting", "contracts", AccessLevel.view)
    await rol_gizle(db_session, "accounting", HiddenCategory.sozlesme_fiyat)
    token = await _login_with_access(
        client, db_session, user_factory, "accounting", f"lim.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    satir = _satirlar(await client.get(XLSX_URL, headers=_auth(token)))[1]
    assert satir[4] is None  # referans fiyat gizli
    assert satir[-2:] == ("15.100.1001", None)  # kaynak poz no gorunur, fiyat tarihi gizli
