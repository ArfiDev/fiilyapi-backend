"""DSC-B3 dünyası: B2'nin `kur(yazanlar=True)` dünyası + `genislet()`.

Eklenenler (sıra SABİT; onay 06.05 kilidi koyar, kilide takılacak her şey onaydan ÖNCE):
1. günlük satırı `e3_i2_s2` (E3, kalem I2, bölüm S2, miktar 2): Elektrik'in KENDİ gerçek bilinmeyen
   satırı (baseline'da `l:I2:S2` yok) — Ç3;
2. aktörler `iki_disiplin` (PM, KAB+DUV) ve `pm_atamasiz` (PM, atama YOK — PUT /settings 403
   testinin F1 pozitif kontrolü, F1: PM'in earned_value düzeyi `_F`);
3. ayar #1 (takvim GET'ten kopya + kartlar C1–C4) → onay 06.05 (snapshot v1) → ayar #2
   (yalnız tolerans "5.00": Ü4 — snapshot "2.00" kalır, canlı "5.00");
4. etiketleme SON PUT'tan sonra (kart kimlikleri her PUT'ta yeniden üretilir).
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.site_diary.models import SiteDiaryLine
from app.modules.users.models import ProjectMember
from tests._disiplin_dunyasi import SIFRE, Dunya, _giris, _kimlik
from tests._proje_ekibi import disiplin_ata
from tests.discipline_scope._b2_golden_araclari import rastgele_kimlikleri_etiketle

D = Decimal
FIXED_TODAY = date(2026, 5, 10)
ONAY_GUNU = date(2026, 5, 6)
KART_ADLARI = ("C1 Beton harcanan", "C2 Karma kazanilan", "C3 Tugla butce", "C4 Boya harcanan")
_TAKVIM_ALANLARI = ("week_start_dow", "weekly_off_days", "standard_daily_hours", "pf_bands")


def _s(d: Dunya, kuyruk: str = "") -> str:
    return f"/sites/{d.santiye.id}/earned-value{kuyruk}"


async def _e3_i2_s2(session: AsyncSession, d: Dunya) -> None:
    kalem = d.i["i2"]
    satir = SiteDiaryLine(
        id=_kimlik(31, 10),
        entry_id=d.gunluk["e3"].id,
        boq_item_id=kalem.id,
        section_id=d.s2.id,
        code=kalem.code,
        description=kalem.description,
        unit=kalem.unit,
        unit_price=kalem.unit_price,
        quantity=D(2),
    )
    session.add(satir)
    await session.flush()
    d.satir["e3_i2_s2"] = satir
    d.etiketler[satir.id] = "<line:e3_i2_s2>"


async def _pm(
    session: AsyncSession, client: AsyncClient, user_factory, d: Dunya, ad: str, kodlar
) -> None:
    user = await user_factory(email=f"{ad}@dsc-b3.co", password=SIFRE, role_key="project_manager")
    session.add(ProjectMember(user_id=user.id, project_id=d.proje.id, role_id=user.role_id))
    for kod in kodlar:
        await disiplin_ata(session, user, d.proje.id, {"KAB": d.kab, "DUV": d.duv}[kod].id)
    d.kullanici[ad] = user
    d.etiketler[user.id] = f"<user:{ad}>"
    d.baslik[ad] = await _giris(client, f"{ad}@dsc-b3.co")


def ayar_govdesi(taban: dict, kartlar: list[dict], tolerans: str) -> dict:
    """PUT gövdesi: takvim/bant alanları GET'ten AYNEN (PUT tam değiştirmedir)."""
    govde = {alan: taban[alan] for alan in _TAKVIM_ALANLARI}
    return {
        **govde,
        "tolerance_points": tolerans,
        "holidays": [],
        "composite_metrics": kartlar,
    }


def kartlar(d: Dunya) -> list[dict]:
    i1, i2, i3 = (str(d.i[k].id) for k in ("i1", "i2", "i3"))
    tanim = (
        ("spent", [i1], i1),
        ("earned", [i1, i2], i1),
        ("budget", [i2], i2),
        ("spent", [i3], i3),
    )
    return [
        {"name": ad, "measure": olcu, "numerator_item_ids": pay, "denominator_item_id": payda}
        for ad, (olcu, pay, payda) in zip(KART_ADLARI, tanim, strict=True)
    ]


async def genislet(session: AsyncSession, client: AsyncClient, user_factory, d: Dunya) -> Dunya:
    admin = d.baslik["atamasiz"]
    await _e3_i2_s2(session, d)
    await _pm(session, client, user_factory, d, "iki_disiplin", ("KAB", "DUV"))
    await _pm(session, client, user_factory, d, "pm_atamasiz", ())
    taban = await client.get(_s(d, "/settings"), headers=admin)
    assert taban.status_code == 200, taban.text
    govde = ayar_govdesi(taban.json(), kartlar(d), taban.json()["tolerance_points"])
    ilk = await client.put(_s(d, "/settings"), headers=admin, json=govde)
    assert ilk.status_code == 200, ilk.text
    onay = await client.post(
        _s(d, f"/reports/daily/{ONAY_GUNU.isoformat()}/approve"), headers=admin
    )
    assert onay.status_code == 200, onay.text
    ikinci = await client.put(
        _s(d, "/settings"), headers=admin, json={**govde, "tolerance_points": "5.00"}
    )
    assert ikinci.status_code == 200, ikinci.text
    for kart in ikinci.json()["composite_metrics"]:
        d.etiketler[uuid.UUID(kart["id"])] = f"<bilesik:{kart['name'][:2]}>"
    await rastgele_kimlikleri_etiketle(session, d)
    return d
