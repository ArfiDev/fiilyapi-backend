"""TKL-B5.1 — eszamanli "varsayilan yap": TEK varsayilan korunur.

Emsal: `test_offers_race.py` (tutulan-kilit + `pg_stat_activity` bariyeri + POZITIF KONTROL).
Oturum 1 A'yi varsayilan yapar ve COMMIT ETMEDEN bekler; oturum 2 B'yi varsayilan yapmaya
calisir.

(a) kilitli: B `pg_advisory_xact_lock`ta BEKLER, A commit olunca eski varsayilani (A) dusurup B'yi
    isaretler → sonda TEK varsayilan: B; hata YOK.
(b) KONTROL (kilit KAPALI): B A'yi goremez (commit'siz), kendi `UPDATE ... is_default = true`
    satirinda kismi tekil indeksin A girdisini BEKLER; A commit olunca `IntegrityError`
    (`uq_offer_templates_single_default`) — kilidin gercekten isi gordugunun kaniti.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.offers import template_service
from app.modules.offers.models import OfferTemplate
from app.modules.offers.template_schemas import TemplateCreate

from .test_offers_race import _kullanici, _Ortam, _ortam, _yaris


async def _iki_sablon(ortam: _Ortam) -> tuple[uuid.UUID, uuid.UUID]:
    async with ortam.Session() as s:
        user = await _kullanici(s, ortam)
        a = await template_service.create_template(s, user, TemplateCreate(name="A"))
        b = await template_service.create_template(s, user, TemplateCreate(name="B"))
        await s.commit()
        return a.id, b.id


async def _varsayilanlar(ortam: _Ortam) -> list[str]:
    async with ortam.Session() as s:
        rows = await s.scalars(select(OfferTemplate.name).where(OfferTemplate.is_default.is_(True)))
        return sorted(rows)


async def _yaris_a_sonra_b(ortam: _Ortam):
    a_id, b_id = await _iki_sablon(ortam)

    async def _a(session: AsyncSession) -> None:
        await template_service.set_default(session, await _kullanici(session, ortam), a_id)

    async def _b(session: AsyncSession) -> None:
        await template_service.set_default(session, await _kullanici(session, ortam), b_id)

    return await _yaris(ortam, _a, _b)


async def test_eszamanli_iki_varsayilan_yap_tek_varsayilan_kalir_ikinci_kazanir() -> None:
    async with _ortam(gonderilmis=False) as ortam:
        bekleyen, hata = await _yaris_a_sonra_b(ortam)

        assert "pg_advisory_xact_lock" in bekleyen, bekleyen
        assert hata is None, f"ikinci istek temiz bitmeliydi: {hata!r}"
        assert await _varsayilanlar(ortam) == ["B"]  # A dustu, TEK varsayilan


async def test_KONTROL_kilitsiz_iki_varsayilan_yap_tekil_indekste_patlar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _kilitsiz(session: AsyncSession) -> None:
        return None

    monkeypatch.setattr(template_service, "_lock_default_slot", _kilitsiz)
    async with _ortam(gonderilmis=False) as ortam:
        bekleyen, hata = await _yaris_a_sonra_b(ortam)

        assert bekleyen.startswith("UPDATE offer_templates"), bekleyen
        assert isinstance(hata, IntegrityError), f"kilitsiz de temiz gecti: {hata!r}"
        assert "uq_offer_templates_single_default" in str(hata.orig)
        assert await _varsayilanlar(ortam) == ["A"]  # indeks son savunma: yine TEK varsayilan
