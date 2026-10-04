"""P-YT3 — `boq/` yer tutucularinin DENETIM bekcileri.

Bu dosya bir alan **BAGLANMADIGI** icin vardir (P-YT2'nin
`tests/modules/sites/test_pyt2_yer_tutucu_denetimi.py` deseninin kardesi).
Denetim `boq`nun ALTI zarfini da yerinde birakti; bu bekciler o kararin
sessizce kaymasini engeller.

🔴 **BURADAKI FARK: KAYNAK VAR.** `sites/`in yer tutuculari cogunlukla "veri
yok" diye kaldi. `boq`da veri VAR ve birlestirme anahtari da VAR:

    boq_items.contract_item_id ──> employer_contract_items.id
                                            ^
              progress_payment_lines.(contract_item_id, site_id)

Yani `contract_total` / `realized_total` / `progress_pct` ILKECE
HESAPLANABILIR. Baglanmama sebebi **IZIN KAPISIDIR (K4)** — asagidaki
`test_K4_*` bekcisi o kapinin bugunku halini ISIMLE cakar.

Bekcilerin ayrisimi:
* `test_K4_*`        — kapinin VARLIGI (matris okunur, DB'siz)
* `test_K3_*`        — zarf kurulumunun TEK yeri (AST, DB'siz)
* `test_zarflar_*`   — zarf hâlâ dogru mu (GERCEK uctan)
* `test_VERI_VARKEN_*` — 🔑 en guclusu: veri kurulur, zarf yine de BOS kalir
"""

import ast
import pathlib
from decimal import Decimal

from sqlalchemy import func, select

from app.core.access import AccessLevel
from app.modules.boq import schemas as boq_schemas
from app.modules.boq import service as boq_service
from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import ProjectContract
from app.modules.roles.models import Role
from app.modules.sites.models import Site

_BOQ_KOKU = pathlib.Path(boq_service.__file__).resolve().parent
_ZARF_ADLARI = {"MetricPlaceholder"}
_IZINLI_DOSYA = "service.py"


async def _login_with_access(client, session, user_factory, role_key: str, email: str) -> str:
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    user.all_projects = True
    await session.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# K4 — IZIN KAPISI (bagli olmamanin GERCEK sebebi)
# --------------------------------------------------------------------------- #


def test_K4_boq_okuyup_contracts_okuyamayan_roller_HÂLÂ_VAR():
    """🔴 `contract_total` / `revision_total` bu yuzden bagli DEGIL.

    `boq` satirini okuyabilen ama `contracts` satirinda `none` olan her rol,
    bir baglama sonrasinda isveren sozlesmesinin BEDELINI BOQ ekranindan
    okurdu — `contracts` kapisi hic calismadan. Bugun iki rol boyle:
    `site_chief` (boq=view/limited) ve `procurement` (boq=view/limited).

    Bu kume BOSALIRSA denetimin gerekcesi curur ve alan baglanabilir hâle
    gelir; bekci tam o an haber verir.
    """
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER

    sizdiran = {
        rol
        for sira, rol in enumerate(ROLE_ORDER)
        if MATRIX["boq"][sira][0] is not AccessLevel.none
        and MATRIX["contracts"][sira][0] is AccessLevel.none
    }

    assert sizdiran == {"site_chief", "procurement"}, (
        f"boq/contracts izin ayrismasi DEGISTI: {sorted(sizdiran)} — "
        "P-YT3'un `contract_total`/`revision_total` gerekcesi bu kumeye dayanir (K4)"
    )


def test_K4_boq_okuyup_progress_payments_okuyamayan_rol_HÂLÂ_VAR():
    """🔴 `realized_total` / `remaining_total` / `grand_progress_pct` /
    `BoqItemResponse.progress_pct` bu yuzden bagli DEGIL.

    `procurement` BOQ'yu okur (`view/limited`, 2026-07-30 kullanici karari:
    satinalma malzemeyi poz uzerinden alir) ama `progress_payments`ta `none`dur.
    Gerceklesen hakedis tutarini BOQ ekranindan basmak, satinalmaya isverene
    kesilen hakedisi ACARDI.
    """
    from app.modules.roles.seed_data import MATRIX, ROLE_ORDER

    sizdiran = {
        rol
        for sira, rol in enumerate(ROLE_ORDER)
        if MATRIX["boq"][sira][0] is not AccessLevel.none
        and MATRIX["progress_payments"][sira][0] is AccessLevel.none
    }

    assert sizdiran == {"procurement"}, (
        f"boq/progress_payments izin ayrismasi DEGISTI: {sorted(sizdiran)} — "
        "P-YT3'un dort hakedis zarfi bu kumeye dayanir (K4)"
    )


# --------------------------------------------------------------------------- #
# K3 — SIFIR KALINTI (yapisal, DB'siz)
# --------------------------------------------------------------------------- #


def _zarf_kurulan_dosyalar() -> dict[str, list[int]]:
    """`boq/` altinda `MetricPlaceholder(...)` CAGRISI yapan dosya + satirlar.

    AST kullanilir, `grep` degil: docstring'deki ya da yorumdaki bir ornek
    `grep`i yaniltir — bu dosyanin KENDISI de dahil (P-YT2 dersi).
    """
    bulgular: dict[str, list[int]] = {}
    for yol in sorted(_BOQ_KOKU.rglob("*.py")):
        agac = ast.parse(yol.read_text(encoding="utf-8"))
        for dugum in ast.walk(agac):
            if not isinstance(dugum, ast.Call):
                continue
            hedef = dugum.func
            ad = hedef.id if isinstance(hedef, ast.Name) else getattr(hedef, "attr", None)
            if ad in _ZARF_ADLARI:
                bulgular.setdefault(yol.name, []).append(dugum.lineno)
    return bulgular


def test_K3_zarf_YALNIZ_service_icinde_kurulur():
    bulgular = _zarf_kurulan_dosyalar()

    assert set(bulgular) == {_IZINLI_DOSYA}, (
        f"yer tutucu zarfi service.py DISINDA kuruluyor: {bulgular} — "
        "anahtarlar iki yerde yasarsa zamanla ayrisir (K3)"
    )


def test_K3_para_yuvarlamasi_boq_icinde_TEK_KOPYADIR():
    """🔴 P-YT2 `Section.budget`i "para formulu tek kopya degil" diye (C)
    birakti. O gerekcenin gecerli kalmasi icin carpimin/yuvarlamanin `boq/`
    icinde TEK tanimi olmalidir — ikinci bir `_quantize_money`, bolum bedelini
    ileride baglayacak dilime IKI aday sunardi.
    """
    tanimlar: dict[str, list[str]] = {}
    for yol in sorted(_BOQ_KOKU.rglob("*.py")):
        agac = ast.parse(yol.read_text(encoding="utf-8"))
        for dugum in agac.body:
            if isinstance(dugum, ast.FunctionDef) and dugum.name in {
                "quantize_money",
                "_quantize_money",
            }:
                tanimlar.setdefault(dugum.name, []).append(yol.name)
            if isinstance(dugum, ast.Assign) and any(
                isinstance(hedef, ast.Name) and hedef.id == "_MONEY" for hedef in dugum.targets
            ):
                tanimlar.setdefault("_MONEY", []).append(yol.name)

    assert tanimlar == {"quantize_money": ["schemas.py"], "_MONEY": ["schemas.py"]}, (
        f"para yuvarlamasi `boq/` icinde birden fazla yerde tanimli: {tanimlar} (K3)"
    )


def test_anahtarlarin_HEPSI_CANLI_bir_izin_modulunu_adlandirir():
    """🔴 P-YT1'in bulduğu sinif kusuru `boq`da ISIMLE cakilir: zarf *"bu modul
    henuz yok"* demek icin tasarlandi, ama iki anahtar da CANLI bir modulu
    adlandiriyor. Yani bekleyen sey MODUL degil, o modulden turemesi gereken
    DEGERIN izin kapisidir.
    """
    from app.modules.roles.seed_data import MODULES

    modul_anahtarlari = {modul["key"] for modul in MODULES}
    kullanilan = {boq_service._CONTRACTS, boq_service._PROGRESS_PAYMENTS}

    assert kullanilan == {"contracts", "progress_payments"}
    assert kullanilan <= modul_anahtarlari, (
        f"`boq` yer tutucu anahtari artik canli bir modulu adlandirmiyor: "
        f"{sorted(kullanilan - modul_anahtarlari)}"
    )


# --------------------------------------------------------------------------- #
# Zarflar HÂLÂ dogru basiliyor mu (GERCEK uctan)
# --------------------------------------------------------------------------- #


async def _bos_boq(seeded_db, project_factory, kod: str) -> Site:
    project = await project_factory(kod)
    site = Site(project_id=project.id, code=f"{kod}-A", name="A-Blok")
    seeded_db.add(site)
    await seeded_db.flush()
    grup = BoqGroup(site_id=site.id, name="TOPRAK VE TEMEL")
    seeded_db.add(grup)
    await seeded_db.flush()
    seeded_db.add(
        BoqItem(
            site_id=site.id,
            group_id=grup.id,
            code="15.150.1002",
            description="Beton",
            unit="m³",
            quantity=Decimal("100.000"),
            unit_price=Decimal("2500.00"),
        )
    )
    await seeded_db.flush()
    return site


async def test_zarflar_DORDU_BOS_IKISI_BAGLI__anahtar_ve_durum_alan_alan(
    client, seeded_db, user_factory, project_factory
):
    """🔴 `available is False` TEK BASINA zayif iddiadir (alan zaten oyle dogar)
    — her satirda ANAHTAR da ELLE yazilir. Anahtari degistirmek bir SOZLESME
    degisikligidir ve tam burada kirilir.

    🔴 ALTI ALAN AYRI AYRI DEGIL, **TAM KUME** olarak karsilastirilir: art arda
    dizilmis alti `assert`te ilki kirilirsa digerleri HIC KOSMAZ ve denetim
    tablosunun yalniz bir satiri gorunur. Tek karsilastirma alti sapmayi da
    ayni anda basar.

    ⚠️ **ILR-1'DE DEGISTI (2026-08-27) — SILINMEDI, KAPSAMI DARALDI.** Eski adi
    *"ALTISI DE BOS"*ti ve alti zarfin da yer tutucu kalmasini cakiyordu. Iki
    ilerleme zarfi (`progress_pct`, `grand_progress_pct`) ARTIK BAGLI; geri
    kalan DORT HAKEDIS/SOZLESME zarfinin gerekcesi (K4) AYNEN gecerlidir ve
    burada cakili kalir. Yani iddia zayiflamadi, IKIYE AYRILDI.

    🔴 Bu yanit `patron` rolundendir — yani IZINLI dal. Izinsiz dalin karsit
    kanidi `test_ilr_ilerleme.py`dedir; tek yon yazmak (K-IKIZ1) her role bos
    donduren bozuk bir kodu yesil gecirirdi.
    """
    site = await _bos_boq(seeded_db, project_factory, "YT3-1")
    headers = await _login_with_access(client, seeded_db, user_factory, "patron", "yt3a@t.co")

    resp = await client.get(f"/sites/{site.id}/boq", headers=headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    totals = govde["totals"]
    kalem = govde["groups"][0]["items"][0]

    pp = kalem["progress_pct"]
    olculen = {"progress_pct": (pp["available"], pp["pending_module"])}
    for alan in (
        "contract_total",
        "realized_total",
        "remaining_total",
        "revision_total",
        "grand_progress_pct",
    ):
        olculen[alan] = (totals[alan]["available"], totals[alan]["pending_module"])

    assert olculen == {
        # ✅ ILR-1'DE BAGLANDI — kaynak GUNLUK. Bos BOQ'da uretim yok, yuzde 0,00.
        "progress_pct": (True, None),
        # (C) TUZAK — sozlesme bedeli hesaplanabilir; site_chief/procurement gormemeli
        "contract_total": (False, "contracts"),
        "realized_total": (False, "progress_payments"),
        "remaining_total": (False, "progress_payments"),
        # (B) GECERLI — repoda REVIZYON KAVRAMI HIC YOK; modul canli, kaynak degil
        "revision_total": (False, "contracts"),
        # ✅ ILR-1'DE BAGLANDI.
        "grand_progress_pct": (True, None),
    }, "P-YT3 siniflandirma tablosu KAYDI — schemas.BoqTotals docstring'i bayatladi"

    assert govde["totals"]["grand_total"] == "250000.00", (
        "GERCEK olan tek toplam bozulmus — 100.000 × 2.500,00"
    )


async def test_VERI_VARKEN_DE_zarflar_BOS_KALIR(client, seeded_db, user_factory, project_factory):
    """🔑 DENETIMIN EN GUCLU BEKCISI.

    Sozlesme kalemi + ona BAGLI BOQ satiri + ONAYLANMIS hakedis satiri kurulur.
    Yani `contract_total` (5.000 × 3.000 = 15.000.000) ve `realized_total`
    (40 × 3.000 = 120.000) ISTENSE HESAPLANABILIRDI. Zarflar yine de BOS
    doner — cunku engel veri degil **izin kapisidir (K4)**.

    Bu bekci olmadan, ileride biri "zaten veri yok" sanip alani sessizce
    baglayabilirdi.

    ⚠️ **ILR-1'DE DEGISTI — IDDIA CIFT YONLU OLDU.** Ayni yanitta HAKEDIS
    zarflari BOS kalirken ILERLEME zarflari DOLU olmalidir. Eski hâli yalniz
    "hepsi bos" diyordu; o hâliyle her zarfi bos donduren bir kod da gecerdi.
    Iki yon birlikte cakilir.
    """
    project = await project_factory("YT3-2")
    seeded_db.add(ProjectContract(project_id=project.id, amount=Decimal("15000000.00")))
    site = Site(project_id=project.id, code="YT3-2-A", name="A-Blok")
    seeded_db.add(site)
    await seeded_db.flush()

    sozlesme_grup = EmployerContractGroup(project_id=project.id, name="TOPRAK")
    seeded_db.add(sozlesme_grup)
    await seeded_db.flush()
    sozlesme_kalemi = EmployerContractItem(
        project_id=project.id,
        group_id=sozlesme_grup.id,
        code="15.150.1002",
        description="Beton",
        unit="m³",
        quantity=Decimal("5000.000"),
        unit_price=Decimal("3000.00"),
    )
    seeded_db.add(sozlesme_kalemi)
    await seeded_db.flush()

    boq_grup = BoqGroup(site_id=site.id, name="TOPRAK")
    seeded_db.add(boq_grup)
    await seeded_db.flush()
    seeded_db.add(
        BoqItem(
            site_id=site.id,
            group_id=boq_grup.id,
            code="15.150.1002",
            description="Beton",
            unit="m³",
            quantity=Decimal("1000.000"),
            unit_price=Decimal("3000.00"),
            contract_item_id=sozlesme_kalemi.id,  # 🔑 BAG KURULDU
        )
    )
    aktor = await user_factory(email="yt3-hk@t.co", password="parola1234", role_key="patron")
    hakedis = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.approved,
        vat_pct=Decimal("20.00"),
        advance_pct=Decimal("0.00"),
        retainage_pct=Decimal("0.00"),
        created_by=aktor.id,
    )
    seeded_db.add(hakedis)
    await seeded_db.flush()
    seeded_db.add(
        ProgressPaymentLine(
            payment_id=hakedis.id,
            contract_item_id=sozlesme_kalemi.id,
            site_id=site.id,
            code="15.150.1002",
            description="Beton",
            unit="m³",
            contract_unit_price=Decimal("3000.00"),
            quantity=Decimal("40.000"),
        )
    )
    await seeded_db.flush()

    # 🔴 POZITIF KONTROL — bekci once GIRDISININ BOS OLMADIGINI kanitlar.
    # Bu iki iddia olmadan test, kurulum sessizce hic yazmamis olsa da yesil
    # kalirdi ve "veri varken de bos" cumlesi hicbir sey soylemezdi.
    assert (
        await seeded_db.scalar(
            select(func.count())
            .select_from(ProgressPaymentLine)
            .where(ProgressPaymentLine.site_id == site.id)
        )
    ) == 1, "kurulum hakedis satirini yazmamis — asagidaki iddia BOSA koser"
    assert (
        await seeded_db.scalar(
            select(func.count())
            .select_from(BoqItem)
            .where(BoqItem.site_id == site.id, BoqItem.contract_item_id.is_not(None))
        )
    ) == 1, "BOQ satiri sozlesme kalemine BAGLANMAMIS — birlestirme anahtari yok"

    headers = await _login_with_access(client, seeded_db, user_factory, "patron", "yt3b@t.co")
    govde = (await client.get(f"/sites/{site.id}/boq", headers=headers)).json()

    kalem = govde["groups"][0]["items"][0]
    # TAM KUME karsilastirmasi (art arda `assert` maskelemesi yok): veri varken
    # DOLAN her alan tek seferde gorunur.
    # ⚠️ ILR-1: `grand_progress_pct` ve `progress_pct` bu KUMEDEN CIKARILDI —
    # ikisi de artik BAGLI ve dolu olmalari BEKLENIR. Kumede kalan DORDU
    # hakedis/sozlesme zarflaridir; K4 gerekcesi yalniz onlar icin gecerlidir.
    dolanlar = {
        alan: govde["totals"][alan]
        for alan in ("contract_total", "realized_total", "remaining_total", "revision_total")
        if govde["totals"][alan]["available"] or govde["totals"][alan]["value"] is not None
    }

    assert dolanlar == {}, (
        f"veri KURULUYKEN su HAKEDIS zarflari doldu: {sorted(dolanlar)} — engel veri "
        "degil izin kapisidir (K4); baglama karari once matris ayrismasini kapatmalidir"
    )

    # 🔴 KARSIT KANIT (K-IKIZ1): ayni yanitta ILERLEME zarflari DOLU olmalidir.
    # Bu iki iddia olmadan, HER zarfi bos donduren bozuk bir kod da yesil gecerdi
    # ve yukaridaki "hakedis zarflari bos" cumlesi hicbir sey bekcilemezdi.
    assert (kalem["progress_pct"]["available"], kalem["progress_pct"]["pending_module"]) == (
        True,
        None,
    ), "ILR-1 fiziksel ilerleme BAGLI degil — bu yanit izinli (`patron`) roldendir"
    assert govde["totals"]["grand_progress_pct"]["available"] is True


async def test_sozlesme_BEDELI_boq_yanitinda_HICBIR_ALANDA_gecmez(
    client, seeded_db, user_factory, project_factory
):
    """🔴 Yalnizca zarflara bakmak yetmez: bedel BASKA bir alandan da sizabilir.

    Sozlesme kaleminin birim fiyati 3.000,00 ama BOQ satirininki 1.250,00.
    Yanitta 3.000,00 GECMEMELIDIR — gecerse bir yerde sozlesme otoritesi
    kopyalanmis demektir.
    """
    project = await project_factory("YT3-3")
    seeded_db.add(ProjectContract(project_id=project.id, amount=Decimal("15000000.00")))
    site = Site(project_id=project.id, code="YT3-3-A", name="A-Blok")
    seeded_db.add(site)
    await seeded_db.flush()
    grup_s = EmployerContractGroup(project_id=project.id, name="TOPRAK")
    seeded_db.add(grup_s)
    await seeded_db.flush()
    kalem_s = EmployerContractItem(
        project_id=project.id,
        group_id=grup_s.id,
        code="15.150.1002",
        description="Beton",
        unit="m³",
        quantity=Decimal("5000.000"),
        unit_price=Decimal("3000.00"),
    )
    seeded_db.add(kalem_s)
    await seeded_db.flush()
    grup_b = BoqGroup(site_id=site.id, name="TOPRAK")
    seeded_db.add(grup_b)
    await seeded_db.flush()
    seeded_db.add(
        BoqItem(
            site_id=site.id,
            group_id=grup_b.id,
            code="15.150.1002",
            description="Beton",
            unit="m³",
            quantity=Decimal("10.000"),
            unit_price=Decimal("1250.00"),
            contract_item_id=kalem_s.id,
        )
    )
    await seeded_db.flush()

    headers = await _login_with_access(client, seeded_db, user_factory, "patron", "yt3c@t.co")
    ham = (await client.get(f"/sites/{site.id}/boq", headers=headers)).text

    assert "3000.00" not in ham, "sozlesme birim fiyati BOQ yanitina sizdi"
    assert "12500.00" in ham, "BOQ'nun KENDI tutari (10 × 1.250,00) kaybolmus"


async def test_boq_ucunu_okuyan_procurement_rolu_HÂLÂ_200_alir(
    client, seeded_db, user_factory, project_factory
):
    """K4 gerekcesinin ONKOSULU: `procurement` BOQ'yu gercekten okuyabiliyor.

    Okuyamiyor olsaydi izin genislemesi argumani cokerdi — bu bekci gerekcenin
    ayagini yere basmasini saglar.
    """
    site = await _bos_boq(seeded_db, project_factory, "YT3-4")
    rol = (await seeded_db.execute(select(Role).where(Role.key == "procurement"))).scalar_one()
    assert rol is not None
    headers = await _login_with_access(client, seeded_db, user_factory, "procurement", "yt3d@t.co")

    resp = await client.get(f"/sites/{site.id}/boq", headers=headers)

    assert resp.status_code == 200, (
        "satinalma BOQ'yu okuyamiyor — K4 gerekcesi yeniden olculmelidir"
    )
    assert resp.json()["totals"]["contract_total"]["available"] is False


def test_semada_ALTI_zarf_alani_vardir_ve_HEPSI_ZORUNLUDUR():
    """Sayim bekcisi: yedinci bir zarf eklenirse (ya da biri baglanip
    kaldirilirsa) denetim tablosu bayatlar.

    🔴 `default_factory` YOKTUR ve olmamalidir: varsayilan bir zarf, cagiran
    unuttugunda sessizce "modul bekleniyor" basar (sales'te fiilen bu oldu).
    """
    zarf_alanlari = {
        ad
        for ad, alan in boq_schemas.BoqTotals.model_fields.items()
        if alan.annotation is boq_schemas.MetricPlaceholder
    } | {
        ad
        for ad, alan in boq_schemas.BoqItemResponse.model_fields.items()
        if alan.annotation is boq_schemas.MetricPlaceholder
    }

    assert zarf_alanlari == {
        "progress_pct",
        "contract_total",
        "realized_total",
        "remaining_total",
        "revision_total",
        "grand_progress_pct",
    }, f"`boq` zarf alanlari degisti: {sorted(zarf_alanlari)}"

    for model in (boq_schemas.BoqTotals, boq_schemas.BoqItemResponse):
        for ad in zarf_alanlari & set(model.model_fields):
            assert model.model_fields[ad].is_required(), (
                f"{model.__name__}.{ad} varsayilan kazandi — cagiran unutursa sessiz bos zarf"
            )
