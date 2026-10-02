"""Uygulamanın router kaydı — **TEK KAYNAK** (AI-0a T1).

`app/main.py` bu demeti sırayla tüketir. Router listesi buraya taşındı çünkü
`main.py` dışında **ikinci bir tüketici** doğdu: `app/modules/ai/readplane.py`
salt-GET bir türev uygulama kurar ve "hangi router'lar var" sorusunun tek bir
cevabı olmalıdır. İki ayrı listenin sessizce ayrışması bu deponun en sevmediği
kusur sınıfıdır.

🔴 **SIRA ANLAMLIDIR — alfabetik değildir, davranıştır.**
FastAPI rotaları KAYIT SIRASINA göre eşler. Aynı metotta aynı şekli taşıyan bir
literal yol ile parametreli bir yol çakışırsa **önce kaydedilen kazanır**. Bu
demette üç router-arası çakışma vardır (dördüncü aday ölçüldü, bugün çakışmıyor —
aşağıda); üçünün de bekçisi
`tests/modules/ai/test_ai0a_router_registry.py::test_router_arasi_golgeleme_*`
testleridir:

  1. `/equipment/rental-invoices` ← `equipment_rental_router`, `equipment_router`
     (`/equipment/{equipment_id}`) ÖNCESİNDE olmalı.
  2. `/equipment/document-types`  ← `equipment_document_router`, aynı gerekçe.
  3. `/personnel/document-types`  ← `personnel_document_type_router`,
     `personnel_router` (`/personnel/{personnel_id}`) ÖNCESİNDE olmalı.
  4. `/documents/slot-types`      ← `documents_link_router` (BC-3) — 🔴 ÖLÇÜLDÜ,
     BUGÜN ÇAKIŞMA YOK: `documents_router`ın `/documents/{document_id}` yolu yalnız
     PATCH/DELETE taşır (GET detay ucu YOKTUR, `/download` üç segmentlidir), sıra
     kısıtı ise yalnız AYNI metotta doğar. Yine de önce kaydedilir (ucuz sigorta)
     ve ön koşul bir tripwire ile kilitlidir: biri `GET /documents/{document_id}`
     açarsa `tests/documents/test_bc3_links_api.py::test_TRIPWIRE_*` kırmızıya
     döner ve sıra o gün gerçekten zorunlu olur.

⚠️ ÖLÇÜLMÜŞ İNCELİK (fastapi 0.141.1): sıra kısıtı yalnız **aynı metotta** doğar.
Yol tutup metot tutmayan bir aday `Match.PARTIAL` olarak saklanır ve döngü FULL
aramaya devam eder — yani parametreli bir rota, FARKLI metotlu bir literal rotayı
gölgeleyemez. Yukarıdaki üç çift de GET/GET'tir, bu yüzden gerçektir.

⚠️ Router'ların **KENDİ İÇLERİNDEKİ** literal/parametre sıraları bu dosyanın işi
DEĞİLDİR; onlar ilgili router modülünün docstring'inde açıklanır ve kendi bekçi
testleri vardır (`test_rota_sirasi_*_UUID_SANILMAZ`).

⚠️ `GET /health` bu demette **YOKTUR** — `main.py`de satır-içi tanımlıdır. Bu
yüzden "okuma düzlemi kümesi == ROUTERS'tan üretilen küme" biçiminde bir bekçi
yanlış olurdu; bekçi uygulamanın **rota tablosu** üzerinden kurulur.

---

## ÇAKIŞMA DEĞERLENDİRMESİ (`main.py`den taşındı, ölçümle tazelendi)

Aşağıdaki router'lar için sıra tuzağı **değerlendirildi ve YOKTUR**; gerekçeler
kaybolmasın diye buraya taşındı:

- `accounting_accounts_router` — yalnız `/chart-of-accounts` kökü; başka hiçbir
  router'ın yoluyla çakışmaz. Kendi içindeki UUID/literal sırası router modül
  docstring'inde (bugün literal yol YOKTUR).
- `accounting_journal_router` — `/journal-entries` ve `/journal` kökleri. FastAPI
  SEGMENT bazında eşler; `/journal` ile `/journal-entries` ayrı köklerdir, önek
  benzerliği eşleşme üretmez. Router-İÇİ tuzak (`/journal-entries/summary` ↔
  `/journal-entries/{entry_id}`) orada işaretlidir; bekçisi
  `test_rota_sirasi_summary_UUID_SANILMAZ`.
- `accounting_periods_router` — `/accounting-periods` kökü. Liste ucu TEK,
  ötekiler ÜÇ segmentlidir ve son segmentleri LİTERALDİR (`close`/`reopen`);
  `/{year}/{month}` int'tir.
  🔴 **DÜZELTME:** `main.py`de bu satırın gerekçesi *"tek `prefix=` kullanan öteki
  router `/equipment`tir"* diyordu. Bu **BAYATTI ve YANLIŞTI** — ölçüldü: 41
  router'ın **13'ü** `APIRouter(prefix=...)` taşır (`/settings`, `/auth`,
  `/projects`, `/employers`, `/dashboard`, `/audit-log`, `/users`, `/approvals`,
  `/equipment`×3, `/company`, …). Vardığı sonuç (çakışma yok) doğrudur, gerekçesi
  çürüktü: doğru gerekçe "repoda `/accounting-periods` ile başlayan başka hiçbir
  yol yoktur"dur.
- `accounting_reports_router` — `prefix` TAŞIMAZ; iki AYRI birinci-seviye yol
  (`/trial-balance`, `/vat-return`). Uygulamanın KÖK seviyesinde `"/{param}"`
  biçiminde hiçbir rota yoktur, dolayısıyla literal kökün UUID sanılması yapısal
  olarak imkânsızdır.
- `approvals_router` — `/approvals` kökünde `"/{param}"` biçiminde hiçbir rota
  açılmamıştır; `/approvals/settings` ve `/approvals/roles` güvendedir. Bekçi:
  `test_modulun_ROTA_KUMESI_tam_olarak_bes_yoldur`.
- `invoicing_router` — tuzak router'ın KENDİ İÇİNDE çözülür (`/invoices/summary`
  ↔ `/invoices/{invoice_id}`).
- `treasury_router` — yalnız `/bank-accounts` kökü. Ödeme uçları
  `/invoices/{id}/payments` altında ve `invoicing_router`ın İÇİNDEDİR.
- `financial_instruments_router` — `/financial-instruments` kökü;
  `treasury_router`dan AYRI, çünkü kendi içinde bir LİTERAL/UUID sırası taşır.
"""

from fastapi import APIRouter

from app.modules.accounting.accounts_router import router as accounting_accounts_router
from app.modules.accounting.periods_router import router as accounting_periods_router
from app.modules.accounting.reports_router import router as accounting_reports_router
from app.modules.accounting.router import router as accounting_journal_router
from app.modules.ai.router import router as ai_router
from app.modules.approvals.router import router as approvals_router
from app.modules.audit.router import router as audit_router
from app.modules.auth.router import router as auth_router
from app.modules.boq.router import router as boq_router
from app.modules.catalog.export_router import router as catalog_export_router
from app.modules.catalog.router import router as catalog_router
from app.modules.company.router import router as company_router
from app.modules.contracts.router import router as contracts_router
from app.modules.customers.router import router as customers_router
from app.modules.dashboard.router import router as dashboard_router
from app.modules.documents.link_router import router as documents_link_router
from app.modules.documents.router import router as documents_router
from app.modules.earned_value.catalog_router import router as earned_value_catalog_router
from app.modules.earned_value.day_router import router as earned_value_day_router
from app.modules.earned_value.report_router import router as earned_value_report_router
from app.modules.earned_value.router import router as earned_value_budget_router
from app.modules.earned_value.settings_router import router as earned_value_settings_router
from app.modules.earned_value.user_discipline_router import (
    router as earned_value_user_discipline_router,
)
from app.modules.equipment.document_router import router as equipment_document_router
from app.modules.equipment.rental_router import router as equipment_rental_router
from app.modules.equipment.router import router as equipment_router
from app.modules.inventory.router import router as inventory_router
from app.modules.invoicing.router import router as invoicing_router
from app.modules.offers.convert_router import router as offers_convert_router
from app.modules.offers.export_router import router as offers_export_router
from app.modules.offers.router import router as offers_router
from app.modules.offers.template_router import router as offer_templates_router
from app.modules.payroll.router import router as payroll_router
from app.modules.personnel.document_type_router import router as personnel_document_type_router
from app.modules.personnel.router import router as personnel_router
from app.modules.procurement.router import router as procurement_router
from app.modules.progress_payments.router import router as progress_payments_router
from app.modules.projects.router import employers_router
from app.modules.projects.router import router as projects_router
from app.modules.roles.router import router as roles_router
from app.modules.sales.router import router as sales_router
from app.modules.settings.router import router as settings_router
from app.modules.site_diary.router import router as site_diary_router
from app.modules.site_planning.router import router as site_planning_router
from app.modules.sites.flat_list_router import router as sites_flat_list_router
from app.modules.sites.router import router as sites_router
from app.modules.sites.section_type_router import router as sites_section_type_router
from app.modules.subcontractor_progress_payments.router import (
    router as subcontractor_progress_payments_router,
)
from app.modules.timesheet.router import router as timesheet_router
from app.modules.treasury.instruments.router import router as financial_instruments_router
from app.modules.treasury.router import router as treasury_router
from app.modules.units.router import router as units_router
from app.modules.users.router import router as users_router

#: 🔴 **DÜZ router listesi — `(router, kwargs)` çiftine ÇEVRİLMEZ.**
#: Gerekçe ölçülmüş: üç mevcut test gezgini (`test_ok1c_dar_kapsam.py`,
#: `_hz1_upcoming.py`, `test_tb9_periods_delete_path.py`) rota ağacını
#: `_IncludedRouter.original_router.routes` üzerinden geziyor. O ağaç **ORİJİNAL**
#: yolu verir, ETKİN yolu değil. Bugün doğru çalışıyorlar çünkü hiçbir include
#: `prefix=` taşımaz (AST ile ölçüldü: 38 çağrı, 0 `prefix=`). Demete prefix
#: eklendiği an o üç gezgin **sessizce yanlış yol** ölçmeye başlar. Prefix ihtiyacı
#: doğarsa önce o gezginler düzeltilir.
#:
#: Ayrıca `app/modules/ai/readplane.py` bu olguya dayanır: rotaların yolu
#: `APIRouter(prefix=...)` kurucusunda ROTA KAYDI SIRASINDA çakıldığı için
#: (include anında değil) okuma düzlemi rotaları düz taşıyabilir. `build_read_plane`
#: bu varsayımı her çağrıda **fiilen doğrular** ve ihlalde patlar.
ROUTERS: tuple[APIRouter, ...] = (
    accounting_accounts_router,
    accounting_journal_router,
    accounting_periods_router,
    accounting_reports_router,
    # AI-0b T6 — `GET /ai/tools` + `GET /ai/context`. `/ai` kokunde
    # `"/{param}"` bicimli hicbir rota YOK, dolayisiyla sira tuzagi da yok.
    ai_router,
    approvals_router,
    audit_router,
    auth_router,
    boq_router,
    catalog_router,
    catalog_export_router,
    company_router,
    contracts_router,
    customers_router,
    dashboard_router,
    # BC-3 `GET /documents/slot-types` (literal, 2 segment): `documents_router`da
    # `GET /documents/{document_id}` YOK (yalnız PATCH/DELETE) → bugün gölgeleme
    # yapısal olarak imkânsız (ölçüldü; sıra ters çevrilince de literal kazanır).
    # Önce kaydedilir: sıfır maliyetli sigorta. Ön koşulun tripwire'ı:
    # `tests/documents/test_bc3_links_api.py::test_TRIPWIRE_documents_routerda_GET_detay_YOK`.
    documents_link_router,
    documents_router,
    employers_router,
    # 🔴 SIRA ZORUNLU (1): kira hakedişi router'ı `equipment_router`dan ÖNCE.
    equipment_rental_router,
    # 🔴 SIRA ZORUNLU (2): belge router'ı da `equipment_router`dan ÖNCE.
    equipment_document_router,
    equipment_router,
    inventory_router,
    invoicing_router,
    # 🔴 SIRA ZORUNLU: `/offers/templates` (literal), `/offers/{offer_id}`den ONCE.
    offer_templates_router,
    offers_router,
    offers_export_router,
    offers_convert_router,
    payroll_router,
    # 🔴 SIRA ZORUNLU (3): `/personnel/document-types`, `/personnel/{personnel_id}`
    # ile AYNI şekli ve AYNI metodu (GET) taşır.
    personnel_document_type_router,
    personnel_router,
    procurement_router,
    progress_payments_router,
    projects_router,
    roles_router,
    sales_router,
    settings_router,
    site_diary_router,
    site_planning_router,
    earned_value_catalog_router,
    earned_value_settings_router,
    earned_value_budget_router,
    earned_value_day_router,
    earned_value_report_router,
    earned_value_user_discipline_router,
    # Düz `GET /sites` (SITE-1a) TEK segmentlidir; `sites_router`ın
    # `/sites/{site_id}` yolu İKİ segmentlidir ve FastAPI segment sayısına göre
    # ayırdığı için çakışma YOKTUR (ölçüldü — `personnel/document-types`
    # tuzağının aksine). Yine de önce kaydedilir: maliyeti sıfır ucuz sigorta.
    sites_flat_list_router,
    sites_router,
    # BLF-B1 `/section-types` (literal, TEK segment): `/sites/...` ile ortusmez, baska
    # router'in yoluyla cakismaz — sira onemsiz (olculdu).
    sites_section_type_router,
    subcontractor_progress_payments_router,
    timesheet_router,
    treasury_router,
    financial_instruments_router,
    units_router,
    users_router,
)
