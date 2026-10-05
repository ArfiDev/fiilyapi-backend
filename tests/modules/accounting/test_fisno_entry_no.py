"""FIS-NO T1 — sunucu üretimli yevmiye fiş numarası (`entry_no`).

Biçim **`YEV-{yıl}-{sıra:04d}`** (`YEV-2026-0214`). Kullanıcının BAĞLADIĞI üç
karar bu dosyada bekçilenir:

1. 🔴 **Sıra YIL bazlıdır** ve her 1 Ocak'ta `0001`e döner; sayaç ŞİRKET
   GENELİNDE tektir (proje/şantiye kırılımı YOK — `journal_entries`de zaten
   `project_id` kolonu da yoktur). Yıl fişin `period_year` kolonundan okunur.
   Dört hane bir TAVAN değil EN AZ genişliktir: 9999'dan sonra numara
   BUDANMAZ, beş haneye uzar.
2. 🔴 **BOŞLUK OLABİLİR.** Fiş silinince numarası boşta kalır, **sayaç GERİ
   ALINMAZ** ve numaralar YENİDEN DİZİLMEZ.
3. 🔴 **Numara `draft` AÇILIRKEN verilir.** `POST /journal-entries` yanıtı
   numarayı zaten taşır; `posted` olurken DEĞİŞMEZ.

## Bu dosyanın ÖLÇMEDİĞİ şey

Gerçek eşzamanlılık (`EŞİK = KİLİT`) BURADA ölçülemez: kök `tests/conftest.py`
`db_session`ı her testi TEK bağlantı üzerinde bir SAVEPOINT'e sarar ve dış
transaction'ı asla COMMIT etmez. İki `asyncio` görevi aynı bağlantıyı paylaşır,
satır kilidi hiç yarışmaz ve KİLİTSİZ bir uygulama da yeşil kalırdı. Yarış
`test_fisno_concurrency.py`de tek kullanımlık veritabanlarıyla ölçülür.

## 🔴 `test_entry_no_govdeden_GELEMEZ_...` NEDEN YEŞİL DOĞAR

`JournalEntryCreate`/`JournalEntryUpdate` şemaları `extra="forbid"` taşır, yani
bu bekçi T1'de ZATEN yeşildir. Görevi bir kusuru bulmak değil, T3'ün
`entry_no`yu yanıt şemasına eklerken YANLIŞLIKLA `Create`/`Update` gövdesine de
eklemesini ENGELLEMEKTİR. Çıplak bir `status_code == 422` bunu yapamazdı:
gövdedeki başka bir kusur da 422 üretir ve bekçi hiçbir şeyi tutmadan yeşil
kalırdı. Bu yüzden pydantic'in `loc` alanının **`entry_no`yu adıyla** işaret
ettiği de iddia edilir.
"""

import re
from decimal import Decimal

from app.core.timezone import today
from tests._silme_yardimci import sil_aile
from tests.modules.accounting._journal import YOL as _YOL
from tests.modules.accounting._journal import govde as _govde
from tests.modules.accounting._journal import iki_yaprak as _iki_yaprak
from tests.modules.accounting._journal import satir as _satir

#: `YEV-` + dört haneli yıl + EN AZ dört haneli sıra. Üst sınır YOKTUR (karar 1).
BICIM = re.compile(r"^YEV-\d{4}-\d{4,}$")


def _no(yil: int, sira: int) -> str:
    return f"YEV-{yil}-{sira:04d}"


async def _fis(client, headers, kasa, saticilar, **ek) -> dict:  # noqa: ANN001
    """Numarayı ÖLÇEN testler için tek satırlık POST — hesaplar DIŞARIDAN gelir.

    `_journal.fis_olustur` her çağrıda YENİ hesap çifti açar; bu dosyada aynı
    test içinde üç-dört fiş kesiliyor ve numaranın hesaplardan BAĞIMSIZ
    (şirket geneli) olduğu ancak hesap çifti SABİTKEN görülür.
    """
    resp = await client.post(_YOL, json=_govde(kasa, saticilar, **ek), headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# Karar 3 — numara `draft` AÇILIRKEN verilir
# --------------------------------------------------------------------------- #


async def test_POST_taslak_fis_yanitinda_entry_no_VARDIR(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 Numara bir "kayıtlaştırma damgası" DEĞİLDİR: taslak yanıtı onu ZATEN
    taşır.

    Numara `post` anında verilseydi taslak listesindeki fişin kimliği yalnız
    UUID olurdu ve kullanıcı ekranda gördüğü fişi telefonda söyleyemezdi.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    govde = await _fis(client, muhasebe_headers, kasa, saticilar)

    assert govde["status"] == "draft"
    assert govde["entry_no"] == _no(2026, 1)
    assert BICIM.match(govde["entry_no"]), govde["entry_no"]


async def test_entry_no_LISTE_ve_DETAY_yanitlarinda_da_vardir(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """Tek başlık şekli (`JournalEntryResponse`) ÜÇ yanıtı da besler.

    Alan yalnız `POST`un döndüğü detay şemasına eklenseydi liste ekranı numarayı
    hiç göremez ve fiş seçme ekranı yine UUID'ye düşerdi.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    olusan = await _fis(client, muhasebe_headers, kasa, saticilar)

    detay = await client.get(f"{_YOL}/{olusan['id']}", headers=muhasebe_headers)
    assert detay.status_code == 200, detay.text
    assert detay.json()["entry_no"] == olusan["entry_no"]

    liste = await client.get(_YOL, headers=muhasebe_headers)
    assert liste.status_code == 200, liste.text
    numaralar = {satir["id"]: satir["entry_no"] for satir in liste.json()["items"]}
    assert numaralar[olusan["id"]] == olusan["entry_no"]


async def test_ikinci_fis_0002_alir_ve_POSTEDe_gecerken_numara_DEGISMEZ(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 Karar 3'ün ikinci yarısı: `draft → posted` numaraya DOKUNMAZ.

    Kayıtlaştırma anında yeniden üretilseydi kullanıcının taslakken not ettiği
    numara ile deftere giren numara AYRIŞIRDI; iddia bu yüzden geçiş
    ÖNCESİ/SONRASI aynı metin üzerinden kurulur.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    birinci = await _fis(client, muhasebe_headers, kasa, saticilar)
    ikinci = await _fis(client, muhasebe_headers, kasa, saticilar)

    assert (birinci["entry_no"], ikinci["entry_no"]) == (_no(2026, 1), _no(2026, 2))

    kayitlasan = await client.post(f"{_YOL}/{birinci['id']}/post", headers=muhasebe_headers)
    assert kayitlasan.status_code == 200, kayitlasan.text
    assert kayitlasan.json()["status"] == "posted"
    assert kayitlasan.json()["entry_no"] == birinci["entry_no"]

    detay = await client.get(f"{_YOL}/{birinci['id']}", headers=muhasebe_headers)
    assert detay.json()["entry_no"] == birinci["entry_no"]


# --------------------------------------------------------------------------- #
# Karar 1 — sıra YIL bazlıdır
# --------------------------------------------------------------------------- #


async def test_YIL_degisince_sayac_0001den_baslar_ve_eski_yil_KENDI_sirasindan_devam_eder(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 İki yıl AYNI veritabanında yan yana durur.

    Sayaç yıldan bağımsız (küresel) olsaydı 2025'in ilk fişi `0003` olurdu.
    Tersi kusur da bekçilenir: 2025 açıldıktan SONRA kesilen 2026 fişi
    `0003`tür — yani yıl sayaçları BİRBİRİNİ SIFIRLAMAZ, her biri kendi
    hattında ilerler.

    Yıl `entry_date`ten değil **`period_year`**den okunur; ikisi
    `ck_journal_entries_period_matches_date` ile zaten kilitlidir.

    🔴 İkinci yıl GEÇMİŞTEDİR (2027 → 2025): `assert_entry_date_not_future`
    ileri tarihli fişi 422 ile reddediyor. İddia DEĞİŞMEDİ — kurulum "iki ayrı
    yıl yan yana" der, "yıl ileride" DEMEZ; sayaçların birbirini sıfırlamaması
    takvim YÖNÜNDEN bağımsızdır.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    ilk_2026 = await _fis(client, muhasebe_headers, kasa, saticilar)
    ikinci_2026 = await _fis(client, muhasebe_headers, kasa, saticilar)
    ilk_2025 = await _fis(client, muhasebe_headers, kasa, saticilar, entry_date="2025-03-04")

    assert (ilk_2026["entry_no"], ikinci_2026["entry_no"]) == (_no(2026, 1), _no(2026, 2))
    assert ilk_2025["period_year"] == 2025
    assert ilk_2025["entry_no"] == _no(2025, 1)

    ucuncu_2026 = await _fis(client, muhasebe_headers, kasa, saticilar)
    ikinci_2025 = await _fis(client, muhasebe_headers, kasa, saticilar, entry_date="2025-11-30")
    assert ucuncu_2026["entry_no"] == _no(2026, 3)
    assert ikinci_2025["entry_no"] == _no(2025, 2)


async def test_sira_AYIN_degismesinden_etkilenmez(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """Sayaç YIL bazlıdır, AY bazlı DEĞİL: Temmuz ve Ağustos aynı hattı sürer.

    Ay bazlı olsaydı `YEV-2025-0001` yılda on iki kez üretilir ve numara TEKİL
    OLMAKTAN çıkardı — biçimde ay alanı yoktur, çakışma sessiz olurdu.

    🔴 Üçü de 2025'e alındı: iddia AY sınırını (7 → 8 → 12, Aralık YIL SONU
    sınırıdır) ölçer ve o sınır korunmuştur; 2026-12-31 bugünden İLERİDE olduğu
    için `assert_entry_date_not_future` onu 422 ile reddederdi.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    temmuz = await _fis(client, muhasebe_headers, kasa, saticilar, entry_date="2025-07-17")
    agustos = await _fis(client, muhasebe_headers, kasa, saticilar, entry_date="2025-08-05")
    aralik = await _fis(client, muhasebe_headers, kasa, saticilar, entry_date="2025-12-31")

    assert (temmuz["period_month"], agustos["period_month"], aralik["period_month"]) == (7, 8, 12)
    assert [temmuz["entry_no"], agustos["entry_no"], aralik["entry_no"]] == [
        _no(2025, 1),
        _no(2025, 2),
        _no(2025, 3),
    ]


# --------------------------------------------------------------------------- #
# Karar 2 — BOŞLUK OLABİLİR, sayaç GERİ ALINMAZ
# --------------------------------------------------------------------------- #


async def test_ORTADAKI_fis_silininde_numarasi_BOSTA_kalir(
    client, admin_headers, muhasebe_headers, hesap_fabrikasi
) -> None:
    """`0002` silinir; yeni fiş `0004` alır ve `0002` GERİ GELMEZ.

    ⚠️ Bu test TEK BAŞINA AYIRT ETMEZ: `max(numara) + 1` ile üreten naif bir
    uygulama da burada `0004` verir. Kararın gerçek bekçisi bir sonraki
    testtir; bu test yalnız "boşluk YENİDEN DİZİLMEZ" yarısını tutar.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fisler = [await _fis(client, muhasebe_headers, kasa, saticilar) for _ in range(3)]
    assert [f["entry_no"] for f in fisler] == [_no(2026, 1), _no(2026, 2), _no(2026, 3)]

    sil = await sil_aile(client, admin_headers, "journal_entry", fisler[1]["id"])
    assert sil.status_code == 204, sil.text

    yeni = await _fis(client, muhasebe_headers, kasa, saticilar)
    assert yeni["entry_no"] == _no(2026, 4)

    liste = await client.get(_YOL, headers=muhasebe_headers)
    numaralar = {satir["entry_no"] for satir in liste.json()["items"]}
    assert _no(2026, 2) not in numaralar
    assert {_no(2026, 1), _no(2026, 3), _no(2026, 4)} <= numaralar


async def test_EN_BUYUK_numara_silinse_bile_sayac_GERI_ALINMAZ(
    client, admin_headers, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 **Bu dilimin tasarım kararının TEK gerçek bekçisi.**

    Üç fişin EN BÜYÜĞÜ (`0003`) silinir. Karar 2 "sayaç geri alınmaz" der:
    sıradaki fiş `0004`tür, `0003` YENİDEN KULLANILMAZ.

    `max(mevcut numaralar) + 1` tabanlı bir uygulama burada `0003`ü İKİNCİ KEZ
    verir ve bu test kırmızı olur — ortadaki satırı silen testin göremediği
    ayrım tam olarak budur. Numaranın yeniden kullanılması mali izi bozar:
    silinen fişi kâğıda basmış bir kullanıcı, aynı numarayı taşıyan BAŞKA bir
    fişle karşılaşırdı.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fisler = [await _fis(client, muhasebe_headers, kasa, saticilar) for _ in range(3)]
    assert [f["entry_no"] for f in fisler] == [_no(2026, 1), _no(2026, 2), _no(2026, 3)]

    sil = await sil_aile(client, admin_headers, "journal_entry", fisler[2]["id"])
    assert sil.status_code == 204, sil.text

    yeni = await _fis(client, muhasebe_headers, kasa, saticilar)
    assert yeni["entry_no"] == _no(2026, 4), (
        "en büyük numara silinince sayaç geri alındı — üretim `max + 1` tabanlı "
        "olabilir; karar 2 sayacın GERİ ALINMAYACAĞINI söyler"
    )


async def test_TUM_fisler_silinse_bile_sayac_bastan_baslamaz(
    client, admin_headers, muhasebe_headers, hesap_fabrikasi
) -> None:
    """Sınır hâli: tabloda o yıla ait HİÇ fiş kalmaz.

    `max + 1` (ya da `COUNT(*) + 1`) burada `0001`e döner ve silinmiş fişlerin
    numaraları yeniden dolaşıma girerdi. Sayaç fişlerden BAĞIMSIZ yaşamalıdır.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fisler = [await _fis(client, muhasebe_headers, kasa, saticilar) for _ in range(2)]

    for fis in fisler:
        sil = await sil_aile(client, admin_headers, "journal_entry", fis["id"])
        assert sil.status_code == 204, sil.text

    yeni = await _fis(client, muhasebe_headers, kasa, saticilar)
    assert yeni["entry_no"] == _no(2026, 3)


# --------------------------------------------------------------------------- #
# Numara İSTEMCİDEN gelmez / İSTEMCİYLE değişmez
# --------------------------------------------------------------------------- #


async def test_entry_no_govdeden_GELEMEZ_POST_ve_PATCH_422(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 T1'de ZATEN YEŞİL doğar (`extra="forbid"`) — modül docstring'ine bak.

    Bekçinin işi T3'ün `entry_no`yu `JournalEntryCreate`/`JournalEntryUpdate`e
    eklemesini ENGELLEMEKTİR. Durum kodu TEK BAŞINA yetmez: 422 gövdedeki
    herhangi bir kusurdan da gelebilir. Bu yüzden pydantic'in `loc`u
    `("body", "entry_no")` olarak ve hata türü `extra_forbidden` olarak
    doğrudan okunur — alan gövdeye açılırsa bu iddia SESSİZCE düşmez, kırılır.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)

    olustur = await client.post(
        _YOL, json=_govde(kasa, saticilar, entry_no=_no(2026, 999)), headers=muhasebe_headers
    )
    assert olustur.status_code == 422, olustur.text
    hatalar = olustur.json()["detail"]
    assert any(
        hata["loc"][-1] == "entry_no" and hata["type"] == "extra_forbidden" for hata in hatalar
    ), olustur.text

    fis = await _fis(client, muhasebe_headers, kasa, saticilar)
    guncelle = await client.patch(
        f"{_YOL}/{fis['id']}", json={"entry_no": _no(2026, 999)}, headers=muhasebe_headers
    )
    assert guncelle.status_code == 422, guncelle.text
    hatalar = guncelle.json()["detail"]
    assert any(
        hata["loc"][-1] == "entry_no" and hata["type"] == "extra_forbidden" for hata in hatalar
    ), guncelle.text


async def test_PATCH_entry_dateI_SONRAKI_YILA_tasisa_bile_numara_AYNI_kalir(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """Numara `PATCH` ile DEĞİŞMEZ — `entry_date` bir sonraki YILA taşınsa bile.

    🔴 Asıl tuzak budur: yıl `period_year`den türetiliyorsa, tarihi 2026'ya
    çeken bir `PATCH` numarayı "yeniden türetmeye" ayartabilir. Numara BİR KEZ
    verilir ve fişin kimliğidir; kaydığı anda kullanıcının elindeki kâğıt yanlış
    fişi gösterirdi.

    🔴 Taşıma GEÇMİŞ yıllar arasındadır (2025 → 2026): `entry_date`in gelecek
    sınırı (`assert_entry_date_not_future`) 2027'yi 422 ile reddeder. İDDİA
    GÜÇLENDİ, zayıflamadı — numaranın yılı (`YEV-2025-…`) artık `period_year`
    (2026) ile AYRIŞIR, yani "yeniden türetme" ayartısı tam olarak ölçülür.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fis = await _fis(client, muhasebe_headers, kasa, saticilar, entry_date="2025-07-17")
    numara = fis["entry_no"]

    resp = await client.patch(
        f"{_YOL}/{fis['id']}",
        json={"description": "Düzeltilmiş açıklama", "entry_date": "2026-01-04"},
        headers=muhasebe_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["description"] == "Düzeltilmiş açıklama"
    assert resp.json()["period_year"] == 2026
    assert resp.json()["entry_no"] == numara
    assert numara == _no(2025, 1), "numara ESKİ yılın hattından gelir"

    detay = await client.get(f"{_YOL}/{fis['id']}", headers=muhasebe_headers)
    assert detay.json()["entry_no"] == numara


async def test_PUT_lines_numarayi_DEGISTIRMEZ(client, muhasebe_headers, hesap_fabrikasi) -> None:
    """Bacak kümesi TOPTAN yazılır; başlık numarası ondan ETKİLENMEZ."""
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fis = await _fis(client, muhasebe_headers, kasa, saticilar)

    resp = await client.put(
        f"{_YOL}/{fis['id']}/lines",
        json={"lines": [_satir(kasa.id, debit="250.00"), _satir(saticilar.id, credit="250.00")]},
        headers=muhasebe_headers,
    )
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["total_debit"]) == Decimal("250.00")
    assert resp.json()["entry_no"] == fis["entry_no"]


# --------------------------------------------------------------------------- #
# Storno — AYRI bir fiştir, AYRI bir numara alır
# --------------------------------------------------------------------------- #


async def test_STORNO_kendi_numarasini_alir_kaynagin_numarasi_DEGISMEZ(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 Storno bir bayrak değil YENİ BİR FİŞTİR (MU-1 kararı) → yeni numara.

    Numarayı orijinalden kopyalasaydı iki fiş aynı numarayla yaşar ve defterde
    hangi satırın hangi fişten geldiği ayırt edilemezdi.

    Stornonun yılı **BUGÜNÜN** yılıdır (`entry_date = timezone.today()`), çünkü
    orijinalin tarihi kapalı bir döneme düşerdi. Beklenti bu yüzden takvimden
    türetilir: aynı yıldaysak sıradaki numara (`0002`), yıl atlamışsak yeni
    yılın ilki (`0001`).
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fis = await _fis(client, muhasebe_headers, kasa, saticilar)
    kayitlastir = await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    assert kayitlastir.status_code == 200, kayitlastir.text

    resp = await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 201, resp.text
    storno = resp.json()

    bugun = today()
    beklenen = _no(bugun.year, 2) if bugun.year == 2026 else _no(bugun.year, 1)
    assert storno["reversal_of_id"] == fis["id"]
    assert storno["entry_no"] == beklenen
    assert storno["entry_no"] != fis["entry_no"]
    assert BICIM.match(storno["entry_no"]), storno["entry_no"]

    orijinal = await client.get(f"{_YOL}/{fis['id']}", headers=muhasebe_headers)
    assert orijinal.json()["status"] == "reversed"
    assert orijinal.json()["entry_no"] == fis["entry_no"]


async def test_STORNO_numarasi_sonraki_fisi_ATLAMAZ(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """Storno sayacı TÜKETİR: ondan sonra açılan taslak bir sonraki numarayı
    alır ve stornonun numarası İKİNCİ KEZ dağıtılmaz.

    Storno ayrı bir sayaçtan (ya da hiç sayaçtan geçmeden) numaralansaydı
    çakışma yalnız üretimde, tekillik kısıtı üzerinden görünürdü.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    fis = await _fis(client, muhasebe_headers, kasa, saticilar)
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    storno = (await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)).json()

    bugun = today()
    if bugun.year != 2026:  # pragma: no cover - takvim savunması
        return

    sonraki = await _fis(client, muhasebe_headers, kasa, saticilar)
    assert storno["entry_no"] == _no(2026, 2)
    assert sonraki["entry_no"] == _no(2026, 3)
