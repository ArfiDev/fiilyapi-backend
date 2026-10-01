"""GKS-B1 — G4 bekçisi (PLANLAMA-SPEC §3.14): kalem TAMAMEN tahsisliyse Bölümsüz iskelet AÇILMAZ.

Bugüne dek backend'de uygulanmamıştı (iskelet her kalem için Bölümsüz açıyordu). POST ve GET
(önizleme) AYNI saf fonksiyondan beslendiği için ikisi de ölçülür. Mutasyon: `skeleton_keys`te
tam tahsis dalını kaldır → bu dosya kırmızı.
"""

import pytest
from httpx import AsyncClient

from tests.site_diary._gks_b1 import GUN, olustur, onizleme, satir
from tests.site_diary.conftest import KarisikSantiye

pytestmark = pytest.mark.asyncio

_TAM_TAHSISLI = ("03", "04")
_KISMI_VEYA_TAHSISSIZ = ("01", "02", "05")


def _bolumsuz_kodlar(satirlar: list[dict]) -> set[str]:
    return {s["code"] for s in satirlar if s["section_id"] is None}


async def test_POST_tam_tahsisli_kalemde_bolumsuz_satir_acilmaz(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    yanit = await olustur(client, admin_headers, ks.site.id)
    bolumsuz = _bolumsuz_kodlar(yanit.json()["lines"])
    assert not bolumsuz & set(_TAM_TAHSISLI)
    assert bolumsuz == set(_KISMI_VEYA_TAHSISSIZ)  # kalan kalemler Bölümsüz'ü KORUR


async def test_GET_tam_tahsisli_kalemde_bolumsuz_satir_acilmaz(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    yanit = await onizleme(client, admin_headers, ks.site.id)
    bolumsuz = _bolumsuz_kodlar(yanit.json()["lines"])
    assert not bolumsuz & set(_TAM_TAHSISLI)
    assert bolumsuz == set(_KISMI_VEYA_TAHSISSIZ)


async def test_tam_tahsisli_kalem_gorunur_kalir_tahsisli_oldugu_her_bolum_icin_satir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    yanit = await onizleme(client, admin_headers, karisik_santiye.site.id)
    uc = [
        (s["section_name"], s["planned_quantity"])
        for s in yanit.json()["lines"]
        if s["code"] == "03"
    ]
    assert uc == [("S1", "70.000"), ("S2", "30.000")]


async def test_gerekirse_bolumsuz_satir_govdeden_elle_eklenir(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    """G4: "gerekirse '+ Bölüm' menüsünde Bölümsüz seçeneğiyle eklenir" — gövde Bölümsüz'ü
    (her zaman yazılabilir) ekleyebilir."""
    ks = karisik_santiye
    yanit = await olustur(client, admin_headers, ks.site.id, lines=[satir(ks, "04", "1", None)])
    assert yanit.status_code == 201, yanit.text
    assert "04" in _bolumsuz_kodlar(yanit.json()["lines"])


async def test_B2_oncesi_imzali_PUT_G4_iskeletinde_409_STALE_CLIENT_veri_silinmez(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    """DOKÜMANTE SONUÇ: `section_id` anahtarsız (B2 öncesi) PUT, kayıtta bölümlü satır varken
    409 alır (`assert_current_line_client`); G4 sonrası tam tahsisli kalemde iskelet bölümlü
    olduğundan bu imza ARTIK yeni günlükte de 409'dur. Yeni istemci anahtarı yollar → geçer."""
    ks = karisik_santiye
    kayit = await olustur(client, admin_headers, ks.site.id)
    eski = await client.put(
        f"/diary/{kayit.json()['id']}/lines",
        json={"lines": [{"boq_item_id": str(ks.items["04"].id), "quantity": "1"}]},
        headers=admin_headers,
    )
    assert eski.status_code == 409, eski.text
    detay = await client.get(f"/diary/{kayit.json()['id']}", headers=admin_headers)
    assert len(detay.json()["lines"]) == len(kayit.json()["lines"])  # silinmedi

    yeni = await client.put(
        f"/diary/{kayit.json()['id']}/lines",
        json={"lines": [satir(ks, "04", "1", "S1")]},
        headers=admin_headers,
    )
    assert yeni.status_code == 200, yeni.text


async def test_gun_parametresi_sonucu_degistirmez_G4_tarihten_bagimsiz(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    a = await onizleme(client, admin_headers, ks.site.id, GUN)
    from datetime import date

    b = await onizleme(client, admin_headers, ks.site.id, date(2027, 1, 5))
    assert [(s["code"], s["section_id"]) for s in a.json()["lines"]] == [
        (s["code"], s["section_id"]) for s in b.json()["lines"]
    ]
