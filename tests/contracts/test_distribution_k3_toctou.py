"""K3'ün ÜÇÜNCÜ yazma kapısının (`contracts/distribution.py`) KOŞULLU kilidi —
envanter kayıt #11'in açık kalan bacağı.

`_assert_quota_covers_section_allocations` kilidi KOŞULLU alır: kotası
DÜŞÜYOR mu kararını, `save_distribution`ın kendi okuma fazında (satır ~484,
`list_boq_items_for_sites` — düz `SELECT`, `FOR UPDATE` YOK) aldığı bir
STALE `BoqItem.quantity` görüntüsüne bakarak verir (satır ~366:
`if row is None or alloc.quantity >= row.quantity: continue`). Karar
"artış" derse `lock_item` de `allocated_total_for_item` de HİÇ ÇAĞRILMAZ.

Bu, `boq/service.py`'nin iki kapısından (`update_item`, `replace_allocations`)
FARKLIDIR: onlar `quantity` verildiğinde KOŞULSUZ kilitler, sonra kilitli
(taze) değere göre karar verir.

Somut senaryo (`READ COMMITTED`, `app/core/db.py` `create_async_engine`
çağrısında `isolation_level` yok — varsayılan): BOQ satırı R, quantity=500,
tahsis 0.

1. T1 (`save_distribution`), R'yi 484. satırda quantity=500 olarak okur.
2. Araya T2 girer ve GERÇEKTEN COMMIT EDER: `boq.service.update_item` ile
   R'nin kotasını 500→1200 yükseltir (tahsis 0 < 1200, geçer), sonra
   `boq.service.replace_allocations` ile bölüme 700 tahsis eder
   (700 <= 1200, geçer).
3. T1, alloc.quantity=600'ü 484'teki STALE 500 ile karşılaştırır:
   600 >= 500 → "artış" sanır, kilit ALMAZ, `_QUANTITY_BELOW_ALLOCATED`
   kontrolünü hiç çalıştırmaz.
4. T1 `row.quantity = 600` yazıp flush/commit eder.

Sonuç: DB'de quantity=600, SUM(tahsis)=700 — K3 KIRIK, negatif
`unallocated_quantity` serileşir. Bu dosya adım 1-4'ü GERÇEK iki bağımsız
bağlantıyla üretir (aynı gerekçeyle `test_distribution_concurrency.py`
`client`/`seeded_db` KULLANMAZ: `db_session` SAVEPOINT'e sarar, gerçek satır
kilidi/READ COMMITTED semantiği test edilemez).
"""

import asyncio
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ConflictError
from app.core.security import hash_password
from app.modules.boq import repository as boq_repository
from app.modules.boq import service as boq_service
from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.boq.schemas import BoqItemAllocationInput, BoqItemAllocationsReplace, BoqItemUpdate
from app.modules.contracts import distribution, distribution_quantity, repository, schemas
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.projects.models import Project, ProjectContract
from app.modules.roles.models import Role
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User
from tests._yaris import YARIS_TAVANI_SN
from tests.conftest import test_engine

pytestmark = pytest.mark.asyncio

_SessionFactory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

_ROLE_KEY = "k3_toctou_yarisi"
_ILK_KOTA = Decimal("500")
_YUKSELTILMIS_KOTA = Decimal("1200")
_ARADA_TAHSIS = Decimal("700")
_T1_HEDEF = Decimal("600")  # 484'teki stale 500'e göre "artış" gibi görünür


class _Kurulum:
    def __init__(
        self,
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        item_id: uuid.UUID,
        site_id: uuid.UUID,
        section_id: uuid.UUID,
        boq_item_id: uuid.UUID,
    ) -> None:
        self.project_id = project_id
        self.user_id = user_id
        self.item_id = item_id
        self.site_id = site_id
        self.section_id = section_id
        self.boq_item_id = boq_item_id


@pytest.fixture
async def kurulum():
    veri = await _kur()
    try:
        yield veri
    finally:
        await _temizle(veri)


async def test_toctou_araya_giren_yukseltme_kotayi_asagi_cekebilir(kurulum: _Kurulum) -> None:
    """KIRMIZI (bugünkü koda karşı): T2 araya girip kotayı yükseltip tahsis
    ekledikten SONRA bile T1 hiç kilit almadan, hiç `ConflictError` almadan
    kotayı 600'e düşürebiliyorsa kapı KOŞULLU kilitleniyordur — TOCTOU açık.

    YEŞİL (fix sonrası): kapı yazılacak HER satırı koşulsuz kilitler, kararını
    kilitli/taze değere göre verir; T1 bu senaryoda `ConflictError` (409)
    almalı, DB'de kota 1200'de kalmalı, tahsis 700 korunmalı.
    """
    read_done = asyncio.Event()
    t2_done = asyncio.Event()

    async def t1() -> str:
        async with _SessionFactory() as s1:
            # --- save_distribution'ın 1. fazı: kilitsiz okuma (satır ~484) ---
            site_boq_items = await repository.list_boq_items_for_sites(s1, [kurulum.site_id])
            existing_by_key = distribution_quantity.index_allocations(site_boq_items)

            read_done.set()
            await asyncio.wait_for(t2_done.wait(), timeout=YARIS_TAVANI_SN)

            alloc = schemas.ContractAllocationInput(
                contract_item_id=kurulum.item_id, site_id=kurulum.site_id, quantity=_T1_HEDEF
            )
            try:
                await distribution._assert_quota_covers_section_allocations(
                    s1, [alloc], existing_by_key, {}
                )
            except ConflictError:
                await s1.rollback()
                return "reddedildi"

            # Kapı geçti — gerçek yazma yolunu taklit et (`_apply_allocations`
            # madde 3 ile BİREBİR).
            row = existing_by_key[(kurulum.item_id, kurulum.site_id)]
            row.quantity = _T1_HEDEF
            await s1.commit()
            return "yazildi"

    async def t2() -> None:
        await asyncio.wait_for(read_done.wait(), timeout=YARIS_TAVANI_SN)
        async with _SessionFactory() as s2:
            actor = await s2.get(User, kurulum.user_id)
            await boq_service.update_item(
                s2, actor, kurulum.boq_item_id, BoqItemUpdate(quantity=_YUKSELTILMIS_KOTA)
            )
            await boq_service.replace_allocations(
                s2,
                actor,
                kurulum.boq_item_id,
                BoqItemAllocationsReplace(
                    allocations=[
                        BoqItemAllocationInput(
                            section_id=kurulum.section_id, quantity=_ARADA_TAHSIS
                        )
                    ]
                ),
            )
            await s2.commit()
        t2_done.set()

    task1 = asyncio.create_task(t1())
    task2 = asyncio.create_task(t2())
    sonuc1 = await asyncio.wait_for(task1, timeout=YARIS_TAVANI_SN)
    await asyncio.wait_for(task2, timeout=YARIS_TAVANI_SN)

    assert sonuc1 == "reddedildi", (
        "T1, araya giren T2'nin yükselttiği kotayı ve eklediği tahsisi hiç "
        "görmeden (kilitsiz stale okuma yüzünden) kotayı düşürebildi — K3'ün "
        "üçüncü kapısı KOŞULLU kilitleniyor (TOCTOU açık)"
    )

    async with _SessionFactory() as dogrulama:
        boq_item = await dogrulama.get(BoqItem, kurulum.boq_item_id)
        assert boq_item is not None
        toplam_tahsis = await boq_repository.allocated_total_for_item(
            dogrulama, kurulum.boq_item_id
        )

    assert toplam_tahsis <= boq_item.quantity, (
        f"K3 KIRIK: quantity={boq_item.quantity}, SUM(tahsis)={toplam_tahsis} "
        "— unallocated_quantity negatif serileşir"
    )
    assert boq_item.quantity == _YUKSELTILMIS_KOTA


async def _kur() -> _Kurulum:
    async with _SessionFactory() as session:
        role = Role(key=_ROLE_KEY, name="K3 TOCTOU Rolü")
        session.add(role)
        await session.flush()
        user = User(
            email="k3-toctou@contracts.co",
            password_hash=hash_password("parola1234"),
            full_name="K3 TOCTOU",
            role_id=role.id,
        )
        session.add(user)

        project = Project(code="CD-TOCTOU-001", name="K3 TOCTOU Projesi")
        session.add(project)
        await session.flush()
        session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
        session.add(
            ProjectContract(
                project_id=project.id,
                contract_no="SZL-2026-TOCTOU",
                amount=Decimal("1000000"),
                advance_pct=Decimal("10"),
                retainage_pct=Decimal("5"),
                vat_pct=Decimal("20"),
            )
        )
        await session.flush()

        group = EmployerContractGroup(project_id=project.id, name="A Grubu", sort_order=1)
        session.add(group)
        await session.flush()
        item = EmployerContractItem(
            project_id=project.id,
            group_id=group.id,
            code="10.100",
            description="Beton dökümü",
            unit="m3",
            quantity=Decimal("100000"),
            unit_price=Decimal("1000.00"),
            sort_order=1,
        )
        session.add(item)

        site = Site(project_id=project.id, code="CD-TC", name="TOCTOU Şantiyesi")
        session.add(site)
        await session.flush()

        section = Section(site_id=site.id, name="Kat 1-5")
        session.add(section)

        boq_group = BoqGroup(site_id=site.id, name="A Grubu", sort_order=1)
        session.add(boq_group)
        await session.flush()

        boq_item = BoqItem(
            site_id=site.id,
            group_id=boq_group.id,
            contract_item_id=item.id,
            code="10.100",
            description="Beton dökümü",
            unit="m3",
            quantity=_ILK_KOTA,
            unit_price=Decimal("1000.00"),
            sort_order=1,
        )
        session.add(boq_item)
        await session.commit()

        return _Kurulum(project.id, user.id, item.id, site.id, section.id, boq_item.id)


async def _temizle(kurulum: _Kurulum) -> None:
    async with _SessionFactory() as session:
        await session.execute(
            delete(BoqItemSectionAllocation).where(
                BoqItemSectionAllocation.boq_item_id == kurulum.boq_item_id
            )
        )
        await session.execute(delete(BoqItem).where(BoqItem.site_id == kurulum.site_id))
        await session.execute(delete(BoqGroup).where(BoqGroup.site_id == kurulum.site_id))
        await session.execute(delete(Section).where(Section.site_id == kurulum.site_id))
        await session.execute(delete(Site).where(Site.id == kurulum.site_id))
        await session.execute(
            delete(EmployerContractItem).where(
                EmployerContractItem.project_id == kurulum.project_id
            )
        )
        await session.execute(
            delete(EmployerContractGroup).where(
                EmployerContractGroup.project_id == kurulum.project_id
            )
        )
        await session.execute(
            delete(ProjectContract).where(ProjectContract.project_id == kurulum.project_id)
        )
        await session.execute(delete(Project).where(Project.id == kurulum.project_id))
        await session.execute(delete(User).where(User.id == kurulum.user_id))
        await session.execute(delete(Role).where(Role.key == _ROLE_KEY))
        await session.commit()
