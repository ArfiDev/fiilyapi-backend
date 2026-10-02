"""Yeni projenin sozlesme TOHUMU: gruplar + kalemler + (istege bagli) tam miktar dagitimi
(TKL-B6.2, TKL-PLAN §4.5).

## Neden ayri servis (B3 toplu servisi degil)
`create_employer_items_bulk` her cagrida `_visible_project` ile TUM projeleri okur, grup acmaz ve
200 kalem tavani semadadir. Donusturme ise AZ ONCE yaratilmis bir projeyi ELINDE tutar: gorunurluk
sorusu yoktur (cagiran yetkiyi kendi kapisinda cozdu), gruplar + kalemler tek geciste yazilir.

## Sozlesme
* Cagiran DOGRULAMAYI yapmistir (kod/grup-adi tekilligi, katalog varligi, tavanlar); bu modul
  dogrulamaz, yazar. DB kisitlari (`uq_employer_contract_items_project_code`, CHECK) son savunmadir.
* Hicbir sey commit edilmez; yalniz `flush` (cagiranin islemi sahibidir).
* `catalog_item_id` kalemde SABIT iz baglari; `price_changed_at` acikca `now(UTC)` (son fiyat
  saglayicisi bu damgayi okur; ayni islemde sonradan gelen fiyat degisimi ondan ONCE damgalanamaz).
* Grup sirasi ve her grubun kalem sirasi GOVDE sirasidir (`sort_order` = indeks, T15).

## Tam dagitim
`distribute_in_full` MEVCUT `distribution._apply_allocations`i cagirir (BOQ grubu sozlesme grup
ADIYLA acilir; ayna alanlar `apply_mirrored_fields` ile kopyalanir). Santiye YENI oldugu icin
mevcut BOQ satiri/grubu yoktur: bos `existing`/`relink`/`group_cache` ile cagrilir.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.distribution import _apply_allocations
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.contracts.schemas import ContractAllocationInput
from app.modules.projects.models import Project

__all__ = [
    "SeedGroupInput",
    "SeedItemInput",
    "SeededContract",
    "SeededGroup",
    "distribute_in_full",
    "seed_contract",
]


@dataclass(frozen=True, slots=True)
class SeedItemInput:
    catalog_item_id: uuid.UUID | None
    code: str
    description: str
    unit: str
    quantity: Decimal
    unit_price: Decimal


@dataclass(frozen=True, slots=True)
class SeedGroupInput:
    name: str
    items: Sequence[SeedItemInput]


@dataclass(frozen=True, slots=True)
class SeededGroup:
    group: EmployerContractGroup
    #: Girdi sirasiyla (`items[i]` ↔ girdinin i. kalemi).
    items: list[EmployerContractItem]


@dataclass(frozen=True, slots=True)
class SeededContract:
    groups: list[SeededGroup]

    @property
    def items(self) -> list[EmployerContractItem]:
        """Tum kalemler: grup sirasi, sonra grup ici sira (govde sirasi)."""
        return [item for seeded in self.groups for item in seeded.items]


async def seed_contract(
    session: AsyncSession, project: Project, groups: Sequence[SeedGroupInput]
) -> SeededContract:
    """`project`in sozlesmesine gruplari + kalemleri yazar. Projenin sozlesme satiri OLMALI."""
    now = datetime.now(UTC)
    seeded: list[SeededGroup] = []
    for group_index, group_input in enumerate(groups):
        group = EmployerContractGroup(
            id=uuid.uuid4(), project_id=project.id, name=group_input.name, sort_order=group_index
        )
        session.add(group)
        items = [
            EmployerContractItem(
                id=uuid.uuid4(),
                project_id=project.id,
                group_id=group.id,
                code=entry.code,
                description=entry.description,
                unit=entry.unit,
                quantity=entry.quantity,
                unit_price=entry.unit_price,
                sort_order=item_index,
                catalog_item_id=entry.catalog_item_id,
                price_changed_at=now,
            )
            for item_index, entry in enumerate(group_input.items)
        ]
        session.add_all(items)
        seeded.append(SeededGroup(group=group, items=items))
    await session.flush()
    return SeededContract(groups=seeded)


async def distribute_in_full(
    session: AsyncSession, seeded: SeededContract, site_id: uuid.UUID
) -> None:
    """TUM tohumlanan kalemleri `site_id` santiyesine TAM miktarla dagitir (S-D2).

    Santiye bu islemde yeni acilmis olmalidir (bos BOQ): yeniden yazma/birlestirme YOKTUR.
    """
    items_by_id = {item.id: item for item in seeded.items}
    group_by_item = {item.id: s.group for s in seeded.groups for item in s.items}
    allocations = [
        ContractAllocationInput(contract_item_id=item.id, site_id=site_id, quantity=item.quantity)
        for item in seeded.items
    ]
    _apply_allocations(session, allocations, items_by_id, group_by_item, {}, {}, {})
    await session.flush()
