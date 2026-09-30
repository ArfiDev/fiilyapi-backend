"""PLN-B2.12 — BOQ bölüm yüzdesi SATIRIN bölümünü okur, başlık yalnız GERİ DÖNÜŞtür.

Kural (`boq/progress.py::_line_section`): üretimin bölümü = satırın `section_id`si;
satır bölümsüzse (eski veri / Bölümsüz yaprak) günlük BAŞLIĞININ `section_id`si.
İkisi de boşsa üretim yalnız şantiye yüzdesine girer. Tekil (`physical_for_section`)
ve toplu (`physical_for_sections`) sorgu AYNI tanımı kullanır.
"""

from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.discipline_scope import UNRESTRICTED
from app.modules.boq.models import BoqItemSectionAllocation
from app.modules.boq.progress import (
    physical_for_section,
    physical_for_sections,
    physical_for_site,
    realized_by_item,
)
from app.modules.sites.models import Section
from tests.site_diary.conftest import VARSAYILAN_TARIH

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def iki_bolum(seeded_db: AsyncSession, santiye) -> tuple[Section, Section]:
    """A ve B bölümleri; ilk kalemin (200) 100'ü A'ya, 100'ü B'ye tahsisli."""
    site, _, items = santiye
    a = Section(site_id=site.id, code="A", name="A Blok")
    b = Section(site_id=site.id, code="B", name="B Blok")
    seeded_db.add_all([a, b])
    await seeded_db.flush()
    for section in (a, b):
        seeded_db.add(
            BoqItemSectionAllocation(
                boq_item_id=items[0].id, section_id=section.id, quantity=Decimal("100")
            )
        )
    await seeded_db.flush()
    return a, b


async def _gonderilmis_gun(
    client: AsyncClient, headers, site_id, baslik: Section | None, satir: dict
) -> None:
    govde = {"entry_date": VARSAYILAN_TARIH.isoformat()}
    if baslik is not None:
        govde["section_id"] = str(baslik.id)
    kayit = await client.post(f"/sites/{site_id}/diary", json=govde, headers=headers)
    assert kayit.status_code == 201, kayit.text
    entry_id = kayit.json()["id"]
    yanit = await client.put(f"/diary/{entry_id}/lines", json={"lines": [satir]}, headers=headers)
    assert yanit.status_code == 200, yanit.text
    gonder = await client.post(f"/diary/{entry_id}/submit", headers=headers)
    assert gonder.status_code == 200, gonder.text


async def _yuzdeler(session: AsyncSession, a: Section, b: Section):
    tekil = (
        await physical_for_section(session, a.id, scope=UNRESTRICTED),
        await physical_for_section(session, b.id, scope=UNRESTRICTED),
    )
    toplu = await physical_for_sections(session, [a.id, b.id], scope=UNRESTRICTED)
    return tekil, (toplu[a.id], toplu[b.id])


async def test_a_bolumlu_satir_KENDI_bolumune_yazilir_baslik_baska_bolum_gosterse_de(
    client: AsyncClient, admin_headers, santiye, iki_bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    a, b = iki_bolum
    await _gonderilmis_gun(
        client,
        admin_headers,
        site.id,
        a,
        {"boq_item_id": str(items[0].id), "quantity": "50", "section_id": str(b.id)},
    )

    tekil, toplu = await _yuzdeler(seeded_db, a, b)

    assert tekil == (Decimal("0.00"), Decimal("50.00"))
    assert toplu == tekil


async def test_b_ESKI_bolumsuz_satir_BASLIK_bolumune_duser(
    client: AsyncClient, admin_headers, santiye, iki_bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    a, b = iki_bolum
    await _gonderilmis_gun(
        client, admin_headers, site.id, b, {"boq_item_id": str(items[0].id), "quantity": "20"}
    )

    tekil, toplu = await _yuzdeler(seeded_db, a, b)

    assert tekil == (Decimal("0.00"), Decimal("20.00"))
    assert toplu == tekil


async def test_c_baslik_ve_satir_bolumsuz_hicbir_bolume_girmez_santiyeye_girer(
    client: AsyncClient, admin_headers, santiye, iki_bolum, seeded_db: AsyncSession
) -> None:
    site, _, items = santiye
    a, b = iki_bolum
    await _gonderilmis_gun(
        client, admin_headers, site.id, None, {"boq_item_id": str(items[0].id), "quantity": "20"}
    )

    tekil, _ = await _yuzdeler(seeded_db, a, b)

    assert tekil == (Decimal("0.00"), Decimal("0.00"))
    assert await realized_by_item(seeded_db, [items[0].id]) == {items[0].id: Decimal("20.000")}


async def test_santiye_yuzdesi_ve_kalem_toplami_bolumden_bagimsiz_AYNEN(
    client: AsyncClient, admin_headers, santiye, iki_bolum, seeded_db: AsyncSession
) -> None:
    """Regresyon: şantiye / kalem düzeyi bölüm kırılımını TOPLAR."""
    site, _, items = santiye
    a, b = iki_bolum
    await _gonderilmis_gun(
        client,
        admin_headers,
        site.id,
        a,
        {"boq_item_id": str(items[0].id), "quantity": "50", "section_id": str(b.id)},
    )

    realized = await realized_by_item(seeded_db, [items[0].id])
    site_pct = await physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    assert realized == {items[0].id: Decimal("50.000")}
    # Pay = 50 × 21.500; payda = 200 × 21.500 + 450 × 1.850.
    beklenen = (Decimal("50") * items[0].unit_price) / (
        Decimal("200") * items[0].unit_price + Decimal("450") * items[1].unit_price
    )
    assert site_pct == (beklenen * 100).quantize(Decimal("0.01"))
