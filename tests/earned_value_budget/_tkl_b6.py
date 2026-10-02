"""TKL-B6.3/B6.4 ortak dunya kurucusu: bolumsuz santiye + sozlesme kalemleri (BOQ'a bagli).

```
Sozlesme gruplari → BOQ gruplari (AYNI AD; dagitimin yazdigi gibi)
CG1 "Betonarme"  ci1 "Beton"   (katalog beton · KAB)   ci2 "TUĞLA" (katalog tugla2 · KAB)
CG2 "Duvar"      ci3 "Tuğla"   (katalog tugla · DUV)
CG3 "Karışık"    ci4 "Beton 2" (katalog beton · KAB)   ci5 "Tuğla 2" (katalog tugla · DUV)
CG4 "Katalogsuz" ci6 "Özel"    (katalog YOK)
```
Santiyede bolum YOK: her kalemin TAM miktari "Bolumsuz" yaprak (`boq/distribution` bolumsuz
santiyede boyle yazar). Kalemler 1:1 BOQ kalemine bagli (`contract_item_id`).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.contract_seed import ContractItemSeed, ContractSeedRequest
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Site

D = Decimal
START = date(2026, 6, 1)
END = date(2026, 12, 31)


@dataclass
class B6Dunya:
    cg: dict[str, EmployerContractGroup]
    ci: dict[str, EmployerContractItem]
    bg: dict[str, BoqGroup]
    bi: dict[str, BoqItem]


_PLAN = (
    # kod, ad, sozlesme grubu, katalog anahtari
    ("ci1", "Beton", "Betonarme", "beton"),
    ("ci2", "TUĞLA", "Betonarme", "tugla2"),
    ("ci3", "Tuğla", "Duvar", "tugla"),
    ("ci4", "Beton 2", "Karışık", "beton"),
    ("ci5", "Tuğla 2", "Karışık", "tugla"),
    ("ci6", "Özel", "Katalogsuz", None),
)


async def kur(
    db: AsyncSession, proje: Project, santiye: Site, katalog: dict, *, boq: bool = True
) -> B6Dunya:
    db.add(ProjectContract(project_id=proje.id, contract_no="B6-SZL", amount=D("1000")))
    await db.flush()
    cg = {
        ad: EmployerContractGroup(project_id=proje.id, name=ad, sort_order=i)
        for i, ad in enumerate(("Betonarme", "Duvar", "Karışık", "Katalogsuz"), 1)
    }
    db.add_all(cg.values())
    await db.flush()
    ci: dict[str, EmployerContractItem] = {}
    for sira, (anahtar, ad, grup, kat) in enumerate(_PLAN, 1):
        ci[anahtar] = EmployerContractItem(
            project_id=proje.id,
            group_id=cg[grup].id,
            code=f"S-{sira:03d}",
            description=ad,
            unit="m3",
            quantity=D(10),
            unit_price=D(100),
            sort_order=sira,
            catalog_item_id=katalog[kat].id if kat else None,
        )
    db.add_all(ci.values())
    await db.flush()
    bg: dict[str, BoqGroup] = {}
    bi: dict[str, BoqItem] = {}
    if boq:
        bg = {
            ad: BoqGroup(site_id=santiye.id, name=ad, sort_order=g.sort_order)
            for ad, g in cg.items()
        }
        db.add_all(bg.values())
        await db.flush()
        for sira, (anahtar, ad, grup, _kat) in enumerate(_PLAN, 1):
            bi[anahtar] = BoqItem(
                site_id=santiye.id,
                group_id=bg[grup].id,
                contract_item_id=ci[anahtar].id,
                code=f"S-{sira:03d}",
                description=ad,
                unit="m3",
                quantity=D(10),
                unit_price=D(100),
                sort_order=sira,
            )
        db.add_all(bi.values())
        await db.flush()
    return B6Dunya(cg=cg, ci=ci, bg=bg, bi=bi)


def istek(
    proje: Project,
    santiye: Site | None,
    dunya: B6Dunya,
    oranlar: dict[str, tuple[Decimal | None, bool]],
    *,
    elle: dict[uuid.UUID, uuid.UUID] | None = None,
    baslangic: date | None = START,
    bitis: date | None = END,
) -> ContractSeedRequest:
    """`oranlar`: kalem anahtari → (unit_mhr, rate_is_offer); listede olmayan kalem DAHIL DEGIL."""
    items = tuple(
        ContractItemSeed(
            contract_item_id=dunya.ci[k].id,
            catalog_item_id=dunya.ci[k].catalog_item_id,
            unit_mhr=mhr,
            rate_is_offer=offer,
        )
        for k, (mhr, offer) in oranlar.items()
    )
    return ContractSeedRequest(
        project_id=proje.id,
        site_id=santiye.id if santiye else None,
        start=baslangic,
        end=bitis,
        items=items,
        group_disciplines=elle or {},
        actor_id=None,
        label="Rev.0 — TKL-TEST",
    )


TUM_ORANLAR: dict[str, tuple[Decimal | None, bool]] = {
    "ci1": (D("2.5"), True),
    "ci2": (D("0.6"), False),
    "ci3": (D("0.7"), True),
    "ci4": (D("1.9"), False),
    "ci5": (D("0.8"), False),
    "ci6": (None, False),
}
