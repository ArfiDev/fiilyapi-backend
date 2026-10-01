"""Teklif Hazirlama denetim metinleri (TKL-B4.2). Ayar metni `core.py`dedir (B4.1).

Kalem/grup TEKIL duzenlemeleri bilincli olarak denetim satiri YAZMAZ (gurultu); yalniz
yapisal olaylar (olustur, kunye, kosul, sil, yeni revizyon, durum gecisi, toplu kalem) yazar.
"""

from datetime import date
from decimal import Decimal

#: Odeme kosulu serbest metindir; denetim satirinda bu uzunlukta kisaltilir.
OFFER_TERMS_SHOWN = 60

#: Toplu kalem satirinda gosterilen poz sayisi (`contracts.BULK_AUDIT_CODES_SHOWN` emsali).
OFFER_BULK_POZ_SHOWN = 10


def offer_created(offer_no: str, title: str, employer_name: str) -> str:
    return f"Teklif oluşturuldu: {offer_no} · {title} ({employer_name})"


def offer_created_from_template(
    offer_no: str, title: str, employer_name: str, template_name: str
) -> str:
    """B5.1: sablondan olusturma (`offer_created` metni DEGISMEDI; kaynak eki ayri mesaj)."""
    return f"Teklif şablondan oluşturuldu: {offer_no} · {title} ({employer_name}) · {template_name}"


def offer_created_from_copy(
    offer_no: str, title: str, employer_name: str, source_offer_no: str, source_rev_no: int
) -> str:
    """B5.1 / SO-8: mevcut tekliften kopya."""
    return (
        f"Teklif kopyadan oluşturuldu: {offer_no} · {title} ({employer_name}) · "
        f"{source_offer_no} Rev.{source_rev_no}"
    )


def offer_template_created(name: str) -> str:
    return f"Teklif şablonu oluşturuldu: {name}"


def offer_template_updated(name: str) -> str:
    return f"Teklif şablonu güncellendi: {name}"


def offer_template_content_replaced(name: str, group_count: int, item_count: int) -> str:
    return f"Teklif şablonu içeriği güncellendi: {name} · {group_count} grup · {item_count} kalem"


def offer_template_default_set(name: str) -> str:
    return f"Varsayılan teklif şablonu değişti: {name}"


def offer_template_deleted(name: str) -> str:
    return f"Teklif şablonu silindi: {name}"


def offer_template_from_offer(name: str, offer_no: str, rev_no: int) -> str:
    return f"Tekliften şablon oluşturuldu: {name} ← {offer_no} Rev.{rev_no}"


def offer_template_copied(name: str, source_name: str) -> str:
    return f"Teklif şablonu kopyalandı: {name} ← {source_name}"


def offer_updated(offer_no: str, title: str, employer_name: str) -> str:
    return f"Teklif künyesi güncellendi: {offer_no} · {title} ({employer_name})"


def offer_conditions_updated(offer_no: str, rev_no: int, parts: list[str]) -> str:
    """`PATCH …/revisions/{rev_no}`: yalniz DEGISEN alanlar, `eski → yeni`."""
    return f"Teklif koşulları güncellendi: {offer_no} Rev.{rev_no} · {' · '.join(parts)}"


def offer_group_deleted(offer_no: str, rev_no: int, name: str, item_count: int) -> str:
    """Icinde kalem olan grubun silinmesi TEK satir (bos grup satir YAZMAZ)."""
    return f"Teklif grubu silindi: {offer_no} Rev.{rev_no} · {name} · {item_count} kalem"


def offer_deleted(offer_no: str, title: str) -> str:
    return f"Teklif silindi: {offer_no} · {title}"


def offer_revision_created(offer_no: str, rev_no: int) -> str:
    return f"Teklif yeni revizyon açıldı: {offer_no} Rev.{rev_no}"


def offer_status_changed(offer_no: str, rev_no: int, action_text: str) -> str:
    """`action_text`: `gönderildi` / `kazanıldı` / `kaybedildi` / `vazgeçildi`."""
    return f"Teklif {action_text}: {offer_no} Rev.{rev_no}"


def offer_items_bulk_created(offer_no: str, rev_no: int, poz_nos: list[str]) -> str:
    """Toplu kalem ekleme TEK denetim satiri: `N kalem eklendi: poz1, poz2, …`."""
    shown = ", ".join(poz_nos[:OFFER_BULK_POZ_SHOWN])
    rest = len(poz_nos) - OFFER_BULK_POZ_SHOWN
    tail = f" … (+{rest})" if rest > 0 else ""
    return f"Teklife {len(poz_nos)} kalem eklendi: {offer_no} Rev.{rev_no} · {shown}{tail}"


def _short_terms(text: str | None) -> str:
    if text is None:
        return "boş"
    return text if len(text) <= OFFER_TERMS_SHOWN else f"{text[:OFFER_TERMS_SHOWN]}…"


def offer_setting_pct_changed(label: str, old: Decimal, new: Decimal) -> str:
    return f"{label} %{old} → %{new}"


def offer_setting_days_changed(old: int, new: int) -> str:
    return f"geçerlilik {old} → {new} gün"


def offer_setting_terms_changed(old: str | None, new: str | None) -> str:
    return f"ödeme koşulu «{_short_terms(old)}» → «{_short_terms(new)}»"


def offer_date_changed(old: date, new: date) -> str:
    return f"teklif tarihi {old.isoformat()} → {new.isoformat()}"


def offer_delivery_changed(old: int | None, new: int | None) -> str:
    shown_old = "boş" if old is None else old
    shown_new = "boş" if new is None else new
    return f"teslim süresi {shown_old} → {shown_new} gün"


def offer_escalation_changed(old: str, new: str) -> str:
    """`old`/`new`: `sabit` ya da `TÜİK endeksli (<endeks turu>)`."""
    return f"fiyat farkı {old} → {new}"


def offer_notes_changed(old: str | None, new: str | None) -> str:
    return f"notlar «{_short_terms(old)}» → «{_short_terms(new)}»"


def offer_settings_changed(parts: list[str]) -> str:
    """`PUT /offers/settings`: yalniz DEGISEN alanlar, `eski → yeni`."""
    return f"Teklif ayarları güncellendi: {' · '.join(parts)}"
