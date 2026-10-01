"""Denetim metinleri — santiye ailesi: santiye/bolum, gunluk, planlama, puantaj.

Dordu de SANTIYE eksenlidir ve ayni ekran ailesinden beslenir; `timesheet`
(15 satir) tek basina bir dosyayi hak etmiyordu.
"""

from datetime import date


def site_created(name: str) -> str:
    return f"Yeni şantiye oluşturuldu: {name}"


def site_draft_created(name: str) -> str:
    """Taslak olusturma yayin olusturmadan AYRI metindir (spec §10).

    Denetim ekraninda "gercekten bir santiye acildi mi" sorusu metinden
    cevaplanabilmelidir; tek metin kullanmak yarim kalmis bir taslagi tamamlanmis
    bir acilis gibi gosterirdi.
    """
    return f"Yeni şantiye taslağı oluşturuldu: {name}"


def site_sections_created(site_name: str, count: int) -> str:
    """Bolumlu form icin TEK OZET satir (`units_bulk_created` deseni, spec §10).

    Bolum basina ayri satir yazilmaz: 5 bolumlu bir form 6 denetim satiri
    uretmez, 2 uretir — gunluk okunabilirligi kaydin sayisindan daha degerlidir.
    """
    return f"Şantiye bölümleri oluşturuldu: {site_name} · {count} bölüm"


def site_updated(name: str) -> str:
    return f"Şantiye güncellendi: {name}"


def site_published(name: str) -> str:
    """`is_draft: true -> false` gecisi (spec §5.3, §10) — duz guncellemeden AYRI."""
    return f"Şantiye taslaktan yayına alındı: {name}"


def site_deleted(project_name: str, name: str) -> str:
    """Metin santiye satiri SILINMEDEN ONCE kurulmalidir (spec §10).

    Sonra kurulursa `project.name` ve `site.name` guvenilir okunamaz ve satir bos
    adla yazilir — yani silinen kaydin NE OLDUGU tamamen kaybolur.
    """
    return f"Şantiye silindi: {project_name} · {name}"


def section_created(site_name: str, name: str) -> str:
    """Bolum adlari santiyeden bagimsiz tekrar edebilir ("Kat 6-10" her blokta
    olabilir); santiye adi olmadan denetim satiri anlamsizlasir."""
    return f"Yeni bölüm oluşturuldu: {site_name} · {name}"


def section_updated(site_name: str, name: str) -> str:
    return f"Bölüm güncellendi: {site_name} · {name}"


def section_published(site_name: str, name: str) -> str:
    """`site_published` deseninin birebiri: `is_draft: true -> false` gecisi
    (spec §5.3) duz guncellemeden AYRI metindir. YENI `AuditAction` ACILMAZ —
    ayrim METINDEDIR, aksiyon `update` olarak kalir."""
    return f"Bölüm taslaktan yayına alındı: {site_name} · {name}"


def section_deleted(site_name: str, name: str) -> str:
    """`site_deleted` ile ayni kural: metin `session.delete`ten ONCE kurulur."""
    return f"Bölüm silindi: {site_name} · {name}"


# --- Santiye gunlugu (site_diary T2) ---
#
# Yeni `AuditAction` GEREKMEDI: gunluk kayit acmak `create`, basligini
# duzenlemek `update`, silmek `delete` aksiyonuna oturur. Denetim ekrani
# aksiyona degil METNE gore okunur.
#
# Kaydin kimligi UUID degil INSAN-OKUR uclusudur (proje · santiye · gun):
# denetim satirini okuyan kisi hangi gunun kaydinin silindigini gormek ister,
# bir kimlik dizisini degil.


def site_diary_entry_created(
    project_name: str, site_name: str, entry_date: date, line_count: int
) -> str:
    return (
        f"Günlük kayıt oluşturuldu: {project_name} · {site_name} · {entry_date.isoformat()} "
        f"· {line_count} poz"
    )


def site_diary_entry_updated(project_name: str, site_name: str, entry_date: date) -> str:
    return f"Günlük kayıt güncellendi: {project_name} · {site_name} · {entry_date.isoformat()}"


def site_diary_lines_saved(
    project_name: str, site_name: str, entry_date: date, line_count: int
) -> str:
    """T3 `PUT …/lines`. `update` aksiyonuna oturur: satir kaydetmek kaydin
    ICERIGINI degistirir, yeni bir kayit ACMAZ."""
    return (
        f"Günlük kayıt poz satırları kaydedildi: {project_name} · {site_name} "
        f"· {entry_date.isoformat()} · {line_count} poz"
    )


def site_diary_entry_submitted(project_name: str, site_name: str, entry_date: date) -> str:
    """T4 `POST …/submit`. `update` aksiyonuna oturur: `AuditAction.approve` HAKEDIS
    onayina ayrilmistir — gunluk gonderimi bir onay DEGILDIR (iki durumlu akis)."""
    return f"Günlük kayıt gönderildi: {project_name} · {site_name} · {entry_date.isoformat()}"


def site_diary_entry_reopened(project_name: str, site_name: str, entry_date: date) -> str:
    """T4 `POST …/reopen` — YALNIZ admin. Denetim satiri kritiktir: gonderilmis bir
    kaydin yeniden yazilabilir hale gelmesi hakedise giden veriyi degistirir."""
    return (
        f"Günlük kayıt taslağa geri alındı: {project_name} · {site_name} · {entry_date.isoformat()}"
    )


def site_diary_entry_deleted(
    project_name: str, site_name: str, entry_date: date, status_label: str, line_count: int
) -> str:
    return (
        f"Günlük kayıt silindi: {project_name} · {site_name} · {entry_date.isoformat()} "
        f"· {status_label} · {line_count} poz"
    )


def timesheet_saved(
    project_name: str, site_name: str, year: int, month: int, cell_count: int
) -> str:
    """Puantaj T3 `PUT …/timesheet`. TEK donem-ozeti olayidir: hucre basina olay
    yazmak 31 gun x 48 isci'lik bir kaydetmede 1488 satir uretir ve denetim
    gunlugunu kullanilamaz hale getirirdi (spec §3).

    `update` aksiyonuna oturur: kaydetme donemin ICERIGINI degistirir, yeni bir
    kayit ACMAZ (`AuditAction.approve` hakedis onayina ayrilmistir — puantajda
    onay akisi YOKTUR, spec §7 S3).
    """
    return (
        f"Puantaj kaydedildi: {project_name} · {site_name} · {year}-{month:02d} "
        f"· {cell_count} hücre"
    )


def timesheet_week_saved(
    project_name: str,
    site_name: str,
    iso_year: int,
    iso_week: int,
    start: date,
    end: date,
    cell_count: int,
) -> str:
    """Puantaj `PUT …/timesheet/week` (PUAN-SAAT). TEK hafta-ozeti olayidir.

    Kaydin kimligi INSAN-OKUR: ISO hafta numarasi TEK BASINA yetmez (kimse
    "2026-W29"u tarihe cevirmez), bu yuzden aralik da yazilir.
    """
    return (
        f"Puantaj haftasi kaydedildi: {project_name} · {site_name} · "
        f"{iso_year}-W{iso_week:02d} ({start.isoformat()} – {end.isoformat()}) "
        f"· {cell_count} hücre"
    )


# --- Santiye planlama (site_planning T3) ---
#
# Dort ucun dordu de `AuditAction.update`tir: kaydetme planin ICERIGINI
# degistirir, yeni bir kayit ACMAZ. Her uc TEK ozet olayi yazar — satir/hucre/
# hedef basina olay yazmak 7 gun x N satirlik bir kaydetmede denetim gunlugunu
# kullanilamaz hale getirirdi (planlama spec §3, puantajin `timesheet_saved`
# gerekcesinin aynisi).
#
# Kaydin kimligi UUID degil INSAN-OKUR ucludur (proje · santiye · hafta).


def site_plan_rows_saved(project_name: str, site_name: str, row_count: int) -> str:
    """`PUT …/plan/rows`. Hafta YOKTUR: satir listesi haftadan bagimsizdir."""
    return f"Şantiye planı satırları kaydedildi: {project_name} · {site_name} · {row_count} satır"


def site_plan_cells_saved(
    project_name: str, site_name: str, week_start: date, cell_count: int
) -> str:
    return (
        f"Şantiye planı hücreleri kaydedildi: {project_name} · {site_name} "
        f"· {week_start.isoformat()} haftası · {cell_count} hücre"
    )


def site_plan_goals_saved(
    project_name: str, site_name: str, week_start: date, goal_count: int
) -> str:
    return (
        f"Şantiye planı haftalık hedefleri kaydedildi: {project_name} · {site_name} "
        f"· {week_start.isoformat()} haftası · {goal_count} hedef"
    )


def site_plan_sprint_saved(project_name: str, site_name: str, name: str | None) -> str:
    """Sprint ADI degistirildi ya da (ad bossa) aktif sprint kapatildi."""
    if name is None:
        return f"Şantiye planı aktif sprinti kaldırıldı: {project_name} · {site_name}"
    return f"Şantiye planı aktif sprinti kaydedildi: {project_name} · {site_name} · {name}"


def section_type_created(name: str) -> str:
    """BLF-B1 — sirket geneli bolum tipi listesine yeni tip eklendi."""
    return f"Bölüm tipi eklendi: {name}"
