"""P-YT2 — `sites/` yer tutucularinin DENETIM bekcileri.

Bu dosya bir alan BAGLADIGI icin degil, **BAGLANMADIGI** icin vardir. Denetim
dokuz alani (B)/(C) olarak birakti; bu bekciler o kararin sessizce kaymasini
engeller:

* **zarf hâlâ DOGRU basiliyor mu** — anahtar ve `available` degeri alan alan
  cakilir. Bir alani "bagladim" sanip yalnizca `available=True` yapmak (degeri
  doldurmadan) ya da anahtari degistirmek tam burada kirilir.
* **K3 — SIFIR KALINTI**: yer tutucu zarflari `sites/` icinde YALNIZ
  `presenters.py`nin UC yardimcisinda kurulur. Dorduncu bir kurulum yeri,
  anahtarlarin zamanla ayrismasi demektir (liste `boq` derken detay `boq_items`
  diyen bir gelecek).

🔴 SAHTE-YESIL: `available is False` iddiasi TEK BASINA zayiftir — alan zaten
oyle dogar. Bu yuzden her satirda ANAHTAR da elle yazilir; anahtari degistiren
bir degisiklik (ki bu bir SOZLESME degisikligidir) bekciyi kirar.

🔴 Zarflar GERCEK okuma uclarindan okunur (`tests/.../test_sites_service.py`
deseni), donusturuculer elle cagrilmaz: uydurma bir ORM nesnesi uzerinde
olculen zarf, ucun onu basmayi unutmasini goremezdi.
"""

import ast
import pathlib

from app.core.discipline_scope import UNRESTRICTED
from app.modules.sites import service
from app.modules.sites.models import Site
from app.modules.sites.schemas import SectionCreate
from app.modules.sites.service import presenters
from app.modules.users.models import UserProjectAccess

_SITES_KOKU = pathlib.Path(presenters.__file__).resolve().parent.parent
#: Yer tutucu zarflarinin sinif adlari (`projects.schemas`ten ithal edilir).
_ZARF_ADLARI = {"MetricPlaceholder", "CountPlaceholder"}
#: Zarflarin kurulmasina IZIN VERILEN tek dosya.
_IZINLI_DOSYA = "presenters.py"


async def _kurulum(session, user_factory, project_factory, kod: str, email: str):
    """Proje + santiye + TASLAK bolum + tum projelere erisen aktor.

    Bolum TASLAK acilir: yayin zorunluluklari (tip · sorumlu · tarihler ·
    bedel) bu bekcinin konusu DEGILDIR ve kurulumu gereksiz yere kirilgan
    yapardi. Yer tutucu zarflari taslak/yayin ayrimindan ETKILENMEZ.
    """
    project = await project_factory(kod)
    site = Site(project_id=project.id, code=f"{kod}-A", name="A-Blok")
    session.add(site)
    await session.flush()
    user = await user_factory(email=email, password="parola1234", role_key="patron")
    session.add(UserProjectAccess(user_id=user.id, project_id=None, all_projects=True))
    await session.flush()
    bolum = await service.create_section(
        session, user, site.id, SectionCreate(name="Kaba Yapı", is_draft=True)
    )
    return project, site, user, bolum


# --------------------------------------------------------------------------- #
# K3 — SIFIR KALINTI (yapisal bekci, DB'siz)
# --------------------------------------------------------------------------- #


def _zarf_kurulan_dosyalar() -> dict[str, list[int]]:
    """`sites/` altinda `MetricPlaceholder(...)`/`CountPlaceholder(...)` CAGRISI
    yapan her dosya ve satir. Sinif TANIMI ya da tip ANNOTASYONU sayilmaz —
    yalnizca CAGRI (yani bir zarfin kuruldugu yer)."""
    bulgular: dict[str, list[int]] = {}
    for yol in sorted(_SITES_KOKU.rglob("*.py")):
        agac = ast.parse(yol.read_text(encoding="utf-8"))
        for dugum in ast.walk(agac):
            if not isinstance(dugum, ast.Call):
                continue
            hedef = dugum.func
            ad = hedef.id if isinstance(hedef, ast.Name) else getattr(hedef, "attr", None)
            if ad in _ZARF_ADLARI:
                bulgular.setdefault(yol.name, []).append(dugum.lineno)
    return bulgular


def test_yer_tutucu_zarflari_YALNIZ_presenters_icinde_kurulur():
    """K3 — ayni zarf iki yerde kurulmaz.

    Bekci `sites/` agacini AST ile tarar; `grep` yerine AST secilmesi
    bilinclidir: yorum satirindaki ya da docstring'deki bir ornek `grep`i
    yaniltir — bu dosyanin KENDISI de dahil."""
    bulgular = _zarf_kurulan_dosyalar()

    assert set(bulgular) == {_IZINLI_DOSYA}, (
        f"yer tutucu zarfi presenters.py DISINDA kuruluyor: {bulgular} — "
        "anahtarlar iki yerde yasarsa zamanla ayrisir (K3)"
    )


def test_zarf_kurucu_YARDIMCILARI_BES_TANEDIR():
    """Kurulum noktalarinin SAYISI da cakilir.

    🔴 BLM-SAY: UC iken BES oldu — `_boq_item_count` ve `_boq_budget` eklendi
    (`boq_item_count` + `budget` BAGLANDI). Bekcinin amaci degismedi: her yeni
    yardimci, gerekcesi YAZILMASI gereken yeni bir kurulum noktasidir.
    """
    agac = ast.parse(pathlib.Path(presenters.__file__).read_text(encoding="utf-8"))
    kurucular = {
        dugum.name
        for dugum in ast.walk(agac)
        if isinstance(dugum, ast.FunctionDef)
        and any(
            isinstance(alt, ast.Call)
            and (alt.func.id if isinstance(alt.func, ast.Name) else None) in _ZARF_ADLARI
            for alt in ast.walk(dugum)
        )
    }

    assert kurucular == {
        "_metric",
        "_count",
        "_worker_count",
        "_boq_item_count",
        "_boq_budget",
    }, f"zarf kurucu yardimcilari degisti: {sorted(kurucular)}"


def test_anahtar_uzayi_IZIN_MODULU_uzayindan_AYRISMIS():
    """🔴 P-YT2'nin OLCULMUS bulgusu — kod `pending_module` icin *"Bunlar MODUL
    ANAHTARIDIR"* diyor ama ALTI anahtarin IKISI tohumlanmis modul kaydinda
    (`roles/seed_data.py:MODULES`) YOKTUR:

    | anahtar | izin modulu mu | not |
    |---|---|---|
    | `progress_payments` `timesheet` `contracts` `boq` | ✅ | gercek modul |
    | `subcontracts` | ❌ | boyle bir modul HIC OLMADI; canlisi `contracts` |
    | `project_costs` | ❌ | KAVRAMSAL anahtar (`projects/cards.py` de kullanir) |

    Bekci ikisini de ISIMLE cakar. Amac ayrismayi DUZELTMEK degil (deger
    yanittadir, degistirmek sozlesme kirmasidir) — SESSIZ kalmasini onlemek:
    biri bu iki anahtari duzeltirse ya da `subcontracts` adinda bir modul
    acilirsa, `presenters.py`deki gerekce bayatlar ve bu test haber verir.
    """
    from app.modules.roles.seed_data import MODULES

    modul_anahtarlari = {modul["key"] for modul in MODULES}
    kullanilan = {
        presenters._PROGRESS_PAYMENTS,
        presenters._TIMESHEET,
        presenters._SUBCONTRACTS,
        presenters._PROJECT_COSTS,
        presenters._CONTRACTS,
        presenters._BOQ,
    }

    assert kullanilan - modul_anahtarlari == {"subcontracts", "project_costs"}, (
        "yer tutucu anahtarlarinin izin modulu kaydiyla ayrisma kumesi DEGISTI — "
        f"bugunku fark: {sorted(kullanilan - modul_anahtarlari)}"
    )
    assert "contracts" in modul_anahtarlari, (
        "`subcontracts`in canli karsiligi `contracts`tir; o da kaybolduysa yorum bayat"
    )


# --------------------------------------------------------------------------- #
# (B)/(C) — zarf HÂLÂ dogru basiliyor mu
# --------------------------------------------------------------------------- #


async def test_BOLUM_zarflari__anahtar_ve_bos_durum_alan_alan(
    seeded_db, user_factory, project_factory
):
    """`to_section` DORT zarf basar: 🔴 BLM-SAY sonrasi UCU BAGLI, BIRI bos.

    P-YT2'nin iki (C) karari OLCEREK curutuldu ve kapandi:

    * `boq_item_count` — gerekce *"mockup 'tamamlanan / toplam' ister"*ti.
      YARISI hâlâ dogru (PAYIN kaynagi yok) ama PAYDA olculebilir; tek `int`
      yuvaya PAYDA basmak DOGRU bir cumledir, payi uydurmak yalan olurdu.
    * `budget` — gerekce IKI engel sayiyordu: para formulunun tek kopyasinin
      cagrilamamasi (P-YT3'te `quantize_money` adi acildi → KALKTI) ve toplu
      okuyucu olmamasi (`boq/counts.py::by_section` → KALKTI).

    Bu bekci ARTIK YER TUTUCULUGU DEGIL, ZARF SEKLINI cakiyor: hangi alan
    hangi kural altinda (dolu `CountPlaceholder` anahtar TASIR, dolu
    `MetricPlaceholder` TASIMAZ) ne donuyor.
    """
    _p, site, user, _b = await _kurulum(
        seeded_db, user_factory, project_factory, "YT-1", "yt1@t.co"
    )

    satir = (
        await service.list_sections_for_site(seeded_db, user, site.id, scope=UNRESTRICTED)
    ).items[0]

    # ⚠️ ILR-1'DE DEGISTI: alan BAGLANDI ve sahibi artik GUNLUKTUR. Bu bolumun
    # TAHSISI YOK (payda 0) — yuzde "0" degil, YOKTUR; zarf bos kalir ama artik
    # `site_diary` anahtarini tasir. Rol `patron`, yani IZIN VAR: bu bos hâl
    # izin kapisi DEGIL, payda yoklugudur (ucuncu hâl `pending_module is None`
    # olurdu — karistirilmasin).
    assert (satir.progress_pct.available, satir.progress_pct.pending_module) == (
        False,
        "site_diary",
    ), "ILR-1: tahsisi olmayan bolumde payda YOK; anahtar gunlugu gostermeli"
    assert (satir.boq_item_count.available, satir.boq_item_count.pending_module) == (
        True,
        "boq",
    ), "BLM-SAY'de BAGLANDI; dolu `CountPlaceholder` anahtari TASIMAYA devam eder"
    assert satir.boq_item_count.count == 0, "tahsisi olmayan bolum: OLCULMUS sifir"
    assert (satir.budget.available, satir.budget.pending_module) == (True, None), (
        "BLM-SAY'de BAGLANDI; dolu `MetricPlaceholder` anahtar TASIMAZ (P10 T3)"
    )
    assert (satir.worker_count.available, satir.worker_count.pending_module) == (
        True,
        "timesheet",
    ), "(A) — T4'te baglandi; dolu zarf `pending_module` tasimaya DEVAM eder"


async def test_SANTIYE_DETAY_zarflari__ikisi_de_C_olarak_kaldi(
    seeded_db, user_factory, project_factory
):
    _p, site, user, _b = await _kurulum(
        seeded_db, user_factory, project_factory, "YT-2", "yt2@t.co"
    )

    detay = await service.get_site_detail(seeded_db, user, site.id, scope=UNRESTRICTED)

    assert (
        detay.total_progress_payment.available,
        detay.total_progress_payment.pending_module,
    ) == (False, "progress_payments"), "(C) — santiye kirilimi YARIM"
    assert (detay.contract_amount.available, detay.contract_amount.pending_module) == (
        False,
        "contracts",
    ), "(C) — santiye duzeyinde sozlesme bedeli SEMADA YOK"
    # ⚠️ ILR-1'DE DEGISTI — bu alan SANTIYE kartinin yuzdesidir (`SiteCard`,
    # `SectionResponse` DEGIL) ve o da baglandi. Santiyenin BOQ'u bos oldugu
    # icin payda 0 → yuzde YOK, anahtar `site_diary`.
    assert (detay.progress_pct.available, detay.progress_pct.pending_module) == (
        False,
        "site_diary",
    ), "ILR-1: BOQ'u bos santiyede payda YOK; kart ile bolum AYNI turevden beslenir"


async def test_ALT_KPI_seridi__uc_bos_bir_bagli(seeded_db, user_factory, project_factory):
    """🔴 `_SUBCONTRACTS` anahtari BILEREK `subcontracts` kaldi ve bu bekci onu
    CAKAR: boyle bir modul YOKTUR (canlisi `contracts`), ama deger yanittadir
    ve degistirmek sozlesme kirmasidir. Duzeltme frontend devriyle gitmeli."""
    proje, _s, user, _b = await _kurulum(
        seeded_db, user_factory, project_factory, "YT-3", "yt3@t.co"
    )

    seri = (await service.list_sites_overview(seeded_db, user, proje.id, scope=UNRESTRICTED)).totals

    assert (
        seri.total_progress_payment.available,
        seri.total_progress_payment.pending_module,
    ) == (False, "progress_payments")
    assert (seri.subcontractor_count.available, seri.subcontractor_count.pending_module) == (
        False,
        "subcontracts",
    ), "BAYAT ETIKET bilincli olarak korundu — raporlandi, degistirilmedi"
    assert (seri.average_margin.available, seri.average_margin.pending_module) == (
        False,
        "project_costs",
    ), "(C) — 'ortalama' tanimsiz: taahhutte marj yapisal olarak `None`"
    assert seri.active_worker_count.available is True, "(A) — T4'te baglandi"
    assert seri.active_worker_count.pending_module == "timesheet"
