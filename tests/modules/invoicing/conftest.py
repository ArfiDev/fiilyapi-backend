"""FAT-1 T3 — fatura CRUD uçlarının login/yetki/kapsam fixture'ları.

`tests/modules/procurement/conftest.py` deseninin kardeşi: kök
`tests/conftest.py`de hazır başlık fixture'ı YOKTUR, her test paketi kendi
`_login`/`_auth` yardımcısını kurar.

İzin matrisi (`roles/seed_data.py:195`, **`invoicing`** — seed'de ZATEN VARDI,
matris DEĞİŞMEDİ):
`"invoicing": [_A, _F, _N, _N, _N, _F, _V, _N]` yani
system_admin=**_A** · patron=_F · site_chief=**_N** · field_engineer=_N ·
hr_manager=_N · accounting=**_F** · project_manager=**_V** · procurement=_N.

Seviye sırası `none < view < draft < request < approve < full < admin`
(`app/core/access.py`). T3'ün kapıları buradan çıkar:

* okuma (`view`)   → PM, muhasebe, patron, sysadmin geçer; şef/saha/İK/satınalma
                     GEÇEMEZ (403);
* yazma (`full`)   → muhasebe/patron/sysadmin; **PM yazamaz** (403);
* silme (`admin`)  → YALNIZ sysadmin — `full` silmeyi KAPSAMAZ, muhasebe 403 alır.

Fixture seçimi bu dört seviyeyi temsil eder:
* `admin_headers`    — `system_admin` (`_A`); `projects=_A` olduğu için
  `visible_projects` süzgecini ATLAR (tüm projeleri görür) ve DELETE'i geçer.
* `muhasebe_headers` — `accounting` (`_F`); `projects=_FIN` olduğu için kapsamı
  `user_project_access`ten gelir — **IDOR testlerinin taşıyıcısı budur.**
* `pm_headers`       — `project_manager` (`_V`): okur, YAZAMAZ.
* `yetkisiz_headers` — `site_chief` (`_N`): okumada bile 403.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.accounting.models import ChartAccount
from app.modules.contracts.models import Subcontractor
from app.modules.customers.models import Customer, CustomerType
from app.modules.invoicing.models import (
    Invoice,
    InvoiceDirection,
    InvoiceDocumentType,
    InvoiceLine,
    InvoicePaymentMethod,
    InvoiceStatus,
)
from app.modules.procurement.models import PurchaseOrder, PurchaseOrderStatus, Supplier
from app.modules.projects.models import Employer, Project
from app.modules.sites.models import Site
from app.modules.treasury.models import (
    BankAccount,
    BankAccountType,
    Payment,
    PaymentMethodKind,
)
from app.modules.users.models import ProjectMember, User


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login_with_access(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    role_key: str,
    email: str,
    project: Project,
) -> dict[str, str]:
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    seeded_db.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await seeded_db.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return _auth(resp.json()["access_token"])


@pytest.fixture
async def gorunen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    return await project_factory(code="FAT-P01", name="Güneşkent Konut")


@pytest.fixture
async def gorunmeyen_proje(seeded_db: AsyncSession, project_factory) -> Project:
    """`muhasebe_headers`/`pm_headers` kullanıcısına ASLA erişim verilmeyen proje."""
    return await project_factory(code="FAT-P02", name="Liman Altyapı")


@pytest.fixture
async def gorunen_santiye(seeded_db: AsyncSession, gorunen_proje: Project) -> Site:
    site = Site(project_id=gorunen_proje.id, code="FAT-A", name="A-Blok Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def gorunmeyen_santiye(seeded_db: AsyncSession, gorunmeyen_proje: Project) -> Site:
    site = Site(project_id=gorunmeyen_proje.id, code="FAT-B", name="Liman Şantiyesi")
    seeded_db.add(site)
    await seeded_db.flush()
    return site


@pytest.fixture
async def admin_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`system_admin` — `invoicing=_A`, `projects=_A` (kapsam süzgecini atlar)."""
    return _auth(await _login(client, user_factory, "system_admin", "admin@fatura.co"))


@pytest.fixture
async def muhasebe_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
    gorunmeyen_proje: Project,
) -> dict[str, str]:
    """`accounting` — `invoicing=_F`; kapsamı YALNIZ `gorunen_proje`dir."""
    return await _login_with_access(
        client, seeded_db, user_factory, "accounting", "muhasebe@fatura.co", gorunen_proje
    )


@pytest.fixture
async def pm_headers(
    client: AsyncClient,
    seeded_db: AsyncSession,
    user_factory,
    gorunen_proje: Project,
) -> dict[str, str]:
    """`project_manager` — `invoicing=_V`: okur, yazamaz (403)."""
    return await _login_with_access(
        client, seeded_db, user_factory, "project_manager", "pm@fatura.co", gorunen_proje
    )


@pytest.fixture
async def yetkisiz_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`site_chief` — `invoicing=_N`: her uçta 403 (okuma dahil)."""
    return _auth(await _login(client, user_factory, "site_chief", "sef@fatura.co"))


@pytest.fixture
async def isveren(seeded_db: AsyncSession) -> Employer:
    employer = Employer(name="Güneşkent Gayrimenkul A.Ş.", tax_number="1234567890")
    seeded_db.add(employer)
    await seeded_db.flush()
    return employer


@pytest.fixture
async def alici(seeded_db: AsyncSession) -> Customer:
    customer = Customer(
        customer_type=CustomerType.company, name="Çelik Holding A.Ş.", tax_number="5566778899"
    )
    seeded_db.add(customer)
    await seeded_db.flush()
    return customer


@pytest.fixture
async def tedarikci(seeded_db: AsyncSession) -> Supplier:
    from app.modules.procurement.models import PaymentTerms

    supplier = Supplier(
        name="Liebherr Kiralama", category="Makine", payment_terms=PaymentTerms.days_30
    )
    seeded_db.add(supplier)
    await seeded_db.flush()
    return supplier


@pytest.fixture
async def taseron(seeded_db: AsyncSession) -> Subcontractor:
    subcontractor = Subcontractor(name="Demirsan Taşeronluk", tax_number="1112223334")
    seeded_db.add(subcontractor)
    await seeded_db.flush()
    return subcontractor


@pytest.fixture
async def gorunmeyen_siparis(
    seeded_db: AsyncSession, gorunmeyen_proje: Project, tedarikci: Supplier, user_factory
) -> PurchaseOrder:
    """KAYNAK referansının IDOR taşıyıcısı: görünmeyen projenin siparişi.

    Gövde içi kaynak referansı görünmüyorsa **404** olmalıdır (ST kanonu) —
    403 kaydın var olduğunu ele verirdi.
    """
    creator = (
        await seeded_db.execute(select(User).where(User.email == "admin@fatura.co"))
    ).scalar_one_or_none() or await user_factory(
        email="siparisci@fatura.co", password="parola1234", role_key="system_admin"
    )
    order = PurchaseOrder(
        order_no="SP-FAT-0001",
        supplier_id=tedarikci.id,
        project_id=gorunmeyen_proje.id,
        total_amount=Decimal("120000.00"),
        status=PurchaseOrderStatus.approved,
        created_by_user_id=creator.id,
    )
    seeded_db.add(order)
    await seeded_db.flush()
    return order


@pytest.fixture
def fatura_fabrikasi(seeded_db: AsyncSession, user_factory):
    """Faturayı İSTENEN DURUMDA doğrudan kurar (`talep_fabrikasi` deseni).

    Uçlardan geçilerek kurulamaz: `sent`/`collected`/`approved` durumlarına
    ulaşmak T4'ün geçiş uçlarını gerektirirdi ve T3'ün durum kapıları kendi
    kendini doğrulardı. Numara da elle verilir — test numara BİÇİMİNİ değil
    DURUMU/KAPSAMI sınar.
    """
    sayac = {"n": 0}

    async def _create(
        *,
        project: Project | None = None,
        site: Site | None = None,
        direction: InvoiceDirection = InvoiceDirection.outgoing,
        status: InvoiceStatus = InvoiceStatus.draft,
        party_name: str = "Güneşkent Gayrimenkul A.Ş.",
        party_tax_number: str | None = "1234567890",
        issue_date: date = date(2026, 7, 18),
        due_date: date | None = None,
        document_type: InvoiceDocumentType = InvoiceDocumentType.einvoice,
        payment_method: InvoicePaymentMethod | None = None,
        invoice_no: str | None = None,
        lines: list[tuple[str, str, str]] | None = None,
        advance_rate: str | None = None,
        retention_rate: str | None = None,
        withholding_rate: str | None = None,
    ) -> Invoice:
        sayac["n"] += 1
        creator = (
            await seeded_db.execute(select(User).where(User.email == "fabrika@fatura.co"))
        ).scalar_one_or_none() or await user_factory(
            email="fabrika@fatura.co", password="parola1234", role_key="system_admin"
        )
        satirlar = lines if lines is not None else [("Kaba İnşaat", "100.000", "1000.00")]
        toplam = sum(Decimal(m) * Decimal(f) for _, m, f in satirlar)
        invoice = Invoice(
            direction=direction,
            invoice_no=invoice_no or f"TEST{sayac['n']:09d}",
            document_type=document_type,
            status=status,
            issue_date=issue_date,
            due_date=due_date,
            payment_method=payment_method,
            party_name=party_name,
            party_tax_number=party_tax_number,
            project_id=None if project is None else project.id,
            site_id=None if site is None else site.id,
            subtotal=toplam,
            advance_rate=None if advance_rate is None else Decimal(advance_rate),
            advance_amount=Decimal("0.00"),
            retention_rate=None if retention_rate is None else Decimal(retention_rate),
            retention_amount=Decimal("0.00"),
            tax_base=toplam,
            vat_amount=toplam * Decimal("0.20"),
            withholding_rate=None if withholding_rate is None else Decimal(withholding_rate),
            withholding_amount=Decimal("0.00"),
            total=toplam * Decimal("1.20"),
            created_by_id=creator.id,
        )
        seeded_db.add(invoice)
        await seeded_db.flush()
        for sira, (aciklama, miktar, fiyat) in enumerate(satirlar):
            seeded_db.add(
                InvoiceLine(
                    invoice_id=invoice.id,
                    sort_order=sira,
                    description=aciklama,
                    unit="m³",
                    quantity=Decimal(miktar),
                    unit_price=Decimal(fiyat),
                    vat_rate=Decimal("20.00"),
                    line_total=Decimal(miktar) * Decimal(fiyat),
                )
            )
        await seeded_db.flush()
        return invoice

    return _create


@pytest.fixture
async def kullanici_kimligi(seeded_db: AsyncSession):
    """Denetim testleri için: e-postadan kullanıcı kimliği çözer."""

    async def _resolve(email: str) -> uuid.UUID:
        return (await seeded_db.execute(select(User).where(User.email == email))).scalar_one().id

    return _resolve


# --------------------------------------------------------------------------- #
# 🔴 MU-3B — MUHASEBE FABRİKALARI BURADAN YENİDEN DIŞA VURULUR
#
# Fatura fişlemesi (`test_mu3b_invoice_posting.py`) hesap planı + dönem satırı
# ister. Fabrikalar KOPYALANMAZ (`tests/modules/posting/conftest.py` ile aynı
# gerekçe): iki kopya bir gün AYRIŞIR ve biri bir parametre kazandığında öteki
# kalırdı. Buraya alınmalarının sebebi teknik: test dosyasında `import` edilen
# bir fixture, aynı adı taşıyan test parametresiyle `F811` üretir.
# --------------------------------------------------------------------------- #
from tests.modules.accounting.conftest import (  # noqa: E402, F401
    donem_fabrikasi,
    hesap_fabrikasi,
    kullanici_id,
)


@pytest.fixture
async def fatura_eslemesi(seeded_db: AsyncSession, hesap_fabrikasi) -> dict[str, ChartAccount]:  # noqa: F811
    """🔴 MU-3B — fatura ailesinin `posting_rules` ÜRÜN eşlemesi.

    Canlıda bu satırları `c0d1e2f3a4b5` migration'ı tohumlar; test kümesi
    migration koşmaz (`Base.metadata.create_all`), bu yüzden geçiş uçlarını
    ölçen HER test onu burada kurmak zorundadır. Eksik olduğunda `send`/
    `approve` **422** verir — fail-closed olan ve olması gereken taraf budur
    (`test_mu3b_invoice_posting.py::test_ESLEME_YOKSA_422_ve_GECIS_de_GERI_ALINIR`
    o dalı BİLEREK bu fixture'sız ölçer).

    🔴 Hesabın TÜRÜ elle yazılmaz, TDHP tohumundan (`chart_seed_data`) okunur:
    elle yazılsaydı `600`ü `expense` sayan bir kurulum mizanın işaretini
    (`balance.SIGN`) sessizce ters çevirir ve mutabakat testi yanlış bir
    büyüklükle tutardı.

    Eşleme `posting.INVOICE_POSTING_RULES`ten kurulur: testte elle yazılsaydı
    üründeki demet bozulduğunda bu kurulum yeşil kalırdı.
    """
    from app.modules.accounting.chart_seed_data import CHART_ACCOUNTS
    from app.modules.accounting.models import JournalSourceType
    from app.modules.invoicing.posting import INVOICE_POSTING_RULES
    from app.modules.posting.models import PostingRule

    tohum = {satir.code: satir for satir in CHART_ACCOUNTS}
    hesaplar: dict[str, ChartAccount] = {}
    for _role_key, kod in INVOICE_POSTING_RULES:
        if kod in hesaplar:
            continue
        kart = tohum[kod]
        hesaplar[kod] = await hesap_fabrikasi(
            kod, name=kart.name, account_type=kart.account_type, is_contra=kart.is_contra
        )
    for role_key, kod in INVOICE_POSTING_RULES:
        seeded_db.add(
            PostingRule(
                source_type=JournalSourceType.invoice,
                role_key=role_key,
                account_id=hesaplar[kod].id,
            )
        )
    await seeded_db.flush()
    return hesaplar


@pytest.fixture
def tam_odeme(seeded_db: AsyncSession, user_factory):
    """🔴 MU-3E İŞ 2 — faturanın toplamını KARŞILAYAN ödeme satırını kurar.

    `mark-collected` artık `Σ payments >= total` arar (kullanıcı kararı
    2026-08-26). Ödemesiz damga muhasebede `120 Alıcılar`ı açık bırakıyordu.

    🔴 Satır `payments_service.create_payment` ile DEĞİL, DOĞRUDAN yazılır ve
    bu bilinçlidir (`fatura_fabrikasi`nın "uçlardan geçilerek kurulamaz"
    gerekçesinin kardeşi): servis yolu aynı işlemde bir NAKİT FİŞİ de yazar ve
    "mark-collected ikinci fiş üretmez" gibi FİŞ SAYAN testlerin ölçtüğü sayıyı
    kurulumun kendisi oynatırdı. Buradaki tek iş, kapının aradığı OLGUYU
    (ödeme satırı var ve toplamı karşılıyor) kurmaktır.
    """

    async def _create(invoice: Invoice) -> Payment:
        account = (
            await seeded_db.execute(select(BankAccount).where(BankAccount.bank_name == "Ziraat"))
        ).scalar_one_or_none()
        if account is None:
            account = BankAccount(
                bank_name="Ziraat",
                account_type=BankAccountType.checking,
                iban=None,
                opening_balance=Decimal("0.00"),
            )
            seeded_db.add(account)
            await seeded_db.flush()
        creator = (
            await seeded_db.execute(select(User).where(User.email == "odeme@fatura.co"))
        ).scalar_one_or_none() or await user_factory(
            email="odeme@fatura.co", password="parola1234", role_key="system_admin"
        )
        payment = Payment(
            invoice_id=invoice.id,
            bank_account_id=account.id,
            method=PaymentMethodKind.transfer,
            amount=invoice.total,
            paid_on=invoice.issue_date,
            created_by_id=creator.id,
        )
        seeded_db.add(payment)
        await seeded_db.flush()
        return payment

    return _create
