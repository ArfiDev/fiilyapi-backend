"""🔴 DAVRANIŞ KANITI (TEARDOWN-B1 bekçi c): istek başına TEK `get_db` oturumu.

`conftest.py`nin `get_db` override'ı her çağrıda AYNI `db_session`ı döndürür;
bu yüzden iki oturum açılması normal testlerde GÖRÜNMEZ. Burada override sayaçlı
bir generator'la değiştirilir (her giriş sayaç++ ve aynı `db_session`ı yield
eder) ve bir istekte kaç kez girildiği ölçülür.

* Kimlik doğrulamalı + `require_permission` kapılı YAZMA ucu
  (`POST /projects/{id}/document-folders`, `documents:full`): uç, `get_current_user`,
  `require_permission` alt bağımlılıkları ve uç gövdesi aynı `DbSession`ı
  paylaşır ⇒ sayaç == 1.
* İndirme ucu (`GET /documents/{id}/download`): BİLİNÇLİ istisna. Akış varyantı
  `scope="request"` ile ikinci bir oturum açar (gövde function kapsamı
  kapandıktan sonra üretilir) ⇒ sayaç == 2.

Yapısal karşılığı: `tests/core/test_getdb_kapsam_bekcisi.py`.
"""

from collections.abc import AsyncGenerator

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.main import app


def _sayacli_override(db_session: AsyncSession) -> list[int]:
    girisler: list[int] = []

    async def _sayacli() -> AsyncGenerator[AsyncSession, None]:
        girisler.append(1)
        yield db_session

    app.dependency_overrides[get_db] = _sayacli
    return girisler


async def test_yazma_ucu_istek_basina_TEK_oturum_acar(
    client: AsyncClient, seeded_db: AsyncSession, proje, sef_headers
) -> None:
    girisler = _sayacli_override(seeded_db)

    resp = await client.post(
        f"/projects/{proje.id}/document-folders", json={"name": "Tek Oturum"}, headers=sef_headers
    )

    assert resp.status_code == 201, resp.text
    assert len(girisler) == 1, (
        "Kimlik + izin + uç TEK oturum paylaşmalı; birden çok giriş bir alt "
        f"bağımlılığın farklı `get_db` kapsamı taşıdığını gösterir: {len(girisler)}"
    )


async def test_indirme_ucu_BILINCLI_istisna_iki_oturum_acar(
    client: AsyncClient, seeded_db: AsyncSession, proje, belge_fabrikasi, sef_headers
) -> None:
    belge = await belge_fabrikasi(proje, "Tek.pdf", data=b"%PDF", size_bytes=4)
    girisler = _sayacli_override(seeded_db)

    resp = await client.get(f"/documents/{belge.id}/download", headers=sef_headers)

    assert resp.status_code == 200, resp.text
    assert len(girisler) == 2, (
        "İndirme ucu function (kimlik/izin/uç) + request (akış blob'u) olmak üzere "
        f"tam İKİ oturum açmalı: {len(girisler)}"
    )
