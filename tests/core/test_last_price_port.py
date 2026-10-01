"""TKL-B3.2 — `app/core/last_price.py` port sozlesmesi (DB'siz; sahte saglayicilarla).

Fikstur kayit fotografini alir/geri yukler: `unregister_all()` kullanan test sonunda
`restore()` etmezse ayni isci surecinde sonra kosan testler SZL/HK kaydini kaybeder.
"""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from app.core import last_price
from app.core.last_price import LastPrice

pytestmark = pytest.mark.asyncio

T0 = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)
KALEM = uuid.uuid4()
BASKA = uuid.uuid4()
DOC_A = uuid.UUID(int=1)
DOC_B = uuid.UUID(int=2)


@pytest.fixture(autouse=True)
def _port_temiz():
    foto = last_price.registered()
    last_price.unregister_all()
    yield
    last_price.restore(foto)


def _fiyat(source: str, fiyat: str, at: datetime, doc_no: str = "X", doc_id=None) -> LastPrice:
    return LastPrice(Decimal(fiyat), at, source, doc_no, doc_id)


def _saglayici(**sonuc: LastPrice) -> AsyncMock:
    (fiyat,) = sonuc.values()
    return AsyncMock(return_value={KALEM: fiyat})


async def test_kayit_yoksa_bos_doner() -> None:
    assert await last_price.latest(None, [KALEM]) == {}


async def test_bos_id_listesinde_saglayici_cagrilmaz() -> None:
    saglayici = _saglayici(a=_fiyat("SZL", "1", T0))
    last_price.register_provider("SZL", saglayici)
    assert await last_price.latest(None, []) == {}
    saglayici.assert_not_awaited()


async def test_birlestirme_en_yeni_at_kazanir_kayit_sirasindan_bagimsiz() -> None:
    eski = _saglayici(a=_fiyat("SZL", "100", T0))
    yeni = _saglayici(a=_fiyat("HK", "120", T0 + timedelta(days=1)))
    for sira in ((("SZL", eski), ("HK", yeni)), (("HK", yeni), ("SZL", eski))):
        last_price.unregister_all()
        for kaynak, s in sira:
            last_price.register_provider(kaynak, s)
        sonuc = await last_price.latest(None, [KALEM])
        assert sonuc[KALEM].source == "HK" and sonuc[KALEM].price == Decimal("120")


async def test_esit_at_ikincil_anahtar_deterministik() -> None:
    # Kaynak onceligi: HK < SZL sirasinda HK kazanir (SOURCE_ORDER), kayit sirasi onemsiz.
    hk = _saglayici(a=_fiyat("HK", "1", T0, "HK-P-1", DOC_A))
    szl = _saglayici(a=_fiyat("SZL", "2", T0, "P", DOC_B))
    for sira in ((("SZL", szl), ("HK", hk)), (("HK", hk), ("SZL", szl))):
        last_price.unregister_all()
        for kaynak, s in sira:
            last_price.register_provider(kaynak, s)
        assert (await last_price.latest(None, [KALEM]))[KALEM].source == "HK"


async def test_esit_at_ve_kaynak_onceligi_yoksa_doc_no_buyuk_olan_kazanir() -> None:
    # Iki bilinmeyen kaynak (ayni sinif): kaynak adi, sonra doc_no kararini verir; sonuc
    # kayit sirasindan bagimsizdir.
    a = _saglayici(a=_fiyat("ZZ", "1", T0, "A", DOC_A))
    b = _saglayici(a=_fiyat("ZZ", "2", T0, "B", DOC_A))
    sonuclar = []
    for sira in ((("ZZ", a), ("ZZ2", b)), (("ZZ2", b), ("ZZ", a))):
        last_price.unregister_all()
        for kaynak, s in sira:
            last_price.register_provider(kaynak, s)
        sonuclar.append((await last_price.latest(None, [KALEM]))[KALEM])
    assert sonuclar[0] == sonuclar[1]


async def test_esit_at_ayni_kaynak_adinda_doc_no_sonra_doc_id() -> None:
    # `_key` birim olcumu: ayni at + ayni kaynak → doc_no, sonra doc_id (buyuk kazanir).
    kucuk = _fiyat("ZZ", "1", T0, "A", uuid.UUID(int=1))
    buyuk_no = _fiyat("ZZ", "2", T0, "B", uuid.UUID(int=1))
    buyuk_id = _fiyat("ZZ", "3", T0, "A", uuid.UUID(int=2))
    assert last_price._key(buyuk_no) > last_price._key(kucuk)
    assert last_price._key(buyuk_id) > last_price._key(kucuk)


async def test_her_saglayici_tek_toplu_cagri_alir() -> None:
    saglayici = AsyncMock(return_value={})
    last_price.register_provider("SZL", saglayici)
    await last_price.latest(None, [KALEM, BASKA, KALEM])
    saglayici.assert_awaited_once()
    assert saglayici.await_args.args[1] == [KALEM, BASKA]


async def test_ayni_source_farkli_saglayici_runtime_error() -> None:
    last_price.register_provider("SZL", _saglayici(a=_fiyat("SZL", "1", T0)))
    with pytest.raises(RuntimeError):
        last_price.register_provider("SZL", _saglayici(a=_fiyat("SZL", "2", T0)))


async def test_ayni_ikili_no_op() -> None:
    saglayici = _saglayici(a=_fiyat("SZL", "1", T0))
    last_price.register_provider("SZL", saglayici)
    last_price.register_provider("SZL", saglayici)
    assert last_price.registered() == (("SZL", saglayici),)


async def test_restore_fotografi_geri_yukler() -> None:
    s = _saglayici(a=_fiyat("SZL", "1", T0))
    last_price.register_provider("SZL", s)
    foto = last_price.registered()
    last_price.unregister_all()
    assert last_price.registered() == ()
    last_price.restore(foto)
    assert last_price.registered() == (("SZL", s),)
