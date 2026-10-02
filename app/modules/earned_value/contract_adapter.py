"""🔶 PLANLAMA ↔ SOZLESME TOHUMU ADAPTORU — `core.contract_seed` portunun EV saglayicisi
(TKL-B6.3; TKL-PLAN §4.3-§4.5). Diger adaptorler gibi TEK isaretli dosya.

Cekirdek (teklif→proje donusturmesi) bu dosyayi GORMEZ: portu cagirir, bu dosya `register()` ile
kaydolur. 🔴 Kayit YERI `catalog_router.py` import yan etkisidir; dusurulurse donusturme
adam-saati SESSIZCE kaybeder — bekcisi `tests/earned_value_budget/test_tkl_b6_adapter.py`.

Kanca (hata YUKSELIR → cagiranin islemi geri alinir; commit ETMEZ):
1. HER ZAMAN: orani dolu her kalem icin `ev_contract_item_rates` satiri (toplu).
2. `site_id` varsa: Rev.0 TASLAGI (dondurma YOK — T34); ortak mantik `contract_rates`.
3. Uyarilar (yapisal `code` + Turkce metin): `mixed_discipline_group`, `no_rate_slot`,
   `item_not_in_site`.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from types import SimpleNamespace
from typing import cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core import contract_seed
from app.core.contract_seed import ContractSeedRequest, SeedWarning
from app.modules.earned_value import contract_rates
from app.modules.earned_value.access import SiteContext
from app.modules.earned_value.models import EvContractItemRate, RateSource
from app.modules.projects.models import Project
from app.modules.sites.models import Site
from app.modules.users.models import User

CODE_NO_RATE_SLOT = "no_rate_slot"
CODE_ITEM_NOT_IN_SITE = "item_not_in_site"


def _has_slot(unit_mhr: Decimal | None) -> bool:
    """`ev_contract_item_rates.unit_mhr > 0` CHECK'i: bos ya da sifir oran yuva sayilmaz."""
    return unit_mhr is not None and unit_mhr > 0


def _write_slots(session: AsyncSession, req: ContractSeedRequest) -> int:
    """Oran yuvalarini TOPLU ekler; yuvasiz kalem sayisini dondurur."""
    rows = [
        EvContractItemRate(
            contract_item_id=item.contract_item_id,
            unit_mhr=item.unit_mhr,
            source=RateSource.OFFER if item.rate_is_offer else RateSource.CATALOG,
        )
        for item in req.items
        if _has_slot(item.unit_mhr)
    ]
    session.add_all(rows)
    return len(req.items) - len(rows)


async def _site_context(session: AsyncSession, req: ContractSeedRequest) -> SiteContext:
    site = await session.get(Site, req.site_id)
    project = await session.get(Project, req.project_id)
    if site is None or project is None or site.project_id != project.id:
        raise ValueError("Sözleşme tohumu: şantiye bu projeye ait değil")
    return SiteContext(site=site, project=project)


async def _actor(session: AsyncSession, actor_id: uuid.UUID | None) -> User:
    """`_draft_for_write` yalniz `actor.id` kullanir (`created_by_user_id` NULL olabilir)."""
    user = await session.get(User, actor_id) if actor_id is not None else None
    return user if user is not None else cast(User, SimpleNamespace(id=None))


def _warnings(
    req: ContractSeedRequest, result: contract_rates.ApplyResult | None, no_slot: int
) -> list[SeedWarning]:
    out: list[SeedWarning] = []
    if result is not None:
        out.extend(
            SeedWarning(
                contract_rates.CODE_MIXED_GROUP,
                contract_rates.MSG_MIXED_GROUP,
                group.contract_group_ids[0],
            )
            for group in result.mixed_groups
        )
    if no_slot:
        out.append(
            SeedWarning(
                CODE_NO_RATE_SLOT,
                f"{no_slot} kalemde adam-saat oranı yok (sözleşmeden doldurulamaz)",
            )
        )
    if result is not None:
        missing = sum(
            1 for i in req.items if i.contract_item_id not in result.linked_contract_item_ids
        )
        if missing:
            out.append(
                SeedWarning(
                    CODE_ITEM_NOT_IN_SITE, f"{missing} sözleşme kalemi şantiyede BOQ'da bulunamadı"
                )
            )
    return out


async def seed_hook(session: AsyncSession, req: ContractSeedRequest) -> list[SeedWarning]:
    no_slot = _write_slots(session, req)
    await session.flush()
    if req.site_id is None:
        return _warnings(req, None, no_slot)
    ctx = await _site_context(session, req)
    window = (req.start, req.end) if req.start is not None and req.end is not None else None
    result = await contract_rates.apply_contract_to_draft(
        session,
        ctx,
        await _actor(session, req.actor_id),
        manual_disciplines=req.group_disciplines,
        window=window,
        force_draft=True,
        label=req.label,
    )
    return _warnings(req, result, no_slot)


def register() -> None:
    contract_seed.register_seed_hook(seed_hook)
