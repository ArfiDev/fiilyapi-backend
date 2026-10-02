"""TKL-B4.2 test fiksturleri (admin, isveren, katalog, sahte son fiyat saglayicisi)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.core import last_price
from app.core.last_price import LastPrice
from app.modules.catalog.models import ContractorType, EvCatalogItem, EvDiscipline
from app.modules.catalog.service import next_poz_no
from app.modules.projects.models import Employer

from .._boq import _auth, _login_with_access

#: Katalog fikstur kalemleri: (ad, referans fiyat, standart adam-saat).
KATALOG = (
    ("Beton", Decimal("100.00"), Decimal("1.5")),
    ("Kalıp", None, Decimal("2")),
    ("Demir", Decimal("50.00"), Decimal("0.75")),
)


@pytest.fixture
async def admin(client, db_session, user_factory, seeded_db):
    token = await _login_with_access(
        client, db_session, user_factory, "system_admin", f"a.{uuid.uuid4().hex[:6]}@tkl.co"
    )
    return _auth(token)


@pytest.fixture
async def isveren(seeded_db) -> Employer:
    employer = Employer(name="Akın İnşaat A.Ş.")
    seeded_db.add(employer)
    await seeded_db.flush()
    return employer


@pytest.fixture(autouse=True)
async def katalog(seeded_db) -> list[EvCatalogItem]:
    """Uc kalem: Beton (ref 100), Kalip (ref YOK), Demir (ref 50). AUTOUSE: `gecis(send)` bos
    revizyona kalem ekleyebilsin (TKL-B4.3 bos gonderim kurali)."""
    disiplin = EvDiscipline(
        code="OFR", name="Teklif", color="#2563eb", default_contractor_type=ContractorType.OWN
    )
    seeded_db.add(disiplin)
    await seeded_db.flush()
    kalemler: list[EvCatalogItem] = []
    for ad, ref, mhr in KATALOG:
        kalem = EvCatalogItem(
            poz_no=await next_poz_no(seeded_db, disiplin),
            discipline_id=disiplin.id,
            name=ad,
            uom="m3",
            standard_unit_mhr=mhr,
            default_contractor_type=ContractorType.OWN,
            ref_price=ref,
        )
        seeded_db.add(kalem)
        await seeded_db.flush()
        kalemler.append(kalem)
    return kalemler


class FakeLastPrice:
    """Sahte SZL saglayicisi: `veri[kalem_id] = LastPrice`; `cagrilar` her cagrinin kimlikleri."""

    def __init__(self) -> None:
        self.veri: dict[uuid.UUID, LastPrice] = {}
        self.cagrilar: list[list[uuid.UUID]] = []

    def koy(self, kalem_id: uuid.UUID, fiyat: str) -> None:
        self.veri[kalem_id] = LastPrice(
            Decimal(fiyat), datetime(2026, 3, 1, tzinfo=UTC), "SZL", "SZL-1", None
        )

    async def __call__(self, session, ids):  # noqa: ANN001
        self.cagrilar.append(list(ids))
        return {i: self.veri[i] for i in ids if i in self.veri}


@pytest.fixture
def son_fiyat():
    foto = last_price.registered()
    last_price.unregister_all()
    sahte = FakeLastPrice()
    last_price.register_provider("SZL", sahte)
    yield sahte
    last_price.restore(foto)


@pytest.fixture
def tohum_kancasi():
    """TKL-B6.2: port SAHTE kancayla degistirilir (EV adaptoru kosuya karismaz); sonunda geri
    yuklenir (yoksa sonra kosan testler EV kaydini kaybeder)."""
    from app.core import contract_seed

    from ._convert import kanca_kur

    sahte, foto = kanca_kur()
    yield sahte
    contract_seed.restore(foto)
