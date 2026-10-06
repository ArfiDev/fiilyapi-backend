"""Personel şemaları — puantaj spec §2, §3 + İK-1 kart genişlemesi (spec §1, §5).

`customers/schemas.py` üçlüsünün (Create/Update/Response) kardeşi. Kaynak/taşeron
uyuşması + TCKN + taslak/yayın zorunluluğu burada DEĞİL `guards.py`/`service.py`de
doğrulanır: PATCH kısmi gövde gönderir, kurallar ancak DB'deki kayıtla
BİRLEŞTİRİLMİŞ değerler üzerinde anlamlıdır.

**İK-1 (spec §1, §5 K3):** yeni kart kolonları eklendi ve HEPSİ Create'te
OPSİYONELdir — zorunluluk taslak-farkındalıklıdır (yayında `service` katmanında
zorlanır). `is_draft` Create'te varsayılan `True` (mockup "Taslak" ile "Personeli
Kaydet" iki ayrı buton; yayın akışı `is_draft=false` gönderir). Foto/vergi no
AÇILMADI (spec §5 K2/K6); belge alt-kaynağı şeması T3'ün işidir.

## 🔴 `iban` — biçim + mod-97 (canlı smoke bulgusu)

Alan bir PARA yüzeyidir (bordronun ödeme talimatı buradan çıkar) ama tek koruması
`max_length=34`tü: normalizasyon bile YOKTU, yani `tr33 0006…` ile
`TR330006…` iki ayrı metin olarak saklanabilirdi. Kural `app/core/iban.py`nin
TEK kaynağındadır ve `treasury` ile PAYLAŞILIR; Create **ve** Update'in ikisine
de bağlıdır — biri açık kalsaydı kapı o uçtan atlatılırdı. Alan **nullable
KALIR** (elden ödeme meşrudur); yalnız DOLU geldiğinde sınanır.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.field_mask import Hassas
from app.core.iban import iban_field_validator
from app.modules.personnel import guards
from app.modules.personnel.models import (
    Gender,
    LeaveStatus,
    MaritalStatus,
    PaymentMethod,
    WageType,
)
from app.modules.site_diary.models import WorkerSource

# 🔴 GECE KARARI (IZN-B4c, personnel): maaş, IBAN, TCKN, SGK no, doğum tarihi, telefon, adres,
# e-posta, acil durum telefonu = `maas_kisisel` (fail-closed: SGK no ve acil durum telefonu da
# kişisel sayıldı). Ad soyad, meslek, işe giriş, cinsiyet/medeni hâl, izin tipi/gerekçesi kimlik
# veya operasyonel veridir, GİZLENMEZ. İzin günü/devreden/borç GÜN sayısıdır, tutar değil → `yok`.
# `Girdi` takma adları İSTEK gövdesindedir (PUT/PATCH'te gizli rol dolu alan gönderirse 403).
KisiselMetin = Annotated[str | None, Hassas.maas_kisisel]
KisiselTarih = Annotated[date | None, Hassas.maas_kisisel]
KisiselTutar = Annotated[Decimal | None, Hassas.maas_kisisel]
GunSayisi = Annotated[Decimal, Hassas.yok]
GunSayisiOpsiyonel = Annotated[Decimal | None, Hassas.yok]


_FREE_LABEL_MAX = 150


class PersonnelCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    # Meslek SERBEST METİN (spec §7 S5) — katalog tablosu YOK, diary `trade` deseni.
    trade: str | None = Field(default=None, max_length=100)
    source: WorkerSource
    subcontractor_id: uuid.UUID | None = None
    # Login ŞART DEĞİL: işçiler `users` kaydı olmadan da puantaja girer (spec §2).
    user_id: uuid.UUID | None = None
    is_active: bool = True

    # --- İK-1 kart alanları (PE 51-118) — HEPSİ opsiyonel (spec §5 K3) --------
    tc_no: KisiselMetin = Field(default=None, max_length=11)
    birth_date: KisiselTarih = None
    gender: Gender | None = None
    marital_status: MaritalStatus | None = None
    phone: KisiselMetin = Field(default=None, max_length=30)
    email: KisiselMetin = Field(default=None, max_length=255)
    address: KisiselMetin = None
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: KisiselMetin = Field(default=None, max_length=30)
    hire_date: date | None = None
    wage_type: WageType | None = None
    wage_amount: KisiselTutar = Field(default=None, ge=0)
    payment_method: PaymentMethod | None = None
    iban: KisiselMetin = Field(default=None, max_length=34)
    sgk_no: KisiselMetin = Field(default=None, max_length=20)
    assigned_project_id: uuid.UUID | None = None
    assigned_section_id: uuid.UUID | None = None
    # Taslak varsayılan (mockup "Taslak" butonu); yayın akışı açıkça `false` gönderir.
    is_draft: bool = True

    _iban_dogrula = iban_field_validator()


class PersonnelUpdate(BaseModel):
    full_name: str | None = Field(default=None, min_length=1, max_length=200)
    trade: str | None = Field(default=None, max_length=100)
    source: WorkerSource | None = None
    subcontractor_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    # Pasifleştirme YOLU: DELETE ucu yoktur (spec §3), çıkarma bu bayrakla yapılır.
    is_active: bool | None = None

    # --- İK-1 kart alanları — kısmi gönderim (spec §5 K3) --------------------
    tc_no: KisiselMetin = Field(default=None, max_length=11)
    birth_date: KisiselTarih = None
    gender: Gender | None = None
    marital_status: MaritalStatus | None = None
    phone: KisiselMetin = Field(default=None, max_length=30)
    email: KisiselMetin = Field(default=None, max_length=255)
    address: KisiselMetin = None
    emergency_contact_name: str | None = Field(default=None, max_length=200)
    emergency_contact_phone: KisiselMetin = Field(default=None, max_length=30)
    hire_date: date | None = None
    wage_type: WageType | None = None
    wage_amount: KisiselTutar = Field(default=None, ge=0)
    payment_method: PaymentMethod | None = None
    iban: KisiselMetin = Field(default=None, max_length=34)
    sgk_no: KisiselMetin = Field(default=None, max_length=20)
    assigned_project_id: uuid.UUID | None = None
    assigned_section_id: uuid.UUID | None = None
    is_draft: bool | None = None

    _iban_dogrula = iban_field_validator()

    # KARARLAR.md §1.10 (kullanıcı kararı 2026-09-23): NOT NULL kolona AÇIK
    # `null` -> 422 alan adıyla (`EmployerContractItemUpdate._acik_null_reddedilir`
    # emsali). `mode="before"` doğrulayıcı YALNIZ gövdede GEÇEN alan için
    # koşar — "alan gönderilmedi" hâli (varsayılan `None`) ETKİLENMEZ; bugüne
    # kadar bu dört alana açık `null` gönderilince ya opak 409 "Veri bütünlüğü
    # hatası" (IntegrityError) ya da — `is_draft` taslakta iken — YANLIŞ
    # gerekçeli bir 422 ("yayın için eksik alanlar") dönüyordu.
    @field_validator(*guards.PERSONNEL_NULLABLE_OLMAYAN_ALANLAR, mode="before")
    @classmethod
    def _acik_null_reddedilir(cls, value: object) -> object:
        if value is None:
            raise ValueError(guards.PERSONNEL_FIELD_NOT_NULL)
        return value


class PersonnelResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # URL-4 · 🔴 KVKK: bu slug YALNIZ `full_name`den üretilir. `tc_no` tablonun
    # tek tekil anahtarıdır ve URL'ye ASLA girmez; telefon/e-posta/TCKN'nin
    # hiçbir parçası slug'a girmez (kullanıcı kararı 2026-09-05).
    slug: str | None = None
    full_name: str
    trade: str | None
    source: WorkerSource
    subcontractor_id: uuid.UUID | None
    user_id: uuid.UUID | None
    is_active: bool

    # --- İK-1 kart alanları ---------------------------------------------------
    tc_no: KisiselMetin
    birth_date: KisiselTarih
    gender: Gender | None
    marital_status: MaritalStatus | None
    phone: KisiselMetin
    email: KisiselMetin
    address: KisiselMetin
    emergency_contact_name: str | None
    emergency_contact_phone: KisiselMetin
    hire_date: date | None
    wage_type: WageType | None
    wage_amount: KisiselTutar
    payment_method: PaymentMethod | None
    iban: KisiselMetin
    sgk_no: KisiselMetin
    assigned_project_id: uuid.UUID | None
    assigned_section_id: uuid.UUID | None
    is_draft: bool


class PersonnelListResponse(BaseModel):
    """`audit`/`users` liste deseninin aynısı: `total` + `limit`/`offset`."""

    items: list[PersonnelResponse]
    total: int
    limit: int
    offset: int


# --- İK-1 T3: belge alt-kaynağı (spec §2, §3, §5 K5) -----------------------


class PersonnelDocumentCreate(BaseModel):
    """Belge takip kaydı — `type_id` XOR `free_label` (tam biri, spec §2).

    XOR pydantic'te BURADA da doğrulanır (giriş katmanı 422): `model_validator`
    "tam biri" kuralını uygular. Servis (`guards.TYPE_XOR_LABEL`) ile DB CHECK
    ikinci ve üçüncü katlardır — biri düşse öteki tutar (WORKFLOW savunma
    derinliği).

    `document_id` BC arşiv künyesine bağdır (opsiyonel; dosyasız takip meşru,
    spec §2). Görünürlük denetimi SERVİSTEDİR (IDOR) — pydantic yalnız biçime bakar.
    """

    type_id: uuid.UUID | None = None
    free_label: str | None = Field(default=None, max_length=_FREE_LABEL_MAX)
    valid_until: date | None = None
    issued_at: date | None = None
    note: str | None = None
    document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _type_xor_label(self) -> "PersonnelDocumentCreate":
        has_type = self.type_id is not None
        has_label = bool(self.free_label and self.free_label.strip())
        if has_type == has_label:
            raise ValueError(guards.TYPE_XOR_LABEL)
        return self


class PersonnelDocumentUpdate(BaseModel):
    """Kısmi güncelleme (spec §3). `type_id`/`free_label` DEĞİŞMEZ — belgenin

    KİMLİĞİ (hangi tip/etiket) sabittir; yanlış tiple açılan kayıt silinip yeniden
    açılır. Yalnız künye alanları ve BC bağı güncellenir. `document_id`
    gönderildiğinde görünürlük denetimi serviste yapılır (create ile aynı IDOR
    korkuluğu); `null` göndermek bağı çözer (dosyasız takibe döner) ve meşrudur.

    `exclude_unset` ile "gönderilmedi" ≠ "null gönderildi" ayrımı korunur.
    """

    valid_until: date | None = None
    issued_at: date | None = None
    note: str | None = None
    document_id: uuid.UUID | None = None


class PersonnelDocumentResponse(BaseModel):
    """Belge kaydı + tip künyesi (JOIN'li, N+1 yok) + TÜREV durum (spec §2, §3).

    `status` ve `days_left` kolon DEĞİL, `status.derive_document_status` ile
    hesaplanır (tek kaynak). `type_name`/`is_mandatory`/`validity_months` katalog
    tipinden gelir ve serbest etiketli kayıtta None'dur.
    """

    id: uuid.UUID
    personnel_id: uuid.UUID
    type_id: uuid.UUID | None
    type_name: str | None
    is_mandatory: bool | None
    validity_months: int | None
    free_label: str | None
    document_id: uuid.UUID | None
    issued_at: date | None
    valid_until: date | None
    note: str | None
    # TÜREV alanlar (kolon değil) — `status.py` tek kaynağından.
    status: str
    days_left: int | None
    created_at: datetime
    updated_at: datetime


# --- İK-1 T4: belge takibi özeti (spec §2, §3 — BT mockup birebir) ----------


class HrDocumentTypeBreakdown(BaseModel):
    """Bir katalog tipinin BT "Belge Tipi Dağılımı" satırı.

    `valid`/`expiring`/`expired` BELGE sayısıdır (aktif+yayın personelin bu tipteki
    kayıtları); `missing` PERSONEL sayısıdır — aktif+yayın personel toplamından bu
    tipte kaydı OLAN aktif+yayın personel sayısı çıkarılır. İki farklı taban (belge
    vs personel) bilinçlidir: `missing` "kayıt YOKLUĞU" olduğundan yalnız kişi
    düzeyinde anlamlıdır. `total_documents` = valid+expiring+expired.

    Opsiyonel tip de (is_mandatory=False) dağılımda GÖSTERİLİR ama KPI `missing`
    toplamına GİRMEZ (o toplam yalnız zorunlu tipler üzerinden — spec §2/§3).
    """

    type_id: uuid.UUID
    type_name: str
    is_mandatory: bool
    validity_months: int | None
    total_documents: Annotated[int, Hassas.yok]
    valid: int
    expiring: int
    expired: int
    missing: int


class HrExpiredDocument(BaseModel):
    """ "Süresi Dolan Belgeler" listesi satırı — en çok geciken önce."""

    id: uuid.UUID
    personnel_id: uuid.UUID
    personnel_name: str
    document_label: str
    project_name: str | None
    valid_until: date
    days_overdue: int


class HrExpiringDocument(BaseModel):
    """ "30 Gün İçinde Bitecek" listesi satırı — en yakın önce."""

    id: uuid.UUID
    personnel_id: uuid.UUID
    personnel_name: str
    document_label: str
    project_name: str | None
    valid_until: date
    days_left: int


class HrDocumentsSummaryResponse(BaseModel):
    """BT özet ucu: 5 KPI + tip dağılımı + iki liste (spec §2/§3).

    Tüm sayılar AKTİF (`is_active=true`) + YAYINDA (`is_draft=false`) personelin
    dünyasını anlatır — ekran çalışan iş gücünün belge uyumunu gösterir; taslak
    (henüz yayınlanmamış) ve pasif (ayrılmış) personelin belgeleri hiçbir sayaca
    girmez (`missing` tanımıyla tutarlı). Durum türevi `status.py` tek kaynağından.
    """

    total_documents: Annotated[int, Hassas.yok]
    valid: int
    expiring: int
    expired: int
    missing: int
    by_type: list[HrDocumentTypeBreakdown]
    expired_documents: list[HrExpiredDocument]
    expiring_documents: list[HrExpiringDocument]


# --- İK-2 T2: izin talebi (spec §3, §5 K2) ---------------------------------
#
# **`extra="forbid"` BİLİNÇLİDİR ve bu dilimin çekirdek kuralıdır (spec §5 K2):**
# `days` SUNUCU hesabıdır, `status`/`decided_by`/`decided_at`/`reject_reason` ise
# T3'ün (onay/ret) alanlarıdır. Pydantic'in varsayılanı bilinmeyen alanı SESSİZCE
# YOK SAYMAKTIR — o hâlde `{"days": 99}` gönderen istemci 201 alır ve sunucunun
# hesabıyla kendi gönderdiği sayının farklı olduğunu HİÇ ÖĞRENMEZ. Emir açık:
# gönderilirse AÇIKÇA REDDEDİLİR (422).


class LeaveRequestCreate(BaseModel):
    """Yeni izin talebi. `days` ve `status` GÖNDERİLEMEZ (üstteki gerekçe).

    `status` her zaman `pending` başlar — talep açan kişi kendi talebini onaylı
    doğuramaz (K4 tek adımlı onay bunu T3'te ayrı uçla verir).

    `document_id` BC arşiv künyesine bağdır (İZ 88 "rapor ekli"); görünürlük
    denetimi SERVİSTEDİR (IDOR) — pydantic yalnız biçime bakar.
    """

    model_config = ConfigDict(extra="forbid")

    personnel_id: uuid.UUID
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    note: str | None = Field(default=None, max_length=2000)
    document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _date_order(self) -> "LeaveRequestCreate":
        if self.end_date < self.start_date:
            raise ValueError(guards.LEAVE_DATE_ORDER)
        return self


class SelfLeaveRequestCreate(BaseModel):
    """İK-2.1 — personelin KENDİ izin talebi. `LeaveRequestCreate`in `personnel_id`
    ALINMIŞ hâlidir ve fark BİLİNÇLİDİR, kopya değil.

    🔴 **`personnel_id` alanı YOKTUR ve `extra="forbid"` onu REDDEDER.** Başkasının
    adına talep açmak bu yüzden 403/404 kararı gerektiren bir YETKİ SORUSU değil,
    **yapısal olarak imkânsız** bir gövdedir: hangi personel adına yazılacağını
    sunucu aktörün `user_id` köprüsünden ÇÖZER, istemci SÖYLEYEMEZ. Cevap (422)
    hedefin var olup olmadığına göre DEĞİŞMEZ — sunucu "bu personel var" bilgisini
    hiçbir biçimde sızdırmaz (IDOR korkuluğu).

    Diğer her şey `LeaveRequestCreate` ile AYNIDIR: `days` SUNUCU hesabıdır,
    `status` her zaman `pending` başlar, `document_id` görünürlüğü SERVİSTE
    denetlenir. Alan kümesi genişletilmez — yetki genişlemesi DAR olmalıdır.
    """

    model_config = ConfigDict(extra="forbid")

    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    note: str | None = Field(default=None, max_length=2000)
    document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _date_order(self) -> "SelfLeaveRequestCreate":
        if self.end_date < self.start_date:
            raise ValueError(guards.LEAVE_DATE_ORDER)
        return self


class LeaveRequestUpdate(BaseModel):
    """Kısmi güncelleme — YALNIZ `pending` kayıtta (kural serviste, 409).

    Tarih sırası BURADA doğrulanamaz: PATCH tek uç gönderebilir (`end_date`),
    kural ancak DB'deki kayıtla BİRLEŞTİRİLMİŞ değerler üzerinde anlamlıdır —
    bu yüzden servis korkuluğundadır (`PersonnelValidationError` -> 422).
    `personnel_id` DEĞİŞTİRİLEMEZ: talebin KİMİN olduğu kimliğidir, yanlış
    personele açılan talep silinip yeniden açılır (`PersonnelDocumentUpdate`
    tip/etiket dondurma emsali).
    """

    model_config = ConfigDict(extra="forbid")

    leave_type_id: uuid.UUID | None = None
    start_date: date | None = None
    end_date: date | None = None
    note: str | None = Field(default=None, max_length=2000)
    document_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _zorunlu_alanlar_null_olamaz(self) -> "LeaveRequestUpdate":
        """AÇIK `null` reddi — "gönderilmedi" ile "null gönderildi" AYRI şeylerdir.

        Kısmi gönderim için alanlar `... | None = None` olmak zorunda, bu yüzden
        tip tek başına null'ı elemez; ayrım YALNIZ `model_fields_set`tedir. Kural
        gövdenin BİÇİMİNE bakar (DB durumuna değil) — o yüzden burada, tarih
        SIRASI kuralı ise birleştirilmiş değer istediği için serviste kalır.
        """
        for alan in guards.LEAVE_NULLABLE_OLMAYAN_ALANLAR:
            if alan in self.model_fields_set and getattr(self, alan) is None:
                raise ValueError(guards.LEAVE_FIELD_NOT_NULL)
        return self


class LeaveTypeResponse(BaseModel):
    """Katalog satırı — SALT OKUMA (spec §1: CRUD ucu AÇILMAZ, ayarlar dilimi).

    Talep formunun tip listesine ihtiyacı vardır; yazma ucu yoktur.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    deducts_from_annual: bool
    is_paid: bool
    requires_document: bool
    color: str | None
    sort_order: int


class LeaveRequestResponse(BaseModel):
    """İZ talep tablosu satırı: kayıt + personel ve tip KÜNYESİ (JOIN'li, N+1 yok).

    `personnel_name`/`personnel_trade` ve `leave_type_*` kolon DEĞİLDİR; liste
    sorgusunun JOIN'inden gelir — ekran satır başına ikinci istek atmasın.
    `deducts_from_annual` künyeye dahildir çünkü hak aşımı uyarısı (İZ 98-99)
    YALNIZ o tiplerde anlamlıdır ve istemci tipi ayrıca sorgulamak zorunda kalmaz.

    `days` TÜREV DEĞİL KOLONdur ama sunucu yazar (spec §5 K2). Karar alanları
    (`decided_*`, `reject_reason`) T2'de hep boştur; T3 doldurur.
    """

    id: uuid.UUID
    personnel_id: uuid.UUID
    personnel_name: str
    personnel_trade: str | None
    leave_type_id: uuid.UUID
    leave_type_name: str
    leave_type_color: str | None
    deducts_from_annual: bool
    start_date: date
    end_date: date
    days: int
    note: str | None
    document_id: uuid.UUID | None
    status: LeaveStatus
    decided_by: uuid.UUID | None
    decided_at: datetime | None
    reject_reason: str | None
    created_at: datetime
    updated_at: datetime


class LeaveRequestListResponse(BaseModel):
    """`PersonnelListResponse` kardeşi (TB3 sayfalama zarfı): `total` + `limit`/`offset`."""

    items: list[LeaveRequestResponse]
    total: int
    limit: int
    offset: int


# --- İK-2 T3: onay/red + bakiye (spec §2, §3, §5 K1/K4/K5) -----------------
#
# **`extra="forbid"` burada da ÇEKİRDEK KURALDIR.** Karar alanları
# (`decided_by`/`decided_at`/`status`) SUNUCU damgasıdır: istemci bir başkasının
# adına imza atamaz. Bakiye tarafında ise `annual_entitlement`/`used`/`remaining`
# KOLON DEĞİLDİR (spec §5 K1) — gönderilirlerse sessizce yok sayılmak yerine
# AÇIKÇA 422 olurlar, aksi hâlde istemci "yazdım" sanıp türevin değişmediğini
# hiç öğrenmezdi. Aynı kapı BC sızıntısını da kapatır: bu yolların hiçbiri
# `document_id` KABUL ETMEZ (K6 bağı yalnız talep POST/PATCH'inde kurulur).


class LeaveApproveRequest(BaseModel):
    """Onay gövdesi — **ALAN YOKTUR** (spec §5 K4: onay TEK adım, veri taşımaz).

    Gövde tümüyle İSTEĞE BAĞLIDIR (uç gövdesiz de çağrılabilir); ama BOŞ OLMAYAN
    bir gövde gönderilirse `extra="forbid"` onu reddeder. Şemayı büsbütün
    kaldırmak bu reddi de kaldırır ve `{"decided_by": ...}` sessizce yutulurdu.
    """

    model_config = ConfigDict(extra="forbid")


class LeaveRejectRequest(BaseModel):
    """Red gövdesi — `reason` ZORUNLU (TH emsali, spec §3).

    Red HER ZAMAN serbesttir (hak aşımı/çakışma onayı engeller ama reddi ASLA):
    İZ 98-99'da hak aşan satırın ✓ butonu pasif, ✗ butonu AKTİFtir. Bu yüzden
    burada hiçbir eşik denetimi YOKTUR; tek zorunluluk gerekçedir.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)

    @model_validator(mode="after")
    def _reason_not_blank(self) -> "LeaveRejectRequest":
        """`min_length=1` TEK BAŞINA yetmez: `"   "` onu geçer ve denetim günlüğüne
        boş bir gerekçe düşerdi."""
        if not self.reason.strip():
            raise ValueError(guards.LEAVE_REJECT_REASON_REQUIRED)
        return self


class LeaveBalanceUpdate(BaseModel):
    """Bakiye yazma gövdesi — **YALNIZ `carried_over`** (spec §1, §5 K1).

    Devreden gün İZ 137'nin "Devreden" sütunudur ve ELLE girilir (otomatik devir
    job'u İK-3). Tablodaki tek gerçek kolon budur; ötekiler türevdir ve gövdede
    kabul EDİLMEZ.
    """

    model_config = ConfigDict(extra="forbid")

    carried_over: GunSayisi = Field(ge=0, max_digits=5, decimal_places=1)


class LeaveBalanceResponse(BaseModel):
    """İZ bakiye tablosu satırı — TEK gerçek kolon + türevler (spec §2).

    `annual_entitlement`/`remaining`/`usage_pct` **None olabilir** ve bu bir veri
    eksikliği DEĞİL, kanonun kendisidir (🔴 fail-closed): kıdem 1 yılı doldurmadıysa
    ya da `hire_date` yoksa hak HESAPLANAMAZ. Ekran bunu İZ 163'teki gibi
    "Hak yok · 1 yıl dolunca hak kazanır" olarak basar — 0 basmaz.

    `seniority_years`/`seniority_months` İZ 134'ün "2 yıl 1 ay" kıdem sütunudur
    (kolon değil, `hire_date` türevi); `hire_date` NULL ise ikisi de None'dur.
    """

    personnel_id: uuid.UUID
    personnel_name: str
    year: int
    hire_date: date | None
    seniority_years: int | None
    seniority_months: int | None
    annual_entitlement: int | None
    carried_over: GunSayisi
    used: int
    remaining: GunSayisiOpsiyonel
    usage_pct: int | None


# --- İK-2 T4: izin özeti (spec §3, §4 — İZ mockup birebir) ------------------


class HrLeavesSummaryResponse(BaseModel):
    """İZ özet ucu: 5 KPI (46-50) + bakiye tablosu (122-170).

    KPI'ların İKİ AYRI zaman ekseni vardır ve bu bilinçlidir:

    * `pending_requests` / `on_leave_today` / `days_used_this_month` **BUGÜNE**
      bağlıdır — geçmiş bir yıl seçmek "bugün izinli"yi anlamsız kılardı,
    * `total_leave_debt` / `carryover_risk_personnel` / `balances` ise SEÇİLEN
      **yıla** bağlıdır (İZ 120 yıl seçici).

    🔴 `unknown_entitlement_personnel` fail-closed kanonun GÖRÜNÜR yüzüdür: hakkı
    hesaplanamayan personel (kıdem<1 ya da `hire_date` NULL, İZ 163-167) borç
    toplamına 0 olarak KARIŞMAZ; ayrı sayılır ki ekran "418 gün" derken kaç kişinin
    hesap dışı kaldığı SÖYLENSİN. Sessiz 0, veri eksiğini bilançoda saklardı.

    Tüm sayılar AKTİF + YAYINDA personelin dünyasını anlatır (İK-1 özet kanonu).
    """

    year: int
    pending_requests: int
    on_leave_today: int
    days_used_this_month: int
    total_leave_debt: GunSayisi
    carryover_risk_personnel: int
    unknown_entitlement_personnel: int
    balances: list[LeaveBalanceResponse]


# --- BOR-TEMIZ T3: belge tipi katalogu (Boşluk #4) --------------------------
#
# `PersonnelDocumentType` modeli + `repository.list_document_types` ZATEN
# VARDI; eksik olan yalnız HTTP ucuydu (`equipment/document_schemas.py`
# `EquipmentDocumentTypeResponse` emsalinin birebiri). CRUD ucu YOK — yönetimi
# ayarlar dilimine ertelenmiştir (İK-1/MK-2 kararının aynısı).


class PersonnelDocumentTypeResponse(BaseModel):
    """`GET /personnel/document-types` satırı — katalog künyesi."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    is_mandatory: bool
    validity_months: int | None
    sort_order: int
    is_active: bool


class PersonnelDocumentTypeListResponse(BaseModel):
    items: list[PersonnelDocumentTypeResponse]
