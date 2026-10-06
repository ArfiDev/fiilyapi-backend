"""IZN-B2 ONARIM — SAYFA BAYRAĞI BEKÇİSİ: bir bayrak yalnız KENDİ düğmelerini açar.

Bulgu (opus çürütücü): köprü "eşiği (M, L)'ye eşit HER bayrak kapıyı açar" kuralıyla çalışırken
Görür ya da Onaylar biti yazma kapısını açıyordu (Şef'e yalnız `ik.izin_yonetimi` Onaylar →
`POST /personnel` geçiyordu). Bu bekçi o sınıfı yapısal olarak kapatır:

BOŞ bir role TEK bir (sayfa, bayrak) verilir — 100 sayfa × {Görür, Düzenler, Onaylar} — ve gerçek
kapı bağımlılıkları 462 operasyon üzerinde çalıştırılır. Açılan HER rota, düğme tablosundaki
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


#: IZN-B5a madde 3 (CEO onaylı bilinçli genişleme): görme eşiği `draft/full` → `view`.
SATIS_ALT_SAYFALAR = (
    "mali.satis_blok",
    "mali.satis_unite",
    "mali.satis_excel",
    "mali.satis_paylasim",
)
#: Bu uçlar ZATEN `projects:view` kapısındaydı (yazma yetkisi servis içi `documents` denetimiyle
#: ayrıca sınanır); `projects:view` bayrağı taşıyan HER sayfa (artık satış alt sayfaları da) açar.
PROJECTS_VIEW_YAZMA_UCLARI = frozenset(
    {("PATCH", "/units/documents/{link_id}"), ("POST", "/units/{owner_id}/documents")}
)


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
    "projects": _p(
        "genel.projeler",
        "genel.proje_takvimi",
        "proje.ozet",
        "proje.paylasim_tablosu",
        # bilinçli fark (IZN-B5a madde 3, CEO onaylı): satış alt sayfalarının Görür biti veri
        # getirmiyordu (görme eşiği draft/full); eşik `view`e çekildi → `projects:view` kapısı.
        *SATIS_ALT_SAYFALAR,
    ),
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
# IZN-B5b: accounting/contracts/equipment/projects full · earned_value draft/full ·
# progress_payments draft kapıları sayfa kapısına taşındı; bu satırların ucu kalmadı.
EDIT_GATE_PAGES: dict[tuple[str, AccessLevel], frozenset[str]] = {
    ("boq", L.full): _p("santiye.is_kalemleri", "santiye.bolum_dagilimi"),
    ("documents", L.full): _p("mali.belge_arsivi", "proje.belgeler", "santiye.belgeler"),
    ("inventory", L.full): _p("stok.stok_depo", "santiye.stok"),
    ("invoicing", L.full): _p("mali.fatura"),
    ("payroll", L.full): _p("mali.bordro", "mali.sgk_bildirimi"),
    ("personnel", L.full): _p("ik.personel", "ik.izin_yonetimi", "ik.belge_sertifika"),
    ("procurement", L.request): _p("stok.satinalma_talepleri"),
    ("procurement", L.full): _p(
        "stok.siparisler", "stok.tedarikciler", "stok.teklif_karsilastirma"
    ),
    ("sales", L.full): _p("mali.satis"),
    ("settings", L.full): _p("ayarlar.sirket_bilgileri"),
    ("site_diary", L.full): _p(
        "saha.gunluk_kayit",
        "santiye.gunluk_kayit",
        "santiye.gunluk_planlama",
        "bolum.gunluk_kayit_detay",
    ),
    ("timesheet", L.full): _p("saha.puantaj", "santiye.puantaj"),
    ("treasury", L.full): _p("mali.hazine", "mali.cek_odeme"),
    ("user_management", L.full): _p("ayarlar.kullanicilar"),
}

_HAKEDIS_ISVEREN = _p("mali.hakedis_isveren", "proje.isveren_hakedis", "santiye.hakedisler")
# IZN-B5b madde 6 (CEO kararı 2): `santiye.hakedisler` yalnız işveren ailesinde.
_HAKEDIS_TASERON = _p("mali.hakedis_taseron", "proje.taseron_hakedis")
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
    ("POST", "/diary/{entry_id}/reopen"): _p(
        "saha.gunluk_kayit", "santiye.gunluk_kayit", "bolum.gunluk_kayit_detay"
    ),
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
    # IZN-B5a madde 8: oran ucu vergi dilimi ucuyla AYNI kapıda (eskiden `payroll:full`).
    ("PUT", "/payroll/rates/{year}/{source}"): _p("ayarlar.bordro_oranlari"),
    ("PUT", "/approvals/settings"): _p("ayarlar.onay_rolleri"),
    ("GET", "/approvals/roles"): _p("ayarlar.onay_rolleri"),
    ("PUT", "/approvals/roles/{user_id}"): _p("ayarlar.onay_rolleri"),
    ("POST", "/roles"): _p("ayarlar.rol_yonetimi"),
    ("PATCH", "/roles/{role_id}"): _p("ayarlar.rol_yonetimi"),
    ("POST", "/roles/{role_id}/copy"): _p("ayarlar.rol_yonetimi"),
    ("PUT", "/roles/{role_id}/pages"): _p("ayarlar.sayfa_izinleri"),
}

# --- IZN-B5b: sayfa ayırma satırları (elle yazıldı; uygulamadan bağımsız) ---
_HI = _HAKEDIS_ISVEREN
_HT = _HAKEDIS_TASERON
_EVB = _EV_FREEZE
_ISV = _p("teklif.isveren_sozlesme", "proje.is_kalemleri")
_DAGILIM = _p("teklif.poz_dagilimi", "proje.is_kalemleri")  # CEO kararı 3
_OFR = "/offers/{offer_id}/revisions/{rev_no}"
_B5B_EDIT_A: dict[tuple[str, str], frozenset[str]] = {
    # madde 1 (CEO kararı 1): oluşturma + düzenleme tek sayfada
    ("PATCH", "/projects/{project_id}"): _p("genel.projeler"),
    # madde 2: satış sekmeleri
    ("POST", "/projects/{project_id}/blocks"): _p("mali.satis_blok"),
    ("PATCH", "/blocks/{block_id}"): _p("mali.satis_blok"),
    ("POST", "/projects/{project_id}/units"): _p("mali.satis_unite"),
    ("PATCH", "/units/{unit_id}"): _p("mali.satis_unite"),
    ("POST", "/projects/{project_id}/units/bulk"): _p("mali.satis_toplu_uretim"),
    ("POST", "/projects/{project_id}/units/bulk/preview"): _p("mali.satis_toplu_uretim"),
    ("POST", "/projects/{project_id}/units/import/validate"): _p("mali.satis_excel"),
    ("POST", "/projects/{project_id}/units/import"): _p("mali.satis_excel"),
    ("PATCH", "/projects/{project_id}/units/allocation"): _p("mali.satis_paylasim"),
    # madde 4: muhasebe
    ("POST", "/chart-of-accounts"): _p("mali.hesap_plani"),
    ("PATCH", "/chart-of-accounts/{account_id}"): _p("mali.hesap_plani"),
    ("POST", "/journal-entries"): _p("mali.yevmiye"),
    ("PATCH", "/journal-entries/{entry_id}"): _p("mali.yevmiye"),
    ("PUT", "/journal-entries/{entry_id}/lines"): _p("mali.yevmiye"),
    ("POST", "/journal-entries/{entry_id}/reverse"): _p("mali.yevmiye"),
    ("POST", "/accounting-periods/{year}/{month}/close"): _p("mali.donem_kapanisi"),
    # madde 11: makine
    ("POST", "/equipment"): _p("saha.makine_ekipman"),
    ("PATCH", "/equipment/{equipment_id}"): _p("saha.makine_ekipman"),
    ("POST", "/equipment/{equipment_id}/documents"): _p("saha.makine_ekipman"),
    ("PATCH", "/equipment/documents/{document_id}"): _p("saha.makine_ekipman"),
    ("POST", "/equipment/work-logs"): _p("saha.makine_calisma"),
    ("PATCH", "/equipment/work-logs/{log_id}"): _p("saha.makine_calisma"),
    ("POST", "/equipment/fuel-logs"): _p("saha.makine_yakit"),
    ("PATCH", "/equipment/fuel-logs/{log_id}"): _p("saha.makine_yakit"),
    ("POST", "/equipment/rental-invoices"): _p("saha.makine_kira"),
    ("PATCH", "/equipment/rental-invoices/{invoice_id}"): _p("saha.makine_kira"),
    ("POST", "/equipment/rental-invoices/{invoice_id}/reload"): _p("saha.makine_kira"),
    ("PATCH", "/equipment/rental-invoice-lines/{line_id}"): _p("saha.makine_kira"),
    # yan bulgu (CEO): belge bağlama yazmaları = sahibin ANA sayfasının Düzenler'i
    ("POST", "/units/{owner_id}/documents"): _p("mali.satis_unite"),
    ("PATCH", "/units/documents/{link_id}"): _p("mali.satis_unite"),
    ("POST", "/sales/{owner_id}/documents"): _p("mali.satis"),
    ("PATCH", "/sales/documents/{link_id}"): _p("mali.satis"),
    ("POST", "/subcontractor-contracts/{owner_id}/documents"): _p("teklif.taseron_sozlesme"),
    ("PATCH", "/subcontractor-contracts/documents/{link_id}"): _p("teklif.taseron_sozlesme"),
}
_B5B_EDIT_B: dict[tuple[str, str], frozenset[str]] = {
    # madde 5: hakediş aileleri
    ("POST", "/projects/{project_id}/progress-payments"): _HI,
    ("PATCH", "/progress-payments/{payment_id}"): _HI,
    ("PUT", "/progress-payments/{payment_id}/lines"): _HI,
    ("POST", "/progress-payments/{payment_id}/refresh-prices"): _HI,
    ("POST", "/progress-payments/{payment_id}/submit"): _HI,
    ("POST", "/subcontractor-contracts/{contract_id}/progress-payments"): _HT,
    ("PATCH", "/subcontractor-progress-payments/{payment_id}"): _HT,
    ("PUT", "/subcontractor-progress-payments/{payment_id}/lines"): _HT,
    ("POST", "/subcontractor-progress-payments/{payment_id}/refresh-prices"): _HT,
    ("POST", "/subcontractor-progress-payments/{payment_id}/submit"): _HT,
    # madde 9: ayar ucu ↔ bütçe uçları ↔ gün dağıtımı (CEO kararı 4)
    ("PUT", f"{_DAY}/settings"): _p("ayarlar.planlama"),
    ("POST", f"{_DAY}/budget/revisions"): _EVB,
    ("PUT", f"{_DAY}/budget/group-disciplines"): _EVB,
    ("PATCH", f"{_DAY}/budget/items/{{boq_item_id}}"): _EVB,
    ("PATCH", f"{_DAY}/budget/leaves"): _EVB,
    ("POST", f"{_DAY}/budget/fill-from-catalog"): _EVB,
    ("POST", f"{_DAY}/budget/fill-from-contract"): _EVB,
    ("PUT", f"{_DAY}/budget/distributions"): _EVB,
    ("PUT", f"{_DAY}/budget/windows"): _EVB,
    ("PUT", f"{_DAY}/days/{{day}}/allocation"): _EVB,
    # madde 10
    ("POST", "/earned-value/disciplines"): _p("planlama.disiplin_yonetimi"),
    ("PATCH", "/earned-value/disciplines/{discipline_id}"): _p("planlama.disiplin_yonetimi"),
    ("POST", "/earned-value/catalog"): _p("planlama.birim_oran_katalogu"),
    ("PATCH", "/earned-value/catalog/{item_id}"): _p("planlama.birim_oran_katalogu"),
    ("POST", "/earned-value/catalog/{item_id}/adopt-actual"): _p("planlama.birim_oran_katalogu"),
}
_B5B_EDIT_C: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/catalog/items"): _p("teklif.is_kalemi_katalogu"),
    ("POST", "/catalog/items/bulk"): _p("teklif.is_kalemi_katalogu"),
    ("PATCH", "/catalog/items/{item_id}"): _p("teklif.is_kalemi_katalogu"),
    ("PUT", "/projects/{project_id}/contract/distribution"): _DAGILIM,
    ("POST", "/projects/{project_id}/contract/groups"): _ISV,
    ("POST", "/projects/{project_id}/contract/items"): _ISV,
    ("POST", "/projects/{project_id}/contract/items/bulk"): _ISV,
    ("PATCH", "/contracts/employer/groups/{group_id}"): _ISV,
    ("PATCH", "/contracts/employer/items/{item_id}"): _ISV,
    ("POST", "/subcontractors"): _p("teklif.taseron_firmalar", "teklif.sozlesmeler"),
    ("PATCH", "/subcontractors/{subcontractor_id}"): _p("teklif.taseron_firmalar"),
    ("POST", "/projects/{project_id}/subcontractor-contracts"): _p(
        "teklif.sozlesmeler", "teklif.taseron_sozlesme"
    ),
    ("PATCH", "/subcontractor-contracts/{contract_id}"): _p("teklif.taseron_sozlesme"),
    ("POST", "/subcontractor-contracts/{contract_id}/items"): _p("teklif.taseron_sozlesme"),
    ("PATCH", "/subcontractor-contracts/items/{item_id}"): _p("teklif.taseron_sozlesme"),
    ("POST", "/subcontractor-contracts/{contract_id}/items/load-from-employer"): _p(
        "teklif.taseron_sozlesme"
    ),
    ("POST", "/offers/templates"): _p("teklif.sablonlar"),
    ("POST", "/offers/templates/from-offer"): _p("teklif.sablonlar"),
    ("PATCH", "/offers/templates/{template_id}"): _p("teklif.sablonlar"),
    ("PUT", "/offers/templates/{template_id}/content"): _p("teklif.sablonlar"),
    ("POST", "/offers/templates/{template_id}/default"): _p("teklif.sablonlar"),
    ("POST", "/offers/templates/{template_id}/copy"): _p("teklif.sablonlar"),
    ("PUT", "/offers/settings"): _p("teklif.teklif_hazirlama"),
    ("POST", "/offers"): _p("teklif.teklif_hazirlama"),
    ("PATCH", "/offers/{offer_id}"): _p("teklif.teklif_hazirlama"),
    ("POST", "/offers/{offer_id}/revisions"): _p("teklif.teklif_hazirlama"),
    ("PATCH", _OFR): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/send"): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/win"): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/lose"): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/withdraw"): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/groups"): _p("teklif.teklif_hazirlama"),
    ("PATCH", _OFR + "/groups/{group_id}"): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/items"): _p("teklif.teklif_hazirlama"),
    ("POST", _OFR + "/items/bulk"): _p("teklif.teklif_hazirlama"),
    ("PATCH", _OFR + "/items/{item_id}"): _p("teklif.teklif_hazirlama"),
}

#: IZN-B5a madde 15: Görür bayraklı sayfa kapılı (`require_pages(..., "view")`) uçlar. Görür ya da
#: Düzenler bayrağı açar (Düzenler Görür'ü içerir); `ayarlar.kullanicilar` bu uçları AÇMAZ.
PAGE_VIEW_ROUTES: dict[tuple[str, str], frozenset[str]] = {
    # IZN-B5b Ek (CEO kararı 6): maliyet özeti satış alt sayfalarına AÇILMAZ (4'lü küme)
    ("GET", "/projects/{project_id}/costs"): _p(
        "genel.projeler", "genel.proje_takvimi", "proje.ozet", "proje.paylasim_tablosu"
    ),
    ("GET", "/modules"): _p("ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"),
    ("GET", "/roles/{role_id}/permissions"): _p("ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"),
    ("GET", "/roles/{role_id}/pages"): _p("ayarlar.rol_yonetimi", "ayarlar.sayfa_izinleri"),
}


# IZN-B5c: sites modülü yazmaları sayfa başına + bölüm belgesi bağlama (+7 Düzenler) ve madde 16 /
# dar görme genişlemesi (+5 Görür). Elle yazıldı (uygulamadan türetilmez).
_B5C_SANTIYE_TUM = (
    "santiye.bolumler", "santiye.is_kalemleri", "santiye.puantaj", "santiye.stok",
    "santiye.hakedisler", "santiye.gunluk_kayit", "santiye.belgeler", "santiye.bolum_dagilimi",
    "santiye.gunluk_ozet", "santiye.gunluk_planlama", "santiye.adam_saat_butcesi",
    "santiye.planlama_paneli", "santiye.gunluk_ilerleme_raporu", "santiye.haftalik_qurr",
)  # fmt: skip
_B5C_BOLUM_TUM = (
    "bolum.detay", "bolum.is_kalemleri", "bolum.puantaj", "bolum.malzeme", "bolum.hakedis",
    "bolum.gunluk_kayit", "bolum.gunluk_kayit_detay",
)  # fmt: skip
_B5C_EDIT: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/projects/{project_id}/sites"): _p("proje.santiyeler"),
    ("PATCH", "/sites/{site_id}"): _p("proje.santiyeler"),
    ("POST", "/sites/{site_id}/sections"): _p("santiye.bolumler"),
    ("PATCH", "/sections/{section_id}"): _p("bolum.detay"),
    ("POST", "/section-types"): _p("santiye.bolumler", "bolum.detay"),
    ("POST", "/sections/{owner_id}/documents"): _p("bolum.detay"),
    ("PATCH", "/sections/documents/{link_id}"): _p("bolum.detay"),
}
_B5C_VIEW: dict[tuple[str, str], frozenset[str]] = {
    ("GET", "/projects/{project_id}/sites"): _p(
        "proje.santiyeler", "santiye.bolumler", "bolum.detay"
    ),
    ("GET", "/projects/{project_id}"): _p(
        "genel.projeler", "genel.proje_takvimi", "mali.satis_blok", "mali.satis_unite",
        "mali.satis_excel", "mali.satis_paylasim", "proje.ozet", "proje.paylasim_tablosu",
        "proje.santiyeler",
    ),
    ("GET", "/sites/{site_id}"): _p("proje.santiyeler", *_B5C_SANTIYE_TUM, *_B5C_BOLUM_TUM),
    ("GET", "/sections/{section_id}"): _p(
        "santiye.bolumler", "santiye.gunluk_kayit", *_B5C_BOLUM_TUM
    ),
    ("GET", "/sites/{site_id}/sections"): _p(
        "santiye.bolumler", "bolum.detay",
        "santiye.stok", "santiye.puantaj", "santiye.gunluk_planlama",
    ),
}  # fmt: skip

PAGE_EDIT_ROUTES = {**PAGE_EDIT_ROUTES, **_B5B_EDIT_A, **_B5B_EDIT_B, **_B5B_EDIT_C, **_B5C_EDIT}
PAGE_VIEW_ROUTES = {**PAGE_VIEW_ROUTES, **_B5C_VIEW}


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
    if gate_flag == "view":
        # Onaylar bayrağı testte Görür düzeyiyle kurulur (`_tek_hucreli_kullanici`) → Görür
        # kapısı da açar (B5c: `bolum.gunluk_kayit_detay` GET kümesinde).
        return flag in ("view", "edit", "approve") and page in PAGE_VIEW_ROUTES.get(
            route, frozenset()
        )
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
        if page in SATIS_ALT_SAYFALAR:
            yazma -= PROJECTS_VIEW_YAZMA_UCLARI  # bilinçli fark (IZN-B5a madde 3), yalnız bu 2 uç
        assert yazma == set(), f"{page} Görür → yazma açıldı: {sorted(yazma)}"


# --- IZN-B5b: YAPISAL bire bir karşılaştırma (körlük onarımı) ---------------------------------
#
# Eski bekçi yalnız "açılan ⊆ tablo" denetliyordu: bir ucun kapısı komşu sayfaya/eski sabite
# döndürüldüğünde (örn. `contracts:full`) o kapıyı AÇAN sayfa tabloda yoksa fark gizli kalıyordu.
# Bu test rotanın GERÇEK kapı bağımlılığını (`require_page`/`require_pages` kapanışındaki
# sayfa kümesi + bayrak) okuyup elle yazılmış tabloyla BİREBİR karşılaştırır.


def test_sayfa_kapili_uclar_tabloyla_birebir() -> None:
    """Rota+bayrak başına TÜM sayfa kapıları (liste; ezilmez) ve `multi_project` karşılaştırılır.

    Tablo satırı = tek kapı, `multi_project=False` varsayılır; bugün `multi_project=True` sayfa
    kapısı yoktur (ölçüldü). İkinci bir sayfa kapısı ya da multi_project farkı KIRMIZI verir.
    """
    uygulama: dict[tuple[str, tuple[str, str]], list[tuple[frozenset[str], bool]]] = {}
    for route, gates in ROTALAR.items():
        for gate in gates:
            if gate.kind != "page":
                continue
            pages, flag = gate.spec
            if flag in ("edit", "view"):
                uygulama.setdefault((flag, route), []).append(
                    (frozenset(pages), gate.multi_project)
                )
    tablo = {("edit", r): [(p, False)] for r, p in PAGE_EDIT_ROUTES.items()} | {
        ("view", r): [(p, False)] for r, p in PAGE_VIEW_ROUTES.items()
    }
    assert sorted(set(uygulama) - set(tablo)) == [], "kapısı sayfaya bağlı ama tabloda yok"
    assert sorted(set(tablo) - set(uygulama)) == [], "tabloda var ama uç sayfa kapısı taşımıyor"
    farkli = {
        k: (
            sorted((sorted(p), m) for p, m in tablo[k]),
            sorted((sorted(p), m) for p, m in uygulama[k]),
        )
        for k in tablo
        if sorted(map(repr, tablo[k])) != sorted(map(repr, uygulama[k]))
    }
    assert farkli == {}, farkli
