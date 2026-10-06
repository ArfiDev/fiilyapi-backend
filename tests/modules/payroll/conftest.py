"""İK-3 T2/T3 — `compute` akışının ve bordro uçlarının fixture'ları.

`tests/timesheet/conftest.py` kardeşi: kök `tests/conftest.py`in
`db_session`/`seeded_db`/`user_factory`/`project_factory` fixture'ları üzerine
kurulur, kardeş test paketlerinden hiçbir şey miras alınmaz.

Oran SEED'i migration'dadır ama test şeması `Base.metadata.create_all` ile
kurulur (migration KOŞMAZ) — bu yüzden oranlar burada AÇIKÇA yaratılır ve
beklentiler seed'in sessizce değişmesine bağlı kalmaz.

## T3 yetki fixture'ları

İzin matrisi (`roles/seed_data.py:182`, **`payroll`** — seed'de ZATEN VAR, matris
DEĞİŞMEDİ): system_admin=**_A** · patron=_F · site_chief=**_N** ·
field_engineer=**_N** · hr_manager=**_F** · accounting=**_F** ·
project_manager=**_N** · procurement=**_N**.

`payroll` satırında `view` seviyeli HİÇBİR hazır rol yoktur; okuma/yazma ayrımı
(spec S9) yine de uçlarda durur — özel roller (`roles` modülü) o seviyeyi
kurabilir. Bu yüzden fixture'lar üç seviyeyi temsil eder:

* `admin_headers`    — `system_admin` (`_A`);
* `ik_headers`       — `hr_manager` (`_F`), bordronun gerçek kullanıcısı;
* `yetkisiz_headers` — `site_chief` (`_N`): OKUMADA BİLE 403.
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.sayfalar import HiddenCategory
from app.modules.accounting.chart_seed_data import CHART_ACCOUNTS
from app.modules.accounting.models import ChartAccount, JournalSourceType
from app.modules.payroll.models import (
    IncomeKind,
    PayrollMinimumWage,
    PayrollOvertimeRate,
    PayrollPeriod,
    PayrollRate,
    PayrollTaxBracket,
)
from app.modules.payroll.posting import PAYROLL_POSTING_RULES
from app.modules.payroll.tax_bracket_seed_data import (
    MINIMUM_WAGE_GROSS_2026,
    TAX_BRACKETS_2026_WAGE,
)
from app.modules.personnel.models import PaymentMethod, Personnel, WageType
from app.modules.posting.models import PostingRule
from app.modules.projects.models import Project
from app.modules.site_diary.models import WorkerSource
from app.modules.sites.models import Site
from app.modules.timesheet.models import TimesheetCode, TimesheetEntry
from app.modules.users.models import User
from tests._hassas_alan import rol_gizli

# BY başlığındaki dönem yerine T1 SEED yılı kullanılır: oran seti 2026'ya bağlıdır.
YIL = 2026
AY = 7


# SGK 70-73 / 79-81 (S1) — `test_payroll_compute.py` ile AYNI sayılar.
#
# 🔴 **IK3-GV K3 — `income_tax_pct = None` DİLİMLİ MOTOR demektir.** `company` ve
# `subcontractor` 2026 tohumunda (migration `b3c4d5e6f7a8`) `NULL`a çekildi:
# gelir vergisi artık `payroll_tax_brackets`ten, kümülatif matrah üzerinden
# hesaplanır. Buraya `10` yazmak, kaldırılmış olan düz oran rejimini fixture
# düzeyinde geri getirir ve dilimli motoru TÜM servis testlerinde devre dışı
# bırakırdı (sahte yeşil).
SGK_4A = {
    "sgk_employee_pct": Decimal("14.000"),
    "unemployment_employee_pct": Decimal("1.000"),
    "income_tax_pct": None,
    "stamp_tax_pct": Decimal("0.759"),
    "sgk_employer_pct": Decimal("20.500"),
    "unemployment_employer_pct": Decimal("2.000"),
    "short_work_pct": Decimal("1.000"),
}
ZERO = dict.fromkeys(SGK_4A, Decimal("0.000"))
#: BY 243 "Serbest Makbuz · %20 Stopaj" — DÜZ oran rejiminde KALIR (GVK m.94).
SERBEST = {**ZERO, "income_tax_pct": Decimal("20.000")}

#: `general` KASTEN YOKTUR (spec §4): bordro tipi değildir, oran satırı da yoktur.
SEED_2026 = {
    WorkerSource.company: SGK_4A,
    WorkerSource.subcontractor: SGK_4A,
    WorkerSource.freelance: SERBEST,
    WorkerSource.intern: ZERO,
}


@pytest.fixture
async def dilimler(db_session: AsyncSession) -> list[PayrollTaxBracket]:
    """IK3-GV — 2026 ÜCRET tarifesi + brüt asgari ücret.

    🔴 `oranlar` fixture'ının AYRILMAZ eşidir: `income_tax_pct = None` (dilimli
    rejim) iken tarife ya da asgari ücret satırı YOKSA `compute` satırı
    `uncomputed`a düşürür (K3 fail-closed) — yani bu fixture olmadan hiçbir
    şirket satırı hesaplanmaz. Değerler `tax_bracket_seed_data`dan gelir, elle
    KOPYALANMAZ: fixture ile tohum ayrışırsa servis testleri üretimde olmayan
    bir tarifeyi doğrularmış olurdu.
    """
    rows = [
        PayrollTaxBracket(
            year=YIL,
            income_kind=IncomeKind.wage,
            ordinal=ordinal,
            upper_bound=upper_bound,
            rate_pct=rate_pct,
        )
        for ordinal, upper_bound, rate_pct in TAX_BRACKETS_2026_WAGE
    ]
    db_session.add_all(rows)
    db_session.add(PayrollMinimumWage(year=YIL, gross_amount=MINIMUM_WAGE_GROSS_2026))
    await db_session.flush()
    return rows


@pytest.fixture
async def fm_carpani(db_session: AsyncSession) -> PayrollOvertimeRate:
    """PUAN-SAAT-3 — yılın fazla mesai çarpanı (mockup E5 358 "× 1,5").

    🔴 `dilimler` ile aynı sınıfta bir TOHUMDUR ve `oranlar`ın ayrılmaz eşidir:
    satır yoksa FM saati OLAN her satır `uncomputed`a düşer (K1 + NULL-EŞİK).
    Fail-closed'u sınayan test bu satırı AÇIKÇA siler — fixture'ı kullanmamak
    yeterli değildir, çünkü `oranlar` onu zaten çeker.
    """
    row = PayrollOvertimeRate(year=YIL, multiplier=Decimal("1.500"))
    db_session.add(row)
    await db_session.flush()
    return row


@pytest.fixture
async def oranlar(db_session: AsyncSession, dilimler, fm_carpani) -> list[PayrollRate]:
    rows = [
        PayrollRate(year=YIL, personnel_source=source, **pct) for source, pct in SEED_2026.items()
    ]
    db_session.add_all(rows)
    await db_session.flush()
    return rows


@pytest.fixture
async def donem(db_session: AsyncSession) -> PayrollPeriod:
    period = PayrollPeriod(year=YIL, month=AY)
    db_session.add(period)
    await db_session.flush()
    return period


@pytest.fixture
async def proje(project_factory) -> Project:
    return await project_factory(code="BR-P01", name="Bordro Projesi")


@pytest.fixture
async def santiye(db_session: AsyncSession, proje: Project) -> Site:
    site = Site(project_id=proje.id, code="BR-A", name="Bordro Şantiyesi")
    db_session.add(site)
    await db_session.flush()
    return site


@pytest.fixture
async def kaydeden(user_factory) -> User:
    return await user_factory(email="bordro@ik3.co", password="parola1234", role_key="system_admin")


@pytest.fixture
def personel_fabrikasi(db_session: AsyncSession):
    async def _create(
        full_name: str,
        *,
        source: WorkerSource = WorkerSource.company,
        wage_type: WageType | None = WageType.daily,
        wage_amount: Decimal | None = Decimal("1800.00"),
        payment_method: PaymentMethod | None = PaymentMethod.bank,
        is_active: bool = True,
        is_draft: bool = False,
    ) -> Personnel:
        person = Personnel(
            full_name=full_name,
            source=source,
            wage_type=wage_type,
            wage_amount=wage_amount,
            payment_method=payment_method,
            is_active=is_active,
            is_draft=is_draft,
        )
        db_session.add(person)
        await db_session.flush()
        return person

    return _create


@pytest.fixture
def puantaj_fabrikasi(db_session: AsyncSession, santiye: Site, kaydeden: User):
    """Belirtilen günlere hücre yazar (PUAN-SAAT: **saat XOR kod**).

    Varsayılan 9 saatlik çalışma günüdür (E5 71 "Normal gün 9 saat") — eski
    `worked` kodunun birebir karşılığı. `code` verilirse hücre KODLUDUR ve saat
    taşımaz; `hours` ile `code` birlikte verilemez.
    """

    async def _create(
        person: Personnel,
        days: list[int],
        *,
        code: TimesheetCode | None = None,
        hours: Decimal | int | None = None,
        month: int = AY,
        year: int = YIL,
    ) -> None:
        if code is None and hours is None:
            hours = Decimal("9.0")
        assert (hours is None) != (code is None), "hücre ya saat ya kod taşır"
        for day in days:
            db_session.add(
                TimesheetEntry(
                    personnel_id=person.id,
                    site_id=santiye.id,
                    project_id=santiye.project_id,
                    work_date=date(year, month, day),
                    hours=None if hours is None else Decimal(hours),
                    code=code,
                    created_by=kaydeden.id,
                )
            )
        await db_session.flush()

    return _create


def satir_of(satirlar, personnel_id: uuid.UUID):
    for satir in satirlar:
        if satir.personnel_id == personnel_id:
            return satir
    raise AssertionError(f"bordro satırı yok: {personnel_id}")


# --- T3: yetki başlıkları --------------------------------------------------


async def _login(client: AsyncClient, user_factory, role_key: str, email: str) -> str:
    await user_factory(email=email, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def admin_headers(client: AsyncClient, user_factory) -> dict[str, str]:
    return _auth(await _login(client, user_factory, "system_admin", "bordro.admin@ik3.co"))


@pytest.fixture
async def ik_headers(client: AsyncClient, user_factory) -> dict[str, str]:
    """`hr_manager` (`payroll=_F`) — bordronun gerçek kullanıcısı."""
    return _auth(await _login(client, user_factory, "hr_manager", "bordro.ik@ik3.co"))


@pytest.fixture
async def gizli_headers(
    client: AsyncClient, seeded_db: AsyncSession, user_factory
) -> dict[str, str]:
    """`maas_kisisel` gizli özel rol (bordro sayfası düzenler) — bordro tutarları `null` döner ve
    PATCH'te dolu tutar 403 alır (IZN-B4c maske testleri). `hr_manager` artık GÖRÜR (madde 20)."""
    await rol_gizli(seeded_db, "bordro_gizli", {HiddenCategory.maas_kisisel})
    return _auth(await _login(client, user_factory, "bordro_gizli", "bordro.gizli@ik3.co"))


@pytest.fixture
async def yetkisiz_headers(client: AsyncClient, user_factory) -> dict[str, str]:
    """`site_chief` (`payroll=_N`) — OKUMADA BİLE 403."""
    return _auth(await _login(client, user_factory, "site_chief", "bordro.sef@ik3.co"))


# --- T3: dört tipli senaryo ------------------------------------------------
#
# BY'nin dört bölümünü (124/172/240/268) + S4'ün "hesaplanamadı" durumunu tek
# fixture'da toplar. Sayılar ORANLARDAN türer, BY/BG tutarlarından DEĞİL (S1):
#
#   şirket   · 5 gün × 1.800 = brüt  9.000,00 · DİLİMLİ          · net 7.650,00
#   taşeron  · 5 gün × 1.800 = brüt  9.000,00 · aynı hesap        · net 7.650,00 (EXCLUDED)
#   serbest  · aylık          brüt 12.500,00 · %20 DÜZ stopaj    · net 10.000,00
#   stajyer  · aylık          brüt  7.500,00 · kesinti YOK       · net  7.500,00
#   ücretsiz · ücret tanımsız → brüt/net `null`, satır UNCOMPUTED (S4)
#
# 🔴 IK3-GV: şirket/taşeron satırının GELİR VERGİSİ ve DAMGASI **0,00**dır ve bu
# DOĞRUDUR — 9.000 TL brüt, 2026 brüt asgari ücretinin (33.030,00) ALTINDADIR,
# yani KK-7 istisnası hesaplanan verginin TAMAMINI karşılar (taban 0, negatif
# vergi üretilmez). Kesinti yalnız SGK işçi (%14 → 1.260,00) + işsizlik işçi
# (%1 → 90,00) = 1.350,00'dir. IK3-GV öncesinde bu satırlar düz %10 gelir
# vergisi + %0,759 damga ile 2.318,31 kesinti üretiyordu — o sayı MEVZUATA
# DAYANMIYORDU (mockup etiketi SGK 72).


@pytest.fixture
async def dort_tip(donem, oranlar, personel_fabrikasi, puantaj_fabrikasi):
    """Beş personel + puantajları. Dönem HENÜZ hesaplanmamıştır."""
    kisiler = {
        "sirket": await personel_fabrikasi("Ayşe Demir"),
        "taseron": await personel_fabrikasi("Mehmet Yılmaz", source=WorkerSource.subcontractor),
        "serbest": await personel_fabrikasi(
            "Kemal Tunç",
            source=WorkerSource.freelance,
            wage_type=WageType.monthly,
            wage_amount=Decimal("12500.00"),
        ),
        "stajyer": await personel_fabrikasi(
            "Burak Aydın",
            source=WorkerSource.intern,
            wage_type=WageType.monthly,
            wage_amount=Decimal("7500.00"),
        ),
        "ucretsiz": await personel_fabrikasi(
            "Zeynep Ak", wage_type=None, wage_amount=None, payment_method=None
        ),
    }
    for kisi in kisiler.values():
        await puantaj_fabrikasi(kisi, [1, 2, 3, 4, 5])
    return kisiler


@pytest.fixture(autouse=True)
async def _mu3e_esleme(db_session: AsyncSession) -> None:
    """🔴 MU-3E — bordro ailesinin `posting_rules` ÜRÜN eşlemesi (AUTOUSE).

    `approve_period` artık `approved` adımında FİŞ KESER. Test şeması
    `Base.metadata.create_all` ile kurulur ve **migration KOŞMAZ**, yani tohum
    hiç uygulanmaz: eşleme olmadan bu paketteki HER onay ucu **422** alırdı.

    🔴 `autouse` seçildi çünkü kusur bir "posting testi" sorunu değil, bu
    paketin TAMAMINI ilgilendiren bir kurulum önkoşuludur (`tests/
    progress_payments/conftest.py`nin `_mu3d_esleme` emsali). Fixture'ı tek tek
    testlere eklemek, unutulan her testi sessizce kırmızıya çevirirdi.

    ⚠️ **`test_payroll_approval_concurrency.py` bunu GÖLGELER** (aynı adla boş
    bir fixture tanımlar): o dosya `db_session`i BİLEREK kullanmaz, iki
    bağımsız bağlantı açıp GERÇEKTEN commit eder ve `seed_reference_data`nın
    commit EDİLMEMİŞ satırlarıyla çakışıp tam küme koşusunu DEADLOCK'ta asılı
    bırakırdı (MU-3D'de ölçüldü). Kendi eşlemesini kendisi kurar.

    Eşleme ÜRÜN demetinden kurulur; elle yazılsaydı `PAYROLL_POSTING_RULES`
    bozulduğunda bu kurulum yeşil kalırdı. Hesap kartının `account_type`/
    `is_contra` değerleri `chart_seed_data`dan okunur, elle YAZILMAZ.
    """
    tohum = {satir.code: satir for satir in CHART_ACCOUNTS}
    for role_key, kod in PAYROLL_POSTING_RULES:
        account = (
            await db_session.execute(select(ChartAccount).where(ChartAccount.code == kod))
        ).scalar_one_or_none()
        if account is None:
            kart = tohum[kod]
            account = ChartAccount(
                code=kart.code,
                name=kart.name,
                account_type=kart.account_type,
                is_contra=kart.is_contra,
            )
            db_session.add(account)
            await db_session.flush()
        db_session.add(
            PostingRule(
                source_type=JournalSourceType.payroll_period,
                role_key=role_key,
                account_id=account.id,
            )
        )
    await db_session.flush()
