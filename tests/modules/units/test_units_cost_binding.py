"""P10 T3 — ünite yanıtlarının `unit_cost` / `expected_profit` zarfları doldu.

Sayılar **UE** = `projedesign/Form - Unite Ekle.dc.html` 91 ("Maliyet ₺980.000")
ve 97-99 ("Beklenen Kâr ₺500.000 = liste fiyatı − maliyet")tan gelir. Maliyet
ELLE GİRİLMEZ (P3 kararı 3 hâlâ geçerli: kolon AÇILMADI) — S3 onaylı iş
kuralıyla TÜRETİLİR: toplam bütçe maliyeti × ünite brüt m² / proje brüt m².

Senaryo: bütçe 9.800.000 · proje brüt m² 1.780 · ünite 178 m² →
9.800.000 × 178/1780 = 980.000 ve 1.480.000 − 980.000 = 500.000 (UE birebir).
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.projects.models import Project
from app.modules.sites.models import Site
from app.modules.units.models import Block, Unit, UnitKind


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, user_factory) -> str:
    await user_factory(email="admin@ucost.co", password="parola1234", role_key="system_admin")
    resp = await client.post(
        "/auth/login", json={"email": "admin@ucost.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


async def _block(session: AsyncSession, project: Project) -> Block:
    site = Site(project_id=project.id, code=f"SNT-{project.code}", name="Şantiye")
    session.add(site)
    await session.flush()
    block = Block(project_id=project.id, site_id=site.id, name="A Blok")
    session.add(block)
    await session.flush()
    return block


async def _unit(
    session: AsyncSession,
    project: Project,
    block: Block,
    unit_no: str,
    *,
    gross_area_m2: str | None,
    list_price: str | None = None,
) -> Unit:
    unit = Unit(
        project_id=project.id,
        block_id=block.id,
        unit_no=unit_no,
        unit_kind=UnitKind.apartment,
        gross_area_m2=None if gross_area_m2 is None else Decimal(gross_area_m2),
        list_price=None if list_price is None else Decimal(list_price),
    )
    session.add(unit)
    await session.flush()
    return unit


async def _ue_senaryosu(
    session: AsyncSession, project_factory, code: str
) -> tuple[Project, Unit, Block]:
    """UE senaryosu: bütçe 9,8M · 1.780 m² toplam · 178 m² ünite."""
    project = await project_factory(code=code, project_type="kendi_yatirim")
    project.budget_material = Decimal("9800000.00")
    block = await _block(session, project)
    unit = await _unit(
        session, project, block, "12", gross_area_m2="178.00", list_price="1480000.00"
    )
    await _unit(session, project, block, "13", gross_area_m2="1602.00", list_price="1000000.00")
    await session.flush()
    return project, unit, block


async def test_unite_listesinde_maliyet_ve_beklenen_kar_gercek_doner(
    client, db_session, user_factory, project_factory
):
    """UE 91/97-99 birebir: 980.000 maliyet · 500.000 beklenen kâr."""
    project, unit, _ = await _ue_senaryosu(db_session, project_factory, "UC-1")
    token = await _login(client, user_factory)

    body = (await client.get(f"/projects/{project.id}/units", headers=_auth(token))).json()

    satir = next(
        row for grup in body["blocks"] for row in grup["units"] if row["id"] == str(unit.id)
    )
    assert satir["unit_cost"]["available"] is True
    assert satir["unit_cost"]["pending_module"] is None
    assert Decimal(satir["unit_cost"]["value"]) == Decimal("980000.00")
    assert Decimal(satir["expected_profit"]["value"]) == Decimal("500000.00")


async def test_m2si_olmayan_unitede_zarf_BOS_KALIR(
    client, db_session, user_factory, project_factory
):
    """S3: m² bilgisi olmayan ünitede maliyet UYDURULMAZ — zarf yer tutucu kalır."""
    project, _, block = await _ue_senaryosu(db_session, project_factory, "UC-2")
    m2siz = await _unit(db_session, project, block, "14", gross_area_m2=None, list_price="900000")
    token = await _login(client, user_factory)

    body = (await client.get(f"/projects/{project.id}/units", headers=_auth(token))).json()

    satir = next(
        row for grup in body["blocks"] for row in grup["units"] if row["id"] == str(m2siz.id)
    )
    for alan in ("unit_cost", "expected_profit"):
        assert satir[alan]["available"] is False, alan
        assert satir[alan]["value"] is None, alan
        assert satir[alan]["pending_module"] == "project_costs", alan


async def test_liste_fiyati_olmayan_unitede_maliyet_dolar_kar_BOS_KALIR(
    client, db_session, user_factory, project_factory
):
    """UE 98 kâr LİSTE FİYATINDAN türer: fiyat yoksa kâr da bilinmez."""
    project = await project_factory(code="UC-3", project_type="kendi_yatirim")
    project.budget_material = Decimal("1000000.00")
    block = await _block(db_session, project)
    unit = await _unit(db_session, project, block, "1", gross_area_m2="100.00")
    await db_session.flush()
    token = await _login(client, user_factory)

    body = (await client.get(f"/projects/{project.id}/units", headers=_auth(token))).json()

    satir = next(
        row for grup in body["blocks"] for row in grup["units"] if row["id"] == str(unit.id)
    )
    assert Decimal(satir["unit_cost"]["value"]) == Decimal("1000000.00")
    assert satir["expected_profit"]["available"] is False
    assert satir["expected_profit"]["pending_module"] == "project_costs"


async def test_tekil_unite_yaniti_LISTEYLE_AYNI_maliyeti_verir(
    client, db_session, user_factory, project_factory
):
    """PATCH sonrası ekranın gördüğü satır listedekiyle AYRIŞAMAZ."""
    project, unit, _ = await _ue_senaryosu(db_session, project_factory, "UC-4")
    token = await _login(client, user_factory)

    resp = await client.patch(f"/units/{unit.id}", json={"layout": "4+1"}, headers=_auth(token))

    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert Decimal(govde["unit_cost"]["value"]) == Decimal("980000.00")
    assert Decimal(govde["expected_profit"]["value"]) == Decimal("500000.00")


async def test_butcesiz_projede_unite_maliyeti_BOS_KALIR(
    client, db_session, user_factory, project_factory
):
    """Bütçe girilmemişse maliyet 0 BASILMAZ (kâr o zaman liste fiyatı kadar görünürdü)."""
    project = await project_factory(code="UC-5", project_type="kendi_yatirim")
    block = await _block(db_session, project)
    unit = await _unit(
        db_session, project, block, "1", gross_area_m2="100.00", list_price="1000000"
    )
    await db_session.flush()
    token = await _login(client, user_factory)

    body = (await client.get(f"/projects/{project.id}/units", headers=_auth(token))).json()

    satir = next(
        row for grup in body["blocks"] for row in grup["units"] if row["id"] == str(unit.id)
    )
    assert satir["unit_cost"]["available"] is False
    assert satir["expected_profit"]["available"] is False


async def test_unite_listesinde_sorgu_sayisi_unite_sayisindan_bagimsizdir(
    db_session, user_factory, project_factory, seeded_db
):
    """Spec §4: ünite başına maliyet sorgusu AÇILMAZ (dağıtım bellekte)."""
    from collections.abc import Iterator
    from contextlib import contextmanager

    from sqlalchemy import event

    from app.modules.units import service
    from tests.conftest import test_engine

    @contextmanager
    def sayac() -> Iterator[list[str]]:
        ifadeler: list[str] = []

        def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
            ifadeler.append(" ".join(statement.split()))

        event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
        try:
            yield ifadeler
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    user = await user_factory(email="olcum@ucost.co", password="parola1234", role_key="patron")
    user.all_projects = True
    kucuk = await project_factory(code="UC-N1", project_type="kendi_yatirim")
    kucuk.budget_material = Decimal("1000000.00")
    kucuk_blok = await _block(seeded_db, kucuk)
    await _unit(seeded_db, kucuk, kucuk_blok, "1", gross_area_m2="100", list_price="1000")
    buyuk = await project_factory(code="UC-N2", project_type="kendi_yatirim")
    buyuk.budget_material = Decimal("1000000.00")
    buyuk_blok = await _block(seeded_db, buyuk)
    for index in range(8):
        await _unit(
            seeded_db, buyuk, buyuk_blok, str(index), gross_area_m2="100", list_price="1000"
        )
    await seeded_db.flush()

    with sayac() as ifadeler:
        await service.list_units(seeded_db, user, kucuk.id)
        kucuk_sayim = len(ifadeler)
    with sayac() as ifadeler:
        yanit = await service.list_units(seeded_db, user, buyuk.id)
        buyuk_sayim = len(ifadeler)

    assert len(yanit.blocks[0].units) == 8
    assert kucuk_sayim == buyuk_sayim, (kucuk_sayim, buyuk_sayim)


# --- P-YT4 DENETİMİ (2026-08-23): TABAN bekçisi ---------------------------------
#
# P10 kanonu: **iki maliyet tabanı AYRIDIR — kart "Harcanan", kâr "Bütçe".**
# `unit_cost`/`expected_profit` BÜTÇE tabanlıdır (`costs.unit_cost` docstring'i:
# "Bütçe bazlıdır, gerçekleşen bazlı DEĞİL"). Yukarıdaki testlerin HİÇBİRİ bunu
# ölçmüyordu: hepsinde projenin harcananı 0'dı, yani tabanı `total_spent`e
# çeviren bir mutasyon HEPSİNİ YEŞİL bırakırdı. Aşağıdaki bekçi tam olarak o
# mutasyonu yakalar — bütçe ile harcanan BİLEREK farklı kurulur.


async def _harcanan_yarat(session: AsyncSession, project: Project, *, brut: Decimal) -> None:
    """Projeye `approved` bir TAŞERON hakedişi kurar — `costs.total_spent`in girdisi.

    Brüt = miktar × birim fiyat; avans/teminat 0 tutulur ki sayı okunur kalsın.
    """
    from app.modules.contracts.models import (
        ContractStatus,
        Subcontractor,
        SubcontractorContract,
        SubcontractorContractItem,
    )
    from app.modules.subcontractor_progress_payments.models import (
        SubcontractorPaymentStatus,
        SubcontractorProgressPayment,
        SubcontractorProgressPaymentLine,
    )
    from app.modules.users.models import User

    sahip = (await session.execute(select(User).limit(1))).scalars().first()
    assert sahip is not None
    sub = Subcontractor(name=f"Taşeron {project.code}")
    session.add(sub)
    await session.flush()
    contract = SubcontractorContract(
        project_id=project.id,
        subcontractor_id=sub.id,
        subcontractor_name=sub.name,
        contract_no=f"TSD-{project.code}",
        status=ContractStatus.active,
        created_by=sahip.id,
    )
    session.add(contract)
    await session.flush()
    item = SubcontractorContractItem(
        contract_id=contract.id,
        code="01.001",
        description="Kaba inşaat",
        unit="m3",
        quantity=Decimal("1000"),
        unit_price=Decimal("1.00"),
        sort_order=0,
    )
    session.add(item)
    await session.flush()
    payment = SubcontractorProgressPayment(
        contract_id=contract.id,
        project_id=project.id,
        sequence_no=1,
        period_year=2026,
        period_month=7,
        status=SubcontractorPaymentStatus.approved,
        vat_pct=Decimal("20.00"),
        advance_pct=Decimal("0.00"),
        retainage_pct=Decimal("0.00"),
        created_by=sahip.id,
    )
    payment.lines = [
        SubcontractorProgressPaymentLine(
            contract_item_id=item.id,
            code=item.code,
            description=item.description,
            unit=item.unit,
            contract_unit_price=item.unit_price,
            coefficient=Decimal("1.000"),
            quantity=brut,
            sort_order=0,
        )
    ]
    session.add(payment)
    await session.flush()


async def test_TABAN_unite_maliyeti_BUTCEDEN_turer_HARCANANDAN_degil(
    client, db_session, user_factory, project_factory
):
    """🔴 TABAN MUTASYON BEKÇİSİ — bütçe 9,8M · harcanan 3,4M, ünite 178/1780 m².

    Bütçe tabanı ⇒ 9.800.000 × 178/1780 = **980.000,00** (UE 91 birebir).
    Harcanan tabanına kayarsa ⇒ 3.400.000 × 178/1780 = 340.000,00 çıkardı ve
    UE 98 "Beklenen Kâr" 500.000 yerine 1.140.000 görünürdü: ünite, bitmemiş bir
    inşaatın düşük gerçekleşen maliyeti yüzünden olduğundan kârlı basılırdı
    (`costs.unit_cost` docstring'inin ölçülmüş gerekçesi).
    """
    project, unit, _ = await _ue_senaryosu(db_session, project_factory, "UC-TABAN")
    # `_login` ÖNCE koşar: hakedişin `created_by`si NOT NULL'dır ve bu dosyada
    # kullanıcıyı yaratan tek yer `user_factory`dir.
    token = await _login(client, user_factory)
    await _harcanan_yarat(db_session, project, brut=Decimal("3400000"))

    body = (await client.get(f"/projects/{project.id}/units", headers=_auth(token))).json()

    satir = next(
        row for grup in body["blocks"] for row in grup["units"] if row["id"] == str(unit.id)
    )
    assert Decimal(satir["unit_cost"]["value"]) == Decimal("980000.00"), (
        "Ünite maliyetinin tabanı kaymış: BÜTÇE olmalı, harcanan DEĞİL (P10 kanonu)."
    )
    assert Decimal(satir["expected_profit"]["value"]) == Decimal("500000.00")

    # Aynı projede HARCANAN gerçekten farklı bir sayı: iki taban yan yana yaşıyor.
    maliyet = (await client.get(f"/projects/{project.id}/costs", headers=_auth(token))).json()
    assert Decimal(maliyet["breakdown"]["construction_spent"]) == Decimal("3400000.00")
    assert Decimal(maliyet["breakdown"]["construction_budget"]) == Decimal("9800000.00")


def test_TABAN_zarflar_hala_MetricPlaceholder_tipindedir() -> None:
    """P-YT4: alan "yer tutucu" DEĞİL ama zarf TİPİ korunur — ikisi ayrı şeydir.

    Zarf, değerin GİRDİYE bağlı olarak bilinmeyebileceğini (m²'siz ünite,
    bütçesiz proje) ifade eder. Düz `Decimal | None`a çevirmek hem kırıcı olurdu
    hem de "neden boş" bilgisini (`pending_module`) silerdi.
    """
    from app.modules.projects.schemas import MetricPlaceholder
    from app.modules.units.schemas import UnitResponse

    for alan in ("unit_cost", "expected_profit"):
        assert UnitResponse.model_fields[alan].annotation is MetricPlaceholder, alan
