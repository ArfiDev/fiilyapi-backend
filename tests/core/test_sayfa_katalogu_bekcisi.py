"""IZN-B1 — sayfa kataloğu (`app/core/sayfalar.py`) bekçileri.

Katalog `IZN-ENVANTER.md`den BİR KEZ üretildi ve sonrasında tek kaynak koddur. Bu dosya onu
envanterin KENDİ tablolarına karşı çakar: aşağıdaki sabitler (`ENVANTER_*`) envanterin §1 ve §4
tablolarından ve onaylı Sayfa İzinleri mockup'ından ELLE alınmış bağımsız bir kopyadır — katalog
değişirse bu kopya da bilinçli güncellenmek zorundadır (sessiz kayma yok).

DB gerektirmez.
"""

import re
from collections import Counter

from app.core.sayfalar import (
    ESKI_MODULLER,
    GRUP_ADLARI,
    ONAY_VAR_ANAHTARLARI,
    SAYFA_ANAHTARLARI,
    SAYFA_BY_KEY,
    SAYFALAR,
    PageGroup,
    PageKey,
    PageKind,
    PageLevel,
    sistem_yoneticisi_sayfalari,
)
from app.modules.roles.seed_data import MODULES, MODULSUZ_VARSAYILAN

#: IZN-ENVANTER §4 "birincil satır numaraları" (modül → envanter satırları).
ENVANTER_MODUL_SATIRLARI: dict[str, list[int]] = {
    "dashboard": [1],
    "approvals": [2, 96],
    "projects": [5, 6, 36, 37, 38, 39, 40, 61, 62, 63],
    "sites": [68, 82],
    "site_diary": [12, 73, 76, 77, 87, 88],
    "timesheet": [7, 70, 84],
    "personnel": [13, 14, 15],
    "payroll": [55, 56, 57, 97],
    "inventory": [30, 71, 85],
    "procurement": [31, 32, 33, 34],
    "progress_payments": [50, 51, 65, 66, 72, 86],
    "accounting": [41, 42, 43, 44, 45, 46, 52, 53, 54],
    "invoicing": [47],
    "treasury": [48, 49],
    "settings": [89, 100],
    "user_management": [93, 94, 95],
    "boq": [69, 75, 83],
    "contracts": [22, 23, 24, 25, 26, 27, 28, 29, 64],
    "sales": [35],
    "documents": [59, 67, 74],
    "equipment": [8, 9, 10, 11],
    "ai": [3],
    "earned_value": [16, 17, 18, 19, 20, 21, 78, 79, 80, 81, 92],
}
#: Envanter "anahtarsız 7 satır".
ENVANTER_ANAHTARSIZ = [4, 58, 60, 90, 91, 98, 99]

#: Onaylı Sayfa İzinleri mockup'ında "Onaylar" kutucuğu basılan 73 açık satır + grupları kapalı
#: 27 satır için karar (#2 Onay Kutusu, #14 İzin Yönetimi, #31 Satın Alma Talepleri,
#: #34 Teklif Karşılaştırma). Toplam 25.
#: IZN-B5a madde 5 (CEO onaylı): #2 Onay Kutusu listeden ÇIKTI — Onaylar biti işlevsizdi
#: (onay kutusunda onay/ret her evrakın KENDİ sayfa bayrağıyla kapılıdır). Toplam 24.
ENVANTER_ONAY_VAR = [
    11, 12, 14, 17, 18, 22, 31, 34, 35, 41, 46, 47, 49, 50, 51, 55, 57, 65, 66, 72, 73, 78,
    80, 88,
]  # fmt: skip

#: Envanter §1 satır sayıları + mockup grup sayıları (9 menü grubu).
GRUP_SAYILARI: dict[PageGroup, int] = {
    PageGroup.genel: 6,
    PageGroup.saha: 6,
    PageGroup.ik: 3,
    PageGroup.planlama: 6,
    PageGroup.teklif: 8,
    PageGroup.stok: 5,
    PageGroup.mali: 25,
    PageGroup.proje_ici: 28,
    PageGroup.ayarlar: 13,
}

ANAHTAR_DESENI = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")
KAYNAK_DESENI = re.compile(r"^[a-z][a-z0-9_]*$")
PAGE_KEY_UZUNLUK_TAVANI = 64  # role_page_permissions.page_key varchar(64)


def test_katalog_tam_100_sayfadir() -> None:
    # Katalog büyürse/küçülürse: (1) seed + (2) CANLIDAKİ eski rollere BACKFILL migration ŞART
    # (yoksa hücresiz sayfa `none` sayılır: sessiz kapanma). Bkz. test_izn_b6me_seed_hucre_bekcisi.
    mesaj = "Katalog sayısı değişti: eski rollere backfill migration ŞART (hücre eksik kalmasın)"
    assert len(SAYFALAR) == 100, mesaj
    assert len(SAYFA_ANAHTARLARI) == 100, mesaj


def test_anahtarlar_tekil_ve_bicimli() -> None:
    assert len(set(SAYFA_ANAHTARLARI)) == len(SAYFA_ANAHTARLARI)
    for key in SAYFA_ANAHTARLARI:
        assert ANAHTAR_DESENI.match(key), key
        assert len(key) <= PAGE_KEY_UZUNLUK_TAVANI, key


def test_envanter_numaralari_1den_100e_tam_ve_tekil() -> None:
    assert sorted(s.envanter_no for s in SAYFALAR) == list(range(1, 101))


def test_eski_modul_ESKI_23_modulden_biridir_ya_da_anahtarsiz_yedidir() -> None:
    seed_modulleri = {row["key"] for row in MODULES}
    assert len(seed_modulleri) == 23
    # Katalogdaki 23'lük küme `roles/seed_data.MODULES` ile birebir aynı (iki liste ayrışamaz).
    assert seed_modulleri == ESKI_MODULLER
    for sayfa in SAYFALAR:
        assert sayfa.eski_modul is None or sayfa.eski_modul in seed_modulleri, sayfa.key


def test_eski_modul_envanter_modul_tablosuyla_BIREBIR() -> None:
    """Envanter §4: hangi satır hangi modülün 'birincil' kapısı."""
    katalog: dict[str, list[int]] = {}
    for sayfa in SAYFALAR:
        if sayfa.eski_modul is not None:
            katalog.setdefault(sayfa.eski_modul, []).append(sayfa.envanter_no)
    assert {k: sorted(v) for k, v in katalog.items()} == ENVANTER_MODUL_SATIRLARI

    anahtarsiz = sorted(s.envanter_no for s in SAYFALAR if s.eski_modul is None)
    assert anahtarsiz == sorted(ENVANTER_ANAHTARSIZ)


def test_modulsuz_sayfalarin_seed_varsayilani_var_ve_fazlasi_yok() -> None:
    modulsuz = {s.key for s in SAYFALAR if s.eski_modul is None}
    assert set(MODULSUZ_VARSAYILAN) == modulsuz
    assert all(isinstance(level, PageLevel) for level in MODULSUZ_VARSAYILAN.values())


def test_grup_sayilari_envanter_ve_mockupla_ayni() -> None:
    sayim = Counter(s.grup for s in SAYFALAR)
    assert dict(sayim) == GRUP_SAYILARI
    assert set(GRUP_ADLARI) == set(PageGroup)
    # Menü kümesi (#1–#59) 59, Geliştirme (#60) Ayarlar grubunda, proje içi 28, Ayarlar #89–#100.
    assert sum(1 for s in SAYFALAR if s.envanter_no <= 59) == 59


def test_tur_proje_ancak_proje_ici_grubundadir() -> None:
    for sayfa in SAYFALAR:
        assert (sayfa.tur is PageKind.proje) == (sayfa.grup is PageGroup.proje_ici), sayfa.key
        assert (sayfa.alt_grup is not None) == (sayfa.grup is PageGroup.proje_ici), sayfa.key


def test_onay_var_kumesi_envanter_ve_mockupla_ayni() -> None:
    assert sorted(SAYFA_BY_KEY[k].envanter_no for k in ONAY_VAR_ANAHTARLARI) == sorted(
        ENVANTER_ONAY_VAR
    )


def test_rotalar_tekil() -> None:
    rotalar = [s.rota for s in SAYFALAR]
    assert len(set(rotalar)) == len(rotalar)


def test_kaynak_adi_bicimli_ve_birden_cok_sayfayi_toplayabilir() -> None:
    for sayfa in SAYFALAR:
        assert KAYNAK_DESENI.match(sayfa.kaynak), sayfa.key
    # Envanter §2 ikizleri: aynı veriyi besleyen sayfalar AYNI kaynakta toplanır.
    assert (
        SAYFA_BY_KEY["mali.hakedis_isveren"].kaynak == SAYFA_BY_KEY["proje.isveren_hakedis"].kaynak
    )
    assert SAYFA_BY_KEY["saha.puantaj"].kaynak == SAYFA_BY_KEY["santiye.puantaj"].kaynak
    assert SAYFA_BY_KEY["stok.stok_depo"].kaynak == SAYFA_BY_KEY["bolum.malzeme"].kaynak


def test_ikizler_kok_sayfada_ve_proje_icine_bakar_ve_ayni_kaynaktadir() -> None:
    kok_ikizli = {s.key for s in SAYFALAR if s.ikizler}
    # Envanter §2 "Kök ↔ şantiye ikizleri" kümesi: 10 kök sayfa.
    assert {SAYFA_BY_KEY[k].envanter_no for k in kok_ikizli} == {
        7, 12, 16, 17, 18, 19, 30, 50, 51, 59
    }  # fmt: skip
    for key in kok_ikizli:
        kok = SAYFA_BY_KEY[key]
        assert kok.tur is PageKind.sirket
        for ikiz_key in kok.ikizler:
            ikiz = SAYFA_BY_KEY[ikiz_key]
            assert ikiz.tur is PageKind.proje, (key, ikiz_key)
    # İkiz kümesi aynı modül kapısını paylaşır ("aynı modül, aynı kapı" — envanter §2), yalnız
    # taşeron/işveren hakediş ikizleri ve günlük kayıt eklentileri aynı `eski_modul`dadır.
    for key in kok_ikizli:
        kok = SAYFA_BY_KEY[key]
        for ikiz_key in kok.ikizler:
            assert SAYFA_BY_KEY[ikiz_key].eski_modul == kok.eski_modul, (key, ikiz_key)


def test_page_key_enum_katalogla_birebir() -> None:
    assert [m.value for m in PageKey] == list(SAYFA_ANAHTARLARI)


def test_sistem_yoneticisi_her_sayfada_duzenler_ve_onay_eylemi_varsa_onaylar() -> None:
    harita = sistem_yoneticisi_sayfalari()
    assert set(harita) == set(SAYFA_ANAHTARLARI)
    for key, (level, approve) in harita.items():
        assert level is PageLevel.edit
        assert approve is SAYFA_BY_KEY[key].onay_var
