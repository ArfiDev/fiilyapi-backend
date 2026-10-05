"""Denetim metinleri — silme motoru (SIL-B1)."""


def deleted_with_dependents(
    detail: str,
    dependent_count: int,
    breakdown: list[str],
    detached: list[str],
    journal_entries: list[str] | None = None,
    closed_period_entries: list[str] | None = None,
    sources_without_entry: list[str] | None = None,
) -> str:
    """Mevcut silme metninin (`site_deleted` …) sonuna TAM "tür → sayı" dökümünü ekler (plan §4).

    `breakdown`: birlikte silinen kayıtlar, hazır `"Ünite 24"` parçaları (TAMAMI; kesilmez).
    `detached`: SİLİNMEYEN, yalnız bağı kopan (SET NULL) kayıtlar, `"Personel 4"` parçaları.
    `journal_entries`: silinen TÜM fişlerin `"YEV-2026-0001"` numaraları (SIL-B2, tam döküm);
    `closed_period_entries`: bunlardan kapalı dönemdekiler `"YEV-2026-0001 (2026-03)"` (dönem
    kilidi atlandı); `sources_without_entry`: fişi giden, kendisi kalan belgeler.
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
    if journal_entries:
        metin += f" · silinen fişler ({len(journal_entries)}): {', '.join(journal_entries)}"
    if closed_period_entries:
        metin += (
            f" · KAPALI DÖNEM fişleri silindi, dönem kilidi atlandı "
            f"({len(closed_period_entries)}): {', '.join(closed_period_entries)}"
        )
    if sources_without_entry:
        metin += f" · fişsiz kalan kaynak belgeler: {', '.join(sources_without_entry)}"
    return metin
