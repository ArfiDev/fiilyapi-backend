"""GKS-B1 ÖLÇÜM (uygulama yok) — kayıtlı taslakta PATCH ile başlık `section_id` değişirse.

Bu dosya BUGÜNKÜ davranışı sabitler (öneri rapordadır): başlık bölümü PATCH'te yalnız
`validate_section` ile doğrulanıp kolona yazılır; satırlara DOKUNULMAZ — yeni bölümün kalemleri
EKLENMEZ, eski bölümün satırları SİLİNMEZ/TAŞINMAZ (satır bölümü kendi kolonundadır).
Yani başlık değişimi YALNIZ ETİKETTİR ve hiçbir satır sessizce kaybolmaz.
"""

import pytest
from httpx import AsyncClient

from tests.site_diary._gks_b1 import olustur, satir
from tests.site_diary.conftest import KarisikSantiye

pytestmark = pytest.mark.asyncio


async def test_patch_baslik_bolumu_satirlara_dokunmaz(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    ks = karisik_santiye
    kayit = await olustur(
        client, admin_headers, ks.site.id, bolum=ks.s1, lines=[satir(ks, "04", "5", "S1")]
    )
    assert kayit.status_code == 201, kayit.text
    once = {(s["id"], s["section_id"], s["quantity"]) for s in kayit.json()["lines"]}

    yanit = await client.patch(
        f"/diary/{kayit.json()['id']}", json={"section_id": str(ks.s2.id)}, headers=admin_headers
    )

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["section_id"] == str(ks.s2.id)
    sonra = {(s["id"], s["section_id"], s["quantity"]) for s in yanit.json()["lines"]}
    assert sonra == once  # satır kimliği, bölümü ve miktarı AYNEN; yeni bölümün kalemi EKLENMEDİ
    assert not any(s["code"] == "05" for s in yanit.json()["lines"])  # 05 yalnız S2'ye tahsisli


async def test_patch_baslik_bolumu_baska_santiyenin_bolumu_422(
    client: AsyncClient, admin_headers, karisik_santiye: KarisikSantiye
) -> None:
    import uuid

    ks = karisik_santiye
    kayit = await olustur(client, admin_headers, ks.site.id)
    yanit = await client.patch(
        f"/diary/{kayit.json()['id']}",
        json={"section_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert yanit.status_code == 422
