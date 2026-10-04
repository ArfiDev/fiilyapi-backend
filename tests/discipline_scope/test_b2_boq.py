"""DSC-B2 — BOQ YAZMA uçları iki aktörlü görünürlük + Ü6 403'leri.

F1: yazma testleri PM ile DEĞİL patron (`_F`) rolüyle koşar — PM'in `boq` yetkisi yazıyı
kapsasa da günlükte `view`dir; burada tüm aktörler yazabilir, yani bir 403/404 İZİN
kapısından değil DİSİPLİN kapısından gelir. Her 403 testinde POZİTİF KONTROL: aynı roldeki
ATAMASIZ eş aynı isteği yapabilir (kapı disiplin kapısı; herkese 403 veren bir bağımlılık
kırmızı olur).

Dünya: I1(G1→KAB=civil_yazar) · I2(G2→DUV=elek_yazar) · I3(G3 eşlemesiz → kısıtlıya görünmez).
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from app.core.discipline_scope import DisciplineScope, visible_group_set, visible_item_set
from app.modules.boq.models import BoqItem
from tests._disiplin_dunyasi import Dunya
from tests.discipline_scope._b2_yardim import YETKI_YOK, YOK_KIMLIK, bos_grup_ac, ham_satirlar, ozet

YAZANLAR = True


def _kalem_govdesi(grup, kod: str = "09.001") -> dict:
    return {
        "group_id": str(grup),
        "code": kod,
        "description": "Yeni",
        "unit": "m2",
        "quantity": "5",
        "unit_price": "2",
    }


# ------------------------------------------------------------------ Ü6: 403 (grup aç / sil)


async def test_grup_acmak_kisitliya_403_atamasiz_esine_serbest(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    url = f"/sites/{dunya.santiye.id}/boq/groups"
    resp = await client.post(url, headers=civil_yazar, json={"name": "Yeni"})
    assert (resp.status_code, resp.json()) == (403, YETKI_YOK)
    pozitif = await client.post(url, headers=yazar_atamasiz, json={"name": "Yeni"})
    assert pozitif.status_code == 201, pozitif.text  # aynı rol, atamasız → 403 DEĞİL


async def test_grup_silmek_kisitli_admine_403_atamasiz_admine_serbest(
    client: AsyncClient, dunya: Dunya, admin_kisitli, atamasiz
) -> None:
    grup = await client.post(
        f"/sites/{dunya.santiye.id}/boq/groups", headers=atamasiz, json={"name": "Silinecek"}
    )
    gid = grup.json()["id"]
    # SIL-B1: DELETE'te disiplin kisiti UYGULANMAZ ("her kosulda Sistem Yoneticisi"); kisitli
    # (disiplin atanmis) Sistem Yoneticisi de siler. Sistem Yoneticisi olmayan 403 alir.
    resp = await client.delete(f"/boq/groups/{gid}", headers=admin_kisitli)
    assert resp.status_code == 204, resp.text
    grup2 = await client.post(
        f"/sites/{dunya.santiye.id}/boq/groups", headers=atamasiz, json={"name": "Silinecek 2"}
    )
    pozitif = await client.delete(f"/boq/groups/{grup2.json()['id']}", headers=atamasiz)
    assert pozitif.status_code == 204, pozitif.text


# ------------------------------------------------------------------ Ü7: yabancı == olmayan


async def test_kalem_ekleme_yabanci_eslemesiz_ve_olmayan_grup_ayni_422(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    url = f"/sites/{dunya.santiye.id}/boq/items"
    olmayan = ozet(await client.post(url, headers=civil_yazar, json=_kalem_govdesi(YOK_KIMLIK)))
    assert olmayan[0] == 422
    for grup in (dunya.g["g2"], dunya.g["g3"]):  # yabancı (DUV) · eşlemesiz
        yabanci = await client.post(url, headers=civil_yazar, json=_kalem_govdesi(grup.id))
        assert ozet(yabanci) == olmayan
        pozitif = await client.post(
            url, headers=yazar_atamasiz, json=_kalem_govdesi(grup.id, f"P.{grup.sort_order}")
        )
        assert pozitif.status_code == 201, pozitif.text
    kendi = await client.post(url, headers=civil_yazar, json=_kalem_govdesi(dunya.g["g1"].id))
    assert kendi.status_code == 201, kendi.text


async def test_grup_guncelleme_yabanci_ve_olmayan_ayni_404_kendi_grubu_serbest(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    olmayan = ozet(
        await client.patch(f"/boq/groups/{YOK_KIMLIK}", headers=civil_yazar, json={"name": "X"})
    )
    assert olmayan[0] == 404
    for grup in (dunya.g["g2"], dunya.g["g3"]):
        resp = await client.patch(f"/boq/groups/{grup.id}", headers=civil_yazar, json={"name": "X"})
        assert ozet(resp) == olmayan
        pozitif = await client.patch(
            f"/boq/groups/{grup.id}", headers=yazar_atamasiz, json={"name": "Y"}
        )
        assert pozitif.status_code == 200, pozitif.text
    kendi = await client.patch(
        f"/boq/groups/{dunya.g['g1'].id}", headers=civil_yazar, json={"name": "Betonarme II"}
    )
    assert kendi.status_code == 200 and kendi.json()["name"] == "Betonarme II"
    assert [i["code"] for i in kendi.json()["items"]] == ["01.001"]  # yanıt yalnız kendi kalemi


async def test_kalem_guncelleme_tahsis_silme_yabanci_ve_olmayan_ayni_404(
    client: AsyncClient, dunya: Dunya, civil_yazar, admin_kisitli, yazar_atamasiz, atamasiz
) -> None:
    i2, i3 = dunya.i["i2"], dunya.i["i3"]
    istekler = (
        (
            "patch",
            lambda k, h: client.patch(f"/boq/items/{k}", headers=h, json={"description": "Z"}),
        ),
        (
            "put",
            lambda k, h: client.put(
                f"/boq/items/{k}/allocations", headers=h, json={"allocations": []}
            ),
        ),
        ("delete", lambda k, h: client.delete(f"/boq/items/{k}", headers=h)),
    )
    for ad, istek in istekler:
        yazar = admin_kisitli if ad == "delete" else civil_yazar  # DELETE `boq:admin` ister
        olmayan = ozet(await istek(YOK_KIMLIK, yazar))
        assert olmayan[0] == 404, ad
        for kalem in (i2, i3):
            assert ozet(await istek(kalem.id, yazar)) == olmayan, (ad, kalem.code)
    # pozitif kontrol: atamasız eşler (i2 üzerinde aynı istekler) 404 DEĞİL
    assert (
        # SZK-B1: i2 sözleşmeye bağlı → description kilitli; pozitif kontrol kilitsiz alanla.
        await client.patch(f"/boq/items/{i2.id}", headers=yazar_atamasiz, json={"sort_order": 9})
    ).status_code == 200
    assert (
        await client.put(
            f"/boq/items/{i2.id}/allocations", headers=yazar_atamasiz, json={"allocations": []}
        )
    ).status_code == 200
    assert (
        await client.delete(f"/boq/items/{i3.id}", headers=atamasiz)
    ).status_code == 409  # 404 DEĞİL


async def test_kalem_tasima_yabanci_grup_ayni_422_kendi_grup_serbest(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    url = f"/boq/items/{dunya.i['i1'].id}"
    olmayan = ozet(await client.patch(url, headers=civil_yazar, json={"group_id": str(YOK_KIMLIK)}))
    assert olmayan[0] == 422
    for grup in (dunya.g["g2"], dunya.g["g3"]):
        yabanci = await client.patch(url, headers=civil_yazar, json={"group_id": str(grup.id)})
        assert ozet(yabanci) == olmayan
    tasi = await client.patch(url, headers=yazar_atamasiz, json={"group_id": str(dunya.g["g2"].id)})
    assert tasi.status_code == 200, tasi.text  # atamasız eş yabancı gruba taşıyabilir
    geri = await client.patch(url, headers=civil_yazar, json={"group_id": str(dunya.g["g1"].id)})
    assert geri.status_code == 404  # taşınan kalem artık civil'e görünmez (kalem disiplini gruptan)


async def test_kendi_kalemini_admin_kapisiyla_siler_yabanci_404(
    client: AsyncClient, dunya: Dunya, admin_kisitli
) -> None:
    """S6: kısıtlı admin KENDİ (günlük satırsız) kalemini siler; yabancı kalem 404."""
    yeni = await client.post(
        f"/sites/{dunya.santiye.id}/boq/items",
        headers=admin_kisitli,
        json=_kalem_govdesi(dunya.g["g1"].id),
    )
    assert yeni.status_code == 201, yeni.text
    assert (
        await client.delete(f"/boq/items/{yeni.json()['id']}", headers=admin_kisitli)
    ).status_code == 204
    assert (
        await client.delete(f"/boq/items/{dunya.i['i2'].id}", headers=admin_kisitli)
    ).status_code == 404


# ------------------------------------------------------------------ boş grup görünürlüğü


async def test_bos_kendi_grubu_gorunur_yabanci_ve_eslemesiz_bos_grup_gorunmez(
    client: AsyncClient, seeded_db, dunya: Dunya, atamasiz, civil_yazar, elek_yazar
) -> None:
    kendi = await bos_grup_ac(seeded_db, dunya, "bos_kab", 1, dunya.kab.id)
    yabanci = await bos_grup_ac(seeded_db, dunya, "bos_duv", 2, dunya.duv.id)
    esleme_yok = await bos_grup_ac(seeded_db, dunya, "bos_yok", 3, None)
    url = f"/sites/{dunya.santiye.id}/boq"
    civil = {g["id"]: g for g in (await client.get(url, headers=civil_yazar)).json()["groups"]}
    elek = {g["id"] for g in (await client.get(url, headers=elek_yazar)).json()["groups"]}
    hepsi = {g["id"] for g in (await client.get(url, headers=atamasiz)).json()["groups"]}
    assert str(kendi.id) in civil and civil[str(kendi.id)]["items"] == []
    assert str(yabanci.id) not in civil and str(esleme_yok.id) not in civil
    assert str(yabanci.id) in elek and str(kendi.id) not in elek
    assert {str(kendi.id), str(yabanci.id), str(esleme_yok.id)} <= hepsi  # atamasız bugünkü gibi
    # bölüm süzgecinde boş grup eskisi gibi düşer
    bolumlu = (await client.get(f"{url}?section_id={dunya.s1.id}", headers=civil_yazar)).json()
    assert str(kendi.id) not in {g["id"] for g in bolumlu["groups"]}
    # boş kendi grubuna kalem eklenir
    ekle = await client.post(
        f"{url}/items", headers=civil_yazar, json=_kalem_govdesi(kendi.id, "BOS.001")
    )
    assert ekle.status_code == 201, ekle.text


async def test_reddedilen_yabanci_istekler_db_yi_degistirmez(
    client: AsyncClient, seeded_db, dunya: Dunya, civil_yazar, admin_kisitli
) -> None:
    """404/422 ile reddedilen yazmalar HİÇBİR satıra dokunmaz (ham SELECT öncesi == sonrası)."""
    tablolar = (
        ("boq_items", "site_id = :s"),
        ("boq_groups", "site_id = :s"),
        ("boq_item_section_allocations", "true"),
    )

    async def dokum() -> list:
        return [await ham_satirlar(seeded_db, t, k, s=dunya.santiye.id) for t, k in tablolar]

    once = await dokum()
    i2, g2 = dunya.i["i2"].id, dunya.g["g2"].id
    await client.patch(f"/boq/items/{i2}", headers=civil_yazar, json={"description": "Z"})
    await client.put(
        f"/boq/items/{i2}/allocations",
        headers=civil_yazar,
        json={"allocations": [{"section_id": str(dunya.s1.id), "quantity": "1"}]},
    )
    await client.delete(f"/boq/items/{i2}", headers=admin_kisitli)
    await client.patch(f"/boq/groups/{g2}", headers=civil_yazar, json={"name": "Z"})
    await client.post(
        f"/sites/{dunya.santiye.id}/boq/items", headers=civil_yazar, json=_kalem_govdesi(g2)
    )
    assert await dokum() == once


async def test_kabul_edilen_kahin_poz_kodu_varligi_sizar_baska_hicbir_sey(
    client: AsyncClient, seeded_db, dunya: Dunya, civil_yazar
) -> None:
    """KABUL EDİLEN KÂHİN (CEO kararı 2026-09-29, Ü7 istisnası): UQ `uq_boq_items_site_code`
    şantiye düzeyindedir → civil, kendi grubuna başka disiplinin kodunu (02.001) girince 409,
    olmayan kodu girince 201/200 alır; yani YALNIZ kodun VARLIĞI sızar. 409 gövdesi ad/tutar/
    kimlik taşımaz. Bu test kabul edilen davranışı ÖLÇER: ileride değişirse görünür olur."""
    yabanci = dunya.i["i2"]
    url = f"/sites/{dunya.santiye.id}/boq/items"
    varolan = await client.post(
        url, headers=civil_yazar, json=_kalem_govdesi(dunya.g["g1"].id, yabanci.code)
    )
    assert varolan.status_code == 409, varolan.text
    assert varolan.json() == {"detail": "Bu poz numarası bu şantiyede zaten kullanılıyor"}
    for sizmamali in (yabanci.description, str(yabanci.id), str(yabanci.unit_price)):
        assert sizmamali not in varolan.text
    yok = await client.post(
        url, headers=civil_yazar, json=_kalem_govdesi(dunya.g["g1"].id, "7.777")
    )
    assert yok.status_code == 201, yok.text  # olmayan kod → 201 (kâhin: 409 ↔ 201 farkı)
    # update_item kod değişimi yolu: aynı ayrım. SZK-B1: sözleşmeye bağlı kalemde `code`
    # kilitli (422) → kâhin yolunu ölçmek için kalem sözleşmeden ayrılır (bağsız = serbest).
    dunya.i["i1"].contract_item_id = None
    await seeded_db.flush()
    kendi = f"/boq/items/{dunya.i['i1'].id}"
    cakisan = await client.patch(kendi, headers=civil_yazar, json={"code": yabanci.code})
    assert cakisan.status_code == 409 and cakisan.json() == varolan.json()
    serbest = await client.patch(kendi, headers=civil_yazar, json={"code": "8.888"})
    assert serbest.status_code == 200, serbest.text


@pytest.mark.parametrize("hangisi", ["kab", "duv", "ikisi", "baska"])
async def test_visible_item_set_grup_kumesiyle_tutarli(seeded_db, dunya: Dunya, hangisi) -> None:
    """🔴 Tutarlılık bekçisi: visible_item_set == {kalem | kalem.group_id ∈ visible_group_set}
    (kalem ve grup tek SQL tanımı `_discipline_of`ı paylaşır; ayrışırlarsa kırmızı)."""
    secim = {"kab": [dunya.kab.id], "duv": [dunya.duv.id], "ikisi": [dunya.kab.id, dunya.duv.id]}
    scope = DisciplineScope(frozenset(secim.get(hangisi, [YOK_KIMLIK])))  # baska: eşlenmemiş id
    kalemler = (await seeded_db.execute(select(BoqItem.id, BoqItem.group_id))).all()
    gruplar = {g.id for g in dunya.g.values()} | {k.group_id for k in kalemler}
    gorunur_gruplar = await visible_group_set(seeded_db, scope, gruplar)
    gorunur_kalemler = await visible_item_set(seeded_db, scope, [k.id for k in kalemler])
    assert gorunur_gruplar is not None and gorunur_kalemler is not None
    assert gorunur_kalemler == {k.id for k in kalemler if k.group_id in gorunur_gruplar}
    if hangisi == "kab":
        assert gorunur_kalemler == {dunya.i["i1"].id}
