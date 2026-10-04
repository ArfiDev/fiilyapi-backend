"""DSC-B2 Ü6 — EV yapısal/toplu uçlar KISITLIYA 403 (`RequireUnrestricted`); IZN-B3: şirket geneli
katalog/disiplin yazmaları HARİÇ (artık açık). F1: her 403'ün
yanında aynı roldeki ATAMASIZ eş 2xx (ya da uca özgü iş kuralı 409/422 — ASLA 403) alır.

Bağımlılık ağacı bekçisi `tests/core/test_disiplin_rota_bekcisi.py::U6_ROTALARI`; bu dosya
davranışı her router'dan (catalog · budget · day · report) temsilciyle kilitler.
"""

from __future__ import annotations

from httpx import AsyncClient

from tests._disiplin_dunyasi import GUN2, Dunya, _kimlik
from tests.discipline_scope._b2_yardim import YETKI_YOK

YAZANLAR = True
KAB_KATALOG = _kimlik(21, 1)


def _ev(d: Dunya, kuyruk: str) -> str:
    return f"/sites/{d.santiye.id}/earned-value{kuyruk}"


async def _iki_yon(
    client: AsyncClient, yontem: str, url: str, kisitli, es, govde=None, pozitif=(200, 201, 204)
) -> None:  # noqa: ANN001
    ret = await client.request(yontem, url, headers=kisitli, json=govde)
    assert ret.status_code == 403 and ret.json() == YETKI_YOK, (yontem, url, ret.text)
    poz = await client.request(yontem, url, headers=es, json=govde)
    assert poz.status_code != 403, (yontem, url, poz.text)
    assert poz.status_code in pozitif, (yontem, url, poz.status_code, poz.text)


# ------------------------------------------------------------------ catalog_router


async def _iki_yon_acik(
    client: AsyncClient, yontem: str, url: str, kisitli, es, govde=None, pozitif=(200, 201, 204)
) -> None:  # noqa: ANN001
    """IZN-B3: şirket geneli yazma — proje başına disiplinli kişi de aynı roldeki atamasız eş gibi
    GEÇER (403 DEĞİL)."""
    for baslik in (kisitli, es):
        yanit = await client.request(yontem, url, headers=baslik, json=govde)
        assert yanit.status_code != 403, (yontem, url, yanit.text)
        assert yanit.status_code in pozitif, (yontem, url, yanit.status_code, yanit.text)


async def test_disiplin_ve_katalog_yazmalari_sirket_geneli_kisitliya_da_acik(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    for kod, baslik in (("MEK", civil_yazar), ("M2", yazar_atamasiz)):
        yeni = {
            "code": kod,
            "name": f"Mekanik {kod}",
            "color": "#aa0000",
            "default_contractor_type": "own",
        }
        yanit = await client.post("/earned-value/disciplines", headers=baslik, json=yeni)
        assert yanit.status_code == 201, yanit.text
        kimlik = yanit.json()["id"]
        yanit = await client.patch(
            f"/earned-value/disciplines/{kimlik}", headers=baslik, json={"name": f"{kod} 2"}
        )
        assert yanit.status_code == 200, yanit.text
    katalog = {
        "discipline_id": str(dunya.kab.id),
        "uom": "m2",
        "standard_unit_mhr": "1.5",
        "default_contractor_type": "own",
    }
    for ad, baslik in (("Kalıp A", civil_yazar), ("Kalıp B", yazar_atamasiz)):
        yanit = await client.post(
            "/earned-value/catalog", headers=baslik, json={**katalog, "name": ad}
        )
        assert yanit.status_code == 201, yanit.text
    await _iki_yon_acik(
        client,
        "PATCH",
        f"/earned-value/catalog/{KAB_KATALOG}",
        civil_yazar,
        yazar_atamasiz,
        {"name": "Beton 2"},
    )
    await _iki_yon_acik(
        client,
        "POST",
        f"/earned-value/catalog/{KAB_KATALOG}/adopt-actual",
        civil_yazar,
        yazar_atamasiz,
        None,
        pozitif=(200, 409),  # tamamlanmış şantiye yok → iş kuralı 409
    )


async def test_disiplin_silme_disiplin_atanmis_sistem_yoneticisi_de_siler(
    client: AsyncClient, dunya: Dunya, admin_kisitli, atamasiz
) -> None:
    """SIL-B1: DELETE'te disiplin kısıtı UYGULANMAZ; kısıtlı (atanmış) Sistem Yöneticisi 204."""
    yeni = {
        "code": "SIL",
        "name": "Silinecek",
        "color": "#00aa00",
        "default_contractor_type": "own",
    }
    kimlik = (await client.post("/earned-value/disciplines", headers=atamasiz, json=yeni)).json()[
        "id"
    ]
    resp = await client.delete(f"/earned-value/disciplines/{kimlik}", headers=admin_kisitli)
    assert resp.status_code == 204, resp.text


# ------------------------------------------------------------------ router (bütçe)


async def test_butce_yapisal_yazmalar_kisitliya_403(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    taban = _ev(dunya, "/budget")
    esleme = {
        "items": [{"boq_group_id": str(dunya.g["g1"].id), "discipline_id": str(dunya.kab.id)}]
    }
    await _iki_yon(client, "PUT", f"{taban}/group-disciplines", civil_yazar, yazar_atamasiz, esleme)
    dagilim = {"items": [{"discipline_id": str(dunya.kab.id), "distribution": "bell"}]}
    await _iki_yon(client, "PUT", f"{taban}/distributions", civil_yazar, yazar_atamasiz, dagilim)
    await _iki_yon(client, "PUT", f"{taban}/windows", civil_yazar, yazar_atamasiz, {"windows": []})
    await _iki_yon(client, "POST", f"{taban}/fill-from-catalog", civil_yazar, yazar_atamasiz)


async def test_revizyon_ac_kisitliya_403_sil_yalniz_sistem_yoneticisi(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz, atamasiz, admin_kisitli
) -> None:
    taban = _ev(dunya, "/budget")
    revizyonlar = (await client.get(f"{taban}/revisions", headers=atamasiz)).json()
    taslak = next(r["id"] for r in revizyonlar if r["status"] == "draft")
    # SIL-B1: taslak silme yalnız Sistem Yöneticisi (disiplin kısıtı DELETE'te uygulanmaz)
    yasak = await client.delete(f"{taban}/revisions/{taslak}", headers=civil_yazar)
    assert yasak.status_code == 403
    silme = await client.delete(f"{taban}/revisions/{taslak}", headers=admin_kisitli)
    assert silme.status_code == 204, silme.text
    await _iki_yon(client, "POST", f"{taban}/revisions", civil_yazar, yazar_atamasiz)


async def test_dondur_kisitliya_403(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    await _iki_yon(
        client,
        "POST",
        _ev(dunya, "/budget/freeze"),
        civil_yazar,
        yazar_atamasiz,
        {},
        pozitif=(200, 422),  # taslakta dondurma engeli olabilir: iş kuralı, ASLA 403
    )


# ------------------------------------------------------------------ day_router · report_router


async def test_rapor_onayi_ve_gun_kilidi_acma_kisitliya_403(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    gun = GUN2.isoformat()
    onay = _ev(dunya, f"/reports/daily/{gun}/approve")
    await _iki_yon(client, "POST", onay, civil_yazar, yazar_atamasiz, {}, pozitif=(200,))
    # rapor onaylandı → gün kilitli; kilit açma önce 403 (kısıtlı), sonra atamasız eş 200
    await _iki_yon(
        client,
        "POST",
        _ev(dunya, f"/days/{gun}/unlock"),
        civil_yazar,
        yazar_atamasiz,
        {"reason": "düzeltme gerekiyor"},
        pozitif=(200,),
    )
