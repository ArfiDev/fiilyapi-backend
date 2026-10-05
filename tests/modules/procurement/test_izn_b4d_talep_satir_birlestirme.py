"""IZN-B4d onarımı — talep PATCH `lines`: `id`li satır KISMİ birleştirilir.

Çürütme bulgusu: maliyet_kar gizli rol yalnız miktarı düzeltince `lines` tam değiştirme
yüzünden tüm tahmini fiyatlar siliniyordu (200, toplam 0). Şimdi satır `id`si ile eşleşen
satırın gönderilmeyen alanları korunur, satır kimliği değişmez.
"""

import uuid
from decimal import Decimal

import pytest

from app.core.sayfalar import HiddenCategory
from tests._hassas_alan import rol_gizle

_YOL = "/purchase-requests"


async def _detay(client, headers, talep_id) -> dict:
    yanit = await client.get(f"{_YOL}/{talep_id}", headers=headers)
    assert yanit.status_code == 200, yanit.text
    return yanit.json()


@pytest.mark.asyncio
async def test_gizli_rol_miktari_duzeltince_fiyatlar_ve_satir_idleri_korunur(
    client, sef_headers, satinalma_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("4.000", "1000.00"), ("6.000", "250.00")])
    once = await _detay(client, satinalma_headers, talep.id)
    idler = [satir["id"] for satir in once["lines"]]

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    gorunen = await _detay(client, sef_headers, talep.id)
    assert all(satir["estimated_unit_price"] is None for satir in gorunen["lines"])
    duzelt = await client.patch(
        f"{_YOL}/{talep.id}",
        json={
            "lines": [
                {"id": satir["id"], "quantity": "5.000" if i == 0 else satir["quantity"]}
                for i, satir in enumerate(gorunen["lines"])
            ]
        },
        headers=sef_headers,
    )
    assert duzelt.status_code == 200, duzelt.text

    sonra = await _detay(client, satinalma_headers, talep.id)
    assert [satir["id"] for satir in sonra["lines"]] == idler
    assert [Decimal(satir["estimated_unit_price"]) for satir in sonra["lines"]] == [
        Decimal("1000"),
        Decimal("250"),
    ]
    assert Decimal(sonra["lines"][0]["quantity"]) == Decimal("5")
    assert Decimal(sonra["estimated_total"]) == Decimal("5000") + Decimal("1500")


@pytest.mark.asyncio
async def test_gizli_rol_fiyati_null_gonderirse_403(
    client, sef_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("4.000", "1000.00")])
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    satir = (await _detay(client, sef_headers, talep.id))["lines"][0]

    for fiyat in (None, "9.00"):
        yanit = await client.patch(
            f"{_YOL}/{talep.id}",
            json={"lines": [{"id": satir["id"], "estimated_unit_price": fiyat}]},
            headers=sef_headers,
        )
        assert yanit.status_code == 403, yanit.text


@pytest.mark.asyncio
async def test_idsiz_satir_yenidir_govdede_olmayan_satir_silinir(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("4.000", "1000.00"), ("6.000", "250.00")])
    ilk, ikinci = (await _detay(client, satinalma_headers, talep.id))["lines"]

    yanit = await client.patch(
        f"{_YOL}/{talep.id}",
        json={
            "lines": [
                {"free_text_name": "Yeni kalem", "free_text_unit": "Adet", "quantity": "2"},
                {"id": ilk["id"]},
            ]
        },
        headers=satinalma_headers,
    )
    assert yanit.status_code == 200, yanit.text
    satirlar = (await _detay(client, satinalma_headers, talep.id))["lines"]
    assert len(satirlar) == 2
    assert satirlar[0]["name"] == "Yeni kalem" and satirlar[0]["estimated_unit_price"] is None
    assert satirlar[0]["id"] not in (ilk["id"], ikinci["id"])
    assert satirlar[1]["id"] == ilk["id"]  # korunan satır, sırası gövdeye göre
    assert Decimal(satirlar[1]["estimated_unit_price"]) == Decimal("1000")
    assert ikinci["id"] not in [satir["id"] for satir in satirlar]  # gövdede yoktu → silindi


@pytest.mark.asyncio
async def test_yabanci_veya_olmayan_satir_id_reddedilir(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("4.000", "1000.00")])
    baska = await talep_fabrikasi(gorunen_proje, lines=[("1.000", "77.00")])
    yabanci = (await _detay(client, satinalma_headers, baska.id))["lines"][0]["id"]

    for kimlik in (yabanci, str(uuid.uuid4())):
        yanit = await client.patch(
            f"{_YOL}/{talep.id}",
            json={"lines": [{"id": kimlik, "quantity": "9"}]},
            headers=satinalma_headers,
        )
        assert yanit.status_code == 404, yanit.text
    # Başka talebin satırı DEĞİŞMEDİ.
    kalan = (await _detay(client, satinalma_headers, baska.id))["lines"][0]
    assert Decimal(kalan["quantity"]) == Decimal("1") and kalan["id"] == yabanci


@pytest.mark.asyncio
async def test_ayni_id_iki_kez_ve_bozuk_yeni_satir_reddedilir(
    client, satinalma_headers, gorunen_proje, talep_fabrikasi
):
    talep = await talep_fabrikasi(gorunen_proje, lines=[("4.000", "1000.00")])
    satir = (await _detay(client, satinalma_headers, talep.id))["lines"][0]
    cift = await client.patch(
        f"{_YOL}/{talep.id}",
        json={"lines": [{"id": satir["id"]}, {"id": satir["id"]}]},
        headers=satinalma_headers,
    )
    assert cift.status_code == 422, cift.text
    eksik = await client.patch(
        f"{_YOL}/{talep.id}",
        json={"lines": [{"free_text_name": "x", "free_text_unit": "Adet"}]},  # miktar yok
        headers=satinalma_headers,
    )
    assert eksik.status_code == 422, eksik.text


@pytest.mark.asyncio
async def test_onay_esigi_birlestirilmis_degerle_hesaplanir(
    client, sef_headers, satinalma_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    """Gizli rol miktarı büyütür; fiyat korunduğu için talep toplamı (eşik girdisi) güncellenir."""
    talep = await talep_fabrikasi(gorunen_proje, lines=[("400.000", "1000.00")])
    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    satir = (await _detay(client, sef_headers, talep.id))["lines"][0]
    yanit = await client.patch(
        f"{_YOL}/{talep.id}",
        json={"lines": [{"id": satir["id"], "quantity": "600.000"}]},
        headers=sef_headers,
    )
    assert yanit.status_code == 200, yanit.text
    sonra = await _detay(client, satinalma_headers, talep.id)
    assert Decimal(sonra["estimated_total"]) == Decimal("600000")


@pytest.mark.asyncio
async def test_gizli_rol_idsiz_yeni_satirda_fiyat_gonderemez_403(
    client, sef_headers, satinalma_headers, seeded_db, gorunen_proje, talep_fabrikasi
):
    """PR4 — PATCH kalem girdisinin `estimated_unit_price` yazma kapısı: `id`li de `id`siz de 403;
    bayraksız rolde aynı gövde 200 (pozitif kontrol)."""
    talep = await talep_fabrikasi(gorunen_proje, lines=[("4.000", "1000.00")])
    yeni = {"free_text_name": "Yeni", "free_text_unit": "Adet", "quantity": "1"}
    acik = await client.patch(
        f"{_YOL}/{talep.id}",
        json={"lines": [{**yeni, "estimated_unit_price": "7.00"}]},
        headers=satinalma_headers,
    )
    assert acik.status_code == 200, acik.text

    await rol_gizle(seeded_db, "site_chief", HiddenCategory.maliyet_kar)
    for fiyat in ("7.00", None):
        yanit = await client.patch(
            f"{_YOL}/{talep.id}",
            json={"lines": [{**yeni, "estimated_unit_price": fiyat}]},
            headers=sef_headers,
        )
        assert yanit.status_code == 403, f"{fiyat!r}: {yanit.text}"


def _serbest(ad: str, miktar: str = "1") -> dict:
    return {"free_text_name": ad, "free_text_unit": "Adet", "quantity": miktar}


@pytest.mark.asyncio
async def test_SOZLESME_post_ve_patch_YANITI_satirlari_govde_sirasiyla_dondurur(
    client, satinalma_headers, gorunen_proje
):
    """FE sözleşmesi: form satır `id`lerini POST/PATCH YANITINDAKİ satırları gövde sırasına
    eşleyerek alır. Garanti: yanıt satırları `sort_order` ASC (= gövde dizini) sırasıyla
    döner; `id`li satırın `id`si korunur, `sort_order`u gövdedeki YENİ yerine eşit olur;
    id'siz yeni satır gövdedeki yerinde doğar."""
    olustur = await client.post(
        _YOL,
        json={
            "project_id": str(gorunen_proje.id),
            "lines": [_serbest("A"), _serbest("B"), _serbest("C")],
        },
        headers=satinalma_headers,
    )
    assert olustur.status_code == 201, olustur.text
    satirlar = olustur.json()["lines"]
    assert [s["name"] for s in satirlar] == ["A", "B", "C"]
    assert [s["sort_order"] for s in satirlar] == [0, 1, 2]
    a, b, c = (s["id"] for s in satirlar)

    # Yeniden sırala + araya yeni satır + B'yi çıkar: gövde = [C, YENİ, A].
    duzelt = await client.patch(
        f"{_YOL}/{olustur.json()['id']}",
        json={"lines": [{"id": c}, _serbest("YENİ", "2"), {"id": a}]},
        headers=satinalma_headers,
    )
    assert duzelt.status_code == 200, duzelt.text
    yanit = duzelt.json()["lines"]
    assert [s["name"] for s in yanit] == ["C", "YENİ", "A"]
    assert [s["sort_order"] for s in yanit] == [0, 1, 2]
    assert yanit[0]["id"] == c and yanit[2]["id"] == a
    assert yanit[1]["id"] not in (a, b, c)
    assert b not in [s["id"] for s in yanit]
