"""TKL-B6.1 — `app/core/contract_seed.py` port sozlesmesi (DB'siz; sahte kancalarla).

Fikstur kayit fotografini alir/geri yukler: `unregister_all()` kullanan test sonunda `restore()`
etmezse ayni isci surecinde sonra kosan testler kaydi kaybeder ve SAHTE-YESIL gecer.
"""

import ast
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.core import contract_seed
from app.core.contract_seed import (
    ContractItemSeed,
    ContractSeedRequest,
    SeedWarning,
)
from app.modules.earned_value.models import RateSource

GRUP = uuid.uuid4()


@pytest.fixture(autouse=True)
def _port_temiz():
    foto = contract_seed.registered()
    contract_seed.unregister_all()
    yield
    contract_seed.restore(foto)


def _istek() -> ContractSeedRequest:
    kalem = ContractItemSeed(uuid.uuid4(), uuid.uuid4(), Decimal("1.5"), True)
    return ContractSeedRequest(
        project_id=uuid.uuid4(),
        site_id=None,
        start=date(2026, 10, 2),
        end=None,
        items=(kalem,),
        group_disciplines={GRUP: uuid.uuid4()},
        actor_id=None,
        label="Rev.0 — TKL-2026-014",
    )


def _kanca(*uyarilar: SeedWarning) -> AsyncMock:
    return AsyncMock(return_value=list(uyarilar))


async def test_kayit_yoksa_bos_doner_ve_hata_yok() -> None:
    assert contract_seed.registered() == ()
    assert await contract_seed.run_seed_hooks(None, _istek()) == []


async def test_kanca_istegi_ve_oturumu_aynen_alir() -> None:
    kanca = _kanca()
    contract_seed.register_seed_hook(kanca)
    istek, oturum = _istek(), object()
    await contract_seed.run_seed_hooks(oturum, istek)  # type: ignore[arg-type]
    kanca.assert_awaited_once_with(oturum, istek)


async def test_ayni_kanca_ikinci_kez_kaydi_no_op() -> None:
    kanca = _kanca()
    contract_seed.register_seed_hook(kanca)
    contract_seed.register_seed_hook(kanca)
    assert contract_seed.registered() == (kanca,)
    await contract_seed.run_seed_hooks(None, _istek())
    assert kanca.await_count == 1


async def test_coklu_kanca_kayit_sirasiyla_kosar_ve_uyarilar_birlesir() -> None:
    sira: list[str] = []
    u1 = SeedWarning("a", "birinci", GRUP)
    u2 = SeedWarning("b", "ikinci")
    u3 = SeedWarning("c", "ucuncu")

    def _kaydeden(ad: str, *uyarilar: SeedWarning):
        async def kanca(session, req):
            sira.append(ad)
            return uyarilar

        return kanca

    for ad, uyarilar in (("1", (u1, u2)), ("2", ()), ("3", (u3,))):
        contract_seed.register_seed_hook(_kaydeden(ad, *uyarilar))
    assert await contract_seed.run_seed_hooks(None, _istek()) == [u1, u2, u3]
    assert sira == ["1", "2", "3"]


async def test_kanca_hatasi_yukselir_sonraki_kanca_kosmaz() -> None:
    once, sonra = _kanca(), _kanca()
    patlayan = AsyncMock(side_effect=RuntimeError("EV taslağı kurulamadı"))
    for k in (once, patlayan, sonra):
        contract_seed.register_seed_hook(k)
    with pytest.raises(RuntimeError, match="EV taslağı"):
        await contract_seed.run_seed_hooks(None, _istek())
    once.assert_awaited_once()
    sonra.assert_not_awaited()


async def test_test_uclusu_kaydi_bosaltir_ve_geri_yukler() -> None:
    kanca = _kanca()
    contract_seed.register_seed_hook(kanca)
    foto = contract_seed.registered()
    contract_seed.unregister_all()
    assert contract_seed.registered() == ()
    contract_seed.restore(foto)
    assert contract_seed.registered() == (kanca,)


async def test_degismez_veri_siniflari() -> None:
    istek = _istek()
    with pytest.raises(AttributeError):
        istek.label = "x"  # type: ignore[misc]
    assert SeedWarning("k", "m").group_id is None
    with pytest.raises(AttributeError):
        istek.items[0].unit_mhr = Decimal(1)  # type: ignore[misc]


def test_port_hicbir_urun_modulunu_import_etmez() -> None:
    kaynak = Path(contract_seed.__file__).read_text(encoding="utf-8")
    ice_aktarilan = {
        n.module or "" for n in ast.walk(ast.parse(kaynak)) if isinstance(n, ast.ImportFrom)
    } | {a.name for n in ast.walk(ast.parse(kaynak)) if isinstance(n, ast.Import) for a in n.names}
    assert not [m for m in ice_aktarilan if m.startswith("app.modules")], ice_aktarilan


def test_rate_source_degerleri_sirali_ve_offer_dahil() -> None:
    """EV `RateSource` ↔ DB enum `ev_rate_source` (migration `f2a6c8e0b4d7`) esitligi migration
    testinde canli DB ile olculur; burada Python tarafi kilitlenir."""
    assert [m.value for m in RateSource] == ["catalog", "history", "manual", "offer"]
