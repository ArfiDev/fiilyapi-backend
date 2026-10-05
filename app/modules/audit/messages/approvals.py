"""Denetim metinleri — onay zinciri motoru (OK-1A).

`APPROVAL_ON_BEHALF_MARK` personel izin ailesiyle PAYLASILIR -> `shared`.
"""

from app.modules.audit.messages.shared import APPROVAL_ON_BEHALF_MARK

# --------------------------------------------------------------------------- #
# OK-1A — onay zinciri motoru
# --------------------------------------------------------------------------- #
#
# 🔴 YENI `AuditAction` UYESI ACILMADI (TB3/T3 kanonu): adim onayi
# `AuditAction.approve`, ret/geri-alma/atama/ayar ise `AuditAction.update`tir.
# Ayrim METINDEDIR.
#
# 🔴 Etiket sozlukleri ENUM SINIFINI ITHAL ETMEZ, duz `str` anahtar kullanir
# (`FINANCIAL_INSTRUMENT_STATUS_LABELS` emsali). Denetim gunlugu bir SUNUM
# katmanidir; ozellik modullerini ithal ederse katman yonu tersine doner.
#
# 🔴 TUTAR metne GIRMEZ. Onay esigi de bir TUTARDIR: eski ve yeni degeri
# gunluge yazsaydik, ayar degistiginde gunlukte donmus bir para kopyasi ikinci
# bir gercek olarak yasamaya devam ederdi (`bank_account_*` / `payment_*`
# kanonu). `COMPANY_UPDATED` de ayni sebeple hicbir alan degeri tasimaz.

APPROVAL_THRESHOLD_UPDATED = "Onay eşiği güncellendi"


#: Onay ROLU etiketleri — `roles/seed_data.py` ROLES adlariyla BIREBIR ayni
#: Turkce karsiliklar (ayni kavramin iki adi olmasin diye).
APPROVAL_ROLE_LABELS: dict[str, str] = {
    "site_chief": "Şantiye Şefi",
    "project_manager": "Proje Müdürü",
    "accounting": "Muhasebe",
    "patron": "Patron",
    "procurement": "Satınalma",
}


#: Evrak ailesi etiketleri (mockup `Onay Kutusu.dc.html` kart başlıkları).
APPROVAL_DOCUMENT_TYPE_LABELS: dict[str, str] = {
    "subcontractor_progress_payment": "Taşeron hakedişi",
    "purchase_request": "Satın alma talebi",
    "progress_payment": "İşveren hakedişi",
}


def _approval_label(document_type: str, step_no: int, total_steps: int, role: str) -> str:
    """Dort denetim metninin TEK kimlik kaynagi.

    Ayri ayri kurulsalardi biri adim sirasini, oteki rolu unutur ve gunlukte
    "hangi imza" sorusu yanitsiz kalirdi. Bilinmeyen bir enum degeri HAM hâliyle
    basilir: sozlukte bulunamayan bir uye metni PATLATMAMALI ama gorunur de
    olmalidir.
    """
    evrak = APPROVAL_DOCUMENT_TYPE_LABELS.get(document_type, document_type)
    rol = APPROVAL_ROLE_LABELS.get(role, role)
    return f"{evrak} · adım {step_no}/{total_steps} · {rol}"


def approval_step_approved(
    document_type: str, step_no: int, total_steps: int, role: str, *, on_behalf: bool
) -> str:
    """Adim onayi. `on_behalf` yalnizca `admin`in KENDI evraginda True olur."""
    metin = f"Onay adımı onaylandı: {_approval_label(document_type, step_no, total_steps, role)}"
    return f"{metin} · {APPROVAL_ON_BEHALF_MARK}" if on_behalf else metin


def approval_chain_rejected(
    document_type: str, step_no: int, total_steps: int, role: str, reason: str, *, on_behalf: bool
) -> str:
    """Ret. Gerekce ZORUNLUDUR (K2) ve zincir silindigi icin TEK kalici izdir."""
    metin = (
        f"Onay zinciri reddedildi: {_approval_label(document_type, step_no, total_steps, role)}"
        f" · Gerekçe: {reason}"
    )
    return f"{metin} · {APPROVAL_ON_BEHALF_MARK}" if on_behalf else metin


def approval_step_rewound(step_no: int, role: str) -> str:
    """Onay adiminin GERI SARILMASI (`/unapprove`, sozlesme Y4).

    Evragin KENDI geri-alma metnine EKLENIR, onun YERINE GECMEZ: mevcut metin
    ESKI ONAYLAYANI ve onay zamanini tasir (H10 dersi) ve o iz KORUNUR — buraya
    yalnizca "hangi imza geri alindi" bilgisi eklenir.

    `_approval_label` KULLANILMAZ: geri alma bir evrak ailesine degil TEK bir
    adima bakar; evrak adi zaten cumlenin ilk yarisindadir ve iki kez yazilmasi
    gunlugu okunmaz yapardi.
    """
    rol = APPROVAL_ROLE_LABELS.get(role, role)
    return f"Geri sarılan onay adımı: {step_no}. adım · {rol}"
