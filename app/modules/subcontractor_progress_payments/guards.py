"""Taşeron hakedişi korkulukları ve Türkçe hata metinleri (spec §2, §5, §6).

## Neden metinlerin çoğu İŞVEREN modülünden İMPORT ediliyor?

İki hakediş ailesi AYNI izin modülünü (`progress_payments`) ve AYNI dört durumlu
makineyi paylaşır (spec §5). Aşağıdaki dört metin iki modülde de KELİMESİ KELİMESİNE
aynı olmak zorundadır — kullanıcı aynı ekran ailesinde iki farklı cümle görmemeli.
`progress_payments/guards.py` bu metinleri iki modülün (contracts/sites) bilinçli
tekrarı gerekçesiyle kopyalamıştı; burada gerekçe TERSİDİR: bağımsız iki sözleşme
değil, TEK ekran ailesinin iki evrağı söz konusudur, bu yüzden metin TEK KOPYA
paylaşılır (plan T2: "kopya kod yazma, paylaşım tercih et").

Bu modüle ÖZGÜ kurallar (`ITEM_PRICE_REQUIRED`, `SECTION_MISMATCH`) burada tanımlanır.
"""

from decimal import Decimal
from typing import Protocol

from app.core.errors import SiteValidationError
from app.modules.progress_payments.guards import (
    INVALID_STATUS_TRANSITION,
    LINES_REQUIRED,
    OPEN_PAYMENT_EXISTS,
    PAYMENT_MISSING,
    PAYMENT_NOT_DELETABLE,
    PERIOD_REQUIRED,
    SOURCE_REPLACED_BASE_MISMATCH,
)

__all__ = [
    "DUPLICATE_LINE",
    "INVALID_STATUS_TRANSITION",
    "ITEM_CONTRACT_MISMATCH",
    "ITEM_PRICE_REQUIRED",
    "LINES_REQUIRED",
    "OPEN_PAYMENT_EXISTS",
    "PAYMENT_MISSING",
    "PAYMENT_NOT_DELETABLE",
    "PERIOD_REQUIRED",
    "REJECTION_REASON_REQUIRED",
    "SECTION_MISMATCH",
    "SOURCE_REPLACED_BASE_MISMATCH",
    "quantity_exceeds_quota",
    "source_quantity_exceeds_quota",
    "validate_reject",
    "validate_submit",
]

# 422 (spec §2 guard'ı): "girilmedi ≠ 0 TL" — fiyatı girilmemiş sözleşme kalemi
# hakedişe alınamaz, çünkü NULL'u 0 saymak taşerona sessizce bedelsiz iş yazardı.
ITEM_PRICE_REQUIRED = (
    "Sözleşmede birim fiyatı girilmemiş kalem var; hakediş oluşturmadan önce fiyatları girin."
)

# 422 (spec §8 S2): `section_id` bilgi alanıdır ama SAHİPSİZ olamaz — sözleşmenin
# şantiyesine (şantiye bağı yoksa projesinin herhangi bir şantiyesine) ait olmalıdır.
# Var olmayan bölüm de AYNI 422'yi alır: bölüm bir alan DEĞERİDİR, kaynak değil
# (`contracts/subcontracts.py._resolve_subcontractor_name` deseninin aynısı).
SECTION_MISMATCH = "Seçilen bölüm bu sözleşmenin şantiyesine ait değil"

# --- T3: satır yazma yolu (`PUT …/lines`, spec §2 guard'ı + §4 kota) ---

# 422 — IDOR yüzeyi: satırdaki kalem BU hakedişin sözleşmesine ait olmalı.
# İşverendeki `ITEM_PROJECT_MISMATCH`in taşeron karşılığıdır; kapsam PROJE değil
# SÖZLEŞMEDİR (taşeron kalemi sözleşmeye bağlıdır, spec §2). Var OLMAYAN kalem de
# AYNI 422'yi alır: kalem bir alan DEĞERİDİR, ayrı bir kaynak değil.
ITEM_CONTRACT_MISMATCH = "Bu kalem bu taşeron sözleşmesine ait değil"

# 409 (`DuplicateError`) — kısmi benzersiz indeksin (payment_id, contract_item_id)
# gövde-içi ön kontrolü; `IntegrityError`a DÜŞMEDEN yakalanır. Şantiye kırılımı
# olmadığı için hücre kimliği tek başına kalemdir (işveren `DUPLICATE_CELL`inin
# şantiyesiz karşılığı).
DUPLICATE_LINE = "Aynı kalem için tek satır gönderilebilir."


def quantity_exceeds_quota(code: str, remaining: Decimal, unit: str) -> str:
    """422 — kümülatif kota aşımı (spec §4; tavan = sözleşme kalem miktarı).

    İşverendeki sabit metinden AYRILIR ve AŞILAN KALEM ile KALAN MİKTARI taşır:
    taşeron hakedişi tek ekranda sözleşmenin TÜM kalemlerini gösterir (O66), tek
    cümlelik genel bir hata kullanıcıyı hangi satırı düzelteceği konusunda
    yalnız bırakırdı. Kalan negatife düşmez (`max(…, 0)`) — sözleşme miktarı
    sonradan düşürülmüşse "kalan −40" gibi anlamsız bir sayı gösterilmez.
    """
    kalan = max(remaining, Decimal("0"))
    return (
        f"Kümülatif hakediş miktarı sözleşme miktarını aşamaz: {code} · kalan {kalan:,.3f} {unit}"
    )


def source_quantity_exceeds_quota(code: str, remaining: Decimal, unit: str) -> str:
    """422 — ÇİFT SAYIM tavanı (TH-PRJGENEL): tavan taşeron sözleşmesinin kalem
    miktarı DEĞİL, kalemin bağlı olduğu **işveren kaleminin** miktarıdır.

    Aynı işveren kalemine iki taşeron sözleşmesi köprü kurabildiği için
    (`subcontractor_contract_items`te UNIQUE yalnız `(contract_id, code)`),
    sözleşme başına kota ikisini birbirinden habersiz sayar ve aynı imalat iki
    kez ödenebilirdi. Bu tavan kaynak kalemin TOPLAMINA karşıdır: bölünmüş iş
    (10 + 6) geçer, çift sayım (16 + 12) geçmez.

    🔴 Metin KARŞI SÖZLEŞMEYİ ADLANDIRMAZ — yalnız KALAN sayıyı taşır; aksi
    hâlde kapsam dışı bir sözleşmenin varlığı hata mesajından sızardı.
    """
    kalan = max(remaining, Decimal("0"))
    return (
        "Bu kalemin bağlı olduğu işveren kalemi için yapılan toplam hakediş "
        f"işveren sözleşmesindeki miktarı aşamaz: {code} · kalan {kalan:,.3f} {unit}"
    )


# --- T4: durum makinesi korkulukları (spec §5) ---

# 422 — ret gerekçesi ZORUNLUDUR (işverenden AYRILAN nokta): işverende gerekçe
# yalnız denetim günlüğüne giden opsiyonel bir metindir, burada ise KALICI
# `rejection_reason` kolonuna yazılır ve L177 "Revize Gerekli" rozetinin
# kullanıcıya gösterilen açıklamasıdır — gerekçesiz bir rozet, taşerona neyi
# revize edeceğini söylemez.
REJECTION_REASON_REQUIRED = "Ret gerekçesi zorunludur."


class _LineLike(Protocol):
    quantity: object


class _PaymentLike(Protocol):
    period_year: object
    lines: list[_LineLike]


def validate_submit(payment: _PaymentLike) -> None:
    """Onaya gönderme zorunlulukları (spec §5) — YALNIZ `submit`te koşar.

    İşveren `progress_payments.guards.validate_submit`in İKİ kuralı burada da
    geçerlidir ve metinleri ORADAN gelir (kullanıcı aynı ekran ailesinde iki
    farklı cümle görmemeli). ÜÇÜNCÜ kuralı ("sözleşme bedeli girilmeden onaya
    gönderilemez") burada YOKTUR: taşeron sözleşmesinde `amount` kolonu yoktur
    (K3 türev ilkesi), bedel `Σ quantity × unit_price`tır ve fiyatsız kalem zaten
    hakedişe ALINAMAZ (`ITEM_PRICE_REQUIRED`) — kural ölü bir kontrol olurdu.
    """
    if payment.period_year is None:
        raise SiteValidationError(PERIOD_REQUIRED)
    if not payment.lines or sum(line.quantity for line in payment.lines) <= 0:
        raise SiteValidationError(LINES_REQUIRED)


def validate_reject(reason: str | None) -> str:
    """Ret gerekçesini doğrular ve KIRPILMIŞ hâlini döner.

    Boş/yalnız boşluktan oluşan gerekçe 422'dir: Pydantic `min_length` tek başına
    yetmez ("   " üç karakterdir), kırpma kuralı TEK kopya burada durur.
    """
    trimmed = (reason or "").strip()
    if not trimmed:
        raise SiteValidationError(REJECTION_REASON_REQUIRED)
    return trimmed
