"""TKL-B6.2 — kanca (EV portu) hata verirse TUM donusturme geri alinir.

Emsal: `tests/contracts/test_tkl_b3_katalog_bagi.py::test_toplu_flush_tasmasi_gercek_rollback_*`.
`client` fiksturunun oturumu hata durumunda geri ALMAZ; burada `get_db` benzeri commit/rollback
yapan bagimlilik takilir. Tohum once COMMIT edilir (rollback onu silmesin).

POZITIF KONTROL: ayni kurulumla kanca HATASIZ ise donusturme kalicidir (rollback testinin
"hicbir sey yazilmadi"si bos bir iddia olmasin).
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.core.db import get_db
from app.main import app
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.offers.models import Offer
from app.modules.projects.models import Project
from app.modules.sites.models import Site

from ._convert import govde, kazanilmis_teklif, url

pytestmark = pytest.mark.usefixtures("tohum_kancasi")


class KancaPatladi(RuntimeError):
    pass


async def _hazirla(client, admin, isveren, katalog, seeded_db):
    kz = await kazanilmis_teklif(client, admin, isveren, katalog)
    await seeded_db.commit()  # tohum dis islemde kalsin

    async def _gercek_gibi():
        try:
            yield seeded_db
            await seeded_db.commit()
        except Exception:
            await seeded_db.rollback()
            raise

    app.dependency_overrides[get_db] = _gercek_gibi
    return kz


async def _sayilar(session) -> dict[str, int]:
    async def _n(model) -> int:
        return await session.scalar(select(func.count()).select_from(model)) or 0

    return {
        "proje": await _n(Project),
        "grup": await _n(EmployerContractGroup),
        "kalem": await _n(EmployerContractItem),
        "santiye": await _n(Site),
    }


async def test_kanca_hata_verirse_proje_sozlesme_santiye_ve_teklif_izi_YOK(
    client, admin, isveren, katalog, seeded_db, tohum_kancasi
) -> None:
    kz = await _hazirla(client, admin, isveren, katalog, seeded_db)
    tohum_kancasi.hata = KancaPatladi("EV adaptoru patladi")

    with pytest.raises(KancaPatladi):
        await client.post(url(kz.offer_id), json=govde(kz, open_site=True), headers=admin)

    assert len(tohum_kancasi.istekler) == 1  # kanca gercekten cagrildi (proje/sozlesme yazilmisti)
    assert await _sayilar(seeded_db) == {"proje": 0, "grup": 0, "kalem": 0, "santiye": 0}
    offer = await seeded_db.scalar(select(Offer).where(Offer.offer_no == kz.offer_no))
    assert offer is not None and offer.project_id is None and offer.converted_at is None
    # teklif hala donusturulebilir: ayni istek kanca duzelince basarili
    tohum_kancasi.hata = None
    resp = await client.post(url(kz.offer_id), json=govde(kz, open_site=True), headers=admin)
    assert resp.status_code == 200, resp.text


async def test_KONTROL_kanca_hatasizsa_donusturme_kalicidir(
    client, admin, isveren, katalog, seeded_db
) -> None:
    kz = await _hazirla(client, admin, isveren, katalog, seeded_db)

    resp = await client.post(url(kz.offer_id), json=govde(kz, open_site=True), headers=admin)

    assert resp.status_code == 200, resp.text
    assert await _sayilar(seeded_db) == {"proje": 1, "grup": 2, "kalem": 4, "santiye": 1}
