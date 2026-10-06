"""ST T2 — depo görünürlüğü: `visible_projects` süzgeci + merkez depo istisnası.

Spec §4 + §7 **S2b** (kullanıcı onaylı): proje kapsamı YALNIZ ŞANTİYELİ depolara
uygulanır; merkez depo (`site_id IS NULL`) `inventory` izni olan HERKESE görünür.

İki dal AYRI AYRI kilitlenir çünkü ikisi de tek yönde kırılabilir:
* kapsam süzgeci gevşerse başka projenin şantiye deposu sızar;
* süzgeç merkez depoya da uygulanırsa şirketin ana ambarı kimseye görünmez.

**Katalogda (`stock_items`) kapsam süzgeci YOKTUR** — o karar
`test_stock_items_api.py`de kilitlidir.
"""

import uuid

import pytest

from app.core.access import AccessLevel
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz


@pytest.mark.asyncio
async def test_gorunmeyen_projenin_deposu_listede_yok(
    client, admin_headers, satinalma_headers, gorunen_santiye, gorunmeyen_santiye, depo_fabrikasi
):
    gorunen = await depo_fabrikasi("D-1 Ambar", site=gorunen_santiye)
    await depo_fabrikasi("D-9 Gizli Ambar", site=gorunmeyen_santiye)

    yanit = await client.get("/warehouses", headers=satinalma_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert [d["id"] for d in govde["items"]] == [str(gorunen.id)]
    assert govde["total"] == 1

    hepsi = await client.get("/warehouses", headers=admin_headers)
    assert hepsi.json()["total"] == 2


@pytest.mark.asyncio
async def test_merkez_depo_izni_olan_herkese_gorunur(
    client, satinalma_headers, sef_headers, depo_fabrikasi
):
    """§7 S2b: merkez depo hiçbir projeye bağlı değildir; proje kapsamına
    tabi tutulsaydı hiç kimse göremezdi (kapsam boş küme olurdu)."""
    merkez = await depo_fabrikasi("Merkez Depo (Sincan)")

    for basliklar in (satinalma_headers, sef_headers):
        yanit = await client.get("/warehouses", headers=basliklar)
        assert yanit.status_code == 200, yanit.text
        assert [d["id"] for d in yanit.json()["items"]] == [str(merkez.id)]


@pytest.mark.asyncio
async def test_gorunmeyen_depo_patchte_404_ve_govde_ayirt_edilemez(
    client, satinalma_headers, gorunmeyen_santiye, depo_fabrikasi
):
    """403 DEĞİL 404: elinde kimlik olan kullanıcı kaydın VAR OLDUĞUNU
    öğrenmemelidir; gövde var olmayan kimliğinkiyle BİREBİR aynıdır."""
    gizli = await depo_fabrikasi("D-9 Gizli Ambar", site=gorunmeyen_santiye)

    gorunmez = await client.patch(
        f"/warehouses/{gizli.id}", json={"name": "X"}, headers=satinalma_headers
    )
    olmayan = await client.patch(
        f"/warehouses/{uuid.uuid4()}", json={"name": "X"}, headers=satinalma_headers
    )
    assert gorunmez.status_code == 404, gorunmez.text
    assert olmayan.status_code == 404
    assert gorunmez.json() == olmayan.json()


@pytest.mark.asyncio
async def test_silmede_kapi_gorunurlukten_once_kosar_403(
    client, seeded_db, satinalma_headers, gorunmeyen_santiye, depo_fabrikasi
):
    """`inventory:admin` TAŞIYAN ama Sistem Yöneticisi OLMAYAN kullanıcı 403 alır (SIL-B1).

    Kapı handler'dan ÖNCE koşar: görünmeyen ve var olmayan depo AYNI 403'ü alır, yani varlık
    sızmaz ("yetkiliyse söyleyebiliriz" kestirmesi yetkili hesabı keşif aracına çevirirdi).
    """
    gizli = await depo_fabrikasi("D-9 Gizli Ambar", site=gorunmeyen_santiye)
    await modul_duzeyi_yaz(seeded_db, "procurement", "inventory", AccessLevel.admin)

    yanit = await client.delete(f"/warehouses/{gizli.id}", headers=satinalma_headers)
    olmayan = await client.delete(f"/warehouses/{uuid.uuid4()}", headers=satinalma_headers)
    assert yanit.status_code == olmayan.status_code == 403, yanit.text
    assert yanit.json() == olmayan.json()
