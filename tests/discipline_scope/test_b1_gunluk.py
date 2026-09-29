"""DSC-B1 — günlük okuma uçları (detay · liste · özet) iki aktörlü görünürlük.

Dünya günlükleri: E1 (05.05, başlık S1) 5 satır (I1·S1, I1·S2, I2·S1, I3·S2, NULL·S1) ·
E2 (06.05, başlık S2) 2 satır (I1·bölümsüz, I2·S1) · E3 (taslak) 2 satır (I2·S1, I3·bölümsüz).
Başlık ORTAKTIR (Ü5), satırlar görünür kalemle süzülür (NULL görünmez, Ü1), işçi sayıları
süzülmez (Ü3).
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient

from tests._disiplin_dunyasi import Dunya

D = Decimal
SAHIP = {"civil": "i1", "elek": "i2"}


def _beklenen_satirlar(d: Dunya, kayit: str, aktor: str | None) -> set[str]:
    """Kayıttaki satırlardan `aktor`un göreceklerinin kimliği (atamasız → hepsi)."""
    ad_kimlik = {ad: str(s.id) for ad, s in d.satir.items() if ad.startswith(f"{kayit}_")}
    if aktor is None:
        return set(ad_kimlik.values())
    return {kimlik for ad, kimlik in ad_kimlik.items() if ad.split("_")[1] == SAHIP[aktor]}


def _beklenen_toplam(d: Dunya, kayit: str, aktor: str | None) -> Decimal:
    gorunur = _beklenen_satirlar(d, kayit, aktor)
    return sum(
        (
            (s.quantity * s.unit_price).quantize(D("0.01"))
            for s in d.satir.values()
            if str(s.id) in gorunur
        ),
        D("0.00"),
    )


async def _detay(client, d: Dunya, baslik, kayit: str, kuyruk: str = "") -> dict:
    resp = await client.get(f"/diary/{d.gunluk[kayit].id}{kuyruk}", headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_detay_satirlari_gorunur_kalemle_suzulur_toplam_ve_basliklar_tutarli(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    for kayit in ("e1", "e2", "e3"):
        tam = await _detay(client, dunya, atamasiz, kayit)
        assert {ln["id"] for ln in tam["lines"]} == _beklenen_satirlar(dunya, kayit, None)
        for aktor, baslik in (("civil", civil), ("elek", elek)):
            govde = await _detay(client, dunya, baslik, kayit)
            assert {ln["id"] for ln in govde["lines"]} == _beklenen_satirlar(dunya, kayit, aktor)
            assert D(govde["lines_total"]) == _beklenen_toplam(dunya, kayit, aktor)
            # ortak baslik + kaynak saati (Ü5/Ü3) suzulmez
            for alan in ("status", "work_done", "entry_date", "section_id", "worker_counts"):
                assert govde[alan] == tam[alan], alan
            assert govde["worker_total"] == tam["worker_total"]


async def test_detay_null_satir_hic_kimseye_gorunmez_atamasiz_gorur(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    null_satir = str(dunya.satir["e1_null_s1"].id)
    assert null_satir in {ln["id"] for ln in (await _detay(client, dunya, atamasiz, "e1"))["lines"]}
    for baslik in (civil, elek):
        assert null_satir not in {
            ln["id"] for ln in (await _detay(client, dunya, baslik, "e1"))["lines"]
        }


async def test_detay_bolum_adlari_yalniz_gorunen_satirlardan_gelir(
    client: AsyncClient, dunya: Dunya, civil, elek
) -> None:
    civil_satirlari = (await _detay(client, dunya, civil, "e1"))["lines"]
    elek_satirlari = (await _detay(client, dunya, elek, "e1"))["lines"]
    assert {ln["section_name"] for ln in civil_satirlari} == {"A Blok", "B Blok"}
    assert {ln["section_name"] for ln in elek_satirlari} == {"A Blok"}


async def test_detay_bolum_baglaminda_komsu_kumesi_gorunur_satirlarla_sinirli(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    """E2 (başlık S2) detayında bölüm bağlamı S2: önceki = E1 yalnız görünür bir S2 satırı varsa.
    E1'in S2 satırları I1 (civil) ve I3 (disiplinsiz): civil E1'i görür, elek GÖRMEZ."""
    q = f"?section_id={dunya.s2.id}"
    assert (await _detay(client, dunya, atamasiz, "e2", q))["prev_id"] == str(dunya.gunluk["e1"].id)
    assert (await _detay(client, dunya, civil, "e2", q))["prev_id"] == str(dunya.gunluk["e1"].id)
    assert (await _detay(client, dunya, elek, "e2", q))["prev_id"] is None


async def test_liste_kayit_basi_toplamlar_gorunur_satirlardan_total_kayit_sayisi(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    for aktor, baslik in ((None, atamasiz), ("civil", civil), ("elek", elek)):
        resp = await client.get(f"/sites/{dunya.santiye.id}/diary", headers=baslik)
        assert resp.status_code == 200, resp.text
        govde = resp.json()
        assert govde["total"] == 3  # ortak baslik: kayit sayisi kisitlida da degismez
        toplamlar = {i["id"]: D(i["lines_total"]) for i in govde["items"]}
        for kayit in ("e1", "e2", "e3"):
            assert toplamlar[str(dunya.gunluk[kayit].id)] == _beklenen_toplam(dunya, kayit, aktor)


async def test_liste_bolum_suzgeci_liste_sayac_ve_satir_sayisi_ayni_kumede(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    """S2 Kural A: başlığı S2 olan gün ∪ GÖRÜNÜR bir S2 satırı olan gün. elek'in E1'deki S2
    satırları (I1, I3) görünmez → E1 listeden DÜŞER; liste, `total` ve `section_line_count`
    aynı kümeyi söyler."""
    e1, e2 = str(dunya.gunluk["e1"].id), str(dunya.gunluk["e2"].id)
    beklenen = {"atamasiz": {e1: 2, e2: 0}, "civil": {e1: 1, e2: 0}, "elek": {e2: 0}}
    for ad, baslik in (("atamasiz", atamasiz), ("civil", civil), ("elek", elek)):
        resp = await client.get(
            f"/sites/{dunya.santiye.id}/diary?section_id={dunya.s2.id}", headers=baslik
        )
        assert resp.status_code == 200, resp.text
        govde = resp.json()
        assert {i["id"]: i["section_line_count"] for i in govde["items"]} == beklenen[ad], ad
        assert govde["total"] == len(beklenen[ad]), ad


async def _ozet(client, d: Dunya, baslik) -> dict:
    resp = await client.get(f"/sites/{d.santiye.id}/diary/summary", headers=baslik)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_ozet_kalemler_ve_toplam_kendi_disiplininden(
    client: AsyncClient, dunya: Dunya, atamasiz, civil, elek
) -> None:
    hepsi, c, e = (
        await _ozet(client, dunya, atamasiz),
        await _ozet(client, dunya, civil),
        await _ozet(client, dunya, elek),
    )
    kimlik = {ad: str(k.id) for ad, k in dunya.i.items()}
    assert {i["boq_item_id"] for i in hepsi["items"]} == {kimlik["i1"], kimlik["i2"], kimlik["i3"]}
    assert {i["boq_item_id"] for i in c["items"]} == {kimlik["i1"]}
    assert {i["boq_item_id"] for i in e["items"]} == {kimlik["i2"]}
    for govde in (c, e):
        assert D(govde["total_amount"]) == sum((D(i["amount"]) for i in govde["items"]), D(0))
        assert govde["entry_count"] == hepsi["entry_count"]  # ortak baslik
    assert D(hepsi["total_amount"]) == sum((D(i["amount"]) for i in hepsi["items"]), D(0))


async def test_ozet_sozlesme_kalemi_alanlari_satirla_birlikte_suzulur(
    client: AsyncClient, dunya: Dunya, civil, elek
) -> None:
    """K7: `contract_item_*` (id · miktar · BİRİM FİYAT) yalnız görünür kalemin satırında."""
    c = (await _ozet(client, dunya, civil))["items"][0]
    e = (await _ozet(client, dunya, elek))["items"][0]
    assert (D(c["contract_item_quantity"]), D(c["contract_item_unit_price"])) == (D(120), D(11))
    assert (D(e["contract_item_quantity"]), D(e["contract_item_unit_price"])) == (D(60), D(22))
    assert c["contract_item_id"] != e["contract_item_id"]
