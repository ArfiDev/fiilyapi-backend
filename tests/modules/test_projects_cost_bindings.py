"""Proje kartı maliyet bağları — ZARF SÖZLEŞMESİ · TİP BAZLI ALAN SETLERİ · N+1.

Spec: E4 75/82/89. `MetricPlaceholder` zarfı: `available=True` ⇒
`pending_module is None`. Kart türevleri proje başına sorgu AÇMAZ ve iddia
tahmine değil `_sorgu_sayaci` ÖLÇÜMÜNE dayanır.

⚠️ Dosya 800 satır tavanını aşınca BÖLÜNDÜ (`_journal.py` emsali): mutasyon
denetimi, taraf ünite sayaçları ve yer tutucu denetimi
`test_projects_cost_bindings_sayaclar.py`ye taşındı; paylaşılan yardımcılar
`_projects_cost_bindings.py`dedir. Hiçbir testin iddiası değişmedi.
"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.core.discipline_scope import UNRESTRICTED
from app.modules.projects.models import ProjectInvestment, ProjectLandShare
from app.modules.projects.schemas import (
    CountPlaceholder,
    MetricPlaceholder,
    metric,
    restricted,
)
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
)
from app.modules.units.models import UnitOwnerSide

from ._projects_cost_bindings import (
    _TENTH,
    _auth,
    _card,
    _contract,
    _envelopes,
    _login,
    _payment,
    _set_budget_lines,
    _sorgu_sayaci_fixture,  # noqa: F401
    _tablo_sayimi,
    _units,
)


def _uclu(zarf: MetricPlaceholder) -> tuple[bool, Decimal | None, str | None]:
    return (zarf.available, zarf.value, zarf.pending_module)


def test_dolu_metric_zarfi_pending_module_tasiyamaz() -> None:
    """`available=True` ⇒ `pending_module is None` (ROADMAP §3 borcu)."""
    with pytest.raises(ValidationError):
        MetricPlaceholder(available=True, value=Decimal("1.00"), pending_module="project_costs")


def test_zarfin_UC_HALI_ve_ARALARINDAKI_FARK() -> None:
    """⚠️ **ILR-1/2'DE DEGISTI — SILINMEDI, UCUNCU HAL EKLENDI.**

    Eski adi `test_bos_metric_zarfi_pending_module_vermeden_kurulamaz`di ve
    `MetricPlaceholder()`in `ValidationError` atmasini cakiyordu. O kural,
    "rolun izni yok" hâli DOGARKEN gevsetilmek ZORUNDA kaldi: pydantic bu hâli
    varsayilanlardan ayirt edemez (ucu de `False`/`None`/`None`).

    🔴 Yerine gecen iddia DAHA GUCLUDUR: uc hâlin UCU DE tek kumede cakilir ve
    aralarindaki FARK gorunur olur. `pending_module` IZIN anlamiyla YUKLENMEZ —
    "modul yazilmadi" ile "yetkin yok" ayri iki durumdur (kullanici karari
    2026-08-27) ve ilkini ikincisi icin kullanmak ekrani YALANCI yapardi.
    """
    olculen = {
        "dolu": _uclu(metric(Decimal("62.00"), "site_diary")),
        "baglanmadi": _uclu(metric(None, "site_diary")),
        "izin_yok": _uclu(restricted()),
    }

    assert olculen == {
        "dolu": (True, Decimal("62.00"), None),
        "baglanmadi": (False, None, "site_diary"),
        # 🔑 `pending_module is None` — sahte bir gerekce SOYLEMEZ.
        "izin_yok": (False, None, None),
    }, "zarfin uc hâli ayrismiyor — ekran 'yetkin yok'u 'modul bekleniyor' diye basar"


def test_dolu_zarf_HALA_pending_module_TASIYAMAZ() -> None:
    """Gevseyen kural YALNIZ bos taraftir; dolu taraf AYNEN cakili kalir."""
    with pytest.raises(ValidationError):
        MetricPlaceholder(available=True, value=Decimal("1.00"), pending_module="site_diary")


def test_count_zarfinin_dolu_iken_pending_module_tasimasi_KIRILMAZ() -> None:
    """PT emsali (puantaj sayaçları) BİLİNÇLİDİR — `CountPlaceholder`a dokunulmaz."""
    counter = CountPlaceholder(available=True, count=2, pending_module="timesheet")

    assert counter.count == 2
    assert counter.pending_module == "timesheet"


def test_metric_fabrikasi_degeri_olani_doldurur_olmayani_bos_birakir() -> None:
    dolu = metric(Decimal("18400000.00"), "project_costs")
    bos = metric(None, "project_costs")

    assert (dolu.available, dolu.value, dolu.pending_module) == (
        True,
        Decimal("18400000.00"),
        None,
    )
    assert (bos.available, bos.value, bos.pending_module) == (False, None, "project_costs")


# --- Tip bazlı alan setleri (E4 75/82/89) ---


async def test_kendi_yatirim_karti_maliyet_kar_marj_gercek_doner(
    client, db_session, user_factory, project_factory
):
    """E4 122 "Toplam Maliyet" = HARCANAN (kullanıcı kararı 2026-08-09): arsa 8,4M
    + inşaat harcanan 10,24M = 18,64M. KY hero ikilisi ("₺20,3M / ₺29,8M bütçe")
    iki sayının FARKLI şeyler olduğunun kanıtıdır.

    Kâr/marj DEĞİŞMEZ: 48,2M − 29,8M bütçe = 18,4M / %38,2 (KY 182/187-188).
    """
    kurucu = await user_factory(
        email="kyharcanan@p10t3.co", password="parola1234", role_key="patron"
    )
    project = await project_factory(code="T3-KY", project_type="kendi_yatirim")
    _set_budget_lines(
        project, material="8000000", labor="5000000", sub="7000000", overhead="1400000"
    )
    db_session.add(ProjectInvestment(project_id=project.id, land_cost=Decimal("8400000.00")))
    await db_session.flush()
    await _units(
        db_session,
        project,
        [
            {"list_price": Decimal("24100000.00")},
            {"list_price": Decimal("24100000.00")},
        ],
    )
    contract = await _contract(db_session, project, kurucu, name="Akın İnşaat")
    await _payment(db_session, contract, kurucu, SubcontractorPaymentStatus.paid, quantity="10240")
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    card = _card(body, project.id, "investment")
    assert card["total_cost"]["available"] is True
    assert Decimal(card["total_cost"]["value"]) == Decimal("18640000.00")
    assert card["total_cost"]["pending_module"] is None
    assert Decimal(card["estimated_profit"]["value"]) == Decimal("18400000.00")
    assert Decimal(card["margin"]["value"]).quantize(_TENTH) == Decimal("38.2")
    # P10 KAPSAMI DIŞI alanlar yer tutucu KALIR (satış/ünite dilimlerinin işi).
    assert card["sold_amount"]["available"] is False
    assert card["sales_ratio"]["available"] is False


async def test_kat_karsiligi_karti_pay_degeri_insaat_maliyeti_ve_marj_verir(
    client, db_session, user_factory, project_factory
):
    """KK 121/135/139-140: 30,4M − 17,6M = 12,8M / %42,1 · arsa 0 kuralı gömülü."""
    project = await project_factory(code="T3-KK", project_type="kat_karsiligi")
    _set_budget_lines(project, material="17600000")
    db_session.add(
        ProjectLandShare(
            project_id=project.id,
            landowner_name="Yılmaz Ailesi",
            our_share_pct=Decimal("55.00"),
            owner_share_pct=Decimal("45.00"),
        )
    )
    await db_session.flush()
    await _units(
        db_session,
        project,
        [
            {
                "appraisal_value": Decimal("30400000.00"),
                "owner_side": UnitOwnerSide.contractor,
            },
            {
                "appraisal_value": Decimal("25000000.00"),
                "owner_side": UnitOwnerSide.landowner,
            },
        ],
    )
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    card = _card(body, project.id, "land_share")
    assert Decimal(card["our_share_value"]["value"]) == Decimal("30400000.00")
    assert Decimal(card["construction_cost"]["value"]) == Decimal("17600000.00")
    assert Decimal(card["estimated_profit"]["value"]) == Decimal("12800000.00")
    assert Decimal(card["margin"]["value"]).quantize(_TENTH) == Decimal("42.1")
    assert card["construction_progress"]["available"] is False


async def test_kendi_yatirim_toplam_maliyeti_ile_kat_karsiligi_insaat_maliyeti_AYRISIR(
    client, db_session, user_factory, project_factory
):
    """Kullanıcı kararı 2026-08-09: iki alan artık BAŞKA şeyler ölçer ve bağları
    KOPARILDI — `total_cost` HARCANAN (E4 122), `construction_cost` BÜTÇE (KK 135).

    Aynı bütçe + aynı hakediş verisiyle iki tipin kartı FARKLI rakam basmalıdır;
    eskiden `construction_cost` bir property olarak `total_cost`u döndürüyordu.
    """
    kurucu = await user_factory(email="ayrisma@p10t3.co", password="parola1234", role_key="patron")
    yatirim = await project_factory(code="T3-AY1", project_type="kendi_yatirim")
    _set_budget_lines(yatirim, material="17600000")
    kat = await project_factory(code="T3-AY2", project_type="kat_karsiligi")
    _set_budget_lines(kat, material="17600000")
    db_session.add(
        ProjectLandShare(
            project_id=kat.id,
            landowner_name="Yılmaz Ailesi",
            our_share_pct=Decimal("55.00"),
            owner_share_pct=Decimal("45.00"),
        )
    )
    await db_session.flush()
    for proje in (yatirim, kat):
        sozlesme = await _contract(db_session, proje, kurucu, name="Akın İnşaat")
        await _payment(
            db_session, sozlesme, kurucu, SubcontractorPaymentStatus.paid, quantity="4000"
        )
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    # Kendi yatırım: arsa girilmemiş → yalnız inşaat HARCANANI.
    assert Decimal(_card(body, yatirim.id, "investment")["total_cost"]["value"]) == Decimal(
        "4000000.00"
    )
    # Kat karşılığı: KK 135 BÜTÇEDİR ve kâr projeksiyonunun tabanıdır — harcanana DÖNMEZ.
    assert Decimal(_card(body, kat.id, "land_share")["construction_cost"]["value"]) == Decimal(
        "17600000.00"
    )


async def test_taahhut_kartinin_harcanani_taseron_hakedislerinden_doner(
    client, db_session, user_factory, project_factory
):
    """E4 181/206/231/256 "Harcanan": spec §2 → taşeron hakedişleri approved+paid BRÜT.

    İşveren hakedişi (`progress_payments`) taahhütte GELİRDİR, harcama değil —
    alanın eski `pending_module`ı bu yüzden yanlış etiketti.
    """
    kurucu = await user_factory(email="harcanan@p10t3.co", password="parola1234", role_key="patron")
    project = await project_factory(
        code="T3-HR", project_type="taahhut", contract_amount="11200000.00"
    )
    contract = await _contract(db_session, project, kurucu, name="Akın İnşaat")
    await _payment(db_session, contract, kurucu, SubcontractorPaymentStatus.paid, quantity="5700")
    await _payment(
        db_session,
        contract,
        kurucu,
        SubcontractorPaymentStatus.approved,
        quantity="840",
        sequence_no=2,
    )
    # Maliyete GİRMEYEN durum (S1): harcananı büyütmemeli.
    await _payment(
        db_session,
        contract,
        kurucu,
        SubcontractorPaymentStatus.pending_approval,
        quantity="9000",
        sequence_no=3,
    )
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    spent = _card(body, project.id, "contracting")["spent"]
    assert spent["available"] is True
    assert Decimal(spent["value"]) == Decimal("6540000.00")
    assert spent["pending_module"] is None


async def test_hakedissiz_taahhut_projesinde_harcanan_SIFIR_gercek_cevaptir(
    client, db_session, user_factory, project_factory
):
    """Kaynak modül CANLI: hakedişi olmayan taahhütte `0.00` "bilinmiyor" değil
    "henüz harcanmadı"dır (`our_share_value`daki gerekçenin aynısı)."""
    project = await project_factory(
        code="T3-H0", project_type="taahhut", contract_amount="5100000.00"
    )
    await db_session.flush()
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    spent = _card(body, project.id, "contracting")["spent"]
    assert spent["available"] is True
    assert Decimal(spent["value"]) == Decimal("0.00")
    assert spent["pending_module"] is None


async def test_taahhut_kartinda_kar_marj_alani_YOKTUR(
    client, db_session, user_factory, project_factory
):
    """E4 180-181: taahhüt kartı yalnız bedel/harcanan basar — kâr alanı İCAT EDİLMEZ."""
    project = await project_factory(
        code="T3-TA", project_type="taahhut", contract_amount="11200000.00"
    )
    _set_budget_lines(project, material="1000000")
    await db_session.flush()
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    card = _card(body, project.id, "contracting")
    # ⚠️ ILR-2: `financial_progress` EKLENDI (opsiyonel alan). Kâr/marj alani
    # HALA YOKTUR — testin asil iddiasi budur ve bozulmadi.
    assert set(card) == {
        "spent",
        "physical_progress",
        "financial_progress",
        "final_progress_payment",
        "worker_count",
        "subcontractor_count",
    }


async def test_butcesiz_projede_maliyet_zarfi_BOS_KALIR(
    client, db_session, user_factory, project_factory
):
    """Bütçe girilmemiş projede KÂR/MARJ bilinmez: toplam 0 çıkar ama bu "maliyet
    ₺0" DEĞİL "bilinmiyor"dur.

    `total_cost` bu kuralın DIŞINDADIR (kullanıcı kararı 2026-08-09): o artık
    HARCANANDIR ve kaynağı (arsa + taşeron hakedişi) canlı olduğu için değer
    daima bilinir — hakedişsiz projede `0.00` gerçek cevaptır.
    """
    project = await project_factory(code="T3-B0", project_type="kendi_yatirim")
    await _units(db_session, project, [{"list_price": Decimal("1000000.00")}])
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    card = _card(body, project.id, "investment")
    for key in ("estimated_profit", "margin"):
        assert card[key]["available"] is False, key
        assert card[key]["value"] is None, key
        assert card[key]["pending_module"] == "project_costs", key
    assert card["total_cost"]["available"] is True
    assert Decimal(card["total_cost"]["value"]) == Decimal("0.00")


async def test_liste_yanitindaki_HER_dolu_zarf_pending_module_tasimaz(
    client, db_session, user_factory, project_factory
):
    """Zarf sözleşmesi UÇ düzeyinde: gövdedeki tüm zarflar taranır (yalnız kart değil)."""
    project = await project_factory(code="T3-ZR", project_type="kendi_yatirim")
    _set_budget_lines(project, material="10000000")
    await db_session.flush()
    await _units(db_session, project, [{"list_price": Decimal("20000000.00")}])
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    zarflar = list(_envelopes(body))
    assert any(zarf["available"] for _, zarf in zarflar)
    for yol, zarf in zarflar:
        if "count" in zarf:
            continue  # CountPlaceholder emsali bilinçli olarak KIRILMAZ
        assert (zarf["pending_module"] is None) == zarf["available"], (yol, zarf)


async def test_detay_ucu_ile_liste_ucu_ayni_maliyet_degerini_verir(
    client, db_session, user_factory, project_factory
):
    """Ekran karttan detaya geçince rakam DEĞİŞMEZ (tek hesap kaynağı)."""
    project = await project_factory(code="T3-DT", project_type="kendi_yatirim")
    _set_budget_lines(project, material="12000000")
    await db_session.flush()
    await _units(db_session, project, [{"list_price": Decimal("20000000.00")}])
    token = await _login(client, user_factory)

    liste = (await client.get("/projects", headers=_auth(token))).json()
    detay = (await client.get(f"/projects/{project.id}", headers=_auth(token))).json()

    assert detay["investment"]["total_cost"] == _card(liste, project.id, "investment")["total_cost"]
    assert Decimal(detay["investment"]["estimated_profit"]["value"]) == Decimal("8000000.00")


# --- N+1 ölçümü (spec §4) ---


async def test_proje_listesinde_sorgu_sayisi_proje_sayisindan_bagimsizdir(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """Spec §4: kart türevleri proje başına sorgu AÇMAZ."""
    from app.modules.projects.service import list_projects_overview

    user = await user_factory(email="olcum@p10t3.co", password="parola1234", role_key="patron")
    user.all_projects = True
    tek = await project_factory(code="T3-N1", project_type="kendi_yatirim")
    _set_budget_lines(tek, material="1000000")
    await _units(db_session, tek, [{"list_price": Decimal("100.00")}])
    await db_session.flush()

    _sorgu_sayaci.clear()
    await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    tek_sayim = _tablo_sayimi(_sorgu_sayaci, "units")

    for sira in range(3):
        proje = await project_factory(code=f"T3-N{sira + 2}", project_type="kendi_yatirim")
        _set_budget_lines(proje, material="1000000")
        await _units(db_session, proje, [{"list_price": Decimal("100.00")} for _ in range(4)])
    await db_session.flush()

    _sorgu_sayaci.clear()
    yanit = await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    cok_sayim = _tablo_sayimi(_sorgu_sayaci, "units")

    assert len(yanit.items) == 4
    assert tek_sayim == cok_sayim, (tek_sayim, cok_sayim)
    assert cok_sayim <= 1, cok_sayim


_TAAHHUT_TABLOLARI = (
    "subcontractor_progress_payments",
    "subcontractor_progress_payment_lines",
)


async def test_taahhut_kartlarinda_sorgu_sayisi_proje_ve_hakedis_sayisindan_bagimsizdir(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """Spec §4: "Harcanan" bağı proje başına sorgu AÇMAZ (1 proje vs 4 çok hakedişli)."""
    from app.modules.projects.service import list_projects_overview

    user = await user_factory(email="taolcum@p10t3.co", password="parola1234", role_key="patron")
    user.all_projects = True
    tek = await project_factory(code="T3-TN1", project_type="taahhut")
    sozlesme = await _contract(db_session, tek, user, name="Tek Taşeron")
    await _payment(db_session, sozlesme, user, SubcontractorPaymentStatus.paid, quantity="10")
    await db_session.flush()

    _sorgu_sayaci.clear()
    await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    tek_sayim = {tablo: _tablo_sayimi(_sorgu_sayaci, tablo) for tablo in _TAAHHUT_TABLOLARI}

    for sira in range(3):
        proje = await project_factory(code=f"T3-TN{sira + 2}", project_type="taahhut")
        for index in range(2):
            ek = await _contract(db_session, proje, user, name=f"Taşeron {index}")
            for no in (1, 2):
                await _payment(
                    db_session,
                    ek,
                    user,
                    SubcontractorPaymentStatus.paid,
                    quantity="10",
                    sequence_no=no,
                )
    await db_session.flush()

    _sorgu_sayaci.clear()
    yanit = await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    cok_sayim = {tablo: _tablo_sayimi(_sorgu_sayaci, tablo) for tablo in _TAAHHUT_TABLOLARI}

    assert len(yanit.items) == 4
    assert tek_sayim == cok_sayim, (tek_sayim, cok_sayim)
    assert all(sayi == 1 for sayi in cok_sayim.values()), cok_sayim


async def test_kendi_yatirim_kartlarinda_harcanan_okumasi_da_TEK_sorgudur(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """Kullanıcı kararı 2026-08-09 harcanan okumasını kendi yatırım projelerine de
    açtı; süzgeç genişledi ama toplu okuma TEK sorgu KALDI (spec §4)."""
    from app.modules.projects.service import list_projects_overview

    user = await user_factory(email="kyolcum@p10t3.co", password="parola1234", role_key="patron")
    user.all_projects = True
    tek = await project_factory(code="T3-KN1", project_type="kendi_yatirim")
    sozlesme = await _contract(db_session, tek, user, name="Tek Taşeron")
    await _payment(db_session, sozlesme, user, SubcontractorPaymentStatus.paid, quantity="10")
    await db_session.flush()

    _sorgu_sayaci.clear()
    await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    tek_sayim = {tablo: _tablo_sayimi(_sorgu_sayaci, tablo) for tablo in _TAAHHUT_TABLOLARI}

    for sira in range(3):
        proje = await project_factory(code=f"T3-KN{sira + 2}", project_type="kendi_yatirim")
        for index in range(2):
            ek = await _contract(db_session, proje, user, name=f"Taşeron {index}")
            for no in (1, 2):
                await _payment(
                    db_session,
                    ek,
                    user,
                    SubcontractorPaymentStatus.paid,
                    quantity="10",
                    sequence_no=no,
                )
    await db_session.flush()

    _sorgu_sayaci.clear()
    yanit = await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    cok_sayim = {tablo: _tablo_sayimi(_sorgu_sayaci, tablo) for tablo in _TAAHHUT_TABLOLARI}

    assert len(yanit.items) == 4
    assert tek_sayim == cok_sayim, (tek_sayim, cok_sayim)
    assert all(sayi == 1 for sayi in cok_sayim.values()), cok_sayim


async def test_harcanan_alani_olmayan_tipte_taseron_okumasi_HIC_kosmaz(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """Ünite okumasının tip süzgecinin aynısı: harcanan alanı olmayan tip
    (kat karşılığı — KK kartı yalnız BÜTÇE basar) hakediş tablosuna DOKUNMAZ.

    Kendi yatırım artık bu süzgecin İÇİNDEDİR (kullanıcı kararı 2026-08-09:
    E4 122 "Toplam Maliyet" = harcanan), taahhütle birlikte okunur.
    """
    from app.modules.projects.service import list_projects_overview

    user = await user_factory(email="tipsuzgec@p10t3.co", password="parola1234", role_key="patron")
    user.all_projects = True
    proje = await project_factory(code="T3-TS0", project_type="kat_karsiligi")
    _set_budget_lines(proje, material="1000")
    await db_session.flush()

    _sorgu_sayaci.clear()
    await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)

    assert _tablo_sayimi(_sorgu_sayaci, "subcontractor_progress_payments") == 0


# --- Mutasyon denetimi ---
