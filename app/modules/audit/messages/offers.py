"""Teklif Hazirlama denetim metinleri (TKL-B4.2). Ayar metni `core.py`dedir (B4.1).

Kalem/grup TEKIL duzenlemeleri bilincli olarak denetim satiri YAZMAZ (gurultu); yalniz
yapisal olaylar (olustur, kunye, kosul, sil, yeni revizyon, durum gecisi, toplu kalem) yazar.
"""

from decimal import Decimal

#: Odeme kosulu serbest metindir; denetim satirinda bu uzunlukta kisaltilir.
OFFER_TERMS_SHOWN = 60

#: Toplu kalem satirinda gosterilen poz sayisi (`contracts.BULK_AUDIT_CODES_SHOWN` emsali).
OFFER_BULK_POZ_SHOWN = 10


def offer_created(offer_no: str, title: str, employer_name: str) -> str:
    return f"Teklif oluşturuldu: {offer_no} · {title} ({employer_name})"


def offer_updated(offer_no: str, title: str, employer_name: str) -> str:
    return f"Teklif künyesi güncellendi: {offer_no} · {title} ({employer_name})"


def offer_conditions_updated(offer_no: str, rev_no: int) -> str:
    return f"Teklif koşulları güncellendi: {offer_no} Rev.{rev_no}"


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


def _short_terms(text: str) -> str:
    return text if len(text) <= OFFER_TERMS_SHOWN else f"{text[:OFFER_TERMS_SHOWN]}…"


def offer_setting_pct_changed(label: str, old: Decimal, new: Decimal) -> str:
    return f"{label} %{old} → %{new}"


def offer_setting_days_changed(old: int, new: int) -> str:
    return f"geçerlilik {old} → {new} gün"


def offer_setting_terms_changed(old: str, new: str) -> str:
    return f"ödeme koşulu «{_short_terms(old)}» → «{_short_terms(new)}»"


def offer_settings_changed(parts: list[str]) -> str:
    """`PUT /offers/settings`: yalniz DEGISEN alanlar, `eski → yeni`."""
    return f"Teklif ayarları güncellendi: {' · '.join(parts)}"
