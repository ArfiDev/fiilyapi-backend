"""IZN-B1 ONARIM — sayfa EŞİK TABLOSU bekçisi ("geçişte kimsenin fiilî yetkisi değişmez").

CEO kararı: hücre = eski seviye o sayfadaki eylemin bugünkü GERÇEK eşiğini karşılıyorsa. Katalogdaki
eşikler (`app/core/sayfalar.py::_ESIKLER`) burada envanterden (`IZN-ENVANTER.md` "İzin modülü
(FE okuması · BE kapısı)", "Düzenler kapsamı", "Onay eylemi" sütunları + `frontend/src` ön yüz
kapıları) ELLE alınmış BAĞIMSIZ bir kopyayla çakılır; ardından seed rollerinin türetilmiş
hücreleri bu tabloyla BİREBİR eşitlenir ve "genişleme sayısı" 0 olarak ölçülür.

Tablonun okunuşu: aşağıdaki kümeler envanter satır numaralarıdır.
"""

import pytest

from app.core.access import AccessLevel
from app.core.sayfalar import SAYFA_BY_KEY, SAYFALAR, PageLevel
from app.modules.roles import seed_data

L = AccessLevel
_SIRA = [L.none, L.view, L.draft, L.request, L.approve, L.full, L.admin]

# --- GÖRME: varsayılan = sayfanın eski modülünde view; istisnalar ---
GORME_ISTISNA = {
    2: [("approvals", L.none)],  # Onay Kutusu: bilerek kapısız
    # 36, 37, 39, 40 (satış alt sayfaları): IZN-B5a madde 3 (CEO onaylı bilinçli genişleme) —
    # Görür biti veri getirmiyordu (eşik draft); eşik `view`e çekildi → varsayılan (istisna YOK).
    38: [("projects", L.full)],  # FE hasAtLeast(full) değilse AccessDenied
    61: [("projects", L.view), ("sites", L.view)],  # iki modül
    89: [("settings", L.none)],  # GET /company kapısız
    96: [("approvals", L.none)],  # GET /approvals/settings kapısız
}
# --- YAZMA (Düzenler): yazma eyleminin gerçek eşiği (uç kapısı ve ön yüz kapısının sıkısı) ---
YAZMA_DRAFT = {17, 50, 51, 65, 66, 72, 78, 92}
YAZMA_REQUEST = {31}
YAZMA_FULL = {
    7, 8, 9, 10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 32, 33, 34, 35,
    36, 37, 38, 39, 40, 41, 42, 46, 47, 48, 49, 55, 57, 59, 64, 67, 68, 69, 70, 71, 73, 74, 75,
    77, 82, 88, 89, 93,
}  # fmt: skip
YAZMA_ADMIN = {5, 94, 95, 96, 97}
YAZMA_ISTISNA = {61: [("projects", L.view), ("sites", L.full)]}  # Şantiye ekle = sites full
YAZMA_YOK = {
    1, 3, 6, 16, 18, 19, 43, 44, 45, 52, 53, 54, 56, 62, 63, 76, 79, 80, 81, 83, 84, 85, 86, 87,
    100,
}  # fmt: skip
# --- ONAY (Onaylar): sayfadaki onay eylemini GERÇEKTEN açan eşik ---
# IZN-B2 DÜZELTMESİ (CEO): hakediş sayfaları (50, 51, 65, 66, 72) admin → approve, talep (31)
# request → approve. "Onayı Geri Al" (admin) Onaylar'a BAĞLI DEĞİL: yalnız Sistem Yöneticisi.
ONAY_REQUEST: set[int] = set()
ONAY_APPROVE = {17, 18, 31, 50, 51, 65, 66, 72, 78, 80}
# 2 (Onay Kutusu): IZN-B5a madde 5 — Onaylar biti işlevsizdi, katalogda onay_var=False.
ONAY_FULL = {11, 14, 34, 35, 41, 47, 49, 55, 57}
# yeniden aç · dönemi yeniden aç · onayı geri al = yalnız admin
ONAY_ADMIN = {12, 46, 73, 88}
ONAY_ISTISNA = {22: [("projects", L.admin), ("contracts", L.full)]}  # dönüştür

#: Eski sade eşlemeye ("approve/full/admin → Onaylar, draft+ → Düzenler") göre eşiği DEĞİŞEN
#: sayfa sayıları (modüllü 93 sayfa içinde) — rapora yazılan özet.
YAZMA_ESIGI_DRAFTTAN_FARKLI = 85
ONAY_ESIGI_APPROVE_DISI = 14
GORME_ESIGI_VIEW_DISI = 5


def _modul(no: int) -> str:
    return next(s.eski_modul for s in SAYFALAR if s.envanter_no == no)


def _beklenen_esik(kume_seviye: dict[int, L], istisna: dict[int, list], no: int):
    if no in istisna:
        return tuple(istisna[no])
    if no in kume_seviye:
        return ((_modul(no), kume_seviye[no]),)
    return None


def _gorme(no: int):
    return _beklenen_esik({}, GORME_ISTISNA, no) or ((_modul(no), L.view),)


def _yazma(no: int):
    seviye = {
        **dict.fromkeys(YAZMA_DRAFT, L.draft),
        **dict.fromkeys(YAZMA_REQUEST, L.request),
        **dict.fromkeys(YAZMA_FULL, L.full),
        **dict.fromkeys(YAZMA_ADMIN, L.admin),
    }
    return _beklenen_esik(seviye, YAZMA_ISTISNA, no)


def _onay(no: int):
    seviye = {
        **dict.fromkeys(ONAY_REQUEST, L.request),
        **dict.fromkeys(ONAY_APPROVE, L.approve),
        **dict.fromkeys(ONAY_FULL, L.full),
        **dict.fromkeys(ONAY_ADMIN, L.admin),
    }
    return _beklenen_esik(seviye, ONAY_ISTISNA, no)


def _modullu() -> list:
    return [s for s in SAYFALAR if s.eski_modul is not None]


def _karsilar(esik, hucreler: dict[str, L]) -> bool:
    return all(_SIRA.index(hucreler[modul]) >= _SIRA.index(seviye) for modul, seviye in esik)


def _rol_modul_seviyeleri() -> dict[str, dict[str, L]]:
    """13 rolün modül → eski düzey haritası (Sistem Yöneticisi hariç)."""
    roller: dict[str, dict[str, L]] = {}
    for role_key in seed_data.ROLE_ORDER:
        if role_key == "system_admin":
            continue
        i = seed_data.ROLE_ORDER.index(role_key)
        roller[role_key] = {m: c[i][0] for m, c in seed_data.MATRIX.items()}
    for role_key in seed_data.IZN_ROLE_ORDER:
        i = seed_data.IZN_ROLE_ORDER.index(role_key)
        roller[role_key] = {m: c[i][0] for m, c in seed_data.IZN_MATRIX.items()}
    return roller


def _beklenen_hucre(no: int, hucreler: dict[str, L]) -> tuple[str, bool]:
    if not _karsilar(_gorme(no), hucreler):
        return "none", False
    yazma, onay = _yazma(no), _onay(no)
    level = "edit" if yazma is not None and _karsilar(yazma, hucreler) else "view"
    return level, onay is not None and _karsilar(onay, hucreler)


def test_katalog_esikleri_envanter_tablosuyla_BIREBIR() -> None:
    for sayfa in _modullu():
        no = sayfa.envanter_no
        assert sayfa.gorme == tuple(_gorme(no)), no
        assert sayfa.yazma == _yazma(no), no
        assert sayfa.onay == _onay(no), no


def test_modulsuz_sayfalarin_esigi_yok() -> None:
    for sayfa in SAYFALAR:
        if sayfa.eski_modul is None:
            assert (sayfa.gorme, sayfa.yazma, sayfa.onay) == (None, None, None), sayfa.key


def test_onay_var_ancak_onay_esigi_olan_sayfada() -> None:
    for sayfa in SAYFALAR:
        assert sayfa.onay_var == (sayfa.onay is not None), sayfa.key


def test_ozet_sayilari_rapordaki_gibi() -> None:
    modullu = _modullu()
    assert len(modullu) == 93
    draft = lambda s: s.yazma == ((s.eski_modul, L.draft),)  # noqa: E731
    assert sum(1 for s in modullu if not draft(s)) == YAZMA_ESIGI_DRAFTTAN_FARKLI
    assert sum(1 for s in modullu if s.onay and s.onay != ((s.eski_modul, L.approve),)) == (
        ONAY_ESIGI_APPROVE_DISI
    )
    assert sum(1 for s in modullu if s.gorme != ((s.eski_modul, L.view),)) == GORME_ESIGI_VIEW_DISI


def test_seed_rollerinin_hucreleri_esik_tablosuyla_BIREBIR_ve_GENISLEME_SIFIR() -> None:
    genisleme = 0
    for role_key, hucreler in _rol_modul_seviyeleri().items():
        turetilen = seed_data.PAGE_MATRIX[role_key]
        for sayfa in _modullu():
            beklenen = _beklenen_hucre(sayfa.envanter_no, hucreler)
            gercek = (turetilen[sayfa.key][0].value, turetilen[sayfa.key][1])
            if role_key in seed_data.IZN_SAYFA_ISTISNALARI and (
                sayfa.key in seed_data.IZN_SAYFA_ISTISNALARI[role_key]
            ):
                continue  # yeni rolün bilinçli sayfa istisnası (plan §7); ayrıca test edilir
            assert gercek == beklenen, (role_key, sayfa.key, gercek, beklenen)
            sira = ["none", "view", "edit"]
            if sira.index(gercek[0]) > sira.index(beklenen[0]) or (gercek[1] and not beklenen[1]):
                genisleme += 1
    assert genisleme == 0


@pytest.mark.parametrize(
    ("role_key", "page_key", "beklenen"),
    [
        # (A) projeler: Düzenler (proje oluştur) yalnız projects:admin
        ("patron", "genel.projeler", ("view", False)),
        ("project_manager", "genel.projeler", ("view", False)),
        # (A) teklif dönüştür: PM ve Patron'da KAPALI
        ("project_manager", "teklif.teklif_hazirlama", ("edit", False)),
        ("patron", "teklif.teklif_hazirlama", ("edit", False)),
        # (A) yeniden aç / dönem aç / onayı geri al: yalnız admin
        ("site_chief", "saha.gunluk_kayit", ("edit", False)),
        ("patron", "saha.gunluk_kayit", ("edit", False)),
        ("patron", "santiye.gunluk_kayit", ("edit", False)),
        ("patron", "bolum.gunluk_kayit_detay", ("edit", False)),
        ("accounting", "mali.donem_kapanisi", ("edit", False)),
        ("patron", "mali.donem_kapanisi", ("edit", False)),
        ("accounting", "mali.hakedis_isveren", ("edit", True)),  # IZN-B2: Onayla/Ödendi = approve
        ("project_manager", "mali.hakedis_taseron", ("edit", True)),
        ("patron", "proje.isveren_hakedis", ("edit", True)),
        ("patron", "proje.taseron_hakedis", ("edit", True)),
        ("patron", "santiye.hakedisler", ("edit", True)),
        ("site_chief", "mali.hakedis_isveren", ("edit", False)),  # draft: onaylayamaz
        ("project_manager", "stok.satinalma_talepleri", ("edit", True)),  # procurement approve
        # (A) Ayarlar yazma
        ("patron", "ayarlar.onay_rolleri", ("view", False)),
        ("patron", "ayarlar.bordro_oranlari", ("view", False)),
        # (B) full eşikli katalog/sipariş sayfaları
        ("project_manager", "planlama.birim_oran_katalogu", ("edit", False)),
        ("site_chief", "planlama.birim_oran_katalogu", ("view", False)),
        ("site_chief", "planlama.disiplin_yonetimi", ("view", False)),
        ("project_manager", "stok.siparisler", ("view", False)),  # PM procurement approve < full
        ("procurement", "stok.siparisler", ("edit", False)),
        ("procurement", "stok.tedarikciler", ("edit", False)),
        ("project_manager", "stok.teklif_karsilastirma", ("view", False)),
        ("procurement", "stok.teklif_karsilastirma", ("edit", True)),
        # (C) Görmez → Görür menü genişlemesi YOK
        # IZN-B5a madde 3 (CEO onaylı bilinçli genişleme): 4 satış alt sayfasının Görür biti artık
        # `projects:view` ile açılır (eşik draft → view); Düzenler eşiği (full) DEĞİŞMEDİ.
        ("hr_manager", "mali.satis_blok", ("view", False)),
        ("hr_manager", "mali.satis_unite", ("view", False)),
        ("hr_manager", "mali.satis_excel", ("view", False)),
        ("hr_manager", "mali.satis_paylasim", ("view", False)),
        ("project_manager", "mali.satis_blok", ("edit", False)),
        ("site_chief", "mali.satis_toplu_uretim", ("none", False)),
        ("patron", "mali.satis_toplu_uretim", ("edit", False)),
        # Onay eylemi gerçekten açılıyorsa Onaylar
        ("accounting", "mali.yevmiye", ("edit", True)),
        ("site_chief", "planlama.adam_saat_butcesi", ("edit", True)),
        ("field_engineer", "planlama.adam_saat_butcesi", ("edit", False)),
    ],
)
def test_ceo_listesindeki_genislemeler_kisilmis(role_key, page_key, beklenen) -> None:
    hucre = seed_data.PAGE_MATRIX[role_key][page_key]
    assert (hucre[0].value, hucre[1]) == beklenen


def test_ayarlar_kapisiz_sayfalar_bugunku_gorunurlugu_korur() -> None:
    """GET kapısız sayfalar (Şirket Bilgileri, Onay Kutusu, Onay Eşiği) bugün herkese açık."""
    for role_key, hucreler in seed_data.PAGE_MATRIX.items():
        if role_key == "viewer":
            continue  # Görüntüleyici: TÜM ayarlar.* Görmez (plan §7)
        for key in ("ayarlar.sirket_bilgileri", "ayarlar.onay_rolleri", "genel.onay_kutusu"):
            assert hucreler[key][0] is not PageLevel.none, (role_key, key)


def test_yeni_roller_plan_7_ile_hizali() -> None:
    plan = seed_data.PAGE_MATRIX["planning_engineer"]
    saha = seed_data.PAGE_MATRIX["field_engineer"]
    # Planlama Müh. = Saha Müh. + Planlama Düzenler; Onaylar YOK; boq Saha Müh. gibi.
    assert plan["planlama.adam_saat_butcesi"] == (PageLevel.edit, False)
    assert plan["planlama.birim_oran_katalogu"] == (PageLevel.edit, False)
    assert plan["planlama.disiplin_yonetimi"] == (PageLevel.edit, False)
    assert plan["santiye.is_kalemleri"] == saha["santiye.is_kalemleri"] == (PageLevel.none, False)
    farklar = {k for k in plan if plan[k] != saha[k]}
    assert farklar == {"planlama.birim_oran_katalogu", "planlama.disiplin_yonetimi"}
    # Görüntüleyici: TÜM ayarlar.* Görmez.
    viewer = seed_data.PAGE_MATRIX["viewer"]
    ayarlar = [k for k in viewer if k.startswith("ayarlar.")]
    assert len(ayarlar) == 13
    assert all(viewer[k] == (PageLevel.none, False) for k in ayarlar)
    # Finans Müdürü = Muhasebe sütunu (procurement dahil kapalı).
    assert seed_data.PAGE_MATRIX["finance_manager"] == seed_data.PAGE_MATRIX["accounting"]
    assert SAYFA_BY_KEY["stok.siparisler"].eski_modul == "procurement"
