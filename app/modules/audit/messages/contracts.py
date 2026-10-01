"""Denetim metinleri — sozlesmeler (P5) + metraj/BOQ.

BOQ (22 satir) sozlesmenin poz tarafidir ve ayni ekran akisindan beslenir.
"""


def boq_group_created(name: str) -> str:
    return f"İş kalemi grubu oluşturuldu: {name}"


def boq_group_updated(name: str) -> str:
    return f"İş kalemi grubu güncellendi: {name}"


def boq_item_created(code: str, description: str) -> str:
    return f"İş kalemi oluşturuldu: {code} — {description}"


def boq_item_updated(code: str, description: str) -> str:
    return f"İş kalemi güncellendi: {code} — {description}"


def boq_item_allocations_replaced(code: str, section_count: int) -> str:
    """BOQ-SEC: pozun bölüm tahsisleri değiştirildi.

    Adet VERİLİR (silme mesajlarının "adet verilmez" kuralının tersi): burada
    sayı bir uyarı değil DEĞİŞİKLİĞİN KENDİSİDİR — `0` "tüm tahsisler
    kaldırıldı" demektir ve denetim kaydında bu ayırt edilebilmelidir.
    """
    return f"İş kalemi bölüm tahsisleri güncellendi: {code} — {section_count} bölüm"


def boq_item_deleted(code: str, description: str) -> str:
    return f"İş kalemi silindi: {code} — {description}"


def boq_group_deleted(name: str) -> str:
    """`boq_group_updated` ailesinin devami. YENI `AuditAction` ACILMAZ —
    aksiyon `delete`tir (`boq_item_deleted` ile ayni), ayrim METINDEDIR."""
    return f"İş kalemi grubu silindi: {name}"


# --- Sözleşmeler (P5, spec §8, task C13) ---
#
# C6-C12 bu aileleri `contracts/router.py`de geçici, modül-içi yardımcılar
# olarak yazdı (task brief kararı — henüz burada yoklardı). C13 onları TEK
# yere taşır; metinler değişmedi, yalnız yerleri değişti.


def employer_contract_group_created(project_name: str, name: str) -> str:
    return f"Sözleşme poz grubu oluşturuldu: {project_name} · {name}"


def employer_contract_group_updated(project_name: str, name: str) -> str:
    return f"Sözleşme poz grubu güncellendi: {project_name} · {name}"


def employer_contract_group_deleted(project_name: str, name: str) -> str:
    return f"Sözleşme poz grubu silindi: {project_name} · {name}"


def employer_contract_item_created(project_name: str, code: str, description: str) -> str:
    """`boq_item_created` deseninin aynısı: kod TEK BAŞINA anlamsız, açıklama

    olmadan denetim satırı hangi kalemin oluştuğunu göstermez (spec §8'in
    kısaltılmış imzası `(project_name, code)` yalnız özet listedir — gerçek
    metin `boq_item_*` ailesinin deseni izlenerek `description` de taşır).
    """
    return f"Sözleşme poz kalemi oluşturuldu: {project_name} · {code} — {description}"


#: Toplu eklemede denetim metnine yazılan en çok kod sayısı (kalanı "+N" ile özetlenir).
BULK_AUDIT_CODES_SHOWN = 10


def employer_contract_items_bulk_created(project_name: str, codes: list[str]) -> str:
    """TKL-B3.1: toplu poz ekleme TEK denetim satırı — `N poz eklendi: kod1, kod2, …`."""
    shown = ", ".join(codes[:BULK_AUDIT_CODES_SHOWN])
    rest = len(codes) - BULK_AUDIT_CODES_SHOWN
    tail = f" … (+{rest})" if rest > 0 else ""
    return f"Sözleşmeye {len(codes)} poz eklendi: {project_name} · {shown}{tail}"


def employer_contract_item_updated(
    project_name: str, code: str, description: str, refreshed_boq_count: int = 0
) -> str:
    """TB4/B3+S7: kalemin AYNA ALAN KÜMESİ (`code`/`description`/`unit`/
    `unit_price` — `contracts.service.MIRRORED_ITEM_FIELDS`) değişimi ayna BOQ
    satırlarını da

    tazeler. Bu yan etki MEVCUT `update` olayının detayına eklenir — yeni bir
    `AuditAction` üyesi açmak gerçek Postgres enum'una migration isterdi
    (TB3-C emsali). Tazeleme olmadıysa metin BİREBİR eskisi gibi kalır.
    """
    detail = f"Sözleşme poz kalemi güncellendi: {project_name} · {code} — {description}"
    if refreshed_boq_count > 0:
        detail += f" · {refreshed_boq_count} BOQ satırı tazelendi"
    return detail


def employer_contract_item_deleted(project_name: str, code: str, description: str) -> str:
    return f"Sözleşme poz kalemi silindi: {project_name} · {code} — {description}"


def contract_distribution_saved(project_name: str, count: int) -> str:
    return f"Poz dağılımı kaydedildi: {project_name} · {count} eşleştirme"


def subcontractor_created(name: str) -> str:
    return f"Taşeron oluşturuldu: {name}"


def subcontractor_updated(name: str) -> str:
    return f"Taşeron güncellendi: {name}"


def subcontractor_deleted(name: str) -> str:
    return f"Taşeron silindi: {name}"


def subcontract_label(contract_no: str | None, subcontractor_name: str | None) -> str:
    """Sözleşme no yoksa taşeron adı, o da yoksa "taslak" — taslak aşamasında

    henüz doldurulmamış alanlar için anlamlı bir denetim etiketi üretir.
    """
    return contract_no or subcontractor_name or "taslak"


def subcontract_created(project_name: str, label: str) -> str:
    return f"Taşeron sözleşmesi oluşturuldu: {project_name} · {label}"


def subcontract_updated(project_name: str, label: str) -> str:
    return f"Taşeron sözleşmesi güncellendi: {project_name} · {label}"


def subcontract_published(project_name: str, label: str) -> str:
    """`site_published` deseninin aynısı: `is_draft: true -> false` geçişi

    (spec §5.3/§8, §10) — düz güncellemeden AYRI metindir.
    """
    return f"Taşeron sözleşmesi taslaktan yayına alındı: {project_name} · {label}"


def subcontract_deleted(project_name: str, label: str) -> str:
    return f"Taşeron sözleşmesi silindi: {project_name} · {label}"


def subcontract_item_created(contract_no: str | None, code: str) -> str:
    return f"Taşeron sözleşmesi kalemi oluşturuldu: {contract_no or 'taslak'} · {code}"


def subcontract_item_updated(contract_no: str | None, code: str) -> str:
    return f"Taşeron sözleşmesi kalemi güncellendi: {contract_no or 'taslak'} · {code}"


def subcontract_item_deleted(contract_no: str | None, code: str) -> str:
    return f"Taşeron sözleşmesi kalemi silindi: {contract_no or 'taslak'} · {code}"


def subcontract_items_loaded(contract_no: str | None, count: int) -> str:
    label = contract_no or "taslak"
    return f"Taşeron sözleşmesi kalemleri işverenden yüklendi: {label} · {count} kalem"
