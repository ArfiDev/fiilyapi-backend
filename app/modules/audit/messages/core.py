"""Denetim metinleri — platform cekirdegi: giris, sirket, kullanici, rol, proje.

Tek basina bir alt modulu hak etmeyecek kadar kucuk olan cekirdek aileler
burada toplandi (`auth` 1 · `company` 3 · `projects` 6 · `users` 11 · `roles`
21 satir).
"""

from decimal import Decimal

from app.core.access import AccessLevel

LOGIN_DETAIL = "Sisteme giriş yapıldı"
COMPANY_UPDATED = "Şirket bilgileri güncellendi"
COMPANY_LOGO_UPDATED = "Şirket logosu güncellendi"
COMPANY_LOGO_REMOVED = "Şirket logosu kaldırıldı"


# Erisim seviyelerinin insan-okur karsiliklari — izin matrisi ekranindaki etiketlerle
# ayni dil (frontend permission-presets.ts). Denetim gunlugu enum degeri gostermez.
ACCESS_LEVEL_LABELS: dict[AccessLevel, str] = {
    AccessLevel.none: "Yok",
    AccessLevel.view: "Görüntüle",
    AccessLevel.draft: "Taslak",
    AccessLevel.request: "Talep",
    AccessLevel.approve: "Onay",
    AccessLevel.full: "Tam",
    AccessLevel.admin: "Süper",
}


def user_created(name: str, role_name: str) -> str:
    return f"Kullanıcı oluşturuldu: {name} · {role_name}"


def user_updated(name: str) -> str:
    return f"Kullanıcı güncellendi: {name}"


def password_reset(name: str) -> str:
    """Parolanin kendisi ASLA metne girmez — yalnizca islemin yapildigi bildirilir."""
    return f"Kullanıcı parolası sıfırlandı: {name}"


def user_deleted(name: str) -> str:
    return f"Kullanıcı silindi: {name}"


def project_access_updated(name: str) -> str:
    return f"Proje erişimi güncellendi: {name}"


def role_created(name: str) -> str:
    return f"Özel rol oluşturuldu: {name}"


def role_renamed(old_name: str, new_name: str) -> str:
    """Eski ad cagri noktasinda islemden ONCE okunmali; sonra okunursa yeni ad iki kez cikar."""
    return f"Rol yeniden adlandırıldı: {old_name} → {new_name}"


def role_deleted(name: str) -> str:
    return f"Rol silindi: {name}"


def permission_changed(role_name: str, module_name: str, level: AccessLevel) -> str:
    """Modul ADI kullanilir (module_key degil) — denetim gunlugu dili insan-okur."""
    return f"İzin değişti: {role_name} · {module_name} → {ACCESS_LEVEL_LABELS[level]}"


def employer_created(name: str) -> str:
    return f"Yeni işveren oluşturuldu: {name}"


def project_created(name: str) -> str:
    return f"Yeni proje oluşturuldu: {name}"


def project_updated(name: str) -> str:
    return f"Proje güncellendi: {name}"


def work_item_created(poz_no: str, name: str, uom: str) -> str:
    return f"İş kalemi kataloğuna kalem eklendi: {poz_no} · {name} ({uom})"


def work_item_updated(poz_no: str, name: str, uom: str) -> str:
    return f"İş kalemi kataloğu kalemi güncellendi: {poz_no} · {name} ({uom})"


def _price_text(price: Decimal | None) -> str:
    """Repodaki para bicimi (`{x:,.2f} TL`, bkz. hakedis silme mesajlari); yok → `—`."""
    return "—" if price is None else f"{price:,.2f} TL"


def work_item_price_updated(
    poz_no: str, name: str, uom: str, old_price: Decimal | None, new_price: Decimal | None
) -> str:
    """`ref_price` DEGISEN guncelleme: `work_item_updated` metni + `eski → yeni` fiyat."""
    return (
        f"{work_item_updated(poz_no, name, uom)} · "
        f"referans fiyat {_price_text(old_price)} → {_price_text(new_price)}"
    )


#: Toplu katalog aktariminda denetim metnine yazilan en cok disiplin kodu (kalani "+N").
WORK_ITEMS_BULK_DISCIPLINES_SHOWN = 10


def work_items_bulk_imported(
    created: int, price_updated: int, unchanged: int, discipline_codes: list[str]
) -> str:
    """KAT-B1: toplu katalog aktarimi TEK denetim satiri — sayilar + dokunulan disiplinler."""
    shown = ", ".join(discipline_codes[:WORK_ITEMS_BULK_DISCIPLINES_SHOWN])
    rest = len(discipline_codes) - WORK_ITEMS_BULK_DISCIPLINES_SHOWN
    tail = f" … (+{rest})" if rest > 0 else ""
    return (
        f"İş kalemi kataloğuna toplu aktarım: {created} kalem eklendi, "
        f"{price_updated} kalemin fiyatı güncellendi, {unchanged} kalem değişmedi"
        f" · disiplinler: {shown}{tail}"
    )
