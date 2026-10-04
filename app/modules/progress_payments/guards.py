"""İşveren hakedişi yazma/geçiş yollarının ORTAK korkulukları ve Türkçe hata
metinleri (spec §6.5, §7, §9.7).

`contracts/guards.py` deseninin birebiri: alan HATA SINIFLARI `app/core/errors.py`'de,
METİNLER modül içinde sabit olarak durur. Kurallar burada TEK kopya durur; router
ve servis katmanları KOPYALAMAZ, ÇAĞIRIR — iki kopya kural zamanla ayrışır ve
ayrışan taraf sessiz bir veri hatası olur.

## Tek cümlelik kural

**Tutarlılık kuralları (§6.5) HER ZAMAN koşar — taslakta bile; zorunluluk
kuralları (§7 submit tablosu) YALNIZ `submit`'te.**

Hakediş sözleşmeler modülündeki taslak/yayın ayrımından farklı olarak taslak
serbestliği yalnız "dönem/satır/sözleşme bedeli eksik olabilir" anlamına gelir
(kalıcı karar 4) — miktar/dağıtım/kota/FF tutarlılık kuralları taslakta da
GEÇERSİZ veri saklanmasını önlemek için her `PUT …/lines` yazımında koşar
(bu modülde DB erişimi gerektirdikleri için `lines.py`/`service.py` içinde
uygulanır, burada yalnız metin sabitleri ve DB'siz `validate_submit` durur).
"""

from decimal import Decimal
from typing import Protocol

from app.core.errors import SiteValidationError

_UNIT_COEFFICIENT = Decimal("1")

# --- Spec §9.7 tablosundan BİREBİR alınmıştır — yeniden yazılmaz. ---

# 404 gövdeleri AYIRT EDİCİ OLMAMALIDIR: görünmeyen bir projedeki GERÇEK hakediş
# ile var olmayan kayıt AYNI mesajı döner (`contracts/guards.py`/`sites/guards.py`
# deseninin aynısı, spec §9.0).
PAYMENT_MISSING = "Hakediş bulunamadı"

# 422 — zorunluluk kuralları (YALNIZ `submit`'te; spec §7 tablosu).
PERIOD_REQUIRED = "Hakediş dönemi seçiniz."
LINES_REQUIRED = "En az bir kalemde miktar giriniz."
CONTRACT_AMOUNT_REQUIRED = "Sözleşme bedeli girilmeden hakediş onaya gönderilemez."

# 422 — kayıt 53/54: hakediş, fatura hakedişten ÖNCE fişlenmiş bir kaynağın
# yerini almış ve faturanın `tax_base`i hakedişin taze hesaplanan tabanıyla
# TOLERANS DIŞI ayrışıyor (`invoicing.source_posting.
# replacing_invoice_base_mismatch`). Onay bu yüzden reddedilir; düzeltme
# faturanın avans/teminat oranlarındadır (`{fatura}` ≠ `{hakedis}`).
SOURCE_REPLACED_BASE_MISMATCH = (
    "Bu hakedişin yerini alan fatura zaten fişlenmiş ({fatura}), ancak hakedişin "
    "gerçek tabanı ({hakedis}) ile eşleşmiyor; hakediş yeniden fişlenemiyor. "
    "Faturanın avans/teminat oranlarını hakedişle eşleyip faturayı düzeltin."
)

# 422 — miktar korkulukları (HER YAZIMDA; spec §6.5). DB erişimi gerektirdikleri
# için `validate_submit` içinde KULLANILMAZ, `lines.py`/`service.py` (H5) bunları
# çağırır — metin sabitleri yine de tek kopya burada durur.
ITEM_NOT_DISTRIBUTED = "Bu poz seçilen şantiyeye dağıtılmadı; önce poz dağılımını yapın."
QUANTITY_EXCEEDS_QUOTA = "Kümülatif hakediş miktarı şantiye kotasını aşamaz."
# `contracts/guards.py:69` metniyle BİREBİR AYNI — kopya değil, iki modülün
# bağımsız sözleşmesi olarak bilinçli tekrar (spec §9.7 dipnotu). `app/core/`
# altında hata METİNLERİ için doğal bir ev YOK (yalnız `app/core/errors.py`
# istisna SINIFLARINI tutar, `SiteValidationError` gibi — metin sabitleri
# değil); yeni bir paylaşılan modül icat etmek modül bağımsızlığı ilkesini
# (bu dosyanın en üstü) ihlal eder. Bunun yerine `test_guards.py`deki
# `test_site_project_mismatch_iki_modulde_senkron` bu iki sabitin SÜRÜKLENMESİNİ
# yakalar: biri değişip diğeri unutulursa test kırmızıya döner.
SITE_PROJECT_MISMATCH = "Seçilen şantiye bu projeye ait değil"
NO_EMPLOYER_CONTRACT = "Bu projenin işveren sözleşmesi yok."
ESCALATION_DISABLED = "Bu sözleşmede fiyat farkı şartı yok."
# §6.5-4 IDOR yüzeyi (spec §9.0): satırdaki kalem bu projenin işveren
# sözleşmesine ait olmalı — H4'te satır snapshot'ı kurulurken (POST'un iç içe
# `lines[]`'ı) ve H5'in `PUT …/lines`'ında ORTAK kullanılır.
ITEM_PROJECT_MISMATCH = "Bu poz bu projenin sözleşmesine ait değil"
# 409 (D1, H4 denetimi) — bu satır yukarıdaki "422 — miktar korkulukları" başlığı
# ALTINDA duruyor ama `DuplicateError` üzerinden fırlatılır (`service._build_lines`),
# `SiteValidationError` DEĞİL: yanıt kodu 422 değil 409'dur (spec §9.7). Kısmi
# benzersiz indeksin (payment, item, site) gövde-içi ön kontrolü —
# `contracts/distribution.py` `DUPLICATE_ALLOCATION` deseninin aynısı:
# IntegrityError'a düşmeden ÖNCE guards'ta yakalanır.
DUPLICATE_CELL = "Aynı poz ve şantiye için tek satır gönderilebilir."

# 409 — durum makinesi + D8 (spec §7, §9.2).
OPEN_PAYMENT_EXISTS = "Bu sözleşmede açık bir hakediş var; önce onu tamamlayın."
INVALID_STATUS_TRANSITION = "Bu durumdan bu işleme geçilemez."
PAYMENT_NOT_DELETABLE = "Onaylanmış veya ödenmiş hakediş silinemez."


def validate_coefficient(coefficient: Decimal | None, *, has_price_escalation: bool) -> None:
    """FF kilidi (spec §10/5) — hakediş BAŞLIĞI ve SATIRI için TEK kopya kural.

    ## Kapsam: yalnız BU İSTEKTE GELEN değer (onaylı sapma, kullanıcı kararı 2026-07-31)

    Kilit `coefficient is None` iken HİÇ koşmaz. Gerekçe kilitlenme senaryosudur:
    FF açıkken katsayılı satır yazılmış bir sözleşmede FF sonradan kapatılırsa,
    kural SAKLANAN katsayı üzerinden koşsaydı taslak bir daha HİÇBİR şekilde
    kaydedilemezdi (kullanıcı katsayı göndermese bile 422). Saklanan ≠1 katsayılar
    bu yüzden KORUNUR (grandfather); kilit yalnız YENİ ≠1 değer yazılmasını önler.

    Çağıran üç yol: `service.create` + `service.update` (başlık `default_coefficient`)
    ve `lines._resolve` (satır `coefficient`). Kuralın üç kopyası OLMAZ — biri
    değişip diğerleri unutulursa hakediş doğuştan kullanılamaz hâle gelirdi
    (H5 denetimi Y1).
    """
    if coefficient is None:
        return
    if not has_price_escalation and coefficient != _UNIT_COEFFICIENT:
        raise SiteValidationError(ESCALATION_DISABLED)


class _LineLike(Protocol):
    """`validate_submit`'in satırlarda okuduğu tek alan."""

    quantity: object


class _PaymentLike(Protocol):
    """`validate_submit`'in okuduğu hakediş alanları.

    Somut bir şemaya/modele BAĞLANMAZ (`contracts/guards.py` desenin aynısı):
    servis katmanı DB'den yüklenmiş `ProgressPayment`'ı doğrudan geçirebilir.
    """

    period_year: object
    lines: list[_LineLike]


class _ContractLike(Protocol):
    """`validate_submit`'in okuduğu sözleşme alanı (§6.3 avans tavanı için)."""

    amount: object


def validate_submit(payment: _PaymentLike, contract: _ContractLike) -> None:
    """Spec §7 "Onaya gönderme zorunluluk kuralları" tablosunu uygular.

    Sıra tablodaki sırayla birebir: dönem → satır varlığı/toplamı → sözleşme
    bedeli. İLK hatada durur (`contracts/guards.py` deseni — form tek seferde
    tek alan gösterir). §6.5 tutarlılık kuralları burada YOKTUR: onlar zaten
    her `PUT …/lines` yazımında koşmuş, taslakta bile geçersiz veri bırakmamıştır.
    """
    if payment.period_year is None:
        raise SiteValidationError(PERIOD_REQUIRED)

    if not payment.lines or sum(line.quantity for line in payment.lines) <= 0:
        raise SiteValidationError(LINES_REQUIRED)

    if contract.amount is None:
        raise SiteValidationError(CONTRACT_AMOUNT_REQUIRED)
