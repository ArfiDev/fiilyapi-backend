"""H9 fixture'ları: özet ucu + `contracts` placeholder'ları (spec §9.6).
`tests/progress_payments/conftest.py` bölmesi.

🔴 Bu modül `conftest.py`ye AÇIKÇA import edilir; fixture görünürlüğü ve kapsamı
bölme öncesiyle AYNIDIR.
"""

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals.models import ApprovalRole
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Site
from app.modules.users.models import User
from tests.modules.approvals.conftest import onay_rolu_ver

from ._fixtures_base import _dagit

# --- H9 fixture'ları: özet ucu + `contracts` placeholder'ları (spec §9.6) ---


async def _ozet_ortami(
    seeded_db: AsyncSession,
    project_factory,
    olusturan: User,
    *,
    code: str,
    amount: Decimal | None,
    plan: list[tuple[ProgressPaymentStatus, Decimal | None]],
    advance_pct: Decimal = Decimal("20"),
    unit_price: Decimal = Decimal("100"),
) -> tuple[Project, Site, EmployerContractItem]:
    """Özet testlerinin ortak kurulumu: `plan` sırasıyla `sequence_no` 1..n
    hakediş üretir; her öğe `(durum, miktar)` — miktar `None` ise satırsız.

    Durum geçiş uçlarıyla kurulamaz (D8 tek açık hakediş kuralı birden çok
    `pending_approval` kaydı meşru uçlardan üretilmesini engeller; SHK 84 ise
    "Onay Bekleyen 3" der): kurulum testin ÖLÇTÜĞÜ şey değil, ÖN KOŞULUDUR.
    """
    project = await project_factory(code=code, name=f"Özet Projesi {code}")
    contract = ProjectContract(
        project_id=project.id,
        contract_no=f"SZL-{code}",
        amount=amount,
        advance_pct=advance_pct,
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    site = Site(project_id=project.id, code=f"SNT-{code}", name=f"Özet Şantiyesi {code}")
    group = EmployerContractGroup(project_id=project.id, name="Özet Grubu", sort_order=1)
    seeded_db.add_all([site, group])
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="03.001",
        description="Özet pozu",
        unit="m³",
        quantity=Decimal("100000"),
        unit_price=unit_price,
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    await _dagit(seeded_db, site, item, Decimal("100000"))

    for sequence_no, (durum, miktar) in enumerate(plan, start=1):
        payment = ProgressPayment(
            project_id=project.id,
            sequence_no=sequence_no,
            status=durum,
            period_year=2026,
            period_month=((sequence_no - 1) % 12) + 1,
            vat_pct=contract.vat_pct,
            advance_pct=contract.advance_pct,
            retainage_pct=contract.retainage_pct,
            created_by=olusturan.id,
        )
        if miktar is not None:
            payment.lines = [
                ProgressPaymentLine(
                    contract_item_id=item.id,
                    site_id=site.id,
                    code=item.code,
                    description=item.description,
                    unit=item.unit,
                    contract_unit_price=item.unit_price,
                    coefficient=Decimal("1.000"),
                    quantity=miktar,
                    group_name=group.name,
                )
            ]
        seeded_db.add(payment)
        await seeded_db.flush()
    return project, site, item


@pytest.fixture
async def dort_onayli_hakedisli_proje(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> uuid.UUID:
    """E14 127-147 altın senaryosu: bedel 11.200.000 · 4 tamamlanmış hakediş
    (3 `approved` + 1 `paid`), her biri 21.000 × ₺100 = 2.100.000 brüt →
    kümülatif 8.400.000 (%75)."""
    project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S01",
        amount=Decimal("11200000"),
        plan=[
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.paid, Decimal("21000")),
        ],
    )
    return project.id


@pytest.fixture
async def karisik_durumlu_proje(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> uuid.UUID:
    """SHK 82/84 sayaçları: 4 tamamlanmış (3 `approved` + 1 `paid`) + 3
    `pending_approval` + 1 `draft`."""
    project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S02",
        amount=Decimal("11200000"),
        plan=[
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.paid, Decimal("21000")),
            (ProgressPaymentStatus.pending_approval, Decimal("5000")),
            (ProgressPaymentStatus.pending_approval, Decimal("5000")),
            (ProgressPaymentStatus.pending_approval, Decimal("5000")),
            (ProgressPaymentStatus.draft, Decimal("5000")),
        ],
    )
    return project.id


@pytest.fixture
async def taslakli_proje(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> uuid.UUID:
    """1 `approved` (2.100.000) + 1 `draft` (2.100.000): kümülatif YALNIZ
    onaylıyı sayar (spec §6.6 `prev` kümesi)."""
    project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S03",
        amount=Decimal("11200000"),
        plan=[
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.draft, Decimal("21000")),
        ],
    )
    return project.id


@pytest.fixture
async def avans_tavanina_dayanan_proje(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> uuid.UUID:
    """§6.3 kümülatif TAVAN özette de koşar: bedel 1.000.000 · avans %20 →
    tavan 200.000. İki onaylı hakediş, her biri 800.000 brüt: ilki 160.000
    kesinti alır, ikincisi tavanda kalan 40.000'i alır → toplam 200.000.
    Basit toplam (`Σ gross × %20`) 320.000 verirdi."""
    project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S04",
        amount=Decimal("1000000"),
        plan=[
            (ProgressPaymentStatus.approved, Decimal("8000")),
            (ProgressPaymentStatus.approved, Decimal("8000")),
        ],
    )
    return project.id


@pytest.fixture
async def iki_projeli_ozet_ortami(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, uuid.UUID]:
    """Toplu (batch) çekimin GRUPLAMA doğruluğu için: iki ayrı proje, farklı
    tutarlarda tamamlanmış hakedişler. Gruplama anahtarı yanlışsa sayılar
    projeler arasında karışır."""
    a_project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S05A",
        amount=Decimal("11200000"),
        plan=[
            (ProgressPaymentStatus.approved, Decimal("21000")),
            (ProgressPaymentStatus.approved, Decimal("21000")),
        ],
    )
    b_project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S05B",
        amount=Decimal("5000000"),
        plan=[(ProgressPaymentStatus.approved, Decimal("3000"))],
    )
    return a_project.id, b_project.id


@pytest.fixture
async def iki_santiyeli_cok_hakedisli_proje(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """`(proje, şantiye)` kırılımı: TEK projede iki şantiye, üç tamamlanmış
    hakediş; her hakediş iki şantiyeye de satır yazar. Toplu çekimin
    gruplaması şantiye düzeyinde de doğru olmalı (E15 "Önceki" kolonu).

    Dönüş: `(project_id, a_site_id, b_site_id)`.
    """
    project = await project_factory(code="PP-S06", name="İki Şantiyeli Proje")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-PP-S06",
        amount=Decimal("10000000"),
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    a_site = Site(project_id=project.id, code="SNT-PP-S06-A", name="A-Blok")
    b_site = Site(project_id=project.id, code="SNT-PP-S06-B", name="B-Blok")
    group = EmployerContractGroup(project_id=project.id, name="Betonarme İşleri", sort_order=1)
    seeded_db.add_all([a_site, b_site, group])
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="03.001",
        description="Beton dökümü",
        unit="m³",
        quantity=Decimal("100000"),
        unit_price=Decimal("100"),
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    await _dagit(seeded_db, a_site, item, Decimal("50000"))
    await _dagit(seeded_db, b_site, item, Decimal("50000"))

    # A şantiyesi her hakedişte 100, B şantiyesi 10 birim: karışırsa sayılar tutmaz.
    for sequence_no in (1, 2, 3):
        payment = ProgressPayment(
            project_id=project.id,
            sequence_no=sequence_no,
            status=ProgressPaymentStatus.approved,
            period_year=2026,
            period_month=sequence_no,
            vat_pct=contract.vat_pct,
            advance_pct=contract.advance_pct,
            retainage_pct=contract.retainage_pct,
            created_by=hakedis_olusturan.id,
        )
        payment.lines = [
            ProgressPaymentLine(
                contract_item_id=item.id,
                site_id=a_site.id,
                code=item.code,
                description=item.description,
                unit=item.unit,
                contract_unit_price=item.unit_price,
                coefficient=Decimal("1.000"),
                quantity=Decimal("100"),
                group_name=group.name,
            ),
            ProgressPaymentLine(
                contract_item_id=item.id,
                site_id=b_site.id,
                code=item.code,
                description=item.description,
                unit=item.unit,
                contract_unit_price=item.unit_price,
                coefficient=Decimal("1.000"),
                quantity=Decimal("10"),
                group_name=group.name,
            ),
        ]
        seeded_db.add(payment)
        await seeded_db.flush()
    return project.id, a_site.id, b_site.id


@pytest.fixture
async def tavana_dayanan_iki_proje(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, uuid.UUID]:
    """Toplu çekimin gruplamasını AVANS TAVANI üzerinden ölçen kurulum.

    A projesi: bedel 1.000.000, avans %20 → tavan 200.000; iki onaylı hakediş,
    her biri 800.000 brüt. A#2'nin kesintisi tavanda kalan 40.000'dir.
    B projesi: bedel 5.000.000 (tavan 1.000.000), `sequence_no=1` onaylı hakediş
    800.000 brüt → 160.000 kesinti.

    Gruplama anahtarı yanlışsa (ya da sıra eşiği kendi kaydını içeriyorsa) A#2'nin
    "önceki" kümesi şişer, tavan aşılmış sayılır ve kesinti 40.000 yerine 0 olur —
    brüt/teminat değişmediği için fark YALNIZ avans üzerinden görünür. Kümülatif
    tavan olmasaydı bu iki hata da sessiz kalırdı.
    """
    a_project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S07A",
        amount=Decimal("1000000"),
        plan=[
            (ProgressPaymentStatus.approved, Decimal("8000")),
            (ProgressPaymentStatus.approved, Decimal("8000")),
        ],
    )
    b_project, _, _ = await _ozet_ortami(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-S07B",
        amount=Decimal("5000000"),
        plan=[(ProgressPaymentStatus.approved, Decimal("8000"))],
    )
    return a_project.id, b_project.id


@pytest.fixture
async def zincir_onaycilari(
    seeded_db: AsyncSession, admin_headers: dict[str, str], muhasebe_headers: dict[str, str]
) -> None:
    """🔴 OK-1A T3 — `submit`ten GEÇEN akışların ONAY ROLÜ ihtiyacı.

    Bu modülün fabrikaları hakedişi doğrudan DB'ye yazar (durumu elle verir), o
    yüzden zincirleri YOKTUR ve bugünkü tek adımlı davranışları sürer. Ama
    `submit` → `approve` sırasını UÇTAN koşan testlerde artık gerçek bir zincir
    açılır ve `/approve` aktörden ADIM ROLÜNÜ ister (uç kapısı `progress_
    payments ≥ approve` yetmez — onay rolü ≠ sistem rolü, K1).

    Fixture bunu OPT-IN bırakır (autouse DEĞİL): rolü olmayan aktörün 403
    aldığını ölçen testler (`test_onay_ROLU_olmayan_aktor_403`) aynı
    `muhasebe_headers`ı KULLANIR ve sessizce yeşile boyanmamalıdır.

    Verilen tek rol `accounting`tir: işveren hakedişi zincirinin eşik ALTI tek
    adımı odur (mockup `Onay Kutusu.dc.html:210-240`). `patron` bilinçli olarak
    VERİLMEZ — eşik üstü akışlar kendi aktörünü kurar.
    """
    for email in ("admin@pp-crud.co", "muhasebe@pp-transitions.co"):
        user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
        await onay_rolu_ver(seeded_db, user, ApprovalRole.accounting)
