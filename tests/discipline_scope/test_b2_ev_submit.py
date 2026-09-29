"""DSC-B2 Ü5/S8 — günlük Gönder ön-koşulu OPAK: `overrun_without_reason` kısıtlıda başka
disiplinin aşımını KALEM ADI/KODU vermeden ("Başka disiplinde N satır ...") bildirir.

Taslak e3 (07.05): I2·S1 (elek) planı aşar (50, önceki 5 + 100) · I3·S2 (eşlemesiz, d:none) planı
aşar (20, önceki 2 + 30) — ikisi de gerekçesiz. Civil için ikisi de BAŞKA disiplin (N=2); elek'in
kendi satırı (I2) aşımı mevcut metinle görür; atamasız eş bugünkü metni görür.
"""

from __future__ import annotations

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.earned_value import diary_adapter as adp
from app.modules.site_diary.models import SiteDiaryLine
from tests._disiplin_dunyasi import GUN3, Dunya, _kimlik

YAZANLAR = True
OPAK = "Başka disiplinde 2 satır planlı miktarı aşıyor (gerekçesiz)"


async def _asim_hazirla(session: AsyncSession, d: Dunya) -> None:
    d.satir["e3_i2_s1"].quantity = Decimal(100)
    session.add(
        SiteDiaryLine(
            id=_kimlik(31, 30),
            entry_id=d.gunluk["e3"].id,
            boq_item_id=d.i["i3"].id,
            section_id=d.s2.id,
            code="03.001",
            description="Boya",
            unit="m2",
            unit_price=Decimal(30),
            quantity=Decimal(30),
        )
    )
    await session.flush()


async def _gonder(client: AsyncClient, d: Dunya, baslik):  # noqa: ANN202
    return await client.post(f"/diary/{d.gunluk['e3'].id}/submit", headers=baslik)


def _asim(resp) -> str | None:  # noqa: ANN001
    """`overrun_without_reason` nedeninin metni (yoksa None)."""
    for oge in resp.json()["reason_items"]:
        if oge["code"] == adp.SUBMIT_OVERRUN:
            return oge["message"]
    return None


async def test_civil_baska_disiplin_asimini_opak_metinle_gorur_kalem_adi_kodu_yok(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    await _asim_hazirla(seeded_db, dunya)
    resp = await _gonder(client, dunya, civil_yazar)
    assert resp.status_code == 422, resp.text
    assert _asim(resp) == OPAK
    for sizinti in ("Tuğla", "02.001", "Boya", "03.001", str(dunya.i["i2"].id)):
        assert sizinti not in resp.text, sizinti


async def test_elek_kendi_asimini_mevcut_metinle_gorur(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, elek_yazar
) -> None:
    await _asim_hazirla(seeded_db, dunya)
    assert _asim(await _gonder(client, dunya, elek_yazar)) == adp.OVERRUN_MESSAGE


async def test_atamasiz_es_bugunku_metni_gorur(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, yazar_atamasiz
) -> None:
    await _asim_hazirla(seeded_db, dunya)
    assert (
        _asim(await _gonder(client, dunya, yazar_atamasiz))
        == "Planlı miktarı aşan satır gerekçesiz"
    )


async def test_asim_yoksa_kisitlida_da_neden_yok(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    """Pozitif kontrol: aşım yokken (varsayılan dünya) opak neden UYDURULMAZ."""
    assert _asim(await _gonder(client, dunya, civil_yazar)) is None


async def test_gun_gorunumundeki_submit_alani_ayni_yoldan_opaktir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    await _asim_hazirla(seeded_db, dunya)
    url = f"/sites/{dunya.santiye.id}/earned-value/days/{GUN3.isoformat()}"
    govde = (await client.get(url, headers=civil_yazar)).json()
    mesajlar = [
        o["message"] for o in govde["submit"]["reason_items"] if o["code"] == adp.SUBMIT_OVERRUN
    ]
    assert mesajlar == [OPAK]
    assert "Tuğla" not in str(govde) and "Boya" not in str(govde["submit"])


async def test_elek_kendi_ve_eslemesiz_asim_birlikte_iki_madde_dogru_sira_ayni_kod(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, elek_yazar, yazar_atamasiz
) -> None:
    """Elek: kendi I2 aşımı + eşlemesiz I3 aşımı → İKİ madde (önce kendi, sonra opak N=1), aynı
    kod; kalem adı/kodu yok; GET `submit` alanı ile POST 422 gövdesi aynı. Kısıtsız: tek madde."""
    await _asim_hazirla(seeded_db, dunya)
    resp = await _gonder(client, dunya, elek_yazar)
    assert resp.status_code == 422, resp.text
    asimlar = [o for o in resp.json()["reason_items"] if o["code"] == adp.SUBMIT_OVERRUN]
    assert [o["message"] for o in asimlar] == [
        adp.OVERRUN_MESSAGE,
        "Başka disiplinde 1 satır planlı miktarı aşıyor (gerekçesiz)",
    ]
    for sizinti in ("Boya", "03.001", str(dunya.i["i3"].id)):
        assert sizinti not in resp.text, sizinti
    url = f"/sites/{dunya.santiye.id}/earned-value/days/{GUN3.isoformat()}"
    gorunum = (await client.get(url, headers=elek_yazar)).json()["submit"]
    assert gorunum["reason_items"] == resp.json()["reason_items"]
    assert gorunum["reasons"] == resp.json()["reasons"]
    tek = await _gonder(client, dunya, yazar_atamasiz)
    assert [
        o["message"] for o in tek.json()["reason_items"] if o["code"] == adp.SUBMIT_OVERRUN
    ] == [adp.OVERRUN_MESSAGE]
