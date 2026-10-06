"""Muhasebe şemaları (MU-1 T3a) — hesap planı gövde ve yanıtları.

`treasury/schemas.py` üçlüsünün (Create/Update/Response) kardeşi.

## 🔴 `extra="forbid"` — sessiz yok sayma YOK

Gövde şemaları bilinmeyen alanı **422** ile reddeder. Gerekçe bu modülde
doğrudan MALİ'dir: `balance` TÜREVDİR (K3, `balance.py`) ve saklanan bir kolonu
yoktur; `class_code` ile `level` de KODDAN türer (K4). Pydantic'in varsayılan
davranışı gövdedeki `balance`ı SESSİZCE atmak olurdu; istemci gönderdiği
bakiyenin yazıldığını sanır, ekranda ise formülün ürettiği başka bir sayı
görürdü.

Reddedilenler: `balance` · `class_code` · `level` (türev) · `id` ·
`created_at`/`updated_at` (sunucu damgaları) · isimsiz her alan.

## Kod deseni ŞEMADA da vardır

`code` alanı `codes.ACCOUNT_CODE_PATTERN`ı DOĞRUDAN kullanır — ikinci bir regex
yazılsaydı biri gevşetilip öteki unutulur ve uç, DB CHECK'inin
(`ck_chart_of_accounts_code_format`) reddedeceği bir kodu kabul ederdi:
kullanıcı alanına özel Türkçe 422 yerine ayrımsız bir 409 alırdı. DB kısıtı SON
savunma olarak yerinde KALIR.

## AÇILMAYAN alanlar (spec §9, icat yasağı)

`parent_id` (hiyerarşi kodun içinde) · `is_contra` (`257`in parantezi bir SUNUM
kuralıdır, hiçbir form onay kutusu çizmemiştir) · sınıf etiketi (HP:187 bandı
kendi satırlarıyla çelişir — K15) · para birimi/kur · proje/şantiye/maliyet
merkezi (üç tabloda da kolonu yok, MU-3). Gövdede gönderilirlerse **422**dir.

`app.core.text.FREE_TEXT_MAX_LENGTH` T3b'nin `description` alanında devreye
girer: kolon `Text`tir (DB'de sınırsız), tavan bu yüzden ŞEMADADIR ve **TÜM
giriş noktaları** (POST + PATCH) AYNI sabitten okur — ayrışsalardı tavan bir
uçtan atlatılabilirdi (belge arşivi T4 dersi). `name`/`detail_note` bu sabite
BAĞLANMAZ: onların tavanı kolonun kendisidir (`String(200)`, `core/text.py`
kuralı).

## 🔴 T3b — yevmiye gövdesinden GELEMEYEN alanlar

`status` (`INITIAL_STATUS` sunucudadır) · `total_debit`/`total_credit`
(satırlardan TÜREtilir, `service._apply_totals` TEK yazım) · `reversal_of_id`
(storno bağını `state_service` kurar) · `period_year`/`period_month`
(`entry_date`ten türer, `ck_journal_entries_period_matches_date` zorlar) ·
`running_balance` (defterin TÜREV sütunu). Hepsi `extra="forbid"` sayesinde
**422**dir.

## 🔴 NULL fail-closed ŞEMADA BAŞLAR

`debit`/`credit` zorunlu `Decimal`dır: NULL, eksik alan ve boş metin **422**dir.
`| None` bırakılıp serviste `or 0` yazılsaydı `Σ` NULL'ı yutar ve **dengesiz fiş
dengede sayılırdı** (spec §4). Tek taraflılık da burada kapanır
(`ck_journal_lines_single_side`in şema karşılığı): `(0,0)` satırı toplama
katkısız olduğu hâlde "en az iki satır" engelini SAHTE biçimde geçirirdi.
"""

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from app.core.field_mask import Hassas
from app.core.text import FREE_TEXT_MAX_LENGTH
from app.modules.accounting import codes, guards
from app.modules.accounting.bordro_hesaplari import bordro_ek_kategori
from app.modules.accounting.models import ChartAccountType, JournalEntryStatus, JournalSourceType

__all__ = [
    "ChartAccountCreate",
    "ChartAccountListResponse",
    "ChartAccountResponse",
    "ChartAccountUpdate",
    "JournalEntryCreate",
    "JournalEntryDetailResponse",
    "JournalEntryListResponse",
    "JournalEntryResponse",
    "JournalEntryUpdate",
    "JournalLineInput",
    "JournalLineResponse",
    "JournalLinesReplace",
    "JournalSummaryResponse",
    "LedgerResponse",
    "LedgerRow",
]

#: Bilinmeyen alan = 422 (modül docstring'i). TEK sabit: her gövde şeması aynı
#: kararı taşısın, biri unutulduğunda o uçtan türev alan sızmasın.
_SIKI = ConfigDict(extra="forbid")

# Model sınırları: `chart_of_accounts.code` String(20) · `name` String(200).
# En uzun yasal kod `NNN.NN` = 6 karakterdir; 20 tavanı kolonla birebir kalır ve
# asıl kapı DESENDİR.
_CODE = Field(min_length=2, max_length=20, pattern=codes.ACCOUNT_CODE_PATTERN)
#: HP:153 `Birikmiş Amortismanlar (-)` — `(-)` ADIN parçasıdır, ayrı bir alan
#: değildir (§1c); bu yüzden ada özel bir karakter kısıtı YOKTUR.
_NAME = Field(min_length=1, max_length=200)
#: 🔑 MT-1/KK-1 — kontra bayrağı. 🔴 Gövdeye AÇILMASI ŞARTTIR: kolonu yalnız
#: modele eklemek, `257`yi kontra işaretlemenin HİÇBİR yolunu bırakmaz ve
#: bilanço netlemesi ölü kod kalırdı (MU-1 §3 "şema katmanı kör noktası"nın
#: ters yönü). Varsayılan `False`tur — hiçbir mevcut istemci gövdesi KIRILMAZ.
#: 🔴 AÇIKLAMA `(-)` SON EKİNE BAKMAZ ve bu bir ayrıntı değil, kuralın
#: KENDİSİDİR (T7 final review bulgusu): `(-)` son ekine göre işaretlemek `257`
#: dışındaki her kontra hesapta işareti TERS çevirir. `501 Ödenmemiş Sermaye
#: (-)` `equity` türündedir ve `SIGN[equity] = −1` onu ZATEN negatife çevirir;
#: kontra işaretlenirse iki kez çevrilir ve sermayeyi düşürecek yerde ARTIRIR
#: (ölçüldü: `Sermaye` 6.000 yerine 14.000 basar, `is_balanced` FALSE olur).
_IS_CONTRA = Field(
    default=False,
    description=(
        "Hesabın doğal bakiye yönü, düştüğü mali tablo kaleminin tarafının "
        "TERSİ ise işaretlenir; bakiyesi o kalemden DÜŞÜLÜR. Örnek: "
        "`257 Birikmiş Amortismanlar (-)` Pasif türdedir ama Aktif tarafta "
        "`Maddi Duran Varlıklar (net)` kalemine düşer → işaretlenir. "
        "`501 Ödenmemiş Sermaye (-)` ise Özkaynak türdedir ve Pasif tarafta "
        "kalır → İŞARETLENMEZ (borç bakiyesi zaten düşürür)."
    ),
)


# 🔴 GECE KARARI (IZN-B4b): defter/mizan/bilanço/KDV tutarları HİÇBİR tek kategoriye tam oturmaz —
# yevmiye hem maliyet/kâr hem kasa-banka (100/102) hareketini taşır. Fail-closed: ikisini birden
# etiketle (HERHANGİ biri gizliyse alan gizlenir; `tum_tutarlar` zaten ikisini de kapsar).
MaliTutar = Annotated[Decimal | None, Hassas.maliyet_kar, Hassas.banka_kasa]


def _bordro_fisi_ek_kategori(
    kaynak: JournalSourceType | None, etiketler: frozenset[Hassas]
) -> frozenset[Hassas]:
    """Bordro dönemi fişi (`payroll_period`) PERSONEL NET MAAŞ toplamını taşır: tutar alanları
    `maas_kisisel` ile DE gizlenir (IZN-B4b onarımı). Yalın yol: kaynak tipini yanıta taşı +
    satır başına `KATEGORI_COZ` (`field_mask`); yeni şema ya da ikinci uç gerekmez."""
    if kaynak is JournalSourceType.payroll_period:
        return etiketler | {Hassas.maas_kisisel}
    return etiketler


#: İSTEK gövdesinde aynı etiket (gizli rol dolu tutar gönderirse 403); tip `None` taşımaz.
MaliTutarGirdi = Annotated[Decimal, Hassas.maliyet_kar, Hassas.banka_kasa]


class ChartAccountCreate(BaseModel):
    """`POST /chart-of-accounts` (HP:50 `+ Hesap Ekle`).

    🔴 K-Ş1: form mockup'ı YOKTUR ve alan İCAT EDİLMEZ — gövde yalnızca HP:58-62'nin
    çizili sütunlarından türer: `Kod` · `Hesap Adı` · `Tür` · `Durum`.
    (`Bakiye` sütunu türevdir, gövdeye giremez.)

    `is_active` gövdeden gelebilir çünkü kullanımdan kaldırma yolu odur (repo
    kanonu: DELETE değil `is_active=false`).

    Ebeveyn kaydı ZORUNLU DEĞİLDİR: `NNN.NN` alt hesap `e5f6a7b8c9d0`
    tohumunda hiç YAZILMAZ (K2) ve kullanıcı doğrudan `120.01` girebilir; ayrıca
    `120` gibi tohumlanmış bir ana hesabı silmiş ya da hiç migrate etmemiş
    olabilir (R14). Zorunlu kılınsaydı hiçbir mockup'ın istemediği bir giriş
    sırası dayatılırdı.
    """

    model_config = _SIKI

    code: str = _CODE
    name: str = _NAME
    account_type: ChartAccountType
    is_active: bool = True
    is_contra: bool = _IS_CONTRA


class ChartAccountUpdate(BaseModel):
    """`PATCH /chart-of-accounts/{id}` — KISMİ gövde.

    Her alan `| None`dır ama bu yalnızca "gönderilmedi" demektir: dört kolonun
    hiçbiri NULLABLE değildir, dolayısıyla açıkça `null` göndermek bir TEMİZLEME
    değildir ve servis onu "değişmedi" sayar (`exclude_unset` + `is not None`).

    🔴 `code` değişimi yalnız hiç fiş satırı olmayan hesapta serbesttir
    (`guards.ACCOUNT_CODE_LOCKED`, 409): satırlar `account_id` ile bağlıdır ama
    defter ve mizan KODU basar.
    """

    model_config = _SIKI

    code: str | None = Field(
        default=None, min_length=2, max_length=20, pattern=codes.ACCOUNT_CODE_PATTERN
    )
    name: str | None = Field(default=None, min_length=1, max_length=200)
    account_type: ChartAccountType | None = None
    is_active: bool | None = None
    is_contra: bool | None = None


class _ChartAccountStored(BaseModel):
    """Yalnız SAKLANAN kolonlar — türev alan taşımaz."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    name: str
    account_type: ChartAccountType
    is_active: bool
    is_contra: bool
    created_at: datetime
    updated_at: datetime


class ChartAccountResponse(_ChartAccountStored):
    """Hesap satırı + ÜÇ TÜREV alan (HP:58-62 tablosunun tamamı).

    * `balance` — 🔴 K3, saklanan bir kolon DEĞİLDİR ve buraya `balance.py`nin
      TEK kaynağından gelir; ikinci bir formül yazılsaydı liste ile detay aynı
      hesap için farklı sayı basar ve hiçbir kolon farkı ele vermezdi.
    * `class_code` — 🔴 K15, KODUN ilk hanesi. HP:187 bandı `SINIF 5` yazıp
      altına `600`/`730`/`760` dizer; **satırlar kazanır**, bant etiketi bir
      sunucu alanı DEĞİLDİR.
    * `level` — grup `1` · ana hesap `2` · alt hesap `3`. Sınıf sayılmaz (kayıt
      değildir). İstemci girintiyi bundan kurar; sunucu HTML girintisi ya da
      `parent_id` göndermez.

    🔴 `is_active` (HP:62 `Durum`) ile `account_type` (HP:60 `Tür`) AYRI
    ŞEYLERDİR — ikisi de Türkçe'de "aktif" okunur ama biri boolean bir kaldırma
    bayrağı, öteki dört üyeli kapalı bir enum'dur (R3).
    """

    balance: MaliTutar
    class_code: str
    level: int

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "ChartAccountResponse", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        """IZN-B5a (21a): yalniz bordro beslenen hesabin bakiyesi `maas_kisisel` ile de gizlenir."""
        return bordro_ek_kategori([model.code], etiketler)

    @classmethod
    def from_row(cls, account, balance: Decimal) -> "ChartAccountResponse":  # noqa: ANN001
        """Satır + bakiye ikilisini TEK yerde birleştirir.

        Dört uç (liste · detay · POST · PATCH) da buradan geçer: ayrı ayrı
        kurulsalardı biri `class_code`u koddan değil bant etiketinden türetebilir
        ve fark yalnız `600`/`730` satırlarında ortaya çıkardı.
        """
        return cls(
            **_ChartAccountStored.model_validate(account).model_dump(),
            balance=balance,
            class_code=codes.class_code(account.code),
            level=codes.level(account.code),
        )


class ChartAccountListResponse(BaseModel):
    """K7 liste zarfı: `items` + `total` + `limit`/`offset` (repo kanonu)."""

    items: list[ChartAccountResponse]
    total: int
    limit: int
    offset: int


# --------------------------------------------------------------------------- #
# T3b — YEVMİYE (E8)
# --------------------------------------------------------------------------- #

#: Para alanlarının ortak sınırı — `Numeric(18,2)` ile BİREBİR. Şemada da
#: durması bilinçlidir: üç haneli kuruş gönderen bir istemci sessizce
#: yuvarlanmak yerine 422 alır ve neyin kabul edildiğini öğrenir.
_MONEY = Field(ge=0, max_digits=18, decimal_places=2)

#: E8:113'ün ALT satırı — `invoice_lines.detail_note` ile aynı ad/rol/ölçü.
#: Tavanı kolonun kendisidir (`String(200)`), `FREE_TEXT_MAX_LENGTH`e BAĞLANMAZ.
_DETAIL_NOTE_MAX = 200


class JournalLineInput(BaseModel):
    """Fişin bir bacağı (E8:102-105) — 🔴 K-Ş1: alan İCAT EDİLMEDİ.

    Gövde yalnızca E8'in ÇİZİLİ sütunlarından türer: `Hesap Kodu` (kimlik olarak
    `account_id`) · `Borç` · `Alacak`. Satırda `description` ve `sort_order`
    YOKTUR: ilki bir fişin iki bacağında tekrarlanır ve ayrışırdı (spec §3c),
    ikincisi gövde DİZİSİNİN İNDEKSİDİR ve sunucu yazar.

    🔴 `debit`/`credit` ZORUNLUDUR (`| None` DEĞİL): NULL/eksik/boş **422**dir.
    """

    model_config = _SIKI

    account_id: uuid.UUID
    debit: MaliTutarGirdi = _MONEY
    credit: MaliTutarGirdi = _MONEY

    @model_validator(mode="after")
    def _tek_taraf(self) -> "JournalLineInput":
        """🔴 `ck_journal_lines_single_side`in ŞEMA karşılığı.

        E8'in altı satırının boş tarafı HEP `—`dir. Burada yakalanmasaydı:
        * `(0,0)` satırı toplama katkısız olduğu hâlde satır SAYISINI şişirir ve
          "en az iki satır" engelini SAHTE biçimde geçirirdi;
        * çift dolu satır DB CHECK'ine düşer, kullanıcıya ayrımsız bir 409
          giderdi.
        DB kısıtı SON savunma olarak yerinde KALIR.
        """
        dolu_borc = self.debit > 0
        dolu_alacak = self.credit > 0
        if dolu_borc == dolu_alacak:
            raise ValueError(guards.LINE_SINGLE_SIDE)
        return self


class JournalEntryCreate(BaseModel):
    """`POST /journal-entries` (E8:67 `+ Yevmiye Kaydı`).

    🔴 K-Ş1: form mockup'ı YOKTUR ve alan İCAT EDİLMEZ — gövde yalnızca E8'in
    çizili sütunlarından türer: `Tarih` (E8:101) · `Açıklama` üst satırı
    (E8:103/113) · `Açıklama` alt satırı (`detail_note`) · satırlar.

    `detail_note` bir FK DEĞİLDİR: E8'in altı örneğinden biri
    (`48 personel · SGK dahil`) hiçbir varlığa çözülmez — heterojen küme =
    SERBEST METİN. FK açılsaydı MU-3'ün (entegrasyon) işi buraya sızardı.
    """

    model_config = _SIKI

    entry_date: date
    description: str = Field(min_length=1, max_length=FREE_TEXT_MAX_LENGTH)
    detail_note: str | None = Field(default=None, max_length=_DETAIL_NOTE_MAX)
    lines: list[JournalLineInput] = Field(default_factory=list)


class JournalEntryUpdate(BaseModel):
    """`PATCH /journal-entries/{id}` — KISMİ gövde, yalnız `draft`ta (409 aksi).

    `entry_date`/`description` için `| None` yalnızca "gönderilmedi" demektir:
    kolonları NULLABLE DEĞİLDİR, dolayısıyla açıkça `null` göndermek bir
    TEMİZLEME değildir ve servis onu "değişmedi" sayar.

    🔴 `detail_note` bunun İSTİSNASIDIR ve kolonu NULLABLE'dır: açıkça `null`
    göndermek onu GERÇEKTEN temizler. Ayrım `exclude_unset` ile korunur —
    onsuz, yalnız açıklamayı düzelten bir istek dayanağı sessizce silerdi.

    Satırlar buradan DEĞİŞMEZ: kümenin tek yazma yolu `PUT …/lines`tir, çünkü
    toplamlar (K1) ancak kümenin TAMAMI bilinirken tutarlı yazılabilir.
    """

    model_config = _SIKI

    entry_date: date | None = None
    description: str | None = Field(default=None, min_length=1, max_length=FREE_TEXT_MAX_LENGTH)
    detail_note: str | None = Field(default=None, max_length=_DETAIL_NOTE_MAX)


class JournalLinesReplace(BaseModel):
    """`PUT /journal-entries/{id}/lines` — kümeyi TOPTAN yazar (hakediş emsali).

    Kısmi güncelleme YOKTUR: `sort_order` dizinin indeksidir ve toplamlar
    kümenin tamamından türer; tek satır güncellenseydi başlık ile satırlar
    ayrışabilirdi.
    """

    model_config = _SIKI

    lines: list[JournalLineInput]


class JournalLineResponse(BaseModel):
    """Yanıt satırı — hesabın KODU ve ADI da taşınır (E8:102).

    İstemcinin hesap planını ayrıca çekmesi gerekmez; N+1'i sunucuya taşımak
    yerine tek `join`de çözülür.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    sort_order: int
    account_id: uuid.UUID
    account_code: str
    account_name: str
    debit: MaliTutar
    credit: MaliTutar
    #: Fişi doğuran belge ailesi (eklemeli; elle fişte `null`): bordro kaynaklı satırın tutarı
    #: `maas_kisisel` ile de gizlenir (`KATEGORI_COZ`).
    source_type: JournalSourceType | None = None

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "JournalLineResponse", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        # IZN-B5a (21a): kaynak tipine EK olarak bordro beslenen HESAP da (elle fis dahil).
        return bordro_ek_kategori(
            [model.account_code], _bordro_fisi_ek_kategori(model.source_type, etiketler)
        )


class JournalEntryResponse(BaseModel):
    """Fiş başlığı — SAKLANAN kolonlar (toplamlar dahil, spec §4 istisnası)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    # 🔑 FIS-NO — `YEV-2026-0214`. YALNIZ yanittadir: `JournalEntryCreate`/
    # `JournalEntryUpdate` gövdesine EKLENMEZ (ikisi de `extra="forbid"` taşır
    # ve istemciden gelen bir numara 422 olur). Bu şekil üç yanıtı birden
    # besler — yalnız detaya eklenseydi liste ekranı numarayı hiç göremez ve
    # fiş seçimi yine UUID'ye düşerdi.
    entry_no: str
    entry_date: date
    period_year: int
    period_month: int
    description: str
    detail_note: str | None
    status: JournalEntryStatus
    total_debit: MaliTutar
    total_credit: MaliTutar
    reversal_of_id: uuid.UUID | None
    created_by_id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    #: Fişi doğuran belge ailesi (eklemeli; elle fişte `null`) — bkz. `JournalLineResponse`.
    source_type: JournalSourceType | None = None

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "JournalEntryResponse", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return _bordro_fisi_ek_kategori(model.source_type, etiketler)


class JournalEntryDetailResponse(JournalEntryResponse):
    """Başlık + bacaklar. Yedi ucun HEPSİ bu şekli döner (liste hariç): tek
    yerde kurulmasaydı `POST` ile `PATCH` farklı alan kümeleri basardı."""

    lines: list[JournalLineResponse]


class JournalEntryListResponse(BaseModel):
    """K7 liste zarfı. `items` BAŞLIKTIR, satır taşımaz: liste ekranı fiş
    seçmek içindir ve her fişin bacaklarını çekmek listeyi N+1'e sokardı."""

    items: list[JournalEntryResponse]
    total: int
    limit: int
    offset: int


class JournalSummaryResponse(BaseModel):
    """E8:79-88 KPI şeridi — ÜÇ kart.

    🔴 `net_balance = ALACAK − BORÇ`. Yön mockup'tan KANITLIDIR:
    `4.120.000 − 3.842.600 = 277.400` (E8:88) tam tutar ve bu, E8'deki tek
    göstermelik-olmayan aritmetiktir.

    Şerit yalnız DÖNEME bağlıdır; hesap süzgeci ALMAZ (E8:72 — KPI'lar tablonun
    ve filtre çubuğunun DIŞINDADIR).
    """

    year: int
    month: int
    total_debit: MaliTutar
    total_credit: MaliTutar
    net_balance: MaliTutar


class LedgerRow(BaseModel):
    """`GET /journal` satırı (E8:101-106) — 🔴 tablo SATIR bazlıdır, fiş bazlı
    değil.

    `running_balance` TÜREVDİR ve gövdeden GELEMEZ; tanımı `ledger.py`dedir.
    `entry_id` + `entry_status` fişe dönüş yolunu açar: defterdeki bir satırdan
    kaynağına gidilemeseydi kullanıcı düzeltmeyi hiç bulamazdı.
    """

    entry_id: uuid.UUID
    entry_date: date
    entry_status: JournalEntryStatus
    account_id: uuid.UUID
    account_code: str
    account_name: str
    description: str
    detail_note: str | None
    debit: MaliTutar
    credit: MaliTutar
    running_balance: MaliTutar
    #: Fişi doğuran belge ailesi (eklemeli; elle fişte `null`) — bordro satırında tutar VE koşan
    #: bakiye `maas_kisisel` ile de gizlenir (bkz. `JournalLineResponse`).
    source_type: JournalSourceType | None = None

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "LedgerRow", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        # IZN-B5a (21a): kaynak tipine EK olarak bordro beslenen HESAP da (elle fis dahil).
        return bordro_ek_kategori(
            [model.account_code], _bordro_fisi_ek_kategori(model.source_type, etiketler)
        )


class LedgerResponse(BaseModel):
    """K7 zarfı + 🔴 **`carried_balance`** (pencere ÖNCESİ toplam).

    Ad `bank_accounts.opening_balance`tan bilinçli olarak AYRIDIR: orası
    SAKLANAN bir kolondur, bu ise TÜREVDİR. Aynı ad kullanılsaydı frontend
    ikisini ayırt edemez ve türetilmiş bir sayıyı düzenlenebilir sanırdı.
    """

    items: list[LedgerRow]
    total: int
    limit: int
    offset: int
    carried_balance: MaliTutar

    #: IZN-B5a (21a): `account_id` suzgeci bordro beslenen bir hesabi gosteriyorsa devreden bakiye
    #: o hesabin birikimidir. YANIT ALANI DEGILDIR (OpenAPI degismez); `ledger.py` doldurur.
    _bordro_hesabi: bool = PrivateAttr(default=False)

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "LedgerResponse", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return etiketler | {Hassas.maas_kisisel} if model._bordro_hesabi else etiketler
