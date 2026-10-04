"""H6 fixture'ları: durum geçişleri (spec §7) — onay rolleri, hakediş fabrikası,
kota bölüşen kümeler ve geçmişli ortamlar. `conftest.py` bölmesi.

🔴 Bu modül `conftest.py`ye AÇIKÇA import edilir; fixture görünürlüğü ve kapsamı
bölme öncesiyle AYNIDIR.
"""

import itertools
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.sites.models import Site
from app.modules.users.models import User

from ._fixtures_base import _auth, _dagit
from ._fixtures_lines import _gecmisli_ortam

# --- H6 fixture'ları: durum geçişleri (spec §7) ---


@pytest.fixture
async def muhasebe_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`accounting` — matriste `progress_payments=_APR` (onay seviyesi).

    `all_projects=True` erişimiyle kurulur: bu fixture'ın testleri YETKİ
    seviyesini (approve var, admin YOK) ölçer, kapsamı değil — kapsam testleri
    `kisitli_headers` ile ayrıca koşar.
    """
    email = "muhasebe@pp-transitions.co"
    await user_factory(email=email, password="parola1234", role_key="accounting")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    user.all_projects = True
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def saha_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`field_engineer` — matriste `progress_payments=_DRF` (taslak seviyesi,
    scope=project): kendi projesinde `submit` yapabilir, `approve` yapamaz."""
    email = "saha@pp-transitions.co"
    await user_factory(email=email, password="parola1234", role_key="field_engineer")
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    user.all_projects = True
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def hakedis_fabrikasi(
    seeded_db: AsyncSession,
    hakedis_sozlesmesi: tuple[Project, ProjectContract],
    hakedis_santiyesi: Site,
    hakedis_kalemi: tuple[EmployerContractItem, str],
    hakedis_olusturan: User,
):
    """İstenen DURUMDA hakediş üreten fabrika — geçiş matrisi testinin dayanağı.

    D8 "tek açık hakediş" uygulama kontrolünün DB karşılığı YOKTUR (spec §7
    devir notu); bu fabrika bilinçli olarak doğrudan DB'ye yazar, böylece
    "aynı projede iki açık hakediş" tehlikesi de kurulabilir. `sequence_no`
    her çağrıda artar (`uq_progress_payments_project_sequence`).
    """
    project, contract = hakedis_sozlesmesi
    item, group_name = hakedis_kalemi
    sequence = itertools.count(1)

    async def _olustur(
        durum: ProgressPaymentStatus,
        *,
        donem: bool = True,
        miktar: Decimal | None = Decimal("100"),
    ) -> uuid.UUID:
        payment = ProgressPayment(
            project_id=project.id,
            sequence_no=next(sequence),
            status=durum,
            period_year=2026 if donem else None,
            period_month=3 if donem else None,
            vat_pct=contract.vat_pct,
            advance_pct=contract.advance_pct,
            retainage_pct=contract.retainage_pct,
            created_by=hakedis_olusturan.id,
        )
        if miktar is not None:
            payment.lines = [
                ProgressPaymentLine(
                    contract_item_id=item.id,
                    site_id=hakedis_santiyesi.id,
                    code=item.code,
                    description=item.description,
                    unit=item.unit,
                    contract_unit_price=item.unit_price,
                    coefficient=Decimal("1.000"),
                    quantity=miktar,
                    group_name=group_name,
                )
            ]
        seeded_db.add(payment)
        await seeded_db.flush()
        return payment.id

    return _olustur


@pytest.fixture
async def gecerli_taslak(hakedis_fabrikasi) -> uuid.UUID:
    """Onaya gönderilmeye HAZIR taslak: dönem dolu + Σ miktar > 0 + sözleşme
    bedeli dolu (`hakedis_sozlesmesi.amount = 11.200.000`)."""
    return await hakedis_fabrikasi(ProgressPaymentStatus.draft)


@pytest.fixture
async def bedelsiz_sozlesmede_taslak(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> uuid.UUID:
    """`contract.amount IS NULL` (taslak sözleşme) + dönem + satır dolu taslak.

    §6.3 avans tavanı uygulanamadığı için `submit` 422 `CONTRACT_AMOUNT_REQUIRED`
    vermelidir — dönem/satır kuralları GEÇİLDİKTEN sonra (sıra kanıtı).
    """
    project = await project_factory(code="PP-T00", name="Bedelsiz Sözleşmeli Proje")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-2026-040",
        amount=None,
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    site = Site(project_id=project.id, code="SNT-2026-040", name="Bedelsiz Şantiye")
    group = EmployerContractGroup(project_id=project.id, name="Bedelsiz Grup", sort_order=1)
    seeded_db.add_all([site, group])
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="07.001",
        description="Bedelsiz sözleşme pozu",
        unit="m³",
        quantity=Decimal("300"),
        unit_price=Decimal("500"),
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    await _dagit(seeded_db, site, item, Decimal("300"))
    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.draft,
        period_year=2026,
        period_month=3,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    payment.lines = [
        ProgressPaymentLine(
            contract_item_id=item.id,
            site_id=site.id,
            code=item.code,
            description=item.description,
            unit=item.unit,
            contract_unit_price=item.unit_price,
            coefficient=Decimal("1.000"),
            quantity=Decimal("50"),
            group_name=group.name,
        )
    ]
    seeded_db.add(payment)
    await seeded_db.flush()
    return payment.id


@pytest.fixture
async def kisitli_projede_onay_bekleyen(
    seeded_db: AsyncSession, kisitli_proje: Project, hakedis_olusturan: User
) -> uuid.UUID:
    """`site_chief_headers`/`kisitli_headers` kullanıcılarının GÖRDÜĞÜ projede
    `pending_approval` hakediş.

    Kapının görünürlükten ÖNCE çalıştığını kanıtlayan testler için şart: kayıt
    görünmeseydi 403 ile 404 ayırt edilemezdi.
    """
    contract = ProjectContract(
        project_id=kisitli_proje.id,
        contract_no="SZL-2026-050",
        amount=Decimal("3000000"),
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    payment = ProgressPayment(
        project_id=kisitli_proje.id,
        sequence_no=1,
        status=ProgressPaymentStatus.pending_approval,
        period_year=2026,
        period_month=3,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    seeded_db.add(payment)
    await seeded_db.flush()
    return payment.id


@pytest.fixture
async def kota_bolusen_iki_hakedis(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, uuid.UUID]:
    """H5'ten DEVREDİLEN zorunluluğun senaryosu (H5 denetimi O2).

    Aynı (kalem, şantiye) çiftinde kota 1.000; AYNI ANDA açık iki
    `pending_approval` hakediş, her biri 600 birim. Her biri TEK BAŞINA kotaya
    sığar (yazma anındaki kontrol ikisini de geçirir, çünkü o kontrol yalnız
    `approved|paid` kümesine bakar); ikisi de onaylanırsa toplam 1.200 > 1.000
    olur. Kotanın nihai bekçisi bu yüzden ONAY anıdır.
    """
    project = await project_factory(code="PP-Q03", name="Kota Bölüşen Proje")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-PP-Q03",
        amount=Decimal("5000000"),
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    site = Site(project_id=project.id, code="SNT-PP-Q03", name="Kota Şantiyesi")
    group = EmployerContractGroup(project_id=project.id, name="Kota Grubu", sort_order=1)
    seeded_db.add_all([site, group])
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="08.001",
        description="Kota bölüşen poz",
        unit="m³",
        quantity=Decimal("1000"),
        unit_price=Decimal("100"),
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    await _dagit(seeded_db, site, item, Decimal("1000"))

    payment_ids: list[uuid.UUID] = []
    for sequence_no in (1, 2):
        payment = ProgressPayment(
            project_id=project.id,
            sequence_no=sequence_no,
            status=ProgressPaymentStatus.pending_approval,
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
                site_id=site.id,
                code=item.code,
                description=item.description,
                unit=item.unit,
                contract_unit_price=item.unit_price,
                coefficient=Decimal("1.000"),
                quantity=Decimal("600"),
                group_name=group.name,
            )
        ]
        seeded_db.add(payment)
        await seeded_db.flush()
        payment_ids.append(payment.id)
    return payment_ids[0], payment_ids[1]


@pytest.fixture
async def ters_sirali_onayli_gecmis(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, EmployerContractItem, Site]:
    """H6 denetimi K1 — kota kümesinin SIRASIZ olduğunu ölçen kurulum.

    Kota 1.000; `sequence_no=2` hakediş ONAYLI (600 birim), `sequence_no=1`
    hakediş TASLAK. Sıra tabanlı bir kota okuması taslağın (seq 1) onaylı
    kaydı (seq 2) "önceki" saymamasına yol açar ve 1.000'e kadar yazmaya izin
    verirdi; sırasız tam küme 600 + yazılan > 1.000 olduğunda 422 verir.

    Durum geçiş uçlarıyla kurulamaz (D8 tek açık hakediş kuralı ile seq 1'in
    taslak, seq 2'nin onaylı olduğu bu diziliş meşru uçlardan ancak
    `unapprove`+`reject` zinciriyle üretilebilir — o zinciri
    `test_transitions.py` uçtan uca ayrıca koşar); burada doğrudan DB kurulumu
    testin ÖLÇTÜĞÜ şey değil, ÖN KOŞULUDUR.
    """
    project = await project_factory(code="PP-Q04", name="Ters Sıralı Onay Projesi")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-PP-Q04",
        amount=Decimal("5000000"),
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    site = Site(project_id=project.id, code="SNT-PP-Q04", name="Ters Sıra Şantiyesi")
    group = EmployerContractGroup(project_id=project.id, name="Ters Sıra Grubu", sort_order=1)
    seeded_db.add_all([site, group])
    await seeded_db.flush()
    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="10.001",
        description="Ters sıralı poz",
        unit="m³",
        quantity=Decimal("1000"),
        unit_price=Decimal("100"),
        sort_order=1,
    )
    seeded_db.add(item)
    await seeded_db.flush()
    await _dagit(seeded_db, site, item, Decimal("1000"))

    taslak = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.draft,
        period_year=2026,
        period_month=1,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    onayli = ProgressPayment(
        project_id=project.id,
        sequence_no=2,
        status=ProgressPaymentStatus.approved,
        period_year=2026,
        period_month=2,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=hakedis_olusturan.id,
    )
    onayli.lines = [
        ProgressPaymentLine(
            contract_item_id=item.id,
            site_id=site.id,
            code=item.code,
            description=item.description,
            unit=item.unit,
            contract_unit_price=item.unit_price,
            coefficient=Decimal("1.000"),
            quantity=Decimal("600"),
            group_name=group.name,
        )
    ]
    seeded_db.add_all([taslak, onayli])
    await seeded_db.flush()
    return taslak.id, item, site


@pytest.fixture
async def onayli_gecmisli_ortam(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, EmployerContractItem, Site, EmployerContractItem]:
    """Önceki hakediş `approved` ve 600 birim: kalan kota 400 (spec §6.5/2)."""
    return await _gecmisli_ortam(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-Q01",
        onceki_durum=ProgressPaymentStatus.approved,
        onceki_miktar=Decimal("600"),
    )


@pytest.fixture
async def taslak_gecmisli_ortam(
    seeded_db: AsyncSession, project_factory, hakedis_olusturan: User
) -> tuple[uuid.UUID, EmployerContractItem, Site, EmployerContractItem]:
    """Önceki hakediş `draft` ve 600 birim: kümülatif kümeye GİRMEZ (§6.6
    `prev = approved|paid`), kalan kota yine 1.000."""
    return await _gecmisli_ortam(
        seeded_db,
        project_factory,
        hakedis_olusturan,
        code="PP-Q02",
        onceki_durum=ProgressPaymentStatus.draft,
        onceki_miktar=Decimal("600"),
    )
