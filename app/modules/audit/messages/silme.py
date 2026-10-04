"""Denetim metinleri — silme motoru (SIL-B1)."""


def deleted_with_dependents(
    detail: str, dependent_count: int, breakdown: list[str], detached: list[str]
) -> str:
    """Mevcut silme metninin (`site_deleted` …) sonuna TAM "tür → sayı" dökümünü ekler (plan §4).

    `breakdown`: birlikte silinen kayıtlar, hazır `"Ünite 24"` parçaları (TAMAMI; kesilmez).
    `detached`: SİLİNMEYEN, yalnız bağı kopan (SET NULL) kayıtlar, `"Personel 4"` parçaları.
    Uzunluk sınırı YOK, bilinçli: `audit_log.detail` `Text`tir ve döküm en çok ağaçtaki tablo sayısı
    (şantiye ailesinde ≤ 67) kadar kısa parça taşır (~2 KB). Kesmek, plan §4'ün "ne silindi"
    kanıtını eksiltirdi. Bağlı kayıt yoksa metin DEĞİŞMEZ: eski satırlarla aynı biçimde kalır.
    """
    metin = detail
    if dependent_count:
        metin += f" · {dependent_count} bağlı kayıtla birlikte silindi"
        if breakdown:
            metin += f" ({', '.join(breakdown)})"
    if detached:
        metin += f" · bağı kopan (silinmedi): {', '.join(detached)}"
    return metin
