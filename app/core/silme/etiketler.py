"""Silme önizlemesinin tablo sözlüğü (SIL-B1): Türkçe ad, örnek kolonları, MALİ sınıfı.

Motor ağacı `Base.metadata`dan okur; bu dosya yalnız KULLANICIYA gösterilen adı ve mali
sınıfı taşır. Etiketi olmayan tablo önizlemede teknik adıyla görünür ve
`tests/core/test_silme_etiket_bekcisi.py` bunu KIRMIZI yapar: yeni bir aile motora
eklenirken kapsadığı her tablonun adı buraya yazılmak zorundadır.

## `mali` sınıfı (KARARLAR §1.7 + CEO eki: SIL-B2 gelene kadar mali kayıt SİLİNMEZ)

`mali=True`  — satırın kendisi bir MALİ KAYITTIR: muhasebe fişi ve satırı, fatura ve satırı,
               ödeme (banka hareketi), çek/senet, bordro dönemi ve satırı.
`mali=<koşul>` — satır yalnız PARASAL SONUCU olduğunda mali sayılır. Koşul, tablonun hangi
               satırlarının mali olduğunu seçen bir SQL ifadesidir:
  * hakediş (işveren/taşeron) ve kira hakedişi: yalnız `approved` / `paid`. Fiş onayda
    doğar; taslak ve onay bekleyen hakedişin fişi, faturası, ödemesi yoktur — silmek hiçbir
    mali kaydı yetim bırakmaz. Hakediş SATIRLARI üstbilgisinin durumunu izler.
  * ünite satışı: DURUMDAN BAĞIMSIZ — durumu `reservation`/`cancelled` DIŞINDA (sözleşmeli satış,
    tapu devri) YA DA `reservation_deposit > 0` (rezervasyona kapora işlenebilir; iptal edilmiş
    satış da kaporayı taşır) YA DA tahsil edilmiş (`paid_amount > 0`) taksiti var;
    taksit: `paid_amount > 0` (KISMİ tahsilat da mali: `paid_at` yalnız tam ödemede dolar).
  * puantaj girdisi: tarihi, KAPANMIŞ bir bordro döneminin (yıl+ay) içine düşüyorsa. "Kapanmış" =
    `payroll_periods.status` ∈ {`pending_approval`, `approved`, `paid`} (`KAPALI_BORDRO_DURUMLARI`).
    Gerekçe: `draft` dönem hâlâ yeniden hesaplanabilir (puantaj değişince bordro türer); onaya
    gönderilmiş (`pending_approval`) dönemin tutarları DONMUŞTUR ve onaylanma/ödenme ayrıca fiş
    doğurur. Şüphede FAIL-CLOSED: onay bekleyen de kapalı sayılır. Dönemsiz ya da `draft` dönemdeki
    puantaj mali DEĞİLDİR.
Diğer her tablo mali DEĞİLDİR (sözleşme kartı, günlük, puantaj, plan, belge…).
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import ColumnElement, String, Table, exists, extract, func, or_, select

#: Bir tablodan MALİ satırları seçen koşul üreticisi: `Table -> ColumnElement[bool]`.
MaliKosulu = Callable[[Table], ColumnElement[bool]]

_ONAYLI_DURUMLAR = ("approved", "paid")
#: Bordro döneminin KAPALI sayıldığı durumlar (modül docstring'indeki gerekçe): `draft` HARİÇ hepsi.
KAPALI_BORDRO_DURUMLARI = ("pending_approval", "approved", "paid")


@dataclass(frozen=True)
class TabloBilgisi:
    etiket: str
    #: Önizlemedeki "örnek kayıtlar" için kolon adları (` · ` ile birleşir). Boş = örnek yok.
    ornek: tuple[str, ...] = ()
    #: `False` | `True` | koşul (bkz. modül docstring'i).
    mali: bool | MaliKosulu = False


def _durum_onayli(tablo: Table) -> ColumnElement[bool]:
    return tablo.c.status.cast(String).in_(_ONAYLI_DURUMLAR)


def _ust_onayli(ust_tablo: str, fk_kolonu: str) -> MaliKosulu:
    """Satır, durumu onaylı/ödenmiş bir ÜSTBİLGİye bağlıysa mali (üstbilgi silinmeyen tablodur)."""

    def kosul(tablo: Table) -> ColumnElement[bool]:
        ust = tablo.metadata.tables[ust_tablo]
        return exists(
            select(1).where(
                ust.c.id == tablo.c[fk_kolonu], ust.c.status.cast(String).in_(_ONAYLI_DURUMLAR)
            )
        )

    return kosul


def _satis_mali(tablo: Table) -> ColumnElement[bool]:
    """Durumdan bağımsız: sözleşmeli/tapu durumu YA DA kapora YA DA tahsilatlı taksit."""
    taksit = tablo.metadata.tables["sale_installments"]
    tahsilatli_taksit = exists(
        select(1).where(taksit.c.sale_id == tablo.c.id, taksit.c.paid_amount > 0)
    )
    return or_(
        tablo.c.status.cast(String).notin_(("reservation", "cancelled")),
        func.coalesce(tablo.c.reservation_deposit, 0) > 0,
        tahsilatli_taksit,
    )


def _taksit_tahsil_edilmis(tablo: Table) -> ColumnElement[bool]:
    return tablo.c.paid_amount > 0


def _puantaj_kapali_donemde(tablo: Table) -> ColumnElement[bool]:
    donem = tablo.metadata.tables["payroll_periods"]
    return exists(
        select(1).where(
            donem.c.year == extract("year", tablo.c.work_date),
            donem.c.month == extract("month", tablo.c.work_date),
            donem.c.status.cast(String).in_(KAPALI_BORDRO_DURUMLARI),
        )
    )


TABLOLAR: dict[str, TabloBilgisi] = {
    # --- Şantiye ailesi: kökler ---
    "sites": TabloBilgisi("Şantiye", ("name",)),
    "sections": TabloBilgisi("Bölüm", ("name",)),
    "blocks": TabloBilgisi("Blok", ("name",)),
    "units": TabloBilgisi("Ünite", ("unit_no",)),
    "unit_sales": TabloBilgisi("Ünite satışı", ("sale_type",), mali=_satis_mali),
    "sale_installments": TabloBilgisi("Satış taksiti", ("label",), mali=_taksit_tahsil_edilmis),
    # --- Metraj / iş kalemi ---
    "boq_groups": TabloBilgisi("İş kalemi grubu", ("name",)),
    "boq_items": TabloBilgisi("İş kalemi (poz)", ("code", "description")),
    "boq_item_section_allocations": TabloBilgisi("İş kalemi bölüm dağıtımı"),
    # --- Belgeler ---
    "documents": TabloBilgisi("Belge", ("filename",)),
    "document_blobs": TabloBilgisi("Belge içeriği"),
    "document_folders": TabloBilgisi("Belge klasörü", ("name",)),
    "section_documents": TabloBilgisi("Bölüm belgesi"),
    "unit_documents": TabloBilgisi("Ünite belgesi"),
    "unit_sale_documents": TabloBilgisi("Satış belgesi"),
    "subcontractor_contract_documents": TabloBilgisi("Taşeron sözleşmesi belgesi"),
    # --- Bölüm ---
    "section_milestones": TabloBilgisi("Bölüm kilometre taşı", ("title",)),
    # --- Günlük, puantaj, plan ---
    "site_diary_entries": TabloBilgisi("Günlük kaydı", ("entry_date",)),
    "site_diary_lines": TabloBilgisi("Günlük miktar satırı", ("code",)),
    "site_diary_worker_counts": TabloBilgisi("Günlük işçi sayısı", ("trade",)),
    "timesheet_entries": TabloBilgisi(
        "Puantaj kaydı", ("work_date",), mali=_puantaj_kapali_donemde
    ),
    "site_plan_rows": TabloBilgisi("Şantiye planı satırı", ("label",)),
    "site_plan_cells": TabloBilgisi("Şantiye planı hücresi"),
    "site_plan_goals": TabloBilgisi("Şantiye planı hedefi", ("title",)),
    "site_plan_sprints": TabloBilgisi("Şantiye planı sprinti", ("name",)),
    # --- Sözleşmeler ---
    "subcontractor_contracts": TabloBilgisi(
        "Taşeron sözleşmesi", ("contract_no", "subcontractor_name")
    ),
    "subcontractor_contract_items": TabloBilgisi("Taşeron sözleşme kalemi", ("code",)),
    # --- Hakedişler (mali: yalnız onaylı/ödenmiş) ---
    "subcontractor_progress_payments": TabloBilgisi(
        "Taşeron hakedişi", ("sequence_no",), mali=_durum_onayli
    ),
    "subcontractor_progress_payment_lines": TabloBilgisi(
        "Taşeron hakediş satırı",
        ("code",),
        mali=_ust_onayli("subcontractor_progress_payments", "payment_id"),
    ),
    "progress_payment_lines": TabloBilgisi(
        "İşveren hakediş satırı", ("code",), mali=_ust_onayli("progress_payments", "payment_id")
    ),
    # --- Fatura, ödeme, muhasebe (mali: her zaman) ---
    "invoices": TabloBilgisi("Fatura", ("invoice_no",), mali=True),
    "invoice_lines": TabloBilgisi("Fatura satırı", ("description",), mali=True),
    "payments": TabloBilgisi("Ödeme / tahsilat", ("paid_on",), mali=True),
    "journal_entries": TabloBilgisi("Muhasebe fişi", ("entry_no",), mali=True),
    "journal_lines": TabloBilgisi("Muhasebe fiş satırı", mali=True),
    # --- FK olmayan bağlar (kancalar) ---
    "approval_chains": TabloBilgisi("Onay zinciri"),
    "approval_steps": TabloBilgisi("Onay adımı"),
    # --- Planlama (kazanılmış değer) ---
    "ev_baseline_curve": TabloBilgisi("Planlama taban eğrisi noktası"),
    "ev_baseline_leaves": TabloBilgisi("Planlama taban yaprağı", ("item_code",)),
    "ev_composite_metric_terms": TabloBilgisi("Planlama bileşik ölçüt terimi"),
    "ev_composite_metrics": TabloBilgisi("Planlama bileşik ölçütü", ("name",)),
    "ev_day_cells": TabloBilgisi("Planlama gün hücresi"),
    "ev_day_codes": TabloBilgisi("Planlama gün kodu"),
    "ev_day_notes": TabloBilgisi("Planlama gün notu"),
    "ev_day_rows": TabloBilgisi("Planlama gün satırı"),
    "ev_day_unlocks": TabloBilgisi("Planlama gün kilidi açma"),
    "ev_distributions": TabloBilgisi("Planlama dağılımı"),
    "ev_group_disciplines": TabloBilgisi("Planlama grup disiplini"),
    "ev_holidays": TabloBilgisi("Planlama tatili", ("date_from",)),
    "ev_item_settings": TabloBilgisi("Planlama kalem ayarı"),
    "ev_leaf_settings": TabloBilgisi("Planlama yaprak ayarı"),
    "ev_report_approvals": TabloBilgisi("Planlama rapor onayı", ("report_date",)),
    "ev_report_snapshots": TabloBilgisi("Planlama rapor görüntüsü", ("report_date",)),
    "ev_revisions": TabloBilgisi("Planlama bütçe revizyonu", ("name",)),
    "ev_site_settings": TabloBilgisi("Planlama şantiye ayarı"),
    "ev_windows": TabloBilgisi("Planlama zaman penceresi"),
    # --- Bağı KOPACAK (SET NULL) kayıtlar: silinmez, yalnız bağ çözülür ---
    "equipment": TabloBilgisi("Makine", ("name",)),
    "equipment_fuel_logs": TabloBilgisi("Makine yakıt kaydı", ("fuel_date",)),
    "equipment_work_logs": TabloBilgisi("Makine çalışma kaydı", ("work_date",)),
    "equipment_rental_invoices": TabloBilgisi("Kira hakedişi", ("invoice_no",)),
    "equipment_rental_invoice_lines": TabloBilgisi("Kira hakedişi satırı"),
    "leave_requests": TabloBilgisi("İzin talebi", ("start_date",)),
    "personnel": TabloBilgisi("Personel", ("full_name",)),
    "personnel_documents": TabloBilgisi("Personel belgesi"),
    "purchase_requests": TabloBilgisi("Satınalma talebi", ("request_no",)),
    "stock_entry_lines": TabloBilgisi("Stok hareketi satırı"),
    "warehouses": TabloBilgisi("Depo", ("name",)),
}

#: Mali sınıfı tanımlı ama bu dilimin ağacına girmeyen mali tablolar (sonraki ailelerin
#: kökleri): çek/senet, bordro, kira hakedişi, işveren hakediş başlığı. Tam liste raporda.
MALI_TABLOLAR_DIGER: dict[str, TabloBilgisi] = {
    "progress_payments": TabloBilgisi("İşveren hakedişi", ("sequence_no",), mali=_durum_onayli),
    "financial_instruments": TabloBilgisi("Çek / senet", mali=True),
    "payroll_periods": TabloBilgisi("Bordro dönemi", mali=True),
    "payroll_lines": TabloBilgisi("Bordro satırı", mali=True),
}

TUM_TABLOLAR: dict[str, TabloBilgisi] = {**TABLOLAR, **MALI_TABLOLAR_DIGER}


def tablo_bilgisi(tablo: str) -> TabloBilgisi:
    """Etiketi olmayan tablo teknik adıyla görünür (bekçi testi bunu hata sayar)."""
    return TUM_TABLOLAR.get(tablo) or TabloBilgisi(tablo)
