"""IZN-B2 ONARIM — SAYFA BAYRAĞI BEKÇİSİ: bir bayrak yalnız KENDİ düğmelerini açar.

Bulgu (opus çürütücü): köprü "eşiği (M, L)'ye eşit HER bayrak kapıyı açar" kuralıyla çalışırken
Görür ya da Onaylar biti yazma kapısını açıyordu (Şef'e yalnız `ik.izin_yonetimi` Onaylar →
`POST /personnel` geçiyordu). Bu bekçi o sınıfı yapısal olarak kapatır:

BOŞ bir role TEK bir (sayfa, bayrak) verilir — 100 sayfa × {Görür, Düzenler, Onaylar} — ve gerçek
kapı bağımlılıkları 460 operasyon üzerinde çalıştırılır. Açılan HER rota, düğme tablosundaki
(aşağıdaki test SABİTLERİ; uygulamadan BAĞIMSIZ elle yazıldı) uçlarla çakışmalıdır:

* Görür  → yalnız GÖRME eşiği tam `(modül, view)` olan sayfanın GET uçları (`VIEW_GATE_PAGES`);
* Düzenler → ek olarak yalnız YAZMA eşiği tam o kapı olan sayfanın yazma uçları
  (`EDIT_GATE_PAGES`) ve §2.4 sayfa kapılı uçlar (`PAGE_EDIT_ROUTES`);
* Onaylar → görme + YALNIZ `APPROVE_ROUTE_PAGES`'te o sayfa için yazılı onay uçları. Onaylar
  bayrağı HİÇBİR modül (`require_permission`) kapısını açmaz.

Mutasyon: `page_gate.gate_flags`'i eski kurala ("eşiği eşit HER bayrak") döndürmek bu testi
KIRMIZI yapar (rapora yazıldı).
"""

import pytest

from app.core.access import AccessLevel
from app.core.sayfalar import ONAY_VAR_ANAHTARLARI, SAYFA_ANAHTARLARI, PageLevel
from app.modules.roles.models import Role, RolePagePermission
from tests.modules.test_izn_b2_kapi_paritesi import ROTALAR, _kullanici, new_gate

L = AccessLevel


def _p(*keys: str) -> frozenset[str]:
    return frozenset(keys)


# --------------------------------------------------------------------------- #
# DÜĞME TABLOSU (test sabiti): hangi sayfa hangi modül kapısını hangi bayrakla açar
# --------------------------------------------------------------------------- #

#: `(modül, view)` kapısını açan sayfalar (GÖRME eşiği tam `(modül, view)`).
VIEW_GATE_PAGES: dict[str, frozenset[str]] = {
    "accounting": _p(
        "mali.yevmiye",
        "mali.hesap_plani",
        "mali.mizan",
        "mali.kdv_beyani",
        "mali.banka_mutabakati",
        "mali.donem_kapanisi",
        "mali.gelir_tablosu",
        "mali.bilanco",
        "mali.nakit_akisi",
    ),
    "ai": _p("genel.fiil_ai"),
    "boq": _p("santiye.is_kalemleri", "santiye.bolum_dagilimi", "bolum.is_kalemleri"),
    "contracts": _p(
        "teklif.teklif_hazirlama",
        "teklif.sablonlar",
        "teklif.sozlesmeler",
        "teklif.taseron_firmalar",
        "teklif.isveren_sozlesme",
        "teklif.poz_dagilimi",
        "teklif.taseron_sozlesme",
        "teklif.is_kalemi_katalogu",
        "proje.is_kalemleri",
    ),
    "dashboard": _p("genel.gosterge_paneli"),
    "documents": _p("mali.belge_arsivi", "proje.belgeler", "santiye.belgeler"),
    "earned_value": _p(
        "planlama.panel",
        "planlama.adam_saat_butcesi",
        "planlama.gunluk_rapor",
        "planlama.haftalik_qurr",
        "planlama.birim_oran_katalogu",
        "planlama.disiplin_yonetimi",
        "santiye.adam_saat_butcesi",
        "santiye.planlama_paneli",
        "santiye.gunluk_ilerleme_raporu",
        "santiye.haftalik_qurr",
        "ayarlar.planlama",
    ),
    "equipment": _p(
        "saha.makine_ekipman", "saha.makine_calisma", "saha.makine_yakit", "saha.makine_kira"
    ),
    "inventory": _p("stok.stok_depo", "santiye.stok", "bolum.malzeme"),
    "invoicing": _p("mali.fatura"),
    "payroll": _p(
        "mali.bordro", "mali.bordro_gecmis", "mali.sgk_bildirimi", "ayarlar.bordro_oranlari"
    ),
    "personnel": _p("ik.personel", "ik.izin_yonetimi", "ik.belge_sertifika"),
    "procurement": _p(
        "stok.satinalma_talepleri",
        "stok.siparisler",
        "stok.tedarikciler",
        "stok.teklif_karsilastirma",
    ),
    "progress_payments": _p(
        "mali.hakedis_isveren",
        "mali.hakedis_taseron",
        "proje.isveren_hakedis",
        "proje.taseron_hakedis",
        "santiye.hakedisler",
        "bolum.hakedis",
    ),
    "projects": _p("genel.projeler", "genel.proje_takvimi", "proje.ozet", "proje.paylasim_tablosu"),
    "sales": _p("mali.satis"),
    "settings": _p("ayarlar.denetim_gunlugu"),
    "site_diary": _p(
        "saha.gunluk_kayit",
        "santiye.gunluk_kayit",
        "santiye.gunluk_ozet",
        "santiye.gunluk_planlama",
        "bolum.gunluk_kayit",
        "bolum.gunluk_kayit_detay",
    ),
    "sites": _p("santiye.bolumler", "bolum.detay"),
    "timesheet": _p("saha.puantaj", "santiye.puantaj", "bolum.puantaj"),
    "treasury": _p("mali.hazine", "mali.cek_odeme"),
    "user_management": _p("ayarlar.kullanicilar", "ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"),
}

#: `(modül, düzey)` yazma kapısını açan sayfalar (YAZMA eşiği tam o kapı; Düzenler bayrağı).
EDIT_GATE_PAGES: dict[tuple[str, AccessLevel], frozenset[str]] = {
    ("accounting", L.full): _p("mali.yevmiye", "mali.hesap_plani", "mali.donem_kapanisi"),
    ("boq", L.full): _p("santiye.is_kalemleri", "santiye.bolum_dagilimi"),
    ("contracts", L.full): _p(
        "teklif.teklif_hazirlama",
        "teklif.sablonlar",
        "teklif.sozlesmeler",
        "teklif.taseron_firmalar",
        "teklif.isveren_sozlesme",
        "teklif.poz_dagilimi",
        "teklif.taseron_sozlesme",
        "teklif.is_kalemi_katalogu",
        "proje.is_kalemleri",
    ),
    ("documents", L.full): _p("mali.belge_arsivi", "proje.belgeler", "santiye.belgeler"),
    ("earned_value", L.draft): _p(
        "planlama.adam_saat_butcesi", "santiye.adam_saat_butcesi", "ayarlar.planlama"
    ),
    ("earned_value", L.full): _p("planlama.birim_oran_katalogu", "planlama.disiplin_yonetimi"),
    ("equipment", L.full): _p(
        "saha.makine_ekipman", "saha.makine_calisma", "saha.makine_yakit", "saha.makine_kira"
    ),
    ("inventory", L.full): _p("stok.stok_depo", "santiye.stok"),
    ("invoicing", L.full): _p("mali.fatura"),
    ("payroll", L.full): _p("mali.bordro", "mali.sgk_bildirimi"),
    ("personnel", L.full): _p("ik.personel", "ik.izin_yonetimi", "ik.belge_sertifika"),
    ("procurement", L.request): _p("stok.satinalma_talepleri"),
    ("procurement", L.full): _p(
        "stok.siparisler", "stok.tedarikciler", "stok.teklif_karsilastirma"
    ),
    ("progress_payments", L.draft): _p(
        "mali.hakedis_isveren",
        "mali.hakedis_taseron",
        "proje.isveren_hakedis",
        "proje.taseron_hakedis",
        "santiye.hakedisler",
    ),
    ("projects", L.full): _p(
        "mali.satis_blok",
        "mali.satis_unite",
        "mali.satis_toplu_uretim",
        "mali.satis_excel",
        "mali.satis_paylasim",
    ),
    ("sales", L.full): _p("mali.satis"),
    ("settings", L.full): _p("ayarlar.sirket_bilgileri"),
    ("site_diary", L.full): _p(
        "saha.gunluk_kayit",
        "santiye.gunluk_kayit",
        "santiye.gunluk_planlama",
        "bolum.gunluk_kayit_detay",
    ),
    ("sites", L.full): _p("santiye.bolumler", "bolum.detay"),
    ("timesheet", L.full): _p("saha.puantaj", "santiye.puantaj"),
    ("treasury", L.full): _p("mali.hazine", "mali.cek_odeme"),
    ("user_management", L.full): _p("ayarlar.kullanicilar"),
}

_HAKEDIS_ISVEREN = _p("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler")
_HAKEDIS_TASERON = _p("mali.hakedis_taseron", "proje.taseron_hakedis", "santiye.hakedisler")
_EV_FREEZE = _p("planlama.adam_saat_butcesi", "santiye.adam_saat_butcesi")
_EV_DAILY = _p("planlama.gunluk_rapor", "santiye.gunluk_ilerleme_raporu")
_EV_UNLOCK = _EV_FREEZE | _EV_DAILY
_DAY = "/sites/{site_id}/earned-value"

#: ONAY EYLEMİ uçları ve onları açan Onaylar sayfaları (düğme tablosunun "A" satırları).
APPROVE_ROUTE_PAGES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/leave-requests/{request_id}/approve"): _p("ik.izin_yonetimi"),
    ("POST", "/leave-requests/{request_id}/reject"): _p("ik.izin_yonetimi"),
    ("POST", "/journal-entries/{entry_id}/post"): _p("mali.yevmiye"),
    ("POST", "/invoices/{invoice_id}/approve"): _p("mali.fatura"),
    ("POST", "/invoices/{invoice_id}/mark-collected"): _p("mali.fatura"),
    ("POST", "/payroll/lines/{line_id}/approve"): _p("mali.bordro"),
    ("POST", "/payroll/lines/{line_id}/reject"): _p("mali.bordro"),
    ("POST", "/payroll/periods/{period_id}/approve"): _p("mali.bordro"),
    ("POST", "/payroll/periods/{period_id}/pay"): _p("mali.bordro"),
    ("POST", "/payroll/periods/{period_id}/sgk-submit"): _p("mali.sgk_bildirimi"),
    ("POST", "/financial-instruments/{instrument_id}/status"): _p("mali.cek_odeme"),
    ("POST", "/sales/{sale_id}/activate"): _p("mali.satis"),
    ("POST", "/sales/{sale_id}/transfer-deed"): _p("mali.satis"),
    ("POST", "/sales/{sale_id}/cancel"): _p("mali.satis"),
    ("POST", "/sales/installments/{installment_id}/pay"): _p("mali.satis"),
    ("POST", "/equipment/rental-invoices/{invoice_id}/approve"): _p("saha.makine_kira"),
    ("POST", "/equipment/rental-invoices/{invoice_id}/pay"): _p("saha.makine_kira"),
    ("POST", "/equipment/rental-invoices/{invoice_id}/reject"): _p("saha.makine_kira"),
    ("POST", "/purchase-requests/{request_id}/quotes/{quote_id}/select-and-order"): _p(
        "stok.teklif_karsilastirma"
    ),
    ("POST", "/purchase-requests/{request_id}/approve"): _p("stok.satinalma_talepleri"),
    ("POST", "/purchase-requests/{request_id}/reject"): _p("stok.satinalma_talepleri"),
    ("POST", "/progress-payments/{payment_id}/approve"): _HAKEDIS_ISVEREN,
    ("POST", "/progress-payments/{payment_id}/reject"): _HAKEDIS_ISVEREN,
    ("POST", "/progress-payments/{payment_id}/mark-paid"): _HAKEDIS_ISVEREN,
    ("POST", "/subcontractor-progress-payments/{payment_id}/approve"): _HAKEDIS_TASERON,
    ("POST", "/subcontractor-progress-payments/{payment_id}/reject"): _HAKEDIS_TASERON,
    ("POST", "/subcontractor-progress-payments/{payment_id}/mark-paid"): _HAKEDIS_TASERON,
    ("POST", "/diary/{entry_id}/reopen"): _p("saha.gunluk_kayit"),
    ("POST", "/accounting-periods/{year}/{month}/reopen"): _p("mali.donem_kapanisi"),
    ("POST", f"{_DAY}/budget/freeze"): _EV_FREEZE,
    ("POST", f"{_DAY}/reports/daily/{{day}}/approve"): _EV_DAILY,
    ("POST", f"{_DAY}/days/{{day}}/unlock"): _EV_UNLOCK,
    ("POST", "/offers/{offer_id}/convert"): _p("teklif.teklif_hazirlama"),
}

#: §2.4 sayfa kapılı (Düzenler bayraklı) uçlar.
PAGE_EDIT_ROUTES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/projects"): _p("genel.projeler"),
    ("POST", "/employers"): _p("genel.projeler"),
    ("PUT", "/payroll/tax-brackets/{year}/{income_kind}"): _p("ayarlar.bordro_oranlari"),
    ("PUT", "/approvals/settings"): _p("ayarlar.onay_rolleri"),
    ("GET", "/approvals/roles"): _p("ayarlar.onay_rolleri"),
    ("PUT", "/approvals/roles/{user_id}"): _p("ayarlar.onay_rolleri"),
    ("POST", "/roles"): _p("ayarlar.rol_yonetimi"),
    ("PATCH", "/roles/{role_id}"): _p("ayarlar.rol_yonetimi"),
    ("POST", "/roles/{role_id}/copy"): _p("ayarlar.rol_yonetimi"),
    ("PUT", "/roles/{role_id}/pages"): _p("ayarlar.sayfa_izinleri"),
}


# --------------------------------------------------------------------------- #
# Bekçi
# --------------------------------------------------------------------------- #


def _perm_izinli(page: str, flag: str, module: str, level: AccessLevel) -> bool:
    if level is L.view:
        return page in VIEW_GATE_PAGES.get(module, frozenset())
    if level in (L.draft, L.request, L.full):
        return flag == "edit" and page in EDIT_GATE_PAGES.get((module, level), frozenset())
    return False  # approve/admin modül kapısını sayfa bayrağı AÇAMAZ


def _gate_izinli(route, gate, page: str, flag: str) -> bool:
    if gate.kind == "perm":
        return _perm_izinli(page, flag, *gate.spec)
    if gate.kind == "any":
        return any(_perm_izinli(page, flag, m, lv) for m, lv in gate.spec)
    pages, gate_flag = gate.spec  # page | chain
    if gate_flag == "approve":
        return flag == "approve" and page in APPROVE_ROUTE_PAGES.get(route, frozenset())
    return flag == "edit" and page in PAGE_EDIT_ROUTES.get(route, frozenset())


async def _tek_hucreli_kullanici(session, page: str, flag: str, sira: int):
    role = Role(key=f"bk_{sira}", name=f"bk_{sira}", emoji="", description="", is_system=False)
    session.add(role)
    await session.flush()
    level, approve = {
        "view": (PageLevel.view, False),
        "edit": (PageLevel.edit, False),
        "approve": (PageLevel.view, True),
    }[flag]
    session.add(
        RolePagePermission(role_id=role.id, page_key=page, level=level, can_approve=approve)
    )
    await session.flush()
    return await _kullanici(session, role.key, f"bk_{sira}@bekci.co")


async def _acilan_rotalar(session, user) -> list[tuple[str, str]]:
    cache: dict[tuple, bool] = {}
    acilan = []
    for route, gates in ROTALAR.items():
        if not gates:
            continue
        gecti = True
        for gate in gates:
            if gate.key not in cache:
                cache[gate.key] = await new_gate(session, user, gate)
            gecti = gecti and cache[gate.key]
        if gecti:
            acilan.append(route)
    return acilan


def _ihlaller(acilan, page: str, flag: str) -> list[tuple[str, str]]:
    ihlal = []
    for route in acilan:
        if not all(_gate_izinli(route, gate, page, flag) for gate in ROTALAR[route]):
            # `any` kapısında alt kapılardan biri yeter; `_gate_izinli` bunu zaten ele alır.
            ihlal.append(route)
    return ihlal


@pytest.mark.parametrize("flag", ["view", "edit", "approve"])
async def test_tek_bayrak_yalniz_kendi_dugmelerini_acar_100_sayfa(seeded_db, flag) -> None:
    sira = 0
    for page in SAYFA_ANAHTARLARI:
        if flag == "approve" and page not in ONAY_VAR_ANAHTARLARI:
            continue
        sira += 1
        user = await _tek_hucreli_kullanici(
            seeded_db, page, flag, sira + {"view": 0, "edit": 1000, "approve": 2000}[flag]
        )
        acilan = await _acilan_rotalar(seeded_db, user)
        ihlal = _ihlaller(acilan, page, flag)
        assert ihlal == [], f"{page} [{flag}] tablo DIŞI rota açtı: {ihlal}"
        onay_uclari = {r for r, pages in APPROVE_ROUTE_PAGES.items() if page in pages}
        if flag == "approve":
            # TAMLIK: Onaylar bayrağı kendi onay uçlarının HEPSİNİ açar.
            eksik = onay_uclari - set(acilan)
            assert eksik == set(), f"{page} Onaylar bayrağı onay ucunu AÇMIYOR: {sorted(eksik)}"
        else:
            # Görür/Düzenler hiçbir onay ucunu açamaz (onay uçları modül kapısından geçmez).
            sizan = onay_uclari & set(acilan)
            assert sizan == set(), f"{page} [{flag}] onay ucunu açtı: {sorted(sizan)}"


# --- çürütücünün somut bulguları (regresyon) -------------------------------------------------


async def test_onaylar_biti_yazma_kapisi_acmaz_cürütücü_bulguları(seeded_db) -> None:
    sira = 5000
    for page, kapali in (
        ("ik.izin_yonetimi", ("POST", "/personnel")),
        ("mali.yevmiye", ("POST", "/journal-entries")),
    ):
        sira += 1
        user = await _tek_hucreli_kullanici(seeded_db, page, "approve", sira)
        acilan = await _acilan_rotalar(seeded_db, user)
        assert kapali not in acilan, f"{page} Onaylar → {kapali} açıldı"


async def test_gorur_biti_yazma_kapisi_acmaz_projects_full_ve_draft(seeded_db) -> None:
    sira = 6000
    for page in (
        "mali.satis_toplu_uretim",
        "mali.satis_blok",
        "mali.satis_unite",
        "mali.satis_excel",
    ):
        sira += 1
        user = await _tek_hucreli_kullanici(seeded_db, page, "view", sira)
        acilan = set(await _acilan_rotalar(seeded_db, user))
        yazma = {r for r in acilan if r[0] in ("POST", "PATCH", "PUT", "DELETE")}
        assert yazma == set(), f"{page} Görür → yazma açıldı: {sorted(yazma)}"
