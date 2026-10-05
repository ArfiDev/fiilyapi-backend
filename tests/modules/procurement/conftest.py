"""SA T2 — tedarikçi + talep uçlarının login/yetki/kapsam fixture'ları.

`tests/modules/inventory/conftest.py` deseninin kardeşi: kök `tests/conftest.py`de
hazır başlık fixture'ı YOKTUR, her test paketi kendi `_login`/`_auth` yardımcısını
kurar.

İzin matrisi (`roles/seed_data.py`, **`procurement`** — 10. modül, grup
STOK_SATINALMA; seed'de ZATEN VARDI, matris DEĞİŞMEDİ):
system_admin=**_A** · patron=_F · site_chief=**_REQ** · field_engineer=**_REQ** ·
hr_manager=**_N** · accounting=**_N** · project_manager=**_APR** · procurement=_F.

Seviye sırası `none < view < draft < request < approve < full < admin`
(`app/core/access.py`). T2'nin kapıları buradan çıkar:

* okuma (`view`)            → şef/saha/PM/satınalma/patron/sysadmin geçer,
                              İK ve muhasebe GEÇEMEZ;
* TALEP yazımı (`request`)  → şef ve saha da geçer (talebi sahadan açan onlardır);
* TEDARİKÇİ yazımı (`full`) → yalnız satınalma/patron/sysadmin — katalog şefin
                              işi DEĞİLDİR, şef tedarikçi ekleyemez (403).

Fixture seçimi bu dört seviyeyi temsil eder:
* `admin_headers`     — `system_admin` (`_A`); `projects=_A` olduğu için
  `visible_projects` süzgecini ATLAR (tüm projeleri görür).
* `satinalma_headers` — `procurement` (`_F`); `projects=_N` olduğu için kapsamı
  `user_project_access`ten gelir — **IDOR testlerinin taşıyıcısı budur.**
* `sef_headers`       — `site_chief` (`_REQ`): talep açar, TEDARİKÇİ AÇAMAZ.
* `yetkisiz_headers`  — `accounting` (`_N`): okumada bile 403.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.inventory.models import StockCategory, StockItem, Warehouse
from app.modules.procurement.models import PaymentTerms, Supplier
from app.modules.projects.models import Project
from app.modules.sites.models import Section, Site
from app.modules.users.models import ProjectMember, User


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def gorunen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="SA-P01", name="Güneşkent A-Blok")


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """`satinalma_headers` kullanıcısına ASLA erişim verilmeyen proje."""
    return await project_factory(code="SA-P02", name="Liman Altyapı")


@pytest.fixture
async def gorunen_santiye(seeded_db: AsyncSession, gorunen_proje: Project) -> Site:
    site = Site(project_id=gorunen_proje.id, code="SA-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunen_bolum(seeded_db: AsyncSession, gorunen_santiye: Site) -> Section:
    section = Section(site_id=gorunen_santiye.id, code="SA-A-1", name="Kat 6–10 Kaba İnşaat")
    seeded_db.add(section)
    await seeded_db.flush()
    return section


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, gorunmeyen_proje: Project) -> Site:
    site = Site(project_id=gorunmeyen_proje.id, code="SA-B", name="Liman Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `procurement=_A`, `projects=_A` (kapsam süzgecini atlar)."""
    token = await _login(client, user_factory, "system_admin", "admin@satinalma.co")
    return _auth(token)


@pytest.fixture
async def satinalma_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
    gorunmeyen_proje: Project,
) -> dict[str, str]:
    """`procurement` — `procurement=_F`; kapsamı YALNIZ `gorunen_proje`dir.

    `projects=_N` olduğu için `visible_projects` `user_project_access`ten okur;
    `gorunmeyen_proje` bilinçli olarak verilmez.
    """
    email = "satinalma@satinalma.co"
    user = await user_factory(email=email, password="parola1234", role_key="procurement")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=gorunen_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def sef_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
) -> dict[str, str]:
    """`site_chief` — `procurement=_REQ`: TALEP açar, TEDARİKÇİ açamaz (403)."""
    email = "sef@satinalma.co"
    user = await user_factory(email=email, password="parola1234", role_key="site_chief")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=gorunen_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def pm_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
) -> dict[str, str]:
    """`project_manager` — `procurement=_APR`: ONAYLAR ama **eşik üstünü onaylayamaz**.

    ₺500K eşiğinin taşıyıcı fixture'ı budur (T3): normal onay kapısı `approve`,
    eşik üstü onay kapısı `full`tur (`transitions.APPROVAL_THRESHOLD_LEVEL`) ve
    PM `full` DEĞİLDİR. `satinalma_headers` (`_F`) ise ikisini de geçer.
    """
    email = "pm@satinalma.co"
    user = await user_factory(email=email, password="parola1234", role_key="project_manager")
    seeded_db.add(ProjectMember(user_id=user.id, project_id=gorunen_proje.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`accounting` — `procurement=_N`: her uçta 403 (okuma dahil)."""
    token = await _login(client, user_factory, "accounting", "muhasebe@satinalma.co")
    return _auth(token)


@pytest.fixture
def tedarikci_fabrikasi(seeded_db: AsyncSession):
    async def _create(
        name: str,
        *,
        category: str | None = "Demir-Çelik",
        tax_no: str | None = "1234567890",
        phone: str | None = "0212 555 00 01",
        payment_terms: PaymentTerms = PaymentTerms.days_30,
        is_active: bool = True,
    ) -> Supplier:
        supplier = Supplier(
            name=name,
            category=category,
            tax_no=tax_no,
            phone=phone,
            payment_terms=payment_terms,
            is_active=is_active,
        )
        seeded_db.add(supplier)
        await seeded_db.flush()
        return supplier

    return _create


@pytest.fixture
def kart_fabrikasi(seeded_db: AsyncSession):
    """ST malzeme kartı — talep kaleminin "stok kartından seç" bacağı (FST 104)."""

    async def _create(
        code: str,
        name: str = "Nervürlü Demir Ø12",
        *,
        category: StockCategory = StockCategory.steel,
        unit: str = "Ton",
    ) -> StockItem:
        item = StockItem(code=code, name=name, category=category, unit=unit, min_stock=None)
        seeded_db.add(item)
        await seeded_db.flush()
        return item

    return _create


@pytest.fixture
def depo_fabrikasi(seeded_db: AsyncSession):
    """ "Mevcut Stok" türevinin kaynağı — bakiye ST depolarından okunur."""

    async def _create(name: str, *, site: Site | None = None) -> Warehouse:
        warehouse = Warehouse(name=name, site_id=None if site is None else site.id)
        seeded_db.add(warehouse)
        await seeded_db.flush()
        return warehouse

    return _create


@pytest.fixture
def stok_girisi_fabrikasi(seeded_db: AsyncSession):
    """Bakiye üretir: `purchase` hareketi + tek satır (ST `balance.legs` kaynağı)."""

    from app.modules.inventory.models import StockEntry, StockEntryLine, StockEntryType

    async def _create(warehouse: Warehouse, item: StockItem, quantity: str) -> StockEntry:
        entry = StockEntry(
            entry_type=StockEntryType.purchase,
            entry_date=date(2026, 7, 20),
            warehouse_id=warehouse.id,
        )
        seeded_db.add(entry)
        await seeded_db.flush()
        seeded_db.add(
            StockEntryLine(entry_id=entry.id, item_id=item.id, quantity=Decimal(quantity))
        )
        await seeded_db.flush()
        return entry

    return _create


async def _resolve_or_create_user(seeded_db, user_factory, email: str, role_key: str):
    """Fabrika kullanıcısını ÇÖZER, yoksa KURAR.

    Fabrikaların `sef_headers`/`satinalma_headers` fixture'larına bağlanması
    istenmiyor: durum makinesi testlerinin çoğu o başlıkları kullanmaz ve
    bağımlılık, ilgisiz bir login'i her teste taşırdı.
    """
    mevcut = (await seeded_db.execute(select(User).where(User.email == email))).scalar_one_or_none()
    if mevcut is not None:
        return mevcut
    return await user_factory(email=email, password="parola1234", role_key=role_key)


@pytest.fixture
def talep_fabrikasi(seeded_db: AsyncSession, user_factory):
    """Durum makinesi testlerinin taşıyıcısı: talebi İSTENEN DURUMDA doğrudan kurar.

    Uçlardan geçilerek kurulamaz — `rejected`/`ordered` gibi durumlara ulaşmak
    testin konusu olan geçişleri kullanmayı gerektirirdi ve matris testi kendi
    kendini doğrulardı. Numara da elle verilir (`SAT-TEST-…`): sunucu üreticisi
    burada devre dışıdır, çünkü test numara biçimini değil DURUMU sınar.
    """
    from app.modules.procurement.models import (
        PurchasePriority,
        PurchaseRequest,
        PurchaseRequestLine,
        PurchaseRequestStatus,
    )

    sayac = {"n": 0}

    async def _create(
        project: Project,
        *,
        status: PurchaseRequestStatus = PurchaseRequestStatus.draft,
        lines: list[tuple[str, str | None]] | None = None,
        needed_by: date | None = date(2026, 9, 1),
        created_by_email: str = "sef@satinalma.co",
        site: Site | None = None,
    ) -> PurchaseRequest:
        sayac["n"] += 1
        creator = await _resolve_or_create_user(
            seeded_db, user_factory, created_by_email, "site_chief"
        )
        request = PurchaseRequest(
            request_no=f"SAT-TEST-{sayac['n']:04d}",
            request_date=date(2026, 8, 12),
            priority=PurchasePriority.normal,
            project_id=project.id,
            site_id=None if site is None else site.id,
            needed_by=needed_by,
            status=status,
            created_by_user_id=creator.id,
        )
        seeded_db.add(request)
        await seeded_db.flush()
        for sira, (quantity, price) in enumerate(lines or []):
            seeded_db.add(
                PurchaseRequestLine(
                    request_id=request.id,
                    free_text_name=f"Kalem {sira + 1}",
                    free_text_unit="Adet",
                    quantity=Decimal(quantity),
                    estimated_unit_price=None if price is None else Decimal(price),
                    sort_order=sira,
                )
            )
        await seeded_db.flush()
        return request

    return _create


@pytest.fixture
def teklif_fabrikasi(seeded_db: AsyncSession):
    """Teklif kaydı — karşılaştırma/seçim testleri için."""
    from app.modules.procurement.models import PurchaseQuote, PurchaseRequest

    async def _create(
        request: PurchaseRequest,
        supplier: Supplier,
        *,
        unit_price: str = "100.00",
        delivery_time: str = "3 iş günü",
        payment_terms: PaymentTerms = PaymentTerms.days_30,
        shipping_included: bool = True,
        shipping_cost: str | None = None,
        is_selected: bool = False,
    ) -> PurchaseQuote:
        quote = PurchaseQuote(
            request_id=request.id,
            supplier_id=supplier.id,
            unit_price=Decimal(unit_price),
            delivery_time=delivery_time,
            payment_terms=payment_terms,
            shipping_included=shipping_included,
            shipping_cost=None if shipping_cost is None else Decimal(shipping_cost),
            is_selected=is_selected,
        )
        seeded_db.add(quote)
        await seeded_db.flush()
        return quote

    return _create


@pytest.fixture
def siparis_fabrikasi(seeded_db: AsyncSession, user_factory):
    """Sipariş kaydı — liste/süzgeç/durum testleri için."""
    from app.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus

    sayac = {"n": 0}

    async def _create(
        project: Project,
        supplier: Supplier,
        *,
        total_amount: str = "120000.00",
        status: PurchaseOrderStatus = PurchaseOrderStatus.approved,
        created_by_email: str = "satinalma@satinalma.co",
        note: str | None = None,
    ) -> PurchaseOrder:
        sayac["n"] += 1
        creator = await _resolve_or_create_user(
            seeded_db, user_factory, created_by_email, "procurement"
        )
        order = PurchaseOrder(
            order_no=f"SP-TEST-{sayac['n']:04d}",
            supplier_id=supplier.id,
            project_id=project.id,
            total_amount=Decimal(total_amount),
            status=status,
            note=note,
            created_by_user_id=creator.id,
        )
        seeded_db.add(order)
        await seeded_db.flush()
        return order

    return _create


@pytest.fixture
async def kullanici_kimligi(seeded_db: AsyncSession):
    """Denetim testleri için: e-postadan kullanıcı kimliği çözer."""

    async def _resolve(email: str) -> uuid.UUID:
        return (await seeded_db.execute(select(User).where(User.email == email))).scalar_one().id

    return _resolve
