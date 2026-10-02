"""TKL-B6.8b — donusturmede DUZENLENEBILIR PROJE KODU (BD-2) + YAPISAL 422 (BD-4).

BD-2: `project.code` verilirse projede O kod; verilmezse sunucu uretir (`PRJ-YYYY-NNN`). Cakisan
kod 409 `Bu proje kodu zaten kullanılıyor` (DB UQ'sunun jenerik "Veri bütünlüğü hatası"ndan
METIN farkiyla ayrilir) ve HICBIR sey yazilmaz. Bicim kurali `ProjectCreate.code` ile ayni
(serbest metin 1–50; PRJ-YYYY-NNN ZORLANMAZ).
BD-4: servis dogrulama 422'leri `detail` AYNEN + `errors: [{loc, message}]`.
"""

from __future__ import annotations

import re
import uuid

import pytest
from sqlalchemy import func, select

from app.core.timezone import today
from app.modules.contracts.models import EmployerContractItem
from app.modules.offers.convert_service import PROJECT_CODE_TAKEN
from app.modules.offers.models import Offer
from app.modules.projects.models import Project, ProjectType

from ._convert import Kazanilmis, govde, kazanilmis_teklif, url
from ._offers import URL

pytestmark = pytest.mark.usefixtures("tohum_kancasi")


@pytest.fixture
async def kz(client, admin, isveren, katalog) -> Kazanilmis:
    return await kazanilmis_teklif(client, admin, isveren, katalog, vat_pct="18")


def _kodlu(kz: Kazanilmis, code) -> dict:
    g = govde(kz)
    g["project"]["code"] = code
    return g


async def _proje_kodu(db_session, project_id: str) -> str:
    project = await db_session.get(Project, uuid.UUID(project_id))
    assert project is not None
    return project.code


# --------------------------------------------------------------------------- BD-2


async def test_verilen_kod_projeye_yazilir(client, admin, db_session, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=_kodlu(kz, "OZEL-2026-A"), headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == "OZEL-2026-A"
    assert await _proje_kodu(db_session, resp.json()["project_id"]) == "OZEL-2026-A"


async def test_kod_basindaki_sondaki_bosluk_kirpilir(client, admin, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=_kodlu(kz, "  KRP-1  "), headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == "KRP-1"


async def test_kod_verilmezse_sunucu_uretir(client, admin, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=govde(kz), headers=admin)

    assert resp.status_code == 200, resp.text
    assert resp.json()["project_code"] == f"PRJ-{today().year}-001"


async def test_kod_null_acikca_verilirse_de_uretilir(client, admin, kz) -> None:
    resp = await client.post(url(kz.offer_id), json=_kodlu(kz, None), headers=admin)

    assert resp.status_code == 200, resp.text
    assert re.fullmatch(r"PRJ-\d{4}-\d{3}", resp.json()["project_code"])


async def test_cakisan_kod_409_ozel_metin_hicbir_sey_yazilmadi(
    client, admin, db_session, kz
) -> None:
    db_session.add(Project(code="DOLU-1", name="Mevcut", project_type=ProjectType.taahhut))
    await db_session.flush()

    resp = await client.post(url(kz.offer_id), json=_kodlu(kz, "DOLU-1"), headers=admin)

    assert resp.status_code == 409, resp.text
    # DB UQ'su "Veri butunlugu hatasi" derdi: ozel metin = KONTROL servis katmaninda
    assert resp.json() == {"detail": PROJECT_CODE_TAKEN}
    assert PROJECT_CODE_TAKEN == "Bu proje kodu zaten kullanılıyor"
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 1  # yalniz mevcut
    assert await db_session.scalar(select(func.count()).select_from(EmployerContractItem)) == 0
    offer = await db_session.get(Offer, uuid.UUID(kz.offer_id))
    assert offer is not None and offer.project_id is None and offer.converted_at is None


@pytest.mark.parametrize("kotu", ["", "   ", "K" * 51, 123])
async def test_gecersiz_kod_422_hicbir_sey_yazilmadi(client, admin, db_session, kz, kotu) -> None:
    resp = await client.post(url(kz.offer_id), json=_kodlu(kz, kotu), headers=admin)

    assert resp.status_code == 422, resp.text
    assert any(e["loc"][-2:] == ["project", "code"] for e in resp.json()["detail"])
    assert await db_session.scalar(select(func.count()).select_from(Project)) == 0


async def test_elle_verilen_uyumlu_kod_sayaci_ilerletir_uyumsuz_ilerletmez(
    client, admin, isveren, katalog, kz
) -> None:
    yil = today().year
    ilk = await client.post(url(kz.offer_id), json=_kodlu(kz, f"PRJ-{yil}-007"), headers=admin)
    assert ilk.status_code == 200, ilk.text

    kz2 = await kazanilmis_teklif(client, admin, isveren, katalog)
    ikinci = await client.post(url(kz2.offer_id), json=_kodlu(kz2, f"PRJ-{yil}-A9"), headers=admin)
    assert ikinci.status_code == 200, ikinci.text  # uyumsuz kod: kabul, ama sayilmaz

    kz3 = await kazanilmis_teklif(client, admin, isveren, katalog)
    uretilen = await client.post(url(kz3.offer_id), json=govde(kz3), headers=admin)

    # max+1: uyumlu 007 sayildi (008), uyumsuz A9 sayilmadi
    assert uretilen.json()["project_code"] == f"PRJ-{yil}-008"


# --------------------------------------------------------------------------- BD-4


async def _422(client, admin, kz, g) -> dict:
    resp = await client.post(url(kz.offer_id), json=g, headers=admin)
    assert resp.status_code == 422, resp.text
    return resp.json()


async def test_ayni_adli_grup_loc_ve_detail_ayni(client, admin, kz) -> None:
    g = govde(kz)
    g["groups"][1]["name"] = "Kaba"

    cevap = await _422(client, admin, kz, g)

    assert cevap["detail"] == "groups[1].name: Aynı adlı grup var (Kaba)"
    assert cevap["errors"] == [
        {"loc": ["groups", 1, "name"], "message": "Aynı adlı grup var (Kaba)"}
    ]


async def test_tekrar_eden_kalem_kodu_loc(client, admin, kz) -> None:
    g = govde(kz)
    g["groups"][1]["items"][1]["code"] = "B-01"

    cevap = await _422(client, admin, kz, g)

    assert cevap["detail"] == "groups[1].items[1].code: Kalem kodu tekrar ediyor (B-01)"
    assert [e["loc"] for e in cevap["errors"]] == [["groups", 1, "items", 1, "code"]]


async def test_offer_item_id_hatalari_loc(client, admin, katalog, kz) -> None:
    g = govde(kz)
    g["groups"][0]["items"][0]["offer_item_id"] = str(uuid.uuid4())
    g["groups"][0]["items"][1]["catalog_item_id"] = str(katalog[2].id)

    cevap = await _422(client, admin, kz, g)

    assert [e["loc"] for e in cevap["errors"]] == [
        ["groups", 0, "items", 0, "offer_item_id"],
        ["groups", 0, "items", 1, "offer_item_id"],
    ]
    assert "son revizyonunda bulunamadı" in cevap["errors"][0]["message"]
    assert "uyuşmuyor" in cevap["errors"][1]["message"]


async def test_group_disciplines_bilinmeyen_grup_loc(client, admin, kz) -> None:
    g = govde(kz)
    g["group_disciplines"] = {"Yok": str(uuid.uuid4())}

    cevap = await _422(client, admin, kz, g)

    assert cevap["detail"] == "group_disciplines: «Yok» adlı grup gövdede yok"
    assert cevap["errors"] == [
        {"loc": ["group_disciplines", "Yok"], "message": "«Yok» adlı grup gövdede yok"}
    ]


async def test_fiyat_farki_eksikleri_loc(client, admin, kz) -> None:
    g = govde(kz)
    g["contract"]["has_price_escalation"] = True

    cevap = await _422(client, admin, kz, g)

    assert [e["loc"] for e in cevap["errors"]] == [
        ["contract", "index_type"],
        ["contract", "base_index_value"],
    ]
    assert cevap["detail"] == (
        "contract.index_type: Fiyat farkı açıkken endeks türü zorunludur; "
        "contract.base_index_value: Fiyat farkı açıkken baz endeks zorunludur"
    )


async def test_fiyat_farki_kapaliyken_verilen_alan_loc(client, admin, kz) -> None:
    g = govde(kz)
    g["contract"]["base_index_value"] = "100"

    cevap = await _422(client, admin, kz, g)

    assert cevap["errors"] == [
        {"loc": ["contract", "base_index_value"], "message": "Fiyat farkı kapalıyken verilemez"}
    ]


async def test_bedel_tavani_loc(client, admin, kz) -> None:
    g = govde(kz)
    g["groups"] = g["groups"][:1]
    g["groups"][0]["items"] = g["groups"][0]["items"][:1]
    g["groups"][0]["items"][0].update(quantity="10000", unit_price="1000000000000")  # tam 1e16

    cevap = await _422(client, admin, kz, g)

    assert cevap["errors"] == [
        {"loc": ["contract", "amount"], "message": "Kalem toplamı sözleşme bedeli sınırını aşıyor"}
    ]
    assert cevap["detail"] == "contract.amount: Kalem toplamı sözleşme bedeli sınırını aşıyor"


async def test_coklu_hata_coklu_errors_ve_detail_birlesik(client, admin, kz) -> None:
    g = govde(kz)
    g["groups"][1]["name"] = "Kaba"
    g["groups"][1]["items"][0]["code"] = "B-01"
    g["contract"]["has_price_escalation"] = True

    cevap = await _422(client, admin, kz, g)

    assert [e["loc"] for e in cevap["errors"]] == [
        ["groups", 1, "name"],
        ["groups", 1, "items", 0, "code"],
        ["contract", "index_type"],
        ["contract", "base_index_value"],
    ]
    assert cevap["detail"].split("; ") == [
        "groups[1].name: Aynı adlı grup var (Kaba)",
        "groups[1].items[0].code: Kalem kodu tekrar ediyor (B-01)",
        "contract.index_type: Fiyat farkı açıkken endeks türü zorunludur",
        "contract.base_index_value: Fiyat farkı açıkken baz endeks zorunludur",
    ]


async def test_diger_teklif_422_govdesi_errors_tasimaz(client, admin, isveren) -> None:
    """Yapisal hata YALNIZ donusturme servis dogrulamasinda: baska `OfferValidationError` 422'si
    (TUIK endeksli ama endeks turu yok) yalniz `detail` kalir — govde sekli DEGISMEZ."""
    govde_ = {"employer_id": str(isveren.id), "title": "X", "price_escalation": "tuik"}

    resp = await client.post(URL, json=govde_, headers=admin)

    assert resp.status_code == 422, resp.text
    assert resp.json() == {"detail": "Fiyat farkı «TÜİK endeksli» iken endeks türü zorunludur"}
