"""Proje maliyet ucu — İLERLEME SÜTUNU · YETKİ/IDOR · N+1 ÖLÇÜMÜ · MUTASYON.

`test_projects_costs_api.py`nin ikinci parçası (800 satır tavanı bölmesi);
paylaşılan yardımcılar `_projects_costs.py`dedir.

İlerleme = ÖDENEN / SÖZLEŞME yüzdesidir; bekleyen hakediş paya GİRMEZ ve
GERÇEK SIFIR, TANIMSIZDAN ayrıdır. Görünmeyen proje var olmayandan ayırt
edilemez. Sorgu sayısı taşeron ve hakediş sayısından bağımsızdır — iddia
tahmine değil `_sorgu_sayaci` ölçümüne dayanır.
"""

import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import event

from app.core.access import AccessLevel
from app.modules.projects.schemas import SubcontractorCostRow, SubcontractorCostSummary
from app.modules.sales.models import UnitSaleStatus
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
)
from app.modules.users.models import ProjectMember
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests.conftest import test_engine

from ._projects_costs import (
    _auth,
    _contract,
    _customer,
    _login,
    _payment,
    _sale,
    _scoped_login,
    _set_budget_lines,
    _units,
)


async def test_ilerleme_ODENEN_bolu_SOZLESME_yuzdesidir(
    client, db_session, user_factory, project_factory
):
    """ "İlerleme" sütunu = `Ödenen / Sözleşme × 100`, iki ondalık.

    Beklenen değerler ELDE hesaplandı; üretim ifadesi testte YENİDEN
    KOŞTURULMADI (aksi hâlde test formülü değil kendini doğrulardı):

    * 5.700.000 / 8.400.000 × 100 = 67,857142… → **67.86** (KY 214'ün `%68`
      bar genişliğinin iki haneli hâli; yuvarlama ISIRIR).
    * 1.200.000 / 2.400.000 × 100 = 50 TAM → **50.00** (KY 222 `%50`).
    * 24.690 / 200.000 × 100 = 12,345 → TAM YARIM, yuvarlama MODUNU ayırt eder:
      `quantize2` ROUND_HALF_UP'tır ve **12.35** verir; ROUND_HALF_EVEN olsaydı
      12.34 gelirdi (4 çift olduğu için aşağı yuvarlardı).
    """
    kurucu = await user_factory(email="ilerleme@p10.co", password="parola1234", role_key="patron")
    project = await project_factory(code="IL-1", project_type="taahhut")
    akin = await _contract(
        db_session,
        project,
        kurucu,
        name="Akın İnşaat",
        work_category="Betonarme",
        item_quantity="8400",
        item_price="1000",
    )
    yilmaz = await _contract(
        db_session,
        project,
        kurucu,
        name="Yılmaz Elektrik",
        work_category="Elektrik",
        item_quantity="2400",
        item_price="1000",
    )
    yarim = await _contract(
        db_session,
        project,
        kurucu,
        name="Zeta Yuvarlama",
        work_category="Mekanik",
        item_quantity="200",
        item_price="1000",
    )
    await _payment(db_session, akin, kurucu, SubcontractorPaymentStatus.paid, quantity="5700")
    await _payment(db_session, yilmaz, kurucu, SubcontractorPaymentStatus.paid, quantity="1200")
    await _payment(db_session, yarim, kurucu, SubcontractorPaymentStatus.paid, quantity="24.69")
    token = await _login(client, user_factory, "system_admin")

    body = (await client.get(f"/projects/{project.id}/costs", headers=_auth(token))).json()

    rows = {row["subcontractor_name"]: row for row in body["subcontractors"]}
    # Payda/pay gerçekten kurulmuş mu — oran doğru çıksın diye önce girdiler ölçülür.
    assert Decimal(rows["Akın İnşaat"]["contract_amount"]) == Decimal("8400000.00")
    assert Decimal(rows["Akın İnşaat"]["paid"]) == Decimal("5700000.00")
    assert Decimal(rows["Akın İnşaat"]["progress_pct"]) == Decimal("67.86")
    assert Decimal(rows["Yılmaz Elektrik"]["progress_pct"]) == Decimal("50.00")
    assert Decimal(rows["Zeta Yuvarlama"]["contract_amount"]) == Decimal("200000.00")
    assert Decimal(rows["Zeta Yuvarlama"]["paid"]) == Decimal("24690.00")
    assert Decimal(rows["Zeta Yuvarlama"]["progress_pct"]) == Decimal("12.35")


async def test_ilerleme_GERCEK_SIFIRI_TANIMSIZDAN_ayirir(
    client, db_session, user_factory, project_factory
):
    """İki "sıfır görünümlü" durum AYNI DEĞERE ÇÖKMEZ (NULL-EŞİK kanonu):

    * bedeli olan ama hiç ödeme görmemiş sözleşme → `0.00` = GERÇEK %0. Mockup
      bunu harfiyen basar (KY 236-243 "Demirci Alüminyum ₺1,8M / ₺0" → `%0`).
    * bedeli `0.00` olan sözleşme → payda TANIMSIZ → `None`. Uydurma bir %0
      basmak "veri yok"u "ilerleme yok" gibi gösterirdi; kullanıcı ekranda
      taşeronun hiç çalışmadığını sanardı. Bu hâl üretimde ERİŞİLEBİLİRDİR ve
      MEŞRUDUR: kalemsiz sözleşme (`items` `default_factory=list`) ile bütün
      kalemlerinin `unit_price`ı NULL olan sözleşme aynı `0.00` bedeli üretir.
    """
    kurucu = await user_factory(email="sifir@p10.co", password="parola1234", role_key="patron")
    project = await project_factory(code="IL-2", project_type="taahhut")
    await _contract(
        db_session,
        project,
        kurucu,
        name="Demirci Alüminyum",
        work_category="Doğrama",
        item_quantity="1800",
        item_price="1000",
    )
    await _contract(db_session, project, kurucu, name="Kalemsiz Ltd", with_item=False)
    await _contract(db_session, project, kurucu, name="Fiyatsiz Ltd", item_price=None)
    token = await _login(client, user_factory, "system_admin")

    body = (await client.get(f"/projects/{project.id}/costs", headers=_auth(token))).json()

    rows = {row["subcontractor_name"]: row for row in body["subcontractors"]}
    gercek_sifir = rows["Demirci Alüminyum"]
    assert Decimal(gercek_sifir["contract_amount"]) == Decimal("1800000.00")
    assert Decimal(gercek_sifir["paid"]) == Decimal("0.00")
    assert Decimal(gercek_sifir["progress_pct"]) == Decimal("0.00")
    for tanimsiz_ad in ("Kalemsiz Ltd", "Fiyatsiz Ltd"):
        tanimsiz = rows[tanimsiz_ad]
        assert Decimal(tanimsiz["contract_amount"]) == Decimal("0.00"), tanimsiz_ad
        assert tanimsiz["progress_pct"] is None, tanimsiz_ad
    # AYRIŞMA: ikisi tek değere çökerse test anlamsızlaşır.
    assert gercek_sifir["progress_pct"] != rows["Kalemsiz Ltd"]["progress_pct"]


async def test_ilerleme_payi_ODENENDIR_BEKLEYEN_paya_GIRMEZ(
    client, db_session, user_factory, project_factory
):
    """AYRIŞMA NOKTASI: `Ödenen / Sözleşme` ile `(Ödenen + Bekleyen) / Sözleşme`
    burada FARKLI cevap verir; bekleyeni 0 olan bir kurulumda test hiçbir şey
    kanıtlamazdı.

    Ölçüm (KY tablosu, iki bağımsız mockup 6/6 satırda aynı formülde buluşuyor):
    5,7/8,4 = %68 basılır, (5,7+0,84)/8,4 = %77,9 basılmaz. Burada da
    5.000.000 / 10.000.000 = **%50.00** beklenir, 8.000.000 / 10.000.000 = %80.00
    DEĞİL.
    """
    kurucu = await user_factory(email="ayrisma@p10.co", password="parola1234", role_key="patron")
    project = await project_factory(code="IL-3", project_type="taahhut")
    akin = await _contract(
        db_session,
        project,
        kurucu,
        name="Akın İnşaat",
        work_category="Betonarme",
        item_quantity="10000",
        item_price="1000",
    )
    await _payment(db_session, akin, kurucu, SubcontractorPaymentStatus.paid, quantity="5000")
    await _payment(
        db_session,
        akin,
        kurucu,
        SubcontractorPaymentStatus.approved,
        quantity="3000",
        sequence_no=2,
    )
    token = await _login(client, user_factory, "system_admin")

    body = (await client.get(f"/projects/{project.id}/costs", headers=_auth(token))).json()

    (row,) = body["subcontractors"]
    assert Decimal(row["contract_amount"]) == Decimal("10000000.00")
    assert Decimal(row["paid"]) == Decimal("5000000.00")
    # Bekleyen GERÇEKTEN doludur: iki formülü ayıran şey budur.
    assert Decimal(row["pending"]) == Decimal("3000000.00")
    assert Decimal(row["progress_pct"]) == Decimal("50.00")
    assert Decimal(row["progress_pct"]) != Decimal("80.00")


def test_tfoot_ILERLEME_TASIMAZ_satir_TASIR():
    """KY 244-248 tfoot'unun "İlerleme" hücresi HARFİYEN BOŞTUR (`<td></td>`),
    KK tablosunun ise tfoot'u hiç yoktur.

    Toplam bir ilerleme yüzdesi eklemek mockup'ın İSTEMEDİĞİ bir sayıyı icat
    etmek olurdu (üstelik "hangi ortalama" sorusunun cevabı da yoktur: satır
    ortalaması ile Σödenen/Σbedel farklı sayılardır). Sütun SATIR düzeyinde
    yaşar, tfoot'ta yaşamaz.
    """
    assert "progress_pct" in SubcontractorCostRow.model_fields
    assert "progress_pct" not in SubcontractorCostSummary.model_fields
    assert set(SubcontractorCostSummary.model_fields) == {"contract_amount", "paid", "pending"}


# --- Yetki ve IDOR ---


async def test_costs_izinsiz_role_403_doner(client, user_factory, project_factory):
    """seed: `procurement` satırında projects = none."""
    project = await project_factory(code="YT-1")
    token = await _login(client, user_factory, "procurement")

    resp = await client.get(f"/projects/{project.id}/costs", headers=_auth(token))

    assert resp.status_code == 403


async def test_costs_kimliksiz_401_doner(client, project_factory):
    project = await project_factory(code="YT-2")
    assert (await client.get(f"/projects/{project.id}/costs")).status_code == 401


async def test_costs_gorunmeyen_proje_var_olmayandan_ayirt_edilemez(
    client, db_session, user_factory, project_factory
):
    """IDOR: kapsam dışı proje ile hiç var olmayan proje AYNI 404 gövdesini verir."""
    izinli = await project_factory(code="ID-1")
    gizli = await project_factory(code="ID-2")
    token = await _scoped_login(client, db_session, user_factory, izinli)

    gizli_resp = await client.get(f"/projects/{gizli.id}/costs", headers=_auth(token))
    yok_resp = await client.get(f"/projects/{uuid.uuid4()}/costs", headers=_auth(token))

    assert gizli_resp.status_code == 404
    assert yok_resp.status_code == 404
    assert gizli_resp.json() == yok_resp.json()
    assert (
        await client.get(f"/projects/{izinli.id}/costs", headers=_auth(token))
    ).status_code == 200


async def test_costs_view_seviyesi_yeterlidir(client, db_session, user_factory, project_factory):
    """Uç OKUMADIR: `projects:view` yeter, `full` şart değildir."""
    project = await project_factory(code="VW-1")
    await modul_duzeyi_yaz(db_session, "site_chief", "projects", AccessLevel.view)
    user = await user_factory(email="sef@p10.co", password="parola1234", role_key="site_chief")
    db_session.add(ProjectMember(user_id=user.id, project_id=project.id, role_id=user.role_id))
    await db_session.flush()
    login = await client.post("/auth/login", json={"email": "sef@p10.co", "password": "parola1234"})
    token = login.json()["access_token"]

    resp = await client.get(f"/projects/{project.id}/costs", headers=_auth(token))

    assert resp.status_code == 200


# --- N+1 ölçümü ---


@pytest.fixture
def _sorgu_sayaci() -> Iterator[list[str]]:
    """T1 desenin aynısı: N+1 iddiası tahmine değil ÖLÇÜME dayanır."""
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


def _tablo_sayimi(ifadeler: list[str], tablo: str) -> int:
    return sum(1 for ifade in ifadeler if f"from {tablo}" in ifade.lower())


_OLCULEN_TABLOLAR = (
    "subcontractor_contracts",
    "subcontractor_contract_items",
    "subcontractor_progress_payments",
    "subcontractor_progress_payment_lines",
    "units",
    # Kullanıcı kararı 2026-08-09: "Gerçekleşen Satış" satış kayıtlarından gelir —
    # satış SAYISI sorgu sayısını BÜYÜTMEMELİDİR.
    "unit_sales",
)


async def test_sorgu_sayisi_taseron_ve_hakedis_sayisindan_bagimsizdir(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """Spec §4: tek gidiş-dönüş hedefi — satır sayısı sorgu sayısını BÜYÜTMEZ."""
    from app.modules.projects import cost_summary

    kurucu = await user_factory(email="olcum@p10.co", password="parola1234", role_key="patron")
    kucuk = await project_factory(code="NP-1", project_type="kendi_yatirim")
    buyuk = await project_factory(code="NP-2", project_type="kendi_yatirim")
    musteri = await _customer(db_session, name="Ölçüm Müşterisi")
    tek = await _contract(
        db_session, kucuk, kurucu, name="Tek Taşeron", item_quantity="10", item_price="1000"
    )
    await _payment(db_session, tek, kurucu, SubcontractorPaymentStatus.paid, quantity="5")
    kucuk_uniteler = await _units(db_session, kucuk, [{"list_price": Decimal("1000.00")}])
    await _sale(
        db_session, kucuk_uniteler[0], musteri, kurucu, UnitSaleStatus.active, price="1000.00"
    )
    for index in range(4):
        contract = await _contract(
            db_session,
            buyuk,
            kurucu,
            name=f"Taşeron {index}",
            item_quantity="10",
            item_price="1000",
        )
        for sira in (1, 2):
            await _payment(
                db_session,
                contract,
                kurucu,
                SubcontractorPaymentStatus.paid,
                quantity="5",
                sequence_no=sira,
            )
    buyuk_uniteler = await _units(
        db_session, buyuk, [{"list_price": Decimal("1000.00")} for _ in range(6)]
    )
    # Küçük projede 1, büyükte 5 satış: satış sayısı sorgu sayısını BÜYÜTMEMELİ.
    for unite in buyuk_uniteler[:5]:
        await _sale(db_session, unite, musteri, kurucu, UnitSaleStatus.active, price="1000.00")
    # `investment` `lazy="selectin"`tir: gerçek yolda sorgu ile YÜKLENMİŞ gelir
    # (uç `visible_projects`ten okur). Testte elle açılan nesnede yükleme
    # tetiklenmediği için tazelenir — ölçüm öncesi, sayaç dışında.
    for proje in (kucuk, buyuk):
        await db_session.refresh(proje, attribute_names=["investment"])

    _sorgu_sayaci.clear()
    await cost_summary.build_project_costs(db_session, kucuk)
    kucuk_sayim = {tablo: _tablo_sayimi(_sorgu_sayaci, tablo) for tablo in _OLCULEN_TABLOLAR}

    _sorgu_sayaci.clear()
    buyuk_yanit = await cost_summary.build_project_costs(db_session, buyuk)
    buyuk_sayim = {tablo: _tablo_sayimi(_sorgu_sayaci, tablo) for tablo in _OLCULEN_TABLOLAR}

    assert len(buyuk_yanit.subcontractors) == 4
    assert kucuk_sayim == buyuk_sayim, (kucuk_sayim, buyuk_sayim)
    assert all(sayi == 1 for sayi in buyuk_sayim.values()), buyuk_sayim


async def test_taahhutte_unite_ve_satis_tablolarina_HIC_dokunulmaz(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """Tip süzgeci (`_UNIT_REVENUE_TYPES`): taahhütte gelir sözleşme bedelidir —
    ne ünite ne satış tablosu OKUNUR."""
    from app.modules.projects import cost_summary

    project = await project_factory(
        code="NP-3", project_type="taahhut", contract_amount="1000000.00"
    )
    await db_session.flush()

    _sorgu_sayaci.clear()
    await cost_summary.build_project_costs(db_session, project)

    assert _tablo_sayimi(_sorgu_sayaci, "units") == 0
    assert _tablo_sayimi(_sorgu_sayaci, "unit_sales") == 0


# --- Mutasyon denetimi ---


async def test_yanit_uretimi_projeyi_ve_uniteleri_degistirmez(
    db_session, user_factory, project_factory
):
    """Okuma ucu MUTASYON YAPMAZ: iki çağrı aynı sonucu verir, ORM alanları sabit kalır."""
    from app.modules.projects import cost_summary

    kurucu = await user_factory(email="mutasyon@p10.co", password="parola1234", role_key="patron")
    project = await project_factory(code="MT-1", project_type="kendi_yatirim")
    _set_budget_lines(project, material="1000000")
    await db_session.flush()
    uniteler = await _units(
        db_session, project, [{"list_price": Decimal("2000000.00"), "gross_area_m2": Decimal("50")}]
    )
    contract = await _contract(db_session, project, kurucu, name="Akın İnşaat")
    await _payment(db_session, contract, kurucu, SubcontractorPaymentStatus.paid, quantity="10")
    await db_session.refresh(project, attribute_names=["investment"])

    birinci = await cost_summary.build_project_costs(db_session, project)
    ikinci = await cost_summary.build_project_costs(db_session, project)

    assert birinci == ikinci
    assert project.budget_material == Decimal("1000000")
    assert uniteler[0].list_price == Decimal("2000000.00")
    # Yanıt şeması dondurulmuş değil ama ÜRETİM saf olmalı: aynı girdi aynı çıktı.
    assert birinci.breakdown.construction_spent == Decimal("10000.00")
