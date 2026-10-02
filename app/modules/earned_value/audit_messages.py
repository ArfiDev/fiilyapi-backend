"""Planlama (EV) denetim gunlugu metinleri.

⚠️ `app/modules/audit/messages` paketinde DEGIL, burada: spec §2.7 planlamanin
ileride ayri kurulabilir bir modul olmasini ister; metinlerin de modulle birlikte
gitmesi gerekir. (Ayrica o paketin anlik goruntu bekcisi TUM sembolleri kilitler —
`tests/test_tbaudit_denetim_metni_anlik_goruntu.py`.) Kural ayni: metne gizli deger
YAZILMAZ, router'a string gomulmez.
"""

from __future__ import annotations

from datetime import date


def _where(project_name: str, site_name: str) -> str:
    return f"{project_name} · {site_name}"


def settings_saved(project_name: str, site_name: str) -> str:
    return f"Planlama ayarları kaydedildi: {_where(project_name, site_name)}"


def discipline_created(code: str, name: str) -> str:
    return f"Disiplin eklendi: {code} · {name}"


def discipline_updated(code: str, name: str) -> str:
    return f"Disiplin güncellendi: {code} · {name}"


def discipline_deleted(code: str, name: str) -> str:
    return f"Disiplin silindi: {code} · {name}"


def user_disciplines_updated(user_name: str, codes: list[str]) -> str:
    scope = ", ".join(codes) if codes else "kısıtsız"
    return f"Kullanıcı disiplin ataması güncellendi: {user_name} · {scope}"


def catalog_item_created(name: str, uom: str) -> str:
    return f"Birim oran kataloğuna iş tipi eklendi: {name} ({uom})"


def catalog_item_updated(name: str, uom: str) -> str:
    return f"Birim oran kataloğu iş tipi güncellendi: {name} ({uom})"


def catalog_standard_adopted(name: str, uom: str, old: str, new: str) -> str:
    return f"Gerçekleşen standart yapıldı: {name} ({uom}) · {old} → {new} a-s/birim"


def group_disciplines_saved(project_name: str, site_name: str, count: int) -> str:
    return (
        f"BOQ grubu–disiplin eşlemesi kaydedildi: {_where(project_name, site_name)} · {count} grup"
    )


def item_settings_saved(project_name: str, site_name: str, item_code: str) -> str:
    return f"Bütçe iş tipi ayarı kaydedildi: {_where(project_name, site_name)} · {item_code}"


def leaves_saved(project_name: str, site_name: str, count: int) -> str:
    return f"Bütçe oranları kaydedildi: {_where(project_name, site_name)} · {count} satır"


def filled_from_catalog(project_name: str, site_name: str, filled: int) -> str:
    return f"Bütçe katalogdan dolduruldu: {_where(project_name, site_name)} · {filled} satır"


def filled_from_contract(
    project_name: str,
    site_name: str,
    filled: int,
    *,
    linked: int = 0,
    mapped: int = 0,
    windows: int = 0,
    draft_opened: bool = False,
) -> str:
    """Yazılan her şey metne girer (satır her zaman; bağ/eşleme/pencere/taslak yalnız varsa)."""
    parts = [f"{filled} satır"]
    parts.extend(
        f"{count} {label}"
        for count, label in (
            (linked, "katalog bağı"),
            (mapped, "grup eşlemesi"),
            (windows, "pencere"),
        )
        if count
    )
    if draft_opened:
        parts.append("taslak açıldı")
    return f"Bütçe sözleşmeden dolduruldu: {_where(project_name, site_name)} · " + " · ".join(parts)


def distributions_saved(project_name: str, site_name: str, count: int) -> str:
    return f"Bütçe dağılım tipleri kaydedildi: {_where(project_name, site_name)} · {count} disiplin"


def windows_saved(project_name: str, site_name: str, count: int) -> str:
    return (
        f"Bütçe yayma pencereleri kaydedildi: {_where(project_name, site_name)} · {count} pencere"
    )


def draft_opened(project_name: str, site_name: str, number: int) -> str:
    return f"Bütçe taslak revizyonu açıldı: {_where(project_name, site_name)} · Rev {number}"


def draft_deleted(project_name: str, site_name: str, number: int) -> str:
    return f"Bütçe taslak revizyonu silindi: {_where(project_name, site_name)} · Rev {number}"


def revision_frozen(project_name: str, site_name: str, number: int, name: str | None) -> str:
    label = f"Rev {number}" if not name else f"Rev {number} — {name}"
    return f"Bütçe baseline donduruldu: {_where(project_name, site_name)} · {label}"


def day_allocation_saved(project_name: str, site_name: str, day: date, cells: int) -> str:
    return (
        f"Günlük saat dağıtımı kaydedildi: {_where(project_name, site_name)} · "
        f"{day.strftime('%d.%m.%Y')} · {cells} hücre"
    )


def day_unlocked(project_name: str, site_name: str, day: date, reason: str) -> str:
    return (
        f"Gün kilidi açıldı: {_where(project_name, site_name)} · "
        f"{day.strftime('%d.%m.%Y')} · gerekçe: {reason}"
    )


def report_approved(project_name: str, site_name: str, day: date, version: int) -> str:
    return (
        f"Günlük ilerleme raporu onaylandı ve gün kilitlendi: {_where(project_name, site_name)} · "
        f"{day.strftime('%d.%m.%Y')} · sürüm {version}"
    )
