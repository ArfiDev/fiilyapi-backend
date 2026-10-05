"""Şantiye günlüğü (T2) fixture'ları — bağımsız kurulum.

Kök `tests/conftest.py`'deki `db_session`/`seeded_db`/`user_factory`/`project_factory`
üzerine kurulur. Kardeş test paketleri (`tests/progress_payments`, `tests/subcontractor_
progress_payments`) pytest tarafından otomatik YÜKLENMEZ; erişim deseni burada
yeniden kurulur.

`tests/progress_payments/test_concurrency.py`'nin bilinen seed sızıntısı borcuna
BULAŞILMAZ: bu paketin hiçbir fixture'ı oradan miras almaz, her fixture kendi
verisini kurar ve kök `db_session` savepoint'i içinde geri alınır.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import NamedTuple

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.contracts.models import (
    EmployerContractGroup,
    EmployerContractItem,
    Subcontractor,
    SubcontractorContract,
    SubcontractorContractItem,
)
from app.modules.projects.models import Project, ProjectContract
from app.modules.site_diary.models import DiaryStatus, SiteDiaryEntry, SiteDiaryLine
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User
from tests.site_diary._port import port  # noqa: F401 — PLN-B2.1 port ikizi

VARSAYILAN_TARIH = date(2026, 7, 15)


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _scoped_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    role_key: str,
    email: str,
    project: Project,
) -> dict[str, str]:
    """Rolü verilen ama kapsamı TEK projeye kısıtlanmış kullanıcı."""
    await user_factory(email=email, password="parola1234", role_key=role_key)
    user = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one()
    seeded_db.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


# --- Erişim/kapsam fixture'ları (izin matrisi: şef/saha=_F, PM=_V, İK=_N) ---


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    token = await _login(client, user_factory, "system_admin", "admin@sd-t2.co")
    return _auth(token)


@pytest.fixture
async def admin_kullanicisi(seeded_db: AsyncSession, admin_headers: dict[str, str]) -> User:
    return (
        await seeded_db.execute(select(User).where(User.email == "admin@sd-t2.co"))
    ).scalar_one()


@pytest.fixture
async def hr_headers(client: AsyncClient, seeded_db: AsyncSession, user_factory) -> dict[str, str]:
    """`hr_manager` — matriste `site_diary=_N`: okuma dahil 403 (kapı en dışta)."""
    token = await _login(client, user_factory, "hr_manager", "ik@sd-t2.co")
    return _auth(token)


@pytest.fixture
async def proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="SD-P01", name="Günlük Kayıt Projesi")


@pytest.fixture
async def sef_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`site_chief` (`site_diary=_F`) — yalnız `proje`ye atanmış."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "site_chief", "sef@sd-t2.co", proje
    )


@pytest.fixture
async def sef_kullanicisi(seeded_db: AsyncSession, sef_headers: dict[str, str]) -> User:
    return (await seeded_db.execute(select(User).where(User.email == "sef@sd-t2.co"))).scalar_one()


@pytest.fixture
async def saha_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`field_engineer` (`site_diary=_F`) — şefle AYNI projede, farklı kullanıcı.

    `can_delete` reddinin kanıtı: seviyesi yeter ama kaydı O AÇMAMIŞTIR.
    """
    return await _scoped_headers(
        client, seeded_db, user_factory, "field_engineer", "saha@sd-t2.co", proje
    )


@pytest.fixture
async def pm_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`project_manager` (`site_diary=_V`) — SALT OKUR; yazma uçlarında 403."""
    return await _scoped_headers(
        client, seeded_db, user_factory, "project_manager", "pm@sd-t2.co", proje
    )


@pytest.fixture
async def patron_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`patron` — matriste `site_diary=_F` (şef/saha ile AYNI seviye).

    T4 `reopen` kapısı `admin` seviyesidir; bu fixture "tam yetkili ama admin
    DEĞİL" hâlinin ikinci kanıtıdır (matris DEĞİŞMEZ — spec §1).
    """
    return await _scoped_headers(
        client, seeded_db, user_factory, "patron", "patron@sd-t2.co", proje
    )


@pytest.fixture
async def muhasebe_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`accounting` — matriste `progress_payments=_APR` ama `site_diary=_N`.

    T5 izin kararının KANITI: hakediş yolunun altındaki öneri ucu YALNIZ hakediş
    izniyle korunsaydı, günlük verisi matriste günlüğü açıkça REDDEDİLEN role
    sızardı. İki kapı da istenir → bu rol 403 alır.
    """
    return await _scoped_headers(
        client, seeded_db, user_factory, "accounting", "muhasebe@sd-t5.co", proje
    )


@pytest.fixture
async def kapsamli_admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory, proje: Project
) -> dict[str, str]:
    """`system_admin` (`site_diary=_A`) ama kapsamı TEK projeye kısıtlı.

    `admin_headers` TÜM projeleri görür; `admin` kapılı `reopen` ucunun IDOR
    yüzeyi ancak kapsamı kısıtlı bir admin ile kanıtlanabilir.
    """
    return await _scoped_headers(
        client, seeded_db, user_factory, "system_admin", "kapsamli@sd-t4.co", proje
    )


# --- Veri kurulumu ---


@pytest.fixture
def santiye_fabrikasi(seeded_db: AsyncSession, project_factory):
    """Proje + şantiye + BOQ grubu + poz kalemleri kurar.

    BOQ iskeleti testinin kaynağı budur: `POST /sites/{id}/diary` bu pozlardan
    satır üretmek ZORUNDADIR (GK'de satır ekle/sil yoktur).
    """

    async def _create(
        code: str,
        *,
        project: Project | None = None,
        item_specs: list[tuple[str, Decimal, Decimal]] | None = None,
    ) -> tuple[Site, Project, list[BoqItem]]:
        if project is None:
            project = await project_factory(code=code, name=f"{code} Projesi")
        site = Site(project_id=project.id, code=f"{code}-SNT", name=f"{code} Şantiyesi")
        seeded_db.add(site)
        await seeded_db.flush()

        group = BoqGroup(site_id=site.id, name="A — Betonarme İşleri", sort_order=0)
        seeded_db.add(group)
        await seeded_db.flush()

        specs = (
            item_specs
            if item_specs is not None
            else [
                ("01.001", Decimal("200.000"), Decimal("21500.00")),
                ("02.001", Decimal("450.000"), Decimal("1850.00")),
            ]
        )
        items: list[BoqItem] = []
        for index, (item_code, quantity, unit_price) in enumerate(specs):
            item = BoqItem(
                site_id=site.id,
                group_id=group.id,
                code=item_code,
                description=f"{item_code} kalemi",
                unit="Ton",
                quantity=quantity,
                unit_price=unit_price,
                sort_order=index,
            )
            seeded_db.add(item)
            items.append(item)
        await seeded_db.flush()
        return site, project, items

    return _create


@pytest.fixture
async def santiye(santiye_fabrikasi, proje: Project) -> tuple[Site, Project, list[BoqItem]]:
    return await santiye_fabrikasi("SD-A", project=proje)


@pytest.fixture
async def bolum(seeded_db: AsyncSession, santiye) -> Section:
    site, _, _ = santiye
    section = Section(site_id=site.id, code="B-1", name="A Blok")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def gorunmeyen_santiye(santiye_fabrikasi) -> Site:
    """`sef_headers`/`pm_headers` kapsamı DIŞINDAKİ projenin şantiyesi (IDOR yüzeyi)."""
    site, _, _ = await santiye_fabrikasi("SD-G")
    return site


@pytest.fixture
def gunluk_fabrikasi(seeded_db: AsyncSession):
    """Doğrudan DB'ye günlük kaydı yazar — durum geçişi uçları T4'tedir."""

    async def _create(
        site: Site,
        creator: User,
        *,
        entry_date: date = VARSAYILAN_TARIH,
        status: DiaryStatus = DiaryStatus.draft,
        lines: list[tuple[str, Decimal, Decimal]] | None = None,
    ) -> SiteDiaryEntry:
        entry = SiteDiaryEntry(
            site_id=site.id,
            project_id=site.project_id,
            entry_date=entry_date,
            status=status,
            created_by=creator.id,
        )
        for code, quantity, unit_price in lines or []:
            entry.lines.append(
                SiteDiaryLine(
                    code=code,
                    description=f"{code} kalemi",
                    unit="Ton",
                    unit_price=unit_price,
                    quantity=quantity,
                )
            )
        seeded_db.add(entry)
        await seeded_db.flush()
        return entry

    return _create


@pytest.fixture
def gunluk_api(client: AsyncClient):
    """UÇLAR üzerinden bir günlük günü kurar (iskelet + miktarlar + gönderim).

    `gunluk_fabrikasi`den FARKI: satırlar `boq_item_id` ile BOQ pozuna bağlıdır —
    hakediş köprüsü (poz → sözleşme kalemi) ancak bu bağ varken kurulabilir.
    """

    async def _gun(
        headers: dict[str, str],
        site_id: uuid.UUID,
        tarih: date,
        satirlar: list[dict],
        *,
        gonder: bool = True,
    ) -> dict:
        kayit = await client.post(
            f"/sites/{site_id}/diary", json={"entry_date": tarih.isoformat()}, headers=headers
        )
        assert kayit.status_code == 201, kayit.text
        entry_id = kayit.json()["id"]
        yanit = await client.put(
            f"/diary/{entry_id}/lines", json={"lines": satirlar}, headers=headers
        )
        assert yanit.status_code == 200, yanit.text
        if not gonder:
            return yanit.json()
        gonderim = await client.post(f"/diary/{entry_id}/submit", headers=headers)
        assert gonderim.status_code == 200, gonderim.text
        return gonderim.json()

    return _gun


@pytest.fixture
def sozlesme_kalemi_fabrikasi(seeded_db: AsyncSession):
    """İşveren sözleşmesi kalemi kurar ve BOQ pozuna KÖPRÜLER.

    T4 `summary` ucunun `contract_item_*` alanlarının kaynağı budur
    (`boq_items.contract_item_id`); T5 "günlükten doldur" önerisi de aynı köprüyü
    tüketecektir.
    """

    async def _create(
        boq_item: BoqItem,
        project: Project,
        *,
        quantity: Decimal = Decimal("1200.000"),
        unit_price: Decimal = Decimal("1850.00"),
    ) -> EmployerContractItem:
        if await seeded_db.get(ProjectContract, project.id) is None:
            seeded_db.add(
                ProjectContract(
                    project_id=project.id,
                    contract_no=f"{project.code}-SZL",
                    amount=Decimal("11200000"),
                )
            )
            await seeded_db.flush()
        group = (
            (
                await seeded_db.execute(
                    select(EmployerContractGroup).where(
                        EmployerContractGroup.project_id == project.id
                    )
                )
            )
            .scalars()
            .first()
        )
        if group is None:
            group = EmployerContractGroup(project_id=project.id, name="A — Betonarme", sort_order=0)
            seeded_db.add(group)
            await seeded_db.flush()
        item = EmployerContractItem(
            project_id=project.id,
            group_id=group.id,
            code=f"SZL-{boq_item.code}",
            description=f"{boq_item.code} sözleşme kalemi",
            unit=boq_item.unit,
            quantity=quantity,
            unit_price=unit_price,
        )
        seeded_db.add(item)
        await seeded_db.flush()
        boq_item.contract_item_id = item.id
        await seeded_db.flush()
        return item

    return _create


@pytest.fixture
def taseron_sozlesmesi_fabrikasi(seeded_db: AsyncSession, admin_kullanicisi: User):
    """Taşeron sözleşmesi + kalemleri kurar; kalemler işveren kalemine KÖPRÜLENİR.

    T5 taşeron önerisinin köprüsü `source_contract_item_id`tir. `site` PARAMETRE
    olarak verilir çünkü spec §7 S5'in kuralı (yalnız `contract.site_id =
    günlük.site_id`) ancak site'sız ve BAŞKA şantiyeli sözleşmelerle kanıtlanır.
    """

    async def _create(
        project: Project,
        *,
        site: Site | None = None,
        kalemler: list[tuple[str, EmployerContractItem | None]] | None = None,
        code: str = "TS",
        quantity: Decimal = Decimal("500.000"),
    ) -> SubcontractorContract:
        taseron = Subcontractor(name=f"{code} Taşeronluk")
        seeded_db.add(taseron)
        await seeded_db.flush()
        contract = SubcontractorContract(
            project_id=project.id,
            site_id=site.id if site is not None else None,
            subcontractor_id=taseron.id,
            subcontractor_name=taseron.name,
            contract_no=f"{code}-{uuid.uuid4().hex[:8]}",
            created_by=admin_kullanicisi.id,
            # 🔴 YAYINDAKİ sözleşmenin ZORUNLU alanları (2026-09-23, kayıt #15):
            # `is_draft` varsayılanı `False`, yani bu fabrika YAYINDA bir sözleşme
            # kurar. `guards.validate_subcontract(is_draft=False)` bu dört alanı
            # şart koşuyor; fabrika onları atlayınca API'den ÜRETİLEMEYEN bir kayıt
            # doğuruyordu. Kayıt #15 PATCH'teki doğrulama boşluğunu kapatınca bu
            # gerçekdışı kurgu beş testi kırdı — kusur kapıda değil, KURGUDAYDI.
            work_category="Kaba İnşaat",
            signature_date=date(2026, 1, 15),
            start_date=date(2026, 1, 20),
            end_date=date(2026, 12, 31),
        )
        seeded_db.add(contract)
        await seeded_db.flush()
        for index, (item_code, source_item) in enumerate(kalemler or []):
            seeded_db.add(
                SubcontractorContractItem(
                    contract_id=contract.id,
                    source_contract_item_id=None if source_item is None else source_item.id,
                    code=item_code,
                    description=f"{item_code} taşeron kalemi",
                    unit="Ton",
                    quantity=quantity,
                    unit_price=Decimal("1000.00"),
                    sort_order=index,
                )
            )
        await seeded_db.flush()
        return contract

    return _create


@pytest.fixture
async def gorunmeyen_gunluk(
    gorunmeyen_santiye: Site, gunluk_fabrikasi, admin_kullanicisi: User
) -> uuid.UUID:
    entry = await gunluk_fabrikasi(gorunmeyen_santiye, admin_kullanicisi)
    return entry.id


@pytest.fixture(autouse=True)
async def _mu3d_esleme(seeded_db: AsyncSession) -> None:
    """🔴 MU-3D — İKİ hakediş ailesinin `posting_rules` eşlemesi, **AUTOUSE**.

    Bu paket hakediş ONAYLARINI koşturur (`test_subcontractor_diary_stamp.py`
    ve `test_employer_diary_stamp.py`, günlükten gelen miktarın onayla
    DONDUĞUNU ölçerler). MU-3D'den sonra onay bir YEVMİYE FİŞİ yazar ve eşleme
    yoksa **422** verir — yani bu dosyalar fişleme yüzünden kırmızı olurdu.

    Eşleme burada `seeded_db`nin rol matrisi gibi bir ALTYAPI ÖN KOŞULUDUR;
    bu paketin ölçtüğü kural DAMGANIN DONMASIDIR, fişleme değil.

    🔴 Fail-closed dalı MASKELENMEZ: eşlemesiz onayın **422** verdiği ve
    geçişin GERİ ALINDIĞI `tests/modules/posting/test_mu3d_hakedis_fisleme.py::
    test_ESLEME_YOKSA_422_ve_ONAY_da_GERI_ALINIR`da, autouse'un ULAŞMADIĞI
    yerde ölçülür.

    🔴 İKİ aile birden kurulur: bu paket hem işveren hem taşeron günlüğünü
    ölçer ve yalnız biri kurulsaydı öteki dosya sessizce kırmızı kalırdı.
    """
    from app.modules.accounting.models import JournalSourceType
    from app.modules.progress_payments.posting import PROGRESS_PAYMENT_POSTING_RULES
    from app.modules.subcontractor_progress_payments.posting import (
        SUBCONTRACTOR_POSTING_RULES,
    )
    from tests._hakedis_esleme import esleme_kur

    await esleme_kur(seeded_db, JournalSourceType.progress_payment, PROGRESS_PAYMENT_POSTING_RULES)
    await esleme_kur(
        seeded_db,
        JournalSourceType.subcontractor_progress_payment,
        SUBCONTRACTOR_POSTING_RULES,
    )


class KarisikSantiye(NamedTuple):
    """GKS-B1 — her iskelet dalını taşıyan şantiye (bkz. `karisik_santiye`)."""

    site: Site
    project: Project
    items: dict[str, BoqItem]
    s1: Section
    s2: Section


#: SABİT bölüm kimlikleri: S1'in UUID'si S2'ninkinden BÜYÜK — UUID sırası ile `sort_order`
#: sırası TERS düşer; sıra mutasyonları (UUID sırasına düşen kod) her koşuda kırmızı olur.
KARISIK_S1_ID = uuid.UUID(int=0x6B51_0000_0000_0000_0000_0000_0000_00F2)
KARISIK_S2_ID = uuid.UUID(int=0x6B51_0000_0000_0000_0000_0000_0000_0001)

#: Kural (A) beklentisi — (kalem kodu, bölüm adı | None, planlı).
KARISIK_BOLUMSUZ = [
    ("01", None, "100"),
    ("02", None, "60"),
    ("03", "S1", "70"),
    ("03", "S2", "30"),
    ("04", "S1", "50"),
    ("05", None, "60"),
]
KARISIK_S1 = [("02", "S1", "40"), ("03", "S1", "70"), ("04", "S1", "50")]
KARISIK_S2 = [("03", "S2", "30"), ("05", "S2", "20")]


@pytest.fixture
async def karisik_santiye(
    seeded_db: AsyncSession, santiye_fabrikasi, proje: Project
) -> KarisikSantiye:
    """GKS-B1 iskelet dalları: 01 tahsissiz (100) · 02 KISMEN S1 40/100 · 03 TAM S1 70 + S2 30 ·
    04 TAM yalnız S1 50/50 · 05 KISMEN S2 20/80. Bölümler: S1 (sıra 0) · S2 (sıra 1)."""
    spec = [
        ("01", Decimal("100.000"), Decimal("10.00")),
        ("02", Decimal("100.000"), Decimal("20.00")),
        ("03", Decimal("100.000"), Decimal("30.00")),
        ("04", Decimal("50.000"), Decimal("40.00")),
        ("05", Decimal("80.000"), Decimal("50.00")),
    ]
    site, project, items = await santiye_fabrikasi("GKS", project=proje, item_specs=spec)
    s1 = Section(id=KARISIK_S1_ID, site_id=site.id, code="S1", name="S1", sort_order=0)
    s2 = Section(id=KARISIK_S2_ID, site_id=site.id, code="S2", name="S2", sort_order=1)
    seeded_db.add_all([s1, s2])
    await seeded_db.flush()
    by_code = {item.code: item for item in items}
    for kod, bolum, miktar in (
        ("02", s1, "40"),
        ("03", s1, "70"),
        ("03", s2, "30"),
        ("04", s1, "50"),
        ("05", s2, "20"),
    ):
        seeded_db.add(
            BoqItemSectionAllocation(
                boq_item_id=by_code[kod].id, section_id=bolum.id, quantity=Decimal(miktar)
            )
        )
    await seeded_db.flush()
    return KarisikSantiye(site, project, by_code, s1, s2)
