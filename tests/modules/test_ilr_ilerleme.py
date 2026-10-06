"""ILR-1/2 — FIZIKSEL + MALI ilerlemenin bekcileri.

Dilimin iddiasi tek cumleye siger: *"ilerleme, GONDERILMIS santiye gunlugunden
PARA AGIRLIKLI turer; mali ilerleme ise ONAYLANMIS ISVEREN hakedisinden gelir
ve ikisi KASTEN ayrisir."* Asagidaki her bekci o cumlenin bir parcasini
mutasyona karsi savunur.

BOLUMLER
--------
A. FORMUL — para agirligi (M1 mutantinin tek gercek bekcisi)
B. K-IKIZ1 KARSIT KANIT — neyin GIRMEDIGI **ve** neyin GERCEKTEN oynattigi
C. KUME — yuzdenin EVRENI bagimsiz kaynaktan (ham SQL) dogrulanir
D. PAYDA — bolum tahsisi ↔ santiye kotasi ↔ payda 0
E. IZIN — her biri CIFT YONLU (izinli DOLAR ↔ izinsiz KISITLI)
F. MALI ↔ FIZIKSEL ayrisimi
G. BESINCI YUZEY — santiye karti (`physical_for_sites`; mutasyon denetiminden dogdu)

🔴 IZIN BEKCILERI NICIN CIFT YONLU: tek yon yazilsaydi ("izinsiz rolde bos")
her role bos donen bozuk bir kod da yesil gecerdi. Iki yon AYNI testte olculur.
"""

from decimal import Decimal

from sqlalchemy import text

from app.core.access import AccessLevel
from app.core.discipline_scope import UNRESTRICTED
from app.modules.boq import progress
from app.modules.progress_payments import project_progress
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.site_diary.models import DiaryStatus
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from . import _ilr

# Canlidaki gercek sekil (`A1 · Kenar Ayak`): m² ile ton'un ortalamasi alinamaz.
_FILIZ = {"quantity": "7440", "unit_price": "412.50", "unit": "adet"}
_DEMIR = {"quantity": "98", "unit_price": "96250.00", "unit": "ton"}


async def _canli_sekil(seeded_db, project_factory, user_factory, kod: str):
    """Iki pozlu SANTIYE + tek gonderilmis gunluk (900 filiz · 12 ton demir)."""
    project = await project_factory(kod)
    site = await _ilr.santiye(seeded_db, project, code=f"{kod}-A")
    boq_grup = await _ilr.grup(seeded_db, site)
    filiz = await _ilr.poz(seeded_db, site, boq_grup, "18.185.1001", **_FILIZ)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    yazan = await _ilr.aktor(seeded_db, user_factory, f"{kod.lower()}-yazan@ilr.co")
    await _ilr.gunluk(seeded_db, site, yazan, [(filiz, "900"), (demir, "12")])
    return project, site, filiz, demir, yazan


# --------------------------------------------------------------------------- #
# A. FORMUL — PARA AGIRLIGI
# --------------------------------------------------------------------------- #


async def test_para_agirlikli_yuzde_MIKTAR_ORTALAMASINDAN_FARKLIDIR(
    seeded_db, project_factory, user_factory
):
    """🔑 M1 MUTANTININ BEKCISI — emrin en kritik kabul kriteri.

    Canli sekil: 7.440 adet filiz ekimi (412,50 ₺) + 98 ton demir (96.250 ₺);
    gerceklesen 900 adet ve 12 ton.

        PAY   = 900 × 412,50 + 12 × 96.250,00 =  1.526.250,00
        PAYDA = 7.440 × 412,50 + 98 × 96.250,00 = 12.501.500,00
        %     = 12,21

    Duz miktar orani ( agirliksiz) 912/7.538 = **%12,10**, oran ortalamasi ise
    **%12,17** verir. Ucu de "makul" gorunur — iste sessiz sacmalama budur.
    Bu yuzden yalniz DOGRU sayi degil, otekilerin FARKLI oldugu da cakilir.
    """
    _, site, _, _, _ = await _canli_sekil(seeded_db, project_factory, user_factory, "ILR-A1")

    olculen = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    duz_miktar_orani = progress.quantize_pct(
        (Decimal("900") + Decimal("12")) / (Decimal("7440") + Decimal("98")) * Decimal("100")
    )
    oran_ortalamasi = progress.quantize_pct(
        (Decimal("900") / Decimal("7440") + Decimal("12") / Decimal("98"))
        / Decimal("2")
        * Decimal("100")
    )

    assert olculen == Decimal("12.21"), (
        f"para agirlikli yuzde bozuldu: {olculen} — PAY 1.526.250,00 / PAYDA 12.501.500,00"
    )
    assert (duz_miktar_orani, oran_ortalamasi) == (Decimal("12.10"), Decimal("12.17"))
    assert olculen not in {duz_miktar_orani, oran_ortalamasi}, (
        "agirliksiz hesap AYNI sayiyi veriyor — bu fikstur para agirligini ARTIK "
        "bekcilemiyor, miktar/fiyat degerleri ayristirilmalidir"
    )


# --------------------------------------------------------------------------- #
# B. K-IKIZ1 — KARSIT KANIT
# --------------------------------------------------------------------------- #


async def test_TASLAK_gunluk_yuzdeye_GIRMEZ__GONDERILINCE_oynar(
    seeded_db, project_factory, user_factory
):
    """Iki yon AYNI testte: taslakken 0, gonderilince 12,21.

    Tek yon ("taslak girmez") yazilsaydi, HER gunlugu eleyen bozuk bir suzgec
    de yesil gecerdi.
    """
    project = await project_factory("ILR-B1")
    site = await _ilr.santiye(seeded_db, project)
    boq_grup = await _ilr.grup(seeded_db, site)
    filiz = await _ilr.poz(seeded_db, site, boq_grup, "18.185.1001", **_FILIZ)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    yazan = await _ilr.aktor(seeded_db, user_factory, "b1@ilr.co")
    entry = await _ilr.gunluk(
        seeded_db, site, yazan, [(filiz, "900"), (demir, "12")], status=DiaryStatus.draft
    )

    taslakken = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    entry.status = DiaryStatus.submitted
    await seeded_db.flush()
    gonderilince = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    assert (taslakken, gonderilince) == (Decimal("0.00"), Decimal("12.21")), (
        f"taslak/gonderilmis ayrimi bozuldu: {taslakken} → {gonderilince}"
    )


async def test_BASKA_santiyenin_gonderilmis_gunlugu_yuzdeye_GIRMEZ(
    seeded_db, project_factory, user_factory
):
    """Kapsam suzgeci: baska PROJENIN baska SANTIYESINDE ayni kodlu pozlara
    gonderilmis gunluk yazilir; olculen santiyenin yuzdesi KIMILDAMAZ."""
    _, site, _, _, _ = await _canli_sekil(seeded_db, project_factory, user_factory, "ILR-B2")
    once = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    komsu_proje = await project_factory("ILR-B2X")
    komsu = await _ilr.santiye(seeded_db, komsu_proje, code="X-BLOK")
    komsu_grup = await _ilr.grup(seeded_db, komsu)
    komsu_filiz = await _ilr.poz(seeded_db, komsu, komsu_grup, "18.185.1001", **_FILIZ)
    komsu_yazan = await _ilr.aktor(seeded_db, user_factory, "b2x@ilr.co")
    await _ilr.gunluk(seeded_db, komsu, komsu_yazan, [(komsu_filiz, "7440")])

    sonra = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)
    komsu_yuzdesi = await progress.physical_for_site(seeded_db, komsu.id, scope=UNRESTRICTED)

    assert (once, sonra) == (Decimal("12.21"), Decimal("12.21")), (
        f"baska santiyenin gunlugu yuzdeye sizdi: {once} → {sonra}"
    )
    # POZITIF KONTROL: komsu uretim GERCEKTEN yazildi (yoksa iddia bosa koser).
    assert komsu_yuzdesi == Decimal("100.00"), (
        f"komsu santiyeye yazilan uretim hic islenmemis ({komsu_yuzdesi}) — "
        "yukaridaki 'sizmadi' iddiasi hicbir sey kanitlamiyor"
    )


async def test_GERCEK_uretim_yuzdeyi_SIFIRDAN_YUKARI_OYNATIR(
    seeded_db, project_factory, user_factory
):
    """🔴 Bu bekci olmadan HER ZAMAN 0 donduren bozuk kod da yesil gecerdi."""
    project = await project_factory("ILR-B3")
    site = await _ilr.santiye(seeded_db, project)
    boq_grup = await _ilr.grup(seeded_db, site)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    yazan = await _ilr.aktor(seeded_db, user_factory, "b3@ilr.co")

    uretimsiz = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)
    await _ilr.gunluk(seeded_db, site, yazan, [(demir, "49")])
    uretimli = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    assert (uretimsiz, uretimli) == (Decimal("0.00"), Decimal("50.00")), (
        f"uretim yuzdeyi oynatmadi: {uretimsiz} → {uretimli} (49/98 = %50)"
    )


async def test_boq_bagi_KOPMUS_satir_yuzdeye_GIRMEZ(seeded_db, project_factory, user_factory):
    """`boq_item_id IS NULL` satir (poz silinmis, `SET NULL`) hangi poza
    yazilacagini BILEMEZ — sayilamaz."""
    project = await project_factory("ILR-B4")
    site = await _ilr.santiye(seeded_db, project)
    boq_grup = await _ilr.grup(seeded_db, site)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    yazan = await _ilr.aktor(seeded_db, user_factory, "b4@ilr.co")
    await _ilr.gunluk(seeded_db, site, yazan, [(demir, "49"), (None, "1000000")])

    olculen = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    kopuk_sayisi = await seeded_db.scalar(
        text("SELECT count(*) FROM site_diary_lines WHERE boq_item_id IS NULL")
    )
    assert kopuk_sayisi == 1, "bagi kopmus satir kurulmamis — iddia bosa koser"
    assert olculen == Decimal("50.00"), (
        f"bagi kopmus satir yuzdeye girdi: {olculen} (beklenen 49/98 = %50)"
    )


async def test_BOLUM_ETIKETSIZ_gunluk_SANTIYEYE_girer_HICBIR_BOLUME_girmez(
    seeded_db, project_factory, user_factory
):
    """`SiteDiaryEntry.section_id` NULL: uretimin hangi bolume ait oldugu BEYAN
    EDILMEMISTIR. Santiye yuzdesine girer, bolum yuzdesine GIRMEZ."""
    project = await project_factory("ILR-B5")
    site = await _ilr.santiye(seeded_db, project)
    section = await _ilr.bolum(seeded_db, site)
    boq_grup = await _ilr.grup(seeded_db, site)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    await _ilr.tahsis(seeded_db, demir, section, "98")
    yazan = await _ilr.aktor(seeded_db, user_factory, "b5@ilr.co")
    await _ilr.gunluk(seeded_db, site, yazan, [(demir, "49")], section=None)

    santiye_yuzdesi = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)
    bolum_yuzdesi = await progress.physical_for_section(seeded_db, section.id, scope=UNRESTRICTED)
    toplu = await progress.physical_for_sections(seeded_db, [section.id], scope=UNRESTRICTED)

    assert (santiye_yuzdesi, bolum_yuzdesi, toplu[section.id]) == (
        Decimal("50.00"),
        Decimal("0.00"),
        Decimal("0.00"),
    ), f"etiketsiz gunlugun kirilimi bozuldu: santiye={santiye_yuzdesi} bolum={bolum_yuzdesi}"


# --------------------------------------------------------------------------- #
# C. KUME — sahte-yesilin 8. hâli
# --------------------------------------------------------------------------- #


async def test_yuzdenin_EVRENI_bagimsiz_kaynaktan_dogrulanir(
    seeded_db, project_factory, user_factory
):
    """🔑 Tek bir "%" sayisi, YANLIS bir evrenden de dogru cikabilir.

    Bu yuzden karsilastirilan sey sayi degil, **(poz kodu, gerceklesen miktar,
    para agirligi)** uclusunun TAM KUMESIDIR. Beklenen kume fiksturun kendi
    degiskenlerinden DEGIL, dogrudan HAM SQL'den turetilir — yani ayni hatayi
    iki kez yapma ihtimali kesilir. Tek karsilastirma: art arda `assert` dizmek
    ilk sapmadan sonrasini gizlerdi.
    """
    project = await project_factory("ILR-C1")
    site = await _ilr.santiye(seeded_db, project)
    boq_grup = await _ilr.grup(seeded_db, site)
    filiz = await _ilr.poz(seeded_db, site, boq_grup, "18.185.1001", **_FILIZ)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    bos = await _ilr.poz(
        seeded_db, site, boq_grup, "99.999.9999", quantity="10", unit_price="1000.00"
    )
    yazan = await _ilr.aktor(seeded_db, user_factory, "c1@ilr.co")
    # Ayni poza IKI gonderilmis gunluk (toplanmali), bir TASLAK (girmemeli),
    # bir de bagi KOPMUS satir (girmemeli).
    await _ilr.gunluk(seeded_db, site, yazan, [(filiz, "500"), (demir, "12")])
    await _ilr.gunluk(
        seeded_db, site, yazan, [(filiz, "400")], tarih=_ilr.sonraki_gun(_ilr.VARSAYILAN_TARIH, 1)
    )
    await _ilr.gunluk(
        seeded_db,
        site,
        yazan,
        [(bos, "10"), (None, "77")],
        tarih=_ilr.sonraki_gun(_ilr.VARSAYILAN_TARIH, 2),
        status=DiaryStatus.draft,
    )

    ham = await seeded_db.execute(
        text(
            """
            SELECT bi.code, SUM(sdl.quantity), SUM(sdl.quantity) * bi.unit_price
            FROM site_diary_lines sdl
            JOIN site_diary_entries sde ON sde.id = sdl.entry_id
            JOIN boq_items bi ON bi.id = sdl.boq_item_id
            WHERE sde.status = 'submitted' AND bi.site_id = :site_id
            GROUP BY bi.code, bi.unit_price
            """
        ),
        {"site_id": site.id},
    )
    beklenen = {(kod, Decimal(miktar), Decimal(agirlik)) for kod, miktar, agirlik in ham}

    pozlar = {p.id: p for p in (filiz, demir, bos)}
    gerceklesen = await progress.realized_by_item(seeded_db, list(pozlar))
    olculen = {
        (pozlar[pid].code, miktar, miktar * pozlar[pid].unit_price)
        for pid, miktar in gerceklesen.items()
    }

    assert beklenen == {
        ("18.185.1001", Decimal("900"), Decimal("371250.00")),
        ("15.185.1002", Decimal("12"), Decimal("1155000.00")),
    }, f"bagimsiz kaynagin kendisi beklenmedik: {sorted(beklenen)}"
    assert olculen == beklenen, (
        f"yuzdenin EVRENI bagimsiz kaynaktan ayrisiyor: olculen={sorted(olculen)} "
        f"beklenen={sorted(beklenen)}"
    )


# --------------------------------------------------------------------------- #
# D. PAYDA
# --------------------------------------------------------------------------- #


async def test_BOLUM_paydasi_TAHSISTIR_santiye_kotasi_DEGIL(
    seeded_db, project_factory, user_factory
):
    """🔑 M3 MUTANTININ BEKCISI. Santiye kotasi 1.200, boluma tahsis 400, o
    bolumde 400 imal edildi → bolum **%100**tur, %33,33 DEGIL."""
    project = await project_factory("ILR-D1")
    site = await _ilr.santiye(seeded_db, project)
    section = await _ilr.bolum(seeded_db, site, "Kat 6-10")
    boq_grup = await _ilr.grup(seeded_db, site)
    beton = await _ilr.poz(
        seeded_db, site, boq_grup, "16.058.1001", quantity="1200", unit_price="2500.00", unit="m3"
    )
    await _ilr.tahsis(seeded_db, beton, section, "400")
    yazan = await _ilr.aktor(seeded_db, user_factory, "d1@ilr.co")
    await _ilr.gunluk(seeded_db, site, yazan, [(beton, "400")], section=section)

    bolum_yuzdesi = await progress.physical_for_section(seeded_db, section.id, scope=UNRESTRICTED)
    toplu = await progress.physical_for_sections(seeded_db, [section.id], scope=UNRESTRICTED)
    santiye_yuzdesi = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)

    assert (bolum_yuzdesi, toplu[section.id]) == (Decimal("100.00"), Decimal("100.00")), (
        f"bolum paydasi TAHSIS degil: {bolum_yuzdesi} (kota paydasi %33,33 verirdi)"
    )
    assert santiye_yuzdesi == Decimal("33.33"), (
        f"santiye paydasi bolum tahsisine kaymis: {santiye_yuzdesi} (400/1.200 beklenir)"
    )


async def test_SANTIYE_paydasi_SANTIYE_BOQUDUR(seeded_db, project_factory, user_factory):
    """Bir bolume HIC tahsis edilmemis poz bile santiye paydasindadir."""
    project = await project_factory("ILR-D2")
    site = await _ilr.santiye(seeded_db, project)
    section = await _ilr.bolum(seeded_db, site)
    boq_grup = await _ilr.grup(seeded_db, site)
    tahsisli = await _ilr.poz(
        seeded_db, site, boq_grup, "16.058.1001", quantity="100", unit_price="1000.00"
    )
    tahsissiz = await _ilr.poz(
        seeded_db, site, boq_grup, "16.058.1002", quantity="100", unit_price="1000.00"
    )
    await _ilr.tahsis(seeded_db, tahsisli, section, "100")
    yazan = await _ilr.aktor(seeded_db, user_factory, "d2@ilr.co")
    await _ilr.gunluk(seeded_db, site, yazan, [(tahsisli, "100")], section=section)

    santiye_yuzdesi = await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED)
    bolum_yuzdesi = await progress.physical_for_section(seeded_db, section.id, scope=UNRESTRICTED)

    assert (santiye_yuzdesi, bolum_yuzdesi) == (Decimal("50.00"), Decimal("100.00")), (
        f"santiye paydasi BOQ'un tamami olmali: {santiye_yuzdesi} (100.000/200.000 = %50), "
        f"bolum paydasi tahsis olmali: {bolum_yuzdesi}"
    )
    assert tahsissiz.id is not None


async def test_PAYDA_SIFIRKEN_yuzde_YOKTUR__zarf_pending_module_TASIR(
    client, seeded_db, project_factory, user_factory
):
    """Payda 0 → yuzde YOKTUR (`None`). Zarf UCUNCU hâl DEGILDIR: rolun izni
    VARDIR, ortada olculecek is yoktur → `pending_module="site_diary"`.

    "0 %" basmak "hic is yok"u "hic ilerleme yok" sanmaktir; ikisi ayni sey
    degildir.
    """
    project = await project_factory("ILR-D3")
    site = await _ilr.santiye(seeded_db, project)
    section = await _ilr.bolum(seeded_db, site)
    await _ilr.grup(seeded_db, site)  # BOQ grubu var, POZ YOK → payda 0
    headers = await _ilr.login(client, seeded_db, user_factory, "patron", "d3@ilr.co")

    assert await progress.physical_for_site(seeded_db, site.id, scope=UNRESTRICTED) is None
    assert await progress.physical_for_section(seeded_db, section.id, scope=UNRESTRICTED) is None

    boq = (await client.get(f"/sites/{site.id}/boq", headers=headers)).json()
    bolumler = (await client.get(f"/sites/{site.id}/sections", headers=headers)).json()

    assert _ilr.zarf(boq["totals"]["grand_progress_pct"]) == (False, "site_diary"), (
        "payda 0 iken zarf UCUNCU hâle (izin yok) kaymis — kaynak yoklugu ile "
        "yetki yoklugu AYRI iki durumdur"
    )
    assert _ilr.zarf(bolumler["items"][0]["progress_pct"]) == (False, "site_diary")


# --------------------------------------------------------------------------- #
# E. IZIN — HER BIRI CIFT YONLU
# --------------------------------------------------------------------------- #


async def test_BOQ_ucunde_yuzde_IZINLIDE_DOLAR_IZINSIZDE_KISITLANIR(
    client, seeded_db, project_factory, user_factory
):
    """`patron` (site_diary=full) ↔ `procurement` (boq=view, site_diary=none).

    IKI zarf birden olculur: `BoqItemResponse.progress_pct` ve
    `BoqTotals.grand_progress_pct`.
    """
    _, site, _, _, _ = await _canli_sekil(seeded_db, project_factory, user_factory, "ILR-E1")
    izinli = await _ilr.login(client, seeded_db, user_factory, "patron", "e1a@ilr.co")
    izinsiz = await _ilr.login(client, seeded_db, user_factory, "procurement", "e1b@ilr.co")

    dolu = (await client.get(f"/sites/{site.id}/boq", headers=izinli)).json()
    kisitli_resp = await client.get(f"/sites/{site.id}/boq", headers=izinsiz)
    assert kisitli_resp.status_code == 200, kisitli_resp.text
    kisitli = kisitli_resp.json()

    dolu_kalemler = {k["code"]: k["progress_pct"] for k in dolu["groups"][0]["items"]}
    kisitli_kalemler = {
        k["code"]: _ilr.zarf(k["progress_pct"]) for k in kisitli["groups"][0]["items"]
    }

    assert _ilr.zarf(dolu["totals"]["grand_progress_pct"]) == (True, None)
    assert dolu["totals"]["grand_progress_pct"]["value"] == "12.21"
    assert dolu_kalemler["15.185.1002"]["value"] == "12.24", (
        f"poz yuzdesi bozuldu: {dolu_kalemler['15.185.1002']} (12/98 beklenir)"
    )
    # 🔴 IZINSIZ YON: UCUNCU hâl — `pending_module` TASIMAZ ("yetkin yok" ≠
    # "modul bekleniyor"). Kalem + toplam TEK kumede karsilastirilir.
    assert {
        "grand": _ilr.zarf(kisitli["totals"]["grand_progress_pct"]),
        **kisitli_kalemler,
    } == {
        "grand": (False, None),
        "18.185.1001": (False, None),
        "15.185.1002": (False, None),
    }, "gunlugu okuyamayan role yuzde sizdi ya da sahte gerekce basildi"


async def test_BOLUM_ucunde_yuzde_IZINLIDE_DOLAR_IZINSIZDE_KISITLANIR(
    client, seeded_db, project_factory, user_factory
):
    """`patron` ↔ `accounting` (sites=view, site_diary=none). Liste UCU **ve**
    detay UCU ayri kod yollaridir; ikisi de olculur."""
    project = await project_factory("ILR-E2")
    site = await _ilr.santiye(seeded_db, project)
    section = await _ilr.bolum(seeded_db, site)
    boq_grup = await _ilr.grup(seeded_db, site)
    demir = await _ilr.poz(seeded_db, site, boq_grup, "15.185.1002", **_DEMIR)
    await _ilr.tahsis(seeded_db, demir, section, "98")
    yazan = await _ilr.aktor(seeded_db, user_factory, "e2y@ilr.co")
    await _ilr.gunluk(seeded_db, site, yazan, [(demir, "49")], section=section)

    # 🔴 Bu test `site_diary` KAPISINI olcer, KAPSAM MASKESINI DEGIL. `accounting`
    #    seed'de `sites = _FIN` (finance) tasir ve `progress_pct` OPERASYONEL
    #    kovadadir (`sites/schemas.py`) — yani uc ucun ucu de maskeden `kisitli()`
    #    donerdi ve `site_diary` kapisi TAMAMEN kaldirilsa bile test YESIL kalirdi.
    #    OLCULDU (2026-09-19): `reads.py`deki iki `can_read` kapisi silinip test
    #    kosuldu, YESIL kaldi. Kapsam ACIKCA `all`a cekilir ki deneyin tek
    #    degiskeni izin hucresi olsun; hucrenin KENDISI de seed'e birakilmaz
    #    (`modul_duzeyi_yaz` kanonu: matris degisince test sessizce anlamsizlasmasin).
    await modul_duzeyi_yaz(seeded_db, "accounting", "sites", AccessLevel.view)
    await modul_duzeyi_yaz(seeded_db, "accounting", "site_diary", AccessLevel.none)

    izinli = await _ilr.login(client, seeded_db, user_factory, "patron", "e2a@ilr.co")
    izinsiz = await _ilr.login(client, seeded_db, user_factory, "accounting", "e2b@ilr.co")

    async def _olc(headers) -> dict:
        liste = (await client.get(f"/sites/{site.id}/sections", headers=headers)).json()
        detay = (await client.get(f"/sections/{section.id}", headers=headers)).json()
        santiye = (await client.get(f"/sites/{site.id}", headers=headers)).json()
        return {
            "liste": liste["items"][0]["progress_pct"],
            "detay": detay["progress_pct"],
            "santiye_detayi": santiye["sections"][0]["progress_pct"],
        }

    dolu = await _olc(izinli)
    kisitli = await _olc(izinsiz)

    assert {ad: (z["available"], z["value"], z["pending_module"]) for ad, z in dolu.items()} == {
        "liste": (True, "50.00", None),
        "detay": (True, "50.00", None),
        "santiye_detayi": (True, "50.00", None),
    }, f"izinli rolde bolum yuzdesi dolmadi: {dolu}"
    assert {ad: _ilr.zarf(z) for ad, z in kisitli.items()} == {
        "liste": (False, None),
        "detay": (False, None),
        "santiye_detayi": (False, None),
    }, f"gunlugu okuyamayan role bolum yuzdesi sizdi ya da sahte gerekce basildi: {kisitli}"


async def test_KART_fiziksel_ve_mali_AYRI_izinlere_bakar(
    client, seeded_db, project_factory, user_factory
):
    """🔴 Tek bir "ilerleme izni" YOKTUR. Uc rol, uc farkli kombinasyon:

    * `patron`         — gunluk ✓ hakedis ✓ → IKISI de dolu
    * `accounting`     — gunluk ✗ hakedis ✓ → fiziksel KISITLI, mali dolu
    * `field_engineer` — gunluk ✓ hakedis ✗ (matris testte kapatilir) →
      fiziksel dolu, mali KISITLI

    Ucuncu satir seed'de YOKTUR (olculdu: gunlugu okuyup hakedisi okuyamayan
    rol yok) ve tam da bu yuzden izin hucresi testte ACIKCA kurulur — yoksa
    ayrismanin ters yonu hic bekcilenmezdi.
    """
    project, site, _, _, yazan = await _canli_sekil(
        seeded_db, project_factory, user_factory, "ILR-E3"
    )
    kalem = await _ilr.isveren_kalemi(seeded_db, project, quantity="98", unit_price="96250.00")
    await _ilr.isveren_hakedisi(
        seeded_db, project, yazan, kalem, site, quantity="49", status=ProgressPaymentStatus.approved
    )
    # 🔴 KISITLI iki satir da KAPIDAN gecmeli, MASKEDEN DEGIL. `/projects`
    #    yaniti `projects` kapsamiyla maskelenir ve seed'de `accounting = _FIN`
    #    (operasyonel = fiziksel gizlenir) · `field_engineer = _LIM` (para = mali
    #    gizlenir) tasir — yani beklenen `(False, None, None)` uclusunu maske TEK
    #    BASINA uretirdi. OLCULDU (2026-09-19): `progress_cards.by_projects`teki
    #    iki `can_read` kapisi `True`ya sabitlenip test kosuldu, YESIL kaldi.
    #    Kapsam ACIKCA `all`a cekilir ki olculen tek sey izin hucresi olsun.
    #    `patron` seed'de zaten `all` tasir ve ACIKCA kurulmaz: kapsami daralirsa
    #    OLUMLU kontrol KIRILIR (sessizce yesil kalmaz), istenen yon budur.
    await modul_duzeyi_yaz(seeded_db, "accounting", "projects", AccessLevel.view)
    await modul_duzeyi_yaz(seeded_db, "field_engineer", "projects", AccessLevel.view)
    await modul_duzeyi_yaz(seeded_db, "accounting", "site_diary", AccessLevel.none)
    await modul_duzeyi_yaz(seeded_db, "field_engineer", "progress_payments", AccessLevel.none)

    async def _olc(role_key: str, email: str) -> dict:
        headers = await _ilr.login(client, seeded_db, user_factory, role_key, email)
        govde = (await client.get("/projects", headers=headers)).json()
        return {
            "physical": _ilr.kart(govde, project.id, "physical_progress"),
            "financial": _ilr.kart(govde, project.id, "financial_progress"),
        }

    olculen = {
        rol: {ad: (z["available"], z["value"], z["pending_module"]) for ad, z in kartlar.items()}
        for rol, kartlar in {
            "patron": await _olc("patron", "e3a@ilr.co"),
            "accounting": await _olc("accounting", "e3b@ilr.co"),
            "field_engineer": await _olc("field_engineer", "e3c@ilr.co"),
        }.items()
    }

    assert olculen == {
        "patron": {"physical": (True, "12.21", None), "financial": (True, "50.00", None)},
        "accounting": {"physical": (False, None, None), "financial": (True, "50.00", None)},
        "field_engineer": {"physical": (True, "12.21", None), "financial": (False, None, None)},
    }, f"kartin iki alani ayri izinlere bakmiyor: {olculen}"


# --------------------------------------------------------------------------- #
# F. MALI ↔ FIZIKSEL AYRISMASI
# --------------------------------------------------------------------------- #


async def test_gunluk_VARKEN_onayli_hakedis_YOKKEN_fiziksel_POZITIF_mali_SIFIR(
    seeded_db, project_factory, user_factory
):
    """Emirdeki canli beklenti — ikisinin ayrismasinin KANITI.

    Mali 0,00 "bilinmiyor" DEGILDIR: sozlesme kalemi VARDIR, onaylanmis hakedis
    YOKTUR.
    """
    project, _, _, _, _ = await _canli_sekil(seeded_db, project_factory, user_factory, "ILR-F1")
    await _ilr.isveren_kalemi(seeded_db, project, quantity="98", unit_price="96250.00")

    fiziksel = await progress.physical_for_project(seeded_db, project.id, scope=UNRESTRICTED)
    mali = await project_progress.financial_for_project(seeded_db, project.id)

    assert (fiziksel, mali) == (Decimal("12.21"), Decimal("0.00")), (
        f"fiziksel/mali ayrismasi bozuldu: fiziksel={fiziksel} mali={mali}"
    )


async def test_TASERON_hakedisi_MALI_ilerlemeyi_OYNATMAZ(seeded_db, project_factory, user_factory):
    """Taseron hakedisi MALIYET tarafidir. Onunla ilerleme olcmek, ne kadar
    HARCADIGINI ne kadar ILERLEDIGIN sanmaktir."""
    project = await project_factory("ILR-F2")
    site = await _ilr.santiye(seeded_db, project)
    yazan = await _ilr.aktor(seeded_db, user_factory, "f2@ilr.co")
    kalem = await _ilr.isveren_kalemi(seeded_db, project, quantity="98", unit_price="96250.00")
    await _ilr.isveren_hakedisi(
        seeded_db, project, yazan, kalem, site, quantity="49", status=ProgressPaymentStatus.approved
    )

    once = await project_progress.financial_for_project(seeded_db, project.id)
    await _ilr.taseron_hakedisi(seeded_db, project, yazan, quantity="98")
    sonra = await project_progress.financial_for_project(seeded_db, project.id)

    taseron_satiri = await seeded_db.scalar(
        text("SELECT count(*) FROM subcontractor_progress_payment_lines")
    )
    assert taseron_satiri == 1, "taseron hakedisi kurulmamis — iddia bosa koser"
    assert (once, sonra) == (Decimal("50.00"), Decimal("50.00")), (
        f"taseron hakedisi mali ilerlemeyi oynatti: {once} → {sonra}"
    )


async def test_TASLAK_ve_BEKLEYEN_isveren_hakedisi_MALI_yuzdeye_GIRMEZ__ONAYLI_girer(
    seeded_db, project_factory, user_factory
):
    """Uc yon AYNI testte: `draft` 0 · `pending_approval` 0 · `approved` oynatir."""
    project = await project_factory("ILR-F3")
    site = await _ilr.santiye(seeded_db, project)
    yazan = await _ilr.aktor(seeded_db, user_factory, "f3@ilr.co")
    kalem = await _ilr.isveren_kalemi(seeded_db, project, quantity="98", unit_price="96250.00")

    await _ilr.isveren_hakedisi(
        seeded_db,
        project,
        yazan,
        kalem,
        site,
        quantity="20",
        status=ProgressPaymentStatus.draft,
        sequence_no=1,
    )
    taslakken = await project_progress.financial_for_project(seeded_db, project.id)

    await _ilr.isveren_hakedisi(
        seeded_db,
        project,
        yazan,
        kalem,
        site,
        quantity="9",
        status=ProgressPaymentStatus.pending_approval,
        sequence_no=2,
    )
    beklerken = await project_progress.financial_for_project(seeded_db, project.id)

    await _ilr.isveren_hakedisi(
        seeded_db,
        project,
        yazan,
        kalem,
        site,
        quantity="49",
        status=ProgressPaymentStatus.approved,
        sequence_no=3,
    )
    onaylanmisken = await project_progress.financial_for_project(seeded_db, project.id)

    assert (taslakken, beklerken, onaylanmisken) == (
        Decimal("0.00"),
        Decimal("0.00"),
        Decimal("50.00"),
    ), (
        f"isveren hakedisi durum suzgeci bozuldu: draft={taslakken} "
        f"pending={beklerken} approved={onaylanmisken}"
    )


# --------------------------------------------------------------------------- #
# G. BESINCI YUZEY — SANTIYE KARTI (`physical_for_sites`)
#
# 🔴 Bu bekci MUTASYON DENETIMINDEN DOGDU: `physical_for_sites` (toplu santiye
# yuzdesi) hicbir testin dokunmadigi TEK yoldu, yani yalnizca ONUN icindeki
# agirligi kaldiran bir mutant SAG KALIRDI. Esdegersiz mutant = eksik bekci.
# --------------------------------------------------------------------------- #


async def test_SANTIYE_KARTI_yuzdesi_para_agirliklidir_ve_KAPSAM_sizdirmaz(
    client, seeded_db, project_factory, user_factory
):
    """Toplu hâl (`physical_for_sites`) tekil hâlle AYNI sayiyi vermelidir ve
    her santiye YALNIZ kendi uretimini gormelidir.

    Ayrica BOQ'u olmayan santiyenin yuzdesi YOKTUR (`None`) — 0 DEGIL.
    """
    project, site, _, _, yazan = await _canli_sekil(
        seeded_db, project_factory, user_factory, "ILR-G1"
    )
    komsu = await _ilr.santiye(seeded_db, project, code="ILR-G1-B")
    komsu_grup = await _ilr.grup(seeded_db, komsu)
    komsu_demir = await _ilr.poz(seeded_db, komsu, komsu_grup, "15.185.1002", **_DEMIR)
    await _ilr.gunluk(seeded_db, komsu, yazan, [(komsu_demir, "49")])
    bos = await _ilr.santiye(seeded_db, project, code="ILR-G1-C")

    toplu = await progress.physical_for_sites(
        seeded_db, [site.id, komsu.id, bos.id], scope=UNRESTRICTED
    )
    tekil = {
        sid: await progress.physical_for_site(seeded_db, sid, scope=UNRESTRICTED)
        for sid in (site.id, komsu.id, bos.id)
    }

    assert toplu == {
        site.id: Decimal("12.21"),
        komsu.id: Decimal("50.00"),
        bos.id: None,
    }, f"toplu santiye yuzdesi bozuldu: {toplu}"
    assert toplu == tekil, (
        f"TOPLU ve TEKIL hâl ayrisiyor: {toplu} != {tekil} — iki carpim iki farkli "
        "'%' uretiyor demektir (K3)"
    )

    # 🔴 KISITLI yon KAPIDAN gecmeli, MASKEDEN DEGIL: `/projects/{id}/sites`
    #    `sites` kapsamiyla maskelenir, `accounting` seed'de `sites = _FIN`
    #    (finance) tasir ve `SiteCard.progress_pct` OPERASYONEL kovadadir — yani
    #    beklenen `(False, None)` ikilisini maske tek basina uretirdi. OLCULDU
    #    (2026-09-19): `reads.site_progress_map`teki `can_read` kapisi silinip
    #    test kosuldu, YESIL kaldi. (Denetim bu dorduncu hâli KACIRDI; kardes uc
    #    testle AYNI sinif.) Kapsam ACIKCA `all`a cekilir ki olculen tek sey izin
    #    hucresi olsun; hucrenin KENDISI de seed'e birakilmaz.
    await modul_duzeyi_yaz(seeded_db, "accounting", "sites", AccessLevel.view)
    await modul_duzeyi_yaz(seeded_db, "accounting", "site_diary", AccessLevel.none)

    izinli = await _ilr.login(client, seeded_db, user_factory, "patron", "g1a@ilr.co")
    izinsiz = await _ilr.login(client, seeded_db, user_factory, "accounting", "g1b@ilr.co")
    dolu = (await client.get(f"/projects/{project.id}/sites", headers=izinli)).json()
    kisitli = (await client.get(f"/projects/{project.id}/sites", headers=izinsiz)).json()

    assert {k["code"]: k["progress_pct"]["value"] for k in dolu["items"]} == {
        "ILR-G1-A": "12.21",
        "ILR-G1-B": "50.00",
        "ILR-G1-C": None,
    }, "santiye KARTI yuzdesi ucta dolmadi"
    assert {k["code"]: _ilr.zarf(k["progress_pct"]) for k in kisitli["items"]} == {
        "ILR-G1-A": (False, None),
        "ILR-G1-B": (False, None),
        "ILR-G1-C": (False, None),
    }, "gunlugu okuyamayan role santiye karti yuzdesi sizdi ya da sahte gerekce basildi"
