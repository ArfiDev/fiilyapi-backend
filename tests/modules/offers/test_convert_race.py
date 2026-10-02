"""TKL-B6.2 — eszamanli iki donusturme: teklif satiri `FOR UPDATE` kilitli → biri 409, TEK proje.

Emsal ve duzenek: `test_offers_race.py` (tutulan-kilit + `pg_stat_activity` bariyeri + POZITIF
KONTROL). Oturum 1 donusturur ve COMMIT ETMEDEN bekler; oturum 2 baslar, beklemesi OLCULUR; sonra
1 commit eder.

KONTROL (kilitsiz): `lock_offer` duz okumaya cevrilirse ikinci oturum teklifi hala `project_id`
BOS okur ve ikinci bir proje kurar. TKL-B6.8b (BD-5) oncesi bu, proje kodu UQ'suna carpip
`IntegrityError` verirdi; kod uretici artik kilitli oldugundan ikinci oturum kod kilidinde bekler,
FARKLI kod alir ve IKI proje olusur (teklifin `project_id`si ikincisini gosterir, birincisi
oksuz) — teklif kilidinin onledigi asil sessiz kayip. `offers.project_id` UQ'su de koruma
SAGLAMAZ (ayni satirin UPDATE'i); ana testte bekleyen sorgunun `FOR UPDATE` olmasi asil kanittir.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ConflictError
from app.modules.offers import convert_service, offer_service
from app.modules.offers.convert_schemas import ConvertRequest
from app.modules.offers.models import Offer, OfferItem
from app.modules.offers.offer_service import OfferAction
from app.modules.projects.models import Project

from ._convert import kalem_govdesi
from .test_offers_race import _kullanici, _Ortam, _ortam, _yaris

pytestmark = [pytest.mark.asyncio, pytest.mark.usefixtures("tohum_kancasi")]


async def _kazan(ortam: _Ortam) -> ConvertRequest:
    """Zemindeki gonderilmis teklifi `won` yapar (COMMIT) ve donusturme govdesini dondurur."""
    async with ortam.Session() as s:
        await offer_service.transition(
            s, await _kullanici(s, ortam), ortam.offer_id, 0, OfferAction.win
        )
        await s.commit()
    async with ortam.Session() as s:
        item = (await s.scalars(select(OfferItem))).one()
        ham = {
            "catalog_item_id": str(item.catalog_item_id),
            "id": str(item.id),
            "description": item.description,
            "unit": item.unit,
        }
    return ConvertRequest.model_validate(
        {
            "project": {
                "name": "Yarış Projesi",
                "city": "Ankara",
                "start_date": "2026-11-01",
                "end_date": "2027-10-31",
            },
            "contract": {
                "contract_no": "YRS-1",
                "signature_date": "2026-10-15",
                "has_price_escalation": False,
            },
            "groups": [{"name": "G", "items": [kalem_govdesi(ham, "Y-01")]}],
        }
    )


async def _projeler(ortam: _Ortam) -> int:
    async with ortam.Session() as s:
        return await s.scalar(select(func.count()).select_from(Project)) or 0


async def _iki_donusturme(ortam: _Ortam, govde: ConvertRequest):
    async def _is(session: AsyncSession) -> None:
        await convert_service.convert_offer(
            session, await _kullanici(session, ortam), ortam.offer_id, govde
        )

    return await _yaris(ortam, _is, _is)


async def test_TKLB62_iki_eszamanli_donusturme_biri_409_tek_proje() -> None:
    async with _ortam(gonderilmis=True) as ortam:
        govde = await _kazan(ortam)

        bekleyen, hata = await _iki_donusturme(ortam, govde)

        assert "FROM offers" in bekleyen and "FOR UPDATE" in bekleyen, bekleyen
        assert isinstance(hata, ConflictError), f"ikinci istek 409 olmaliydi: {hata!r}"
        assert "zaten dönüştürüldü" in str(hata)
        assert await _projeler(ortam) == 1
        async with ortam.Session() as s:
            offer = await s.get(Offer, ortam.offer_id)
            assert offer is not None and offer.project_id is not None


async def test_TKLB62_KONTROL_kilitsiz_ikinci_donusturme_iki_proje_birincisi_oksuz(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POZITIF KONTROL: teklif kilidi KAPALI → ikinci oturum teklifi hala donusturulmemis okur ve
    ikinci bir proje kurar. TKL-B6.8b (BD-5) sonrasi proje kodu uretici kilitli oldugundan ikinci
    oturum artik proje kodu UQ'suna CARPIP reddedilmez: kod kilidinde (`pg_advisory_xact_lock`)
    bekler, birincinin commit'inden sonra FARKLI bir kod alir ve ikisi de commit eder → IKI proje;
    teklifin `project_id`si ikincisini gosterir, birincisi OKSUZ kalir. Bu, teklif kilidinin
    onledigi asil sessiz kayiptir (eski iddia: UQ cokusu = jenerik 409; daha zayif kanit)."""

    async def _kilitsiz(session: AsyncSession, offer_id: uuid.UUID) -> Offer:
        offer = await session.get(Offer, offer_id)
        assert offer is not None
        return offer

    monkeypatch.setattr(convert_service, "lock_offer", _kilitsiz)
    async with _ortam(gonderilmis=True) as ortam:
        govde = await _kazan(ortam)

        bekleyen, hata = await _iki_donusturme(ortam, govde)

        assert "pg_advisory_xact_lock" in bekleyen, bekleyen
        assert hata is None, f"kilitsiz de ikinci donusturme temiz gecmeliydi (iki proje): {hata!r}"
        assert await _projeler(ortam) == 2  # kayip: ayni tekliften IKI proje
        async with ortam.Session() as s:
            offer = await s.get(Offer, ortam.offer_id)
            kodlar = sorted((await s.scalars(select(Project.code))).all())
            ikinci = await s.scalar(select(Project.id).where(Project.code == kodlar[-1]))
            assert offer is not None and offer.project_id == ikinci  # birincisi oksuz
