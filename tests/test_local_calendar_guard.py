"""TB5 T4 — YEREL TAKVIM BEKCISI: kusurun nuksetmesini yapisal olarak engeller.

Kusur (spec §1, uretimde kanitli): `date.today()` sunucunun yerel saatini
(Railway'de UTC) okur. TR UTC+3 oldugu icin her gun 21:00-24:00 arasinda DUNU
dondurur — pesinat vadesi "dun" yazilir, kayit dogdugu anda gecikmis gorunur.

🔴 UC KALIP ARANIR, IKISI DEGIL. T3'un bulgusu bekcinin kapsamini genisletti:
pesinat kusurunun ASIL kok nedeni `date.today()` DEGILDI — `sale.created_at.date()`
idi, yani bir `timestamptz`ten HAM UTC GUNU okumak. `date.today()` taramasi bunu
KAPATMAZDI. Kalip 3 bu sinifi kapatir.

Bekci AST tabanlidir, duz metin grep DEGIL: yorumdaki `# date.today()` ya da bir
dizedeki metin TETIKLEMEZ, gercek cagri ise KACMAZ (ikisi de asagida testli).

⚠️ 2026-08-14 dersi: frontend'in BFF bekcisinin tek iddiasi `length > 0`'di,
hicbir sey yakalamiyordu. "Test var" != "test bekcilik ediyor". Bu yuzden burada
(a) tarayicinin gercekten dosya gordugu, (b) her kalibin sentetik bir kacagi
yakaladigi, (c) dogru araclarin YANLIS ALARM uretmedigi ve (d) istisna listesinin
BAYAT olmadigi AYRI AYRI iddia edilir.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

APP_KOK = Path(__file__).resolve().parents[1] / "app"

#: `app/` altinda beklenen asgari dosya sayisi. Tarayici bir gun yanlis dizine
#: bakarsa ya da glob bozulursa "0 bulgu" YESIL gorunurdu; bu esik onu kirmizi yapar.
ASGARI_TARANAN_DOSYA = 200

#: Kalip 1+2: takvimi sunucunun yerel saatinden (UTC) okuyan standart kutuphane
#: cagrilari. `datetime.utcnow()` ayrica naive doner — sessizce yerel saat sayilir.
KALIP_DATE_TODAY = "date.today()"
KALIP_UTCNOW = "datetime.utcnow()"

#: Kalip 3: bir `timestamptz` degerinden HAM yerel takvim/saat cikarimi.
#: `created_at.date()` UTC gununu verir; TR gecesi 21:00-24:00'te bir gun geridedir.
KALIP_TIMESTAMPTZ = "timestamptz-uzerinde-yerel-takvim"

#: Bir datetime'dan yerel takvim/saat bileseni cikaran uyeler. `isoformat()`
#: BILEREK DISARIDA: ofseti koruyarak serilestirir, gun kaydirmaz.
_TAKVIM_CIKARIMLARI = frozenset(
    {"date", "year", "month", "day", "weekday", "isocalendar", "strftime", "hour", "minute"}
)

#: Cikarimin GUVENLI oldugu sarmalayicilar: bunlarin donusu zaten TR'ye cevrilmistir.
_GUVENLI_SARMALAYICILAR = frozenset({"to_display", "today", "day_start_utc", "day_end_utc"})

#: 🔴 C SINIFI ISTISNALAR — (dosya, kalip) -> TEK SATIR gerekce.
#: Liste BAYATLAYAMAZ: karsiligi kalmayan bir istisna testi kirmizi yapar
#: (bkz. `test_istisna_listesi_bayat_degil`).
ISTISNALAR: dict[tuple[str, str], str] = {
    (
        "app/core/timezone.py",
        KALIP_TIMESTAMPTZ,
    ): (
        "C: kanonik kaynagin TA KENDISI — `datetime.now(DISPLAY_TIMEZONE).date()` "
        "burada BIR KEZ yazilir, baska hicbir yerde tekrarlanmaz."
    ),
}


@dataclass(frozen=True)
class Bulgu:
    dosya: str
    satir: int
    kalip: str
    kaynak: str

    def __str__(self) -> str:  # pragma: no cover - yalnizca hata mesajinda
        return f"{self.dosya}:{self.satir}  [{self.kalip}]  {self.kaynak}"


def timestamptz_alanlari(kok: Path) -> frozenset[str]:
    """`Mapped[datetime]` olarak tanimli TUM kolon adlarini modellerden TUREtir.

    Elle yazilmis bir liste, yeni bir `*_at` kolonu acildiginda sessizce bayatlardi.
    Kaynak modellerin kendisidir: yeni timestamptz kolonu ACILIR ACILMAZ bekcinin
    kapsamina girer.
    """
    adlar: set[str] = set()
    for yol in sorted(kok.rglob("models.py")):
        agac = ast.parse(yol.read_text(encoding="utf-8"))
        for dugum in ast.walk(agac):
            if not isinstance(dugum, ast.AnnAssign) or not isinstance(dugum.target, ast.Name):
                continue
            annotation = ast.unparse(dugum.annotation).replace(" ", "")
            if annotation in ("Mapped[datetime]", "Mapped[datetime|None]"):
                adlar.add(dugum.target.id)
    return frozenset(adlar)


def _timestamptz_mi(ad: str, ts_alanlari: frozenset[str]) -> bool:
    """Model kolonu ya da `*_at` adli bir yerel/parametre — ikisi de timestamptz tasir.

    `previous_approved_at` gibi FONKSIYON PARAMETRELERI hicbir modele bagli
    degildir; yalnizca kolon adlarina bakan bir bekci audit metinlerindeki ham
    UTC damgasini KACIRIRDI (bu dilimde fiilen iki tane bulundu).
    """
    return ad in ts_alanlari or ad.endswith("_at")


@dataclass(frozen=True)
class _Baglanti:
    """Dosyadaki `datetime` ithallerinin YEREL adlari.

    🔴 Bu cozumleme ZORUNLUDUR: bekcinin ilk hali tabani duz `date` adiyla
    ariyordu ve `from datetime import date as d; d.today()` mutasyonu KACTI
    (2026-08-15, T4 mutasyon turu 1). Takma ad kor noktasi boyle kapandi.
    """

    date_adlari: frozenset[str]
    modul_adlari: frozenset[str]


def _datetime_baglantilari(agac: ast.AST) -> _Baglanti:
    date_adlari = {"date"}  # cozumlenemeyen durumda guvenli taraf
    modul_adlari = {"datetime"}
    for dugum in ast.walk(agac):
        if isinstance(dugum, ast.ImportFrom) and dugum.module == "datetime":
            for ad in dugum.names:
                if ad.name == "date":
                    date_adlari.add(ad.asname or ad.name)
        elif isinstance(dugum, ast.Import):
            for ad in dugum.names:
                if ad.name == "datetime":
                    modul_adlari.add(ad.asname or ad.name)
    return _Baglanti(frozenset(date_adlari), frozenset(modul_adlari))


def _date_today_mi(dugum: ast.Call, baglanti: _Baglanti) -> bool:
    """`date.today()` / `datetime.date.today()` / takma adli hali — `timezone.today()` DEGIL."""
    islev = dugum.func
    if not isinstance(islev, ast.Attribute) or islev.attr != "today":
        return False
    taban = islev.value
    if isinstance(taban, ast.Name):
        return taban.id in baglanti.date_adlari
    if isinstance(taban, ast.Attribute) and taban.attr == "date":
        return not isinstance(taban.value, ast.Name) or taban.value.id in baglanti.modul_adlari
    return False


def _utcnow_mi(dugum: ast.Call) -> bool:
    """`utcnow` yalnizca `datetime.datetime`da vardir — taban cozumlemesi gerekmez."""
    islev = dugum.func
    return isinstance(islev, ast.Attribute) and islev.attr == "utcnow"


def _ham_datetime_kaynagi_mi(dugum: ast.expr, ts_alanlari: frozenset[str]) -> bool:
    """Cikarimin uygulandigi ifade TR'ye cevrilmemis bir datetime mi?"""
    if isinstance(dugum, ast.Attribute):
        return _timestamptz_mi(dugum.attr, ts_alanlari)
    if isinstance(dugum, ast.Name):
        return _timestamptz_mi(dugum.id, ts_alanlari)
    if isinstance(dugum, ast.Call):
        islev = dugum.func
        ad = islev.attr if isinstance(islev, ast.Attribute) else getattr(islev, "id", "")
        if ad in _GUVENLI_SARMALAYICILAR:
            return False
        return ad in ("now", "utcnow")
    return False


def bulgular(kaynak: str, dosya: str, ts_alanlari: frozenset[str]) -> list[Bulgu]:
    """Tek bir kaynak metnindeki uc kalibi AST ile bulur."""
    agac = ast.parse(kaynak)
    baglanti = _datetime_baglantilari(agac)
    satirlar = kaynak.splitlines()
    bulunan: list[Bulgu] = []

    def _kaydet(dugum: ast.AST, kalip: str) -> None:
        satir = getattr(dugum, "lineno", 0)
        metin = satirlar[satir - 1].strip() if 0 < satir <= len(satirlar) else ""
        bulunan.append(Bulgu(dosya=dosya, satir=satir, kalip=kalip, kaynak=metin))

    for dugum in ast.walk(agac):
        if isinstance(dugum, ast.Call):
            if _date_today_mi(dugum, baglanti):
                _kaydet(dugum, KALIP_DATE_TODAY)
            elif _utcnow_mi(dugum):
                _kaydet(dugum, KALIP_UTCNOW)
        elif isinstance(dugum, ast.Attribute) and dugum.attr in _TAKVIM_CIKARIMLARI:
            if _ham_datetime_kaynagi_mi(dugum.value, ts_alanlari):
                _kaydet(dugum, KALIP_TIMESTAMPTZ)

    return bulunan


def tara(kok: Path, ts_alanlari: frozenset[str]) -> tuple[list[Bulgu], int]:
    """Bir agac altindaki her `.py` dosyasini tarar; (bulgular, taranan dosya sayisi)."""
    bulunan: list[Bulgu] = []
    taranan = 0
    for yol in sorted(kok.rglob("*.py")):
        taranan += 1
        goreli = yol.relative_to(kok.parent).as_posix()
        bulunan.extend(bulgular(yol.read_text(encoding="utf-8"), goreli, ts_alanlari))
    return bulunan, taranan


# --------------------------------------------------------------------------- #
# Bekcinin ASIL iddiasi
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def ts_alanlari() -> frozenset[str]:
    return timestamptz_alanlari(APP_KOK)


def test_tarayici_gercekten_dosya_goruyor(ts_alanlari: frozenset[str]) -> None:
    """ "0 bulgu" ancak agac GERCEKTEN tarandiysa bir sey ifade eder."""
    _, taranan = tara(APP_KOK, ts_alanlari)
    assert taranan >= ASGARI_TARANAN_DOSYA, (
        f"yalnizca {taranan} dosya tarandi — glob bozulmus olabilir"
    )


def test_timestamptz_alan_kumesi_modellerden_turetiliyor(ts_alanlari: frozenset[str]) -> None:
    """Kume modellerden okunur; elle yazilmis olsaydi yeni kolonda bayatlardi."""
    assert {"created_at", "updated_at", "approved_at", "paid_at", "occurred_at"} <= ts_alanlari
    assert len(ts_alanlari) >= 10


def test_app_agacinda_yerel_takvim_kacagi_yok(ts_alanlari: frozenset[str]) -> None:
    bulunan, _ = tara(APP_KOK, ts_alanlari)
    kacaklar = [b for b in bulunan if (b.dosya, b.kalip) not in ISTISNALAR]
    assert not kacaklar, "Yerel takvim kacagi:\n" + "\n".join(str(b) for b in kacaklar)


def test_istisna_listesi_bayat_degil(ts_alanlari: frozenset[str]) -> None:
    """Karsiligi kalmayan istisna SILINIR — bayat istisna gercek bir kacagi orter."""
    bulunan, _ = tara(APP_KOK, ts_alanlari)
    fiili = {(b.dosya, b.kalip) for b in bulunan}
    bayat = sorted(set(ISTISNALAR) - fiili)
    assert not bayat, f"Karsiligi kalmamis istisna(lar): {bayat}"


def test_her_istisnanin_gerekcesi_var() -> None:
    for anahtar, gerekce in ISTISNALAR.items():
        assert gerekce.strip(), f"{anahtar} icin gerekce yazilmamis"


# --------------------------------------------------------------------------- #
# 🔴 MUTASYON KANITI — bekcinin kendisi bekcilik ediyor mu?
# --------------------------------------------------------------------------- #

_KACAKLAR = [
    ("from datetime import date\n\nbugun = date.today()\n", KALIP_DATE_TODAY),
    ("import datetime\n\nbugun = datetime.date.today()\n", KALIP_DATE_TODAY),
    # 🔴 TAKMA AD: bekcinin ilk hali bu ikisini KACIRDI (T4 mutasyon turu 1).
    ("from datetime import date as d\n\nbugun = d.today()\n", KALIP_DATE_TODAY),
    ("import datetime as dt\n\nbugun = dt.date.today()\n", KALIP_DATE_TODAY),
    ("from datetime import datetime as dt\n\nan = dt.utcnow()\n", KALIP_UTCNOW),
    ("from datetime import datetime\n\nan = datetime.utcnow()\n", KALIP_UTCNOW),
    ("def f(sale):\n    return sale.created_at.date()\n", KALIP_TIMESTAMPTZ),
    ("def f(sale):\n    return sale.created_at.year\n", KALIP_TIMESTAMPTZ),
    ("def f(row):\n    return row.paid_at.month\n", KALIP_TIMESTAMPTZ),
    ("def f(approved_at):\n    return approved_at.strftime('%d.%m.%Y')\n", KALIP_TIMESTAMPTZ),
    ("from datetime import UTC, datetime\n\ng = datetime.now(UTC).date()\n", KALIP_TIMESTAMPTZ),
]


@pytest.mark.parametrize(("kaynak", "beklenen"), _KACAKLAR, ids=[k for _, k in _KACAKLAR])
def test_gercek_cagri_kacmaz(kaynak: str, beklenen: str, ts_alanlari: frozenset[str]) -> None:
    bulunan = bulgular(kaynak, "sentetik.py", ts_alanlari)
    assert beklenen in {b.kalip for b in bulunan}, f"KACTI: {kaynak!r}"


_TEMIZLER = [
    # Yorum ve dize: AST tabanli bekci bunlari GORMEZ (grep gorurdu).
    ("yorum", "# date.today() burada YASAK\nx = 1\n"),
    ("dize", "MESAJ = 'date.today() kullanma'\nY = \"datetime.utcnow()\"\n"),
    ("docstring", '"""Aciklama: date.today() ve created_at.date() kullanilmaz."""\n'),
    # Dogru araclar YANLIS ALARM uretmemeli.
    ("timezone.today", "from app.core import timezone\n\nb = timezone.today()\n"),
    ("today().year", "from app.core.timezone import today\n\ny = today().year\n"),
    (
        "to_display",
        "from app.core import timezone\n\ndef f(s):\n"
        "    return timezone.to_display(s.created_at).date()\n",
    ),
    # Cikarimsiz olay damgasi C sinifidir, dokunulmaz.
    (
        "cikarimsiz damga",
        "from datetime import UTC, datetime\n\ndef f(r):\n    r.paid_at = datetime.now(UTC)\n",
    ),
    # `date` tipli kolonlar (timestamptz DEGIL) kapsam disidir.
    ("date kolonu", "def f(item):\n    return item.end_date.year\n"),
]


@pytest.mark.parametrize(("ad", "kaynak"), _TEMIZLER, ids=[a for a, _ in _TEMIZLER])
def test_yanlis_alarm_uretilmez(ad: str, kaynak: str, ts_alanlari: frozenset[str]) -> None:
    del ad
    assert bulgular(kaynak, "sentetik.py", ts_alanlari) == []


def test_agac_taramasi_geri_konan_bir_cagriyi_kirmizi_yapar(
    tmp_path: Path, ts_alanlari: frozenset[str]
) -> None:
    """MUTASYON: bir dosyaya `date.today()` GERI KONUNCA bekci kirmizi olur.

    Sentetik metin degil, `tara()` ile GERCEK dizin yolu kullanilir — `app/`
    taramasiyla ayni kod yolu.
    """
    sahte_app = tmp_path / "app"
    (sahte_app / "modules").mkdir(parents=True)
    (sahte_app / "modules" / "temiz.py").write_text(
        "from app.core.timezone import today\n\nb = today()\n", encoding="utf-8"
    )

    bulunan, _ = tara(sahte_app, ts_alanlari)
    assert bulunan == []

    (sahte_app / "modules" / "bozuk.py").write_text(
        "from datetime import date\n\nb = date.today()\n", encoding="utf-8"
    )
    bulunan, _ = tara(sahte_app, ts_alanlari)
    assert [(b.dosya, b.kalip) for b in bulunan] == [("app/modules/bozuk.py", KALIP_DATE_TODAY)]


# --------------------------------------------------------------------------- #
# TMP-FIX #167 dersi — TESTLERDE dondurulmus "bugun" ve yerel takvim
# --------------------------------------------------------------------------- #
#
# `BUGUN = timezone.today()` modul duzeyinde IMPORT ANINDA donar: TR gece
# yarisindan sonra (UTC 21:00+) uygulama ertesi gunu, test ise dunu gorur.
# `date.today()` ve `datetime.utcnow()` ise sunucu yerel saatini (UTC) okur.
# Cozum: "bugun" CAGRI ANINDA (fonksiyon/fixture icinde) `timezone.today()` ile hesaplanir.

TESTLER_KOK = Path(__file__).resolve().parent

KALIP_MODUL_DUZEYI_TODAY = "modul-duzeyinde-today()"


def _today_cagrisi_mi(dugum: ast.Call) -> bool:
    islev = dugum.func
    if isinstance(islev, ast.Name):
        return islev.id == "today"
    return isinstance(islev, ast.Attribute) and islev.attr == "today"


def _import_aninda_calisan_today(dugum: ast.AST) -> list[ast.Call]:
    """Fonksiyon/sinif GOVDESINE girmeden `today()` cagrilarini toplar.

    Dekorator ve varsayilan arguman ifadeleri import aninda calisir; onlar taranir.
    """
    if isinstance(dugum, ast.Lambda):
        return []
    bulunan: list[ast.Call] = []
    if isinstance(dugum, ast.Call) and _today_cagrisi_mi(dugum):
        bulunan.append(dugum)
    if isinstance(dugum, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        ozel: list[ast.AST] = list(dugum.decorator_list)
        if isinstance(dugum, ast.ClassDef):
            ozel += dugum.bases
        else:
            ozel += [*dugum.args.defaults, *[d for d in dugum.args.kw_defaults if d]]
        for o in ozel:
            bulunan.extend(_import_aninda_calisan_today(o))
        return bulunan
    for cocuk in ast.iter_child_nodes(dugum):
        bulunan.extend(_import_aninda_calisan_today(cocuk))
    return bulunan


def bulgular_testler(kaynak: str, dosya: str) -> list[Bulgu]:
    """Testler icin: `date.today()`/`utcnow()` HER YERDE + modul duzeyinde `today()`."""
    agac = ast.parse(kaynak)
    baglanti = _datetime_baglantilari(agac)
    satirlar = kaynak.splitlines()
    bulunan: list[Bulgu] = []

    def _kaydet(dugum: ast.AST, kalip: str) -> None:
        satir = getattr(dugum, "lineno", 0)
        metin = satirlar[satir - 1].strip() if 0 < satir <= len(satirlar) else ""
        bulunan.append(Bulgu(dosya=dosya, satir=satir, kalip=kalip, kaynak=metin))

    for dugum in ast.walk(agac):
        if isinstance(dugum, ast.Call):
            if _date_today_mi(dugum, baglanti):
                _kaydet(dugum, KALIP_DATE_TODAY)
            elif _utcnow_mi(dugum):
                _kaydet(dugum, KALIP_UTCNOW)
    for dugum in _import_aninda_calisan_today(agac):
        if not _date_today_mi(dugum, baglanti):  # date.today() zaten yukarida sayildi
            _kaydet(dugum, KALIP_MODUL_DUZEYI_TODAY)
    return bulunan


def _testleri_tara() -> tuple[list[Bulgu], int]:
    bulunan: list[Bulgu] = []
    taranan = 0
    for yol in sorted(TESTLER_KOK.rglob("*.py")):
        if yol == Path(__file__).resolve():
            continue  # bu dosyadaki sentetik kaynaklar dizedir, ama kendini taramak anlamsiz
        taranan += 1
        goreli = yol.relative_to(TESTLER_KOK.parent).as_posix()
        bulunan.extend(bulgular_testler(yol.read_text(encoding="utf-8"), goreli))
    return bulunan, taranan


ASGARI_TARANAN_TEST_DOSYASI = 200


def test_testler_agacinda_dondurulmus_bugun_ve_yerel_takvim_yok() -> None:
    bulunan, taranan = _testleri_tara()
    assert taranan >= ASGARI_TARANAN_TEST_DOSYASI, f"yalnizca {taranan} test dosyasi tarandi"
    assert not bulunan, "Testlerde dondurulmus 'bugun' (TMP-FIX #167):\n" + "\n".join(
        str(b) for b in bulunan
    )


_TEST_KACAKLARI = [
    ("from app.core import timezone\n\nBUGUN = timezone.today()\n", KALIP_MODUL_DUZEYI_TODAY),
    ("from app.core.timezone import today\n\n_G = today() - 1\n", KALIP_MODUL_DUZEYI_TODAY),
    (
        "from app.core import timezone\n\ndef f(x=timezone.today()):\n    return x\n",
        KALIP_MODUL_DUZEYI_TODAY,
    ),
    ("from datetime import date\n\ndef f():\n    return date.today()\n", KALIP_DATE_TODAY),
    ("from datetime import datetime\n\ndef f():\n    return datetime.utcnow()\n", KALIP_UTCNOW),
]


@pytest.mark.parametrize(("kaynak", "beklenen"), _TEST_KACAKLARI)
def test_test_bekcisi_sentetik_kacagi_yakalar(kaynak: str, beklenen: str) -> None:
    assert beklenen in {b.kalip for b in bulgular_testler(kaynak, "sentetik.py")}


def test_test_bekcisi_cagri_aninda_today_icin_alarm_vermez() -> None:
    kaynak = (
        "from app.core import timezone\n\n"
        "def f():\n    return timezone.today()\n\n"
        "class T:\n    def g(self):\n        return timezone.today()\n"
    )
    assert bulgular_testler(kaynak, "sentetik.py") == []
