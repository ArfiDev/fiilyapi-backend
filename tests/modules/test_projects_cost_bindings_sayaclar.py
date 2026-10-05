"""Proje kartı — MUTASYON DENETİMİ · TARAF ÜNİTE SAYAÇLARI · YER TUTUCU DENETİMİ.

`test_projects_cost_bindings.py`nin ikinci parçası (800 satır tavanı bölmesi);
paylaşılan yardımcılar `_projects_cost_bindings.py`dedir.

Mockup otoritesi: `projedesign/Ekran 4 - Projeler.dc.html` 148-149 — kartın
paylaşım şeridi "Biz %55 · 23 ünite" / "Arsa %45 · 19" basar. İki sayı da düz
`owner_side` sayımıdır. SIFIR GERÇEK CEVAPTIR ve bilinmeyenden AYRIDIR.

Yer tutucu bekçileri gerekçelerin ÇÜRÜMESİNİ engeller: `pending_module` artık
"modül yok" demiyor, canlı bir kaynağı adlandırıyor.
"""

import inspect
from decimal import Decimal

import pytest

from app.core.discipline_scope import UNRESTRICTED
from app.modules.projects.models import Project, ProjectLandShare
from app.modules.sites.models import Site
from app.modules.units.models import Block, Unit, UnitKind, UnitOwnerSide

from ._projects_cost_bindings import (
    _auth,
    _card,
    _login,
    _set_budget_lines,
    _sorgu_sayaci_fixture,  # noqa: F401
    _tablo_sayimi,
    _units,
)


async def test_kart_hesabi_orm_nesnesini_DEGISTIRMEZ(
    db_session, user_factory, project_factory, seeded_db
):
    from app.modules.projects.service import list_projects_overview

    user = await user_factory(email="mutasyon@p10t3.co", password="parola1234", role_key="patron")
    user.all_projects = True
    project = await project_factory(code="T3-MU", project_type="kendi_yatirim")
    _set_budget_lines(project, material="5000000")
    await db_session.flush()
    uniteler = await _units(db_session, project, [{"list_price": Decimal("9000000.00")}])
    once = (project.budget_material, project.budget, uniteler[0].list_price)

    await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)

    assert (project.budget_material, project.budget, uniteler[0].list_price) == once


# --- Kat karşılığı TARAF ünite sayaçları (E4 148-149) ---
#
# Mockup otoritesi: `projedesign/Ekran 4 - Projeler.dc.html` 148-149 — kartın
# paylaşım şeridi "Biz %55 · 23 ünite" / "Arsa %45 · 19" basar. İki sayı da düz
# `owner_side` sayımıdır ve `GET /projects/{id}/land-share/summary` ucunun
# `our_side.unit_count` / `owner_side.unit_count` alanlarıyla AYNI sayılardır.


async def _kk_projesi(db_session, project_factory, *, code: str) -> Project:
    """Kat karşılığı proje + `ProjectLandShare` kaydı.

    Kayıt ŞART: `_land_share_card` kaydı olmayan projede `None` döner ve kart
    hiç kurulmaz — sayaç testi o hâlde neyi ölçtüğünü bilemezdi.
    """
    project = await project_factory(code=code, project_type="kat_karsiligi")
    db_session.add(
        ProjectLandShare(
            project_id=project.id,
            landowner_name="Yılmaz Ailesi",
            our_share_pct=Decimal("55.00"),
            owner_share_pct=Decimal("45.00"),
        )
    )
    await db_session.flush()
    return project


async def test_kat_karsiligi_karti_taraf_unite_sayaclarini_GERCEK_doner(
    client, db_session, user_factory, project_factory
):
    """E4 148-149 şeridinin iki sayacı ZARFIN İÇİNDE gerçeğe bağlanır.

    🔴 SAHTE-YEŞİL YASAĞI: beklenen 3/2 sayıları AŞAĞIDAKİ kurulumdan ELDE
    sayılmıştır (üç `contractor`, iki `landowner`, bir de taraflandırılmamış).
    Beklentiyi `len([u for u in units if u.owner_side is ...])` ile üretmek
    uygulamanın aynasını uygulamaya karşı sınamak olurdu — o test yüklem
    kaymasını GÖREMEZ.

    Altıncı (atanmamış) ünite bilinçlidir: hiçbir tarafa sayılmadığı için
    3 + 2 ≠ 6 olur ve "toplam ünite sayısını basıyor" hatası yakalanır.
    """
    project = await _kk_projesi(db_session, project_factory, code="T3-TS1")
    await _units(
        db_session,
        project,
        [
            {"appraisal_value": Decimal("100.00"), "owner_side": UnitOwnerSide.contractor},
            {"appraisal_value": Decimal("100.00"), "owner_side": UnitOwnerSide.contractor},
            {"appraisal_value": Decimal("100.00"), "owner_side": UnitOwnerSide.contractor},
            {"appraisal_value": Decimal("100.00"), "owner_side": UnitOwnerSide.landowner},
            {"appraisal_value": Decimal("100.00"), "owner_side": UnitOwnerSide.landowner},
            {"appraisal_value": Decimal("100.00"), "owner_side": None},
        ],
    )
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    card = _card(body, project.id, "land_share")
    # Zarf ŞEKLİ `_worker_count` emsalinin AYNISIDIR: dolu `CountPlaceholder`
    # `pending_module` TAŞIMAYA DEVAM EDER (bkz. `CountPlaceholder` notu).
    assert card["our_unit_count"] == {"available": True, "count": 3, "pending_module": "units"}
    assert card["owner_unit_count"]["available"] is True
    assert card["owner_unit_count"]["count"] == 2

    # Kart ile özet ucu AYNI projede AYNI sayıyı söyler (yüklem tek kopyadır).
    ozet = (
        await client.get(f"/projects/{project.id}/land-share/summary", headers=_auth(token))
    ).json()
    assert ozet["our_side"]["unit_count"] == 3
    assert ozet["owner_side"]["unit_count"] == 2
    assert ozet["unassigned"]["unit_count"] == 1


async def test_taraf_sayacinda_SIFIR_gercek_cevaptir_bilinmeyenden_AYRIDIR(
    client, db_session, user_factory, project_factory
):
    """🔴 K2 — "0" ile "bilinmiyor" AYNI TESTTE ayrışır.

    Kaynak modül (`units`) CANLIDIR: bizim payımızda ünite olmaması bir CEVAPTIR,
    eksik veri değil. Üç hâl tek testte karşılaştırılır:

    1. hiç ünitesi olmayan kat karşılığı proje → DOLU zarf + `count == 0`,
    2. üniteleri olan ama HİÇBİRİ taraflandırılmamış proje (noter paylaşımı
       öncesi gerçek dünya hâli) → yine DOLU zarf + `count == 0`,
    3. kat karşılığı OLMAYAN proje → alan kartta HİÇ YOKTUR (0 da basmaz).

    (1) ile (2) bu kartta AYNI cevabı verir ve vermelidir: ikisinde de bizim
    payımızda sıfır ünite vardır. Aradaki farkı taşıyan yer bu kart DEĞİL,
    `land-share/summary` ucunun `unassigned` bölümüdür — orada (1) sıfır, (2)
    üç ünite gösterir. (3) ise "alan yok" hâlidir ve 0'dan yapısal olarak ayrıdır.
    """
    bos = await _kk_projesi(db_session, project_factory, code="T3-TS2")
    atanmamis = await _kk_projesi(db_session, project_factory, code="T3-TS3")
    await _units(
        db_session,
        atanmamis,
        [{"appraisal_value": Decimal("100.00"), "owner_side": None} for _ in range(3)],
    )
    yatirim = await project_factory(code="T3-TS4", project_type="kendi_yatirim")
    _set_budget_lines(yatirim, material="1000")
    await db_session.flush()
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()

    # (1) ünitesi HİÇ olmayan proje: boş yer tutucu DEĞİL, dolu zarf içinde 0.
    bos_card = _card(body, bos.id, "land_share")
    assert bos_card["our_unit_count"]["available"] is True
    assert bos_card["our_unit_count"]["count"] == 0
    assert bos_card["owner_unit_count"]["available"] is True
    assert bos_card["owner_unit_count"]["count"] == 0

    # (2) üniteleri var ama taraflandırılmamış: iki taraf da yine dolu 0.
    atanmamis_card = _card(body, atanmamis.id, "land_share")
    assert atanmamis_card["our_unit_count"]["available"] is True
    assert atanmamis_card["our_unit_count"]["count"] == 0
    assert atanmamis_card["owner_unit_count"]["available"] is True
    assert atanmamis_card["owner_unit_count"]["count"] == 0

    # (1) ile (2) arasındaki fark KAYBOLMAZ; başka uçta durur.
    def _ozet_url(project_id) -> str:
        return f"/projects/{project_id}/land-share/summary"

    bos_ozet = (await client.get(_ozet_url(bos.id), headers=_auth(token))).json()
    atanmamis_ozet = (await client.get(_ozet_url(atanmamis.id), headers=_auth(token))).json()
    assert bos_ozet["unassigned"]["unit_count"] == 0
    assert atanmamis_ozet["unassigned"]["unit_count"] == 3

    # (3) alan HİÇ YOK hâli: kendi yatırım kartında bu iki anahtar bulunmaz.
    yatirim_card = _card(body, yatirim.id, "investment")
    assert "our_unit_count" not in yatirim_card
    assert "owner_unit_count" not in yatirim_card


def test_taraf_yuklemi_TEK_dosyada_yasar_kopyalanmaz() -> None:
    """🔴 K3 — yapısal bekçi: "ünite hangi tarafta" yüklemi ÜÇ yerde kopyaydı.

    `land_share.get_summary` (özet ucu), `costs.our_share_value` (pay değeri) ve
    kart bağı aynı `owner_side` karşılaştırmasını ayrı ayrı yazsaydı, TEK
    kopyada yapılan bir kayma (ör. atanmamış üniteyi "bizim" saymak) kart ile
    özet ucunun AYNI proje hakkında farklı sayı söylemesi demek olurdu — ve
    hiçbir davranış testi bunu yakalamazdı, çünkü her uç kendi kopyasına göre
    doğru kalırdı. Yüklem bu yüzden `unit_sides.py`de TEK kopyadır.

    Bekçi `codes.py` emsalinin (`tests/modules/units/test_units_block_codes.py`)
    aynısıdır: kaynak metni okunur, dizge aranır.
    """
    from app.modules.projects import cost_cards, costs, land_share, unit_sides

    for modul in (land_share, costs, cost_cards):
        kaynak = inspect.getsource(modul)
        for yuklem in ("UnitOwnerSide.contractor", "UnitOwnerSide.landowner", "owner_side is None"):
            assert yuklem not in kaynak, (
                f"{modul.__name__} taraf yüklemini KENDİ yazıyor ({yuklem!r}); "
                "tek kopya app/modules/projects/unit_sides.py'dedir."
            )

    tek_kaynak = inspect.getsource(unit_sides)
    assert "UnitOwnerSide.contractor" in tek_kaynak
    assert "UnitOwnerSide.landowner" in tek_kaynak
    assert "owner_side is None" in tek_kaynak


async def test_taraf_sayaclari_unite_sayisi_arttikca_SORGU_ACMAZ(
    db_session, user_factory, project_factory, _sorgu_sayaci: list[str]
):
    """🔴 N+1 bekçisi: sayaçlar ZATEN yüklü listeden türer, yeni sorgu AÇMAZ.

    Ünite 2'den 12'ye çıkarken `units` tablosuna giden ifade sayısı DEĞİŞMEZ.
    Sayaçlar için ayrı bir `SELECT count(*)` yazılsaydı bu ölçüm büyürdü.
    """
    from app.modules.projects.service import list_projects_overview

    user = await user_factory(email="tarafn1@p10t3.co", password="parola1234", role_key="patron")
    user.all_projects = True
    proje = await _kk_projesi(db_session, project_factory, code="T3-TSN")
    site = Site(project_id=proje.id, code="SNT-TSN", name="Şantiye")
    db_session.add(site)
    await db_session.flush()
    blok = Block(project_id=proje.id, site_id=site.id, name="A Blok")
    db_session.add(blok)
    await db_session.flush()

    async def _unite_ekle(ilk: int, son: int) -> None:
        """Tek numaralar BİZ, çift numaralar ARSA."""
        for no in range(ilk, son + 1):
            db_session.add(
                Unit(
                    project_id=proje.id,
                    block_id=blok.id,
                    unit_no=str(no),
                    unit_kind=UnitKind.apartment,
                    appraisal_value=Decimal("100.00"),
                    owner_side=(
                        UnitOwnerSide.contractor if no % 2 == 1 else UnitOwnerSide.landowner
                    ),
                )
            )
        await db_session.flush()

    await _unite_ekle(1, 2)
    _sorgu_sayaci.clear()
    az = await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    az_sayim = _tablo_sayimi(_sorgu_sayaci, "units")

    await _unite_ekle(3, 12)
    _sorgu_sayaci.clear()
    cok = await list_projects_overview(db_session, user, None, None, scope=UNRESTRICTED)
    cok_sayim = _tablo_sayimi(_sorgu_sayaci, "units")

    # Sayaçların GERÇEKTEN büyüdüğünü de doğrula: sabit 0 dönen bir uygulama
    # sorgu ölçümünü sahte-yeşil geçerdi (1/1 → 6/6, elde sayıldı).
    assert (az.items[0].land_share.our_unit_count.count) == 1
    assert (az.items[0].land_share.owner_unit_count.count) == 1
    assert (cok.items[0].land_share.our_unit_count.count) == 6
    assert (cok.items[0].land_share.owner_unit_count.count) == 6
    assert az_sayim == cok_sayim, (az_sayim, cok_sayim)
    assert cok_sayim <= 1, cok_sayim


def test_taraf_KUMESININ_KENDISI_bekcilidir_yeni_enum_uyesi_sessizce_gecmez() -> None:
    """🔴 Bir kümeyle çalışan bekçi, KÜMENİN KENDİSİNİ de sınamalıdır (MT-2 kanonu).

    `unit_sides.partition` üç kümeye ayırır ve üçüncüsü (`unassigned`) ekranda
    *"noter paylaşımı henüz yapılmadı"* diye okunur — yani bir OLGU iddiasıdır.
    Ayrım `else` ile yazılsaydı `UnitOwnerSide`a eklenecek DÖRDÜNCÜ bir hâl
    sessizce "atanmamış" sayılır ve ekran ATANMIŞ bir üniteyi atanmamış diye
    basardı; sayılar yine tutacağı için hiçbir davranış testi de görmezdi.

    Bu bekçi iki katmanlıdır:
    1. enum'un BUGÜNKÜ üye kümesi çakılır — üye eklenirse bu test kırmızı olur
       ve geliştirici `unit_sides`ı karara bağlamak zorunda kalır;
    2. bilinmeyen bir taraf değeri `ValueError` ile PATLAR (sessizce bir kümeye
       düşmez) — sahte bir `Unit` ile doğrudan sınanır.
    """
    from types import SimpleNamespace

    from app.modules.projects import unit_sides
    from app.modules.units.models import UnitOwnerSide

    assert {uye.value for uye in UnitOwnerSide} == {"contractor", "landowner"}, (
        "`UnitOwnerSide` genisledi: `unit_sides.partition` uc kumesi ve bu testin "
        "beklentisi birlikte karara baglanmalidir."
    )

    with pytest.raises(ValueError, match="Bilinmeyen taraf"):
        unit_sides.partition([SimpleNamespace(owner_side="ortak_alan")])


# --- YER TUTUCU DENETİMİ 2026-08-22: gerekçeler ÇÜRÜMESİN diye çakılır ---
#
# Denetimin bulgusu: `pending_module` artık "modül yok" DEMİYOR — sahaya çıkan
# 13 anahtarın hepsi CANLI bir kaynağı adlandırıyor (`service._metric`
# docstring'i). Kalan yer tutucuların GERÇEK engelleri çağrı yerlerine yazıldı;
# aşağıdaki iki bekçi o yazıların hâlâ doğru olduğunu ölçer.


def test_projects_progress_pct_sutununun_YAZMA_YOLU_YOKTUR() -> None:
    """🔴 Denetimin BAŞ BULGUSU: `projects.progress_pct` YAZIMI ÖLÜ bir fosildir.

    Bu bir DİLEK değil, ÖLÇÜLMÜŞ durumdur. `construction_progress` yer tutucusunun
    (`service._land_share_card`) en tehlikeli tuzağı, bu sütunun apaçık bir kaynak
    gibi görünmesidir: `Numeric(5,2), nullable=False, default=0` (models.py:143),
    zaten HAM servis ediliyor (`ProjectListItem.progress_pct` zarf DEĞİL düz
    `Decimal`) — ama hiçbir HTTP isteği onu SET EDEMEZ, çünkü ne `ProjectCreate`
    ne `ProjectUpdate` böyle bir alan taşır. Buna rağmen daima 0 da değildir:
    `alembic/versions/795d6498e4da_projects_seed.py` üç demo projeye 42.50/15.00/
    100.00 yazar ve o revizyon head'in atasıdır. Yani sütun, kullanıcının açtığı
    her projede kalıcı 0; üç tohum satırında ise hiçbir formdan düzeltilemeyen
    donmuş bir değer.

    ⚠️ BU TEST KIRMIZIYA DÖNERSE: biri yazma yolu açmış demektir ve
    `construction_progress` tuzak yorumu (service.py, `_land_share_card`)
    YENİDEN KARARA BAĞLANMALIDIR — fosil artık fosil değildir.
    """
    from app.modules.projects.schemas import ProjectCreate, ProjectUpdate

    assert "progress_pct" not in ProjectCreate.model_fields, (
        "`ProjectCreate`e `progress_pct` eklenmis: `construction_progress` fosil "
        "gerekcesi artik gecerli olmayabilir, yeniden karara baglayin."
    )
    assert "progress_pct" not in ProjectUpdate.model_fields, (
        "`ProjectUpdate`e `progress_pct` eklenmis: `construction_progress` fosil "
        "gerekcesi artik gecerli olmayabilir, yeniden karara baglayin."
    )


async def test_govdeye_konan_progress_pct_POST_ve_PATCH_te_YOK_SAYILIR(client, user_factory):
    """Yapısal iddianın DAVRANIŞ tarafı: gövdeye elle yazmak da işe yaramaz.

    Pydantic varsayılanı `extra="ignore"`tır (`ProjectCreate`te `extra=` ayarı
    YOKTUR), bu yüzden fazladan anahtar 422 vermez — SESSİZCE DÜŞER. Sessiz
    düşüş tam da tehlikeli olan hâldir: istemci "gönderdim" sanır, sütun 0 kalır.
    Bu yüzden hem yaratma hem güncelleme yolu ölçülür.
    """
    token = await _login(client, user_factory)

    created = await client.post(
        "/projects",
        json={
            "code": "T3-FOSIL",
            "name": "Fosil Sütun Testi",
            "project_type": "taahhut",
            "is_draft": True,
            "progress_pct": "42.50",
        },
        headers=_auth(token),
    )
    assert created.status_code == 201, created.text
    assert Decimal(created.json()["progress_pct"]) == Decimal("0")

    project_id = created.json()["id"]
    patched = await client.patch(
        f"/projects/{project_id}",
        json={"name": "Fosil Sütun Testi 2", "progress_pct": "99.00"},
        headers=_auth(token),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["name"] == "Fosil Sütun Testi 2"  # PATCH GERÇEKTEN koştu
    assert Decimal(patched.json()["progress_pct"]) == Decimal("0")


# Denetimin SINIFLANDIRMASI (kart, alan, anahtar, sınıf) — gerekçelerin TAM
# metni `service.py`deki çağrı yerlerindedir, burada yalnız ÇAKILIR:
#
#   (A) BAYAT  — kaynak CANLI, alan yalnızca henüz bağlanmadı (toplu okuyucu
#                ve/veya süzgeç kararı eksik).
#   (C) TUZAK  — bağlamak AKTİF OLARAK YANLIŞ olurdu: ya mockup kendi etiketiyle
#                çelişiyor, ya zarfın ŞEKLİ alanı ifade edemiyor, ya da anahtar
#                yapısal olarak karşılanamaz.
# ⚠️ **ILR-1'DE BIR SATIR DUSTU (2026-08-27):**
# `("contracting", "physical_progress", "progress_payments", "C")` SILINDI —
# cunku (C) gerekcesi ÇÜRÜDÜ. O gerekce *"mockup kendi kendiyle celisiyor:
# etiket FIZIKSEL ama sayi MALI (`Harcanan / Sözleşme Bedeli`)"* diyordu ve
# "baglayacak kisi ONCE alanin hangisi oldugunu karara baglamali" diye
# bekletiyordu. Kullanici 2026-08-27'de KARARI VERDI: ikisi AYRI alandir.
# `physical_progress` artik GUNLUKTEN turer (`site_diary`), yaninda YENI
# `financial_progress` onaylanmis isveren hakedisinden turer. Bekcileri
# `tests/modules/test_ilr_ilerleme.py`dedir.
_DENETIM_2026_08_22 = (
    ("contracting", "final_progress_payment", "progress_payments", "C"),
    ("contracting", "subcontractor_count", "subcontracts", "A"),
    ("investment", "sold_amount", "units", "A"),
    ("investment", "sales_ratio", "units", "C"),
    ("investment", "unit_summary", "units", "C"),
    ("land_share", "construction_progress", "progress_payments", "C"),
)

_KART_TIPI = {
    "contracting": "taahhut",
    "investment": "kendi_yatirim",
    "land_share": "kat_karsiligi",
}


async def test_denetimin_ALTI_yer_tutucusu_hala_BOS_ve_anahtarini_TASIYOR(
    client, db_session, user_factory, project_factory
):
    """🔴 Gerekçe ÇÜRÜME bekçisi — altı alanın hepsi tek tabloda çakılı.

    Bu test alanların bağlanmasını YASAKLAMAZ; bağlayanı, kaydedilmiş gerekçeyi
    OKUMAYA ZORLAR. Biri `service.py`deki tuzak/bayat notunu okumadan bir alanı
    doldurursa burası kırmızıya döner ve o notu güncellemek zorunda kalır.

    Anahtarın kendisi de çakılıdır: `pending_module` artık "modül yok" demiyor,
    "veri hangi modülün mülkiyetinde" diyor — anahtarı sessizce değiştirmek,
    çağrı yerindeki gerekçeyi de geçersiz kılar.
    """
    await _kk_projesi(db_session, project_factory, code="T3-DEN-KK")
    await project_factory(code="T3-DEN-TA", project_type="taahhut")
    await project_factory(code="T3-DEN-KY", project_type="kendi_yatirim")
    token = await _login(client, user_factory)

    body = (await client.get("/projects", headers=_auth(token))).json()
    kartlar = {
        kart: next(
            row[kart]
            for row in body["items"]
            if row["project_type"] == tip and row["code"].startswith("T3-DEN-")
        )
        for kart, tip in _KART_TIPI.items()
    }

    for kart, alan, anahtar, sinif in _DENETIM_2026_08_22:
        zarf = kartlar[kart][alan]
        assert zarf["available"] is False, (
            f"{kart}.{alan} BAGLANMIS: `service.py`deki SINIF ({sinif}) gerekcesi "
            f"okunup guncellenmeli (bu testin satiri da silinmeli)."
        )
        assert zarf["pending_module"] == anahtar, (
            f"{kart}.{alan} anahtari degismis ({zarf['pending_module']!r} != "
            f"{anahtar!r}): cagri yerindeki gerekce de gozden gecirilmeli."
        )


# --- P-YT4 DENETİMİ (2026-08-23): `construction_progress` anahtarının AÇIK UCU ---


def test_taseron_tarafinda_FIZIKSEL_ILERLEME_HESABI_YOKTUR() -> None:
    """🔴 `construction_progress` anahtarının neden DEĞİŞMEDİĞİNİ çakar.

    P-YT1 ölçtü: `_land_share_card.construction_progress` yer tutucusunun
    `pending_module="progress_payments"` anahtarı YAPISAL OLARAK
    KARŞILANAMAZ — kat karşılığı projesinde İŞVEREN yoktur, o modül bu değeri
    asla veremez. P-YT1 kusuru yazdı ama düzeltmedi.

    P-YT4 doğru anahtarı ARADI ve **bugün doğru bir anahtar OLMADIĞINI** ölçtü:
    işveren tarafında fiziksel ilerleme `progress_payments/service.py`de
    (`physical_numerator` → `_progress_block.physical_pct`) hesaplanır; TAŞERON
    tarafında böyle bir hesap HİÇ YOKTUR. Anahtarı "olabilecek" bir modüle
    çevirmek ölçülmüş bir olguyu tahminle değiştirmek olurdu.

    ⚠️ BU TEST KIRMIZIYA DÖNERSE: taşeron tarafına fiziksel ilerleme yazılmış
    demektir ve `projects/cards.py::_land_share_card` içindeki TUZAK A notu
    YENİDEN KARARA BAĞLANMALIDIR — artık dürüst bir anahtar VARDIR.
    """
    import pathlib

    modul = (
        pathlib.Path(__file__).resolve().parents[2]
        / "app"
        / "modules"
        / "subcontractor_progress_payments"
    )
    assert modul.is_dir(), "TH modülü taşınmış: bu bekçinin yolu güncellenmeli."
    fiziksel = {
        dosya.name: [
            satir for satir in dosya.read_text(encoding="utf-8").splitlines() if "physical" in satir
        ]
        for dosya in sorted(modul.glob("*.py"))
    }
    bulunan = {ad: satirlar for ad, satirlar in fiziksel.items() if satirlar}
    assert bulunan == {}, (
        "Taşeron tarafında fiziksel ilerleme belirmiş "
        f"({bulunan}): `_land_share_card.construction_progress` anahtarı "
        "(`progress_payments`) yeniden karara bağlanmalı."
    )
    # Karşı kutup: işveren tarafında hesap GERÇEKTEN vardır — bekçi "hiçbir yerde
    # yok" diye boş bir iddia kurmuyor, ASİMETRİYİ ölçüyor.
    isveren = (
        pathlib.Path(__file__).resolve().parents[2]
        / "app"
        / "modules"
        / "progress_payments"
        / "service.py"
    ).read_text(encoding="utf-8")
    assert "physical_numerator" in isveren
