"""DSC-B3 Ü4 — onayli gunluk snapshot'inin kisitli kullanici icin BUDANMASI (saf, DB'siz).

Snapshot ONAY ANINDA kisitsiz uretilmis bir `DailyReport`'tur ve degismez. Kisitli okuyucuya
YENIDEN HESAPLAMADAN yalniz izinli `d:` bloklari kalir; toplam/iz/altbilgi gibi disiplinler
arasi alanlar DUSER (S6). Baslik alanlari (gun/hafta no, onaylayan, eksik/taslak gunler, hava,
esikler) AYNEN kalir.

Blok varsayimi: `quantities` sirali `d:` → `g:` → `i:` bloklaridir. Bloku belirsiz satir
(oncesinde `d:` yok / bilinmeyen onek) ATILIR — fail-closed.
"""

from __future__ import annotations

from app.modules.earned_value.schemas_reports import DailyReport, WarningOut

_DISCIPLINE_KINDS = frozenset({"discipline", "discipline_own", "discipline_subcon"})
# Gun hedefli uyarilardan yalniz baslik niteligindekiler kalir (disiplin bilgisi tasimaz).
_KEPT_DAY_CODES = frozenset({"missing_diary", "draft_diary"})


def _block_map(report: DailyReport) -> dict[str, str]:
    """node_id → sahibi `d:` kimligi; blogu belirsiz satirlar haritada YOKTUR."""
    owners: dict[str, str] = {}
    current: str | None = None
    for row in report.quantities:
        prefix = row.node_id.split(":", 1)[0]
        if prefix == "d":
            current = row.node_id
            owners[row.node_id] = row.node_id
        elif prefix in ("g", "i") and current is not None:
            owners[row.node_id] = current
    return owners


def _owner_of(target_id: str | None, owners: dict[str, str]) -> str | None:
    if not target_id:
        return None
    if target_id.startswith("l:"):
        parts = target_id.split(":")
        return owners.get(f"i:{parts[1]}") if len(parts) >= 2 else None
    return owners.get(target_id)


def _keep_warning(w: WarningOut, owners: dict[str, str], allowed: set[str]) -> bool:
    if w.target == "day":
        return w.code in _KEPT_DAY_CODES
    return _owner_of(w.target_id, owners) in allowed


def restrict_snapshot(report: DailyReport, allowed_d: set[str]) -> DailyReport:
    owners = _block_map(report)
    return report.model_copy(
        update={
            "kpis": [
                k for k in report.kpis if k.kind in _DISCIPLINE_KINDS and k.node_id in allowed_d
            ],
            "quantities": [q for q in report.quantities if owners.get(q.node_id) in allowed_d],
            "trend": [],
            "footer": None,
            "unrated_entries": [
                w for w in report.unrated_entries if _keep_warning(w, owners, allowed_d)
            ],
            "warnings": [w for w in report.warnings if _keep_warning(w, owners, allowed_d)],
        }
    )
