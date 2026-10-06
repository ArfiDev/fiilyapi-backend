"""KVKK korkulukları — **modül bazlı ifşa seviyesi + alan maskesi** (A1 / S5).

Bu dosya §9-A1'in kullanıcı kararını (K1, 2026-09-04) **koda** geçirir:

> `personnel` · `payroll` · `customers` · `sales` sağlayıcıya **KAPALIDIR**.
> AMA agrega istisnası vardır: bordro **dönem toplamları** ve özlük **KPI**'ları
> açılabilir — koşulu: **sıfır kişi adı** ve **sıfır S5(c) anahtarı**.

🔴 **ÖLÇÜM K1'İN BİR PREMISE'İNİ DÜZELTTİ:** kararın saydığı dört addan yalnız
**üçü** bu depoda bir izin modülüdür. `customers` `MODULES` listesinde YOKTUR
(`customers/router.py:42` kapısını `require_permission("sales", ...)` ile
kurar) — yani müşteri verisi zaten `sales` satırının altındadır. Detay ve
sonucu `AI_IFSA`daki `sales` yorumundadır.

## 🔴 NİYE ÜÇ DURUM, NİYE İKİ DEĞİL

İki durumlu bir bayrak (`açık`/`kapalı`) K1'i **ifade edemez**: karar
"`payroll` kapalıdır" DEĞİL, "`payroll`un satırları kapalı, dönem toplamları
açılabilir"dir. İki durumla yazılsaydı ya bordro tümüyle kapanır (kullanıcının
açtığı kapı kapanırdı) ya da tümüyle açılır (dolayısıyla `wage_amount`).

| Seviye | Ne demek | Yaptırım NEREDE |
|---|---|---|
| `KAPALI` | Verisi sağlayıcıya **hiç** gitmez | kayıt: araç **KAYDEDİLEMEZ** |
| `AGREGA` | Yalnız toplam/KPI; satır ve kimlik YOK | kayıt: **şema** + dispatch: **zarf** |
| `ACIK` | Kısıt yok (S5(c) yasağı yine geçerli) | — |

## Alan maskesi KVKK korkuluğunun YERİNE GEÇMEZ

Alan maskesi (`core/mask_route` + `core/field_mask`) araçların sardığı ucun yanıtını rolün
`hidden_fields` kümesine göre daraltır ve AI hattı bu maskeden GEÇER. Ama modül bazlı ifşa
seviyesi (`AI_IFSA`) maskeye değil sabit bir listeye dayanır ve öyle kalmalıdır: maske bir
YÖNETİCİ kararıdır ve rol başına değişir, KVKK kısıtı ise sabittir ve role bakmaksızın
uygulanmalıdır (`personnel`/`payroll` bunun örneğidir).

Bu yüzden burada bayrağın okunduğu yer bir liste değil, **kaydın kendisidir**:
`dogrula_spec()` `ToolRegistry.__init__` içinde koşar ve ihlalli bir araç
kaydedilemez → uygulama açılmaz. "Katalog bir LİSTELEME'dir, yaptırım
dispatch'tedir" dersinin bir adım ilerisi: **yaptırım KAYIT anındadır**, çünkü
kaydedilemeyen bir araç ne katalogda ne dispatch'te görünebilir.

⚠️ **Dürüst not (eşdeğer mutant değildir).** Kayıt anındaki şema taraması
`ToolSpec.yanit_modeli`nin **statik** alan kümesini görür. Ama ölçüldü:
`AiPuantajHaftasi.totals` `dict[str, Any]`dır ve `AiYetkilerim.sayfalar`
`dict[str, str]`dir — bu iki alanın **anahtarları şemada YOKTUR**. Yani statik
tarama bir handler'ın çalışma anında koyduğu `tc_no` anahtarını göremez. Bu
yüzden `ToolRegistry.invoke` dönen zarfı **ayrıca** tarar; iki tarama farklı
şeyleri ölçer ve biri diğerinin yerine geçmez.

## 🔴 ALT DİZİ EŞLEŞMESİ YASAK — ÖLÇÜLDÜ

896 pydantic alan adı tarandı; `sgk` alt dizisi **9** isabet verdi ve
bunlardan **7'si** tam olarak K1'in AÇMAK istediği şeydi:
`sgk_base_total` · `sgk_employee_pct` · `sgk_employee_total` ·
`sgk_employer_pct` · `sgk_employer_total` · `sgk_payable_total` ·
`sgk_premium_total`. Yani `"sgk" in ad` yazan bir maske, bordro dönem
toplamlarını **kişisel veri sanıp** kullanıcının açtığı kapıyı kapatırdı.
Aynı tuzak `wage`de de var: `wage_amount` yasak, `wage_type` (`monthly`/`daily`
enum'u) değil.

**Eşleşme TAM ANAHTAR üzerindendir.** `KASTEN_DISARIDA` bu kararın kaydıdır,
`NAIF_MASKE_KOKLERI` ise kararın **önemsiz olmadığının** ölçüsüdür: kökle
eşleşen bir maske o on anahtarın onunu da yakalardı.
"""

from __future__ import annotations

import enum
import re
import types
import typing
from collections.abc import Iterable, Mapping
from typing import Any, Final

from pydantic import BaseModel

from app.core.iban import validate_iban


class IfsaSeviyesi(str, enum.Enum):
    """Bir izin modülünün verisinin üçüncü taraf sağlayıcıya ifşa seviyesi."""

    #: Sağlayıcıya HİÇ gitmez. Bu modülün verisini taşıyan araç kaydedilemez.
    KAPALI = "kapali"
    #: Yalnız toplam/KPI. Kişi adı ve S5(c) anahtarı taşıyan araç kaydedilemez.
    AGREGA = "agrega"
    #: Modül bazlı kısıt yok. S5(c) alan yasağı **yine de** geçerlidir.
    ACIK = "acik"


#: 🔴 **VARSAYILAN YOK.** 23 izin modülünün 23'ü de burada ADIYLA geçer ve
#: `test_ai2a_korkuluklar.py::test_ifsa_haritasi_TUM_IZIN_MODULLERINI_ADIYLA_KAPSAR`
#: küme eşitliğiyle bunu kilitler (🔴 bu satır eskiden var olmayan bir
#: `test_ifsa_haritasi_TUM_MODULLERI_KAPSAR` adını gösteriyordu)
#: (sihirli sayı yasak — B1 kanonu). 23. modül açılırsa test kırmızı olur ve
#: ekleyen kişi **bilinçli bir KVKK kararı** vermek zorunda kalır; sessiz bir
#: `ACIK` varsayılanına düşemez.
#:
#: Kaynak: `app/modules/roles/seed_data.py::MODULES` (ölçüldü: 23 anahtar).
AI_IFSA: Final[Mapping[str, IfsaSeviyesi]] = {
    # --- K1: KAPALI ---------------------------------------------------------
    # 🔴 **K1 DÖRT AD SAYAR AMA BU DEPODA ÜÇ İZİN MODÜLÜ VARDIR.** Ölçüldü:
    # `roles/seed_data.py::MODULES` 22 anahtar taşır ve `"customers"` **onların
    # arasında YOKTUR** (`command grep -n '"customers"' seed_data.py` → EXIT=1).
    # `app/modules/customers/` bir dizin ve bir tablodur, bir izin modülü değil:
    # `customers/router.py:42-43` kapısını `require_permission("sales", ...)`
    # ile kurar. Yani K1'in "customers" dediği veri bu haritada `sales`
    # satırının altındadır ve KAPALIdır.
    #
    # ⚠️ Ama `customers` verisi `sales`ten SIZABİLİR: `invoicing` modülü
    # `party_tax_number` · `party_address` · `party_name` taşır (ölçüldü,
    # `invoicing/models.py:329-330`). `invoicing`i tümüyle kapatmak K1'de YOK
    # ve uydurulmadı; onun yerine alıcı kimliği taşıyan anahtarlar
    # `YASAK_ALAN_ANAHTARLARI`na kondu ve **her modülde, her araçta** yasaktır.
    # Bu, K1'in "customers KAPALI" cümlesinin bu şemadaki uygulanabilir hâlidir
    # ve raporda kullanıcı onayına sunulur.
    "sales": IfsaSeviyesi.KAPALI,
    # --- K1: AGREGA (istisna) ----------------------------------------------
    # "özlük KPI'ları açılabilir" — koşul: sıfır kişi adı + sıfır S5(c).
    "personnel": IfsaSeviyesi.AGREGA,
    # "bordro dönem toplamları açılabilir" — aynı koşul.
    "payroll": IfsaSeviyesi.AGREGA,
    # PLN-B2 (§3.11 B1-11): gunluk saat dagitimi KISI × is kodu × saat hucreleri tasir →
    # KISISEL VERI. Kural "dugum toplamlari acik, kisi duzeyi kapali" = AGREGA'nin taniminin
    # kendisi (kisi adi / S5(c) tasiyan arac kaydedilemez). B1'de ACIK'ti (kisi verisi yoktu).
    "earned_value": IfsaSeviyesi.AGREGA,
    # --- ACIK ---------------------------------------------------------------
    "accounting": IfsaSeviyesi.ACIK,
    "ai": IfsaSeviyesi.ACIK,
    "approvals": IfsaSeviyesi.ACIK,
    "boq": IfsaSeviyesi.ACIK,
    "contracts": IfsaSeviyesi.ACIK,
    "dashboard": IfsaSeviyesi.ACIK,
    # ⚠️ `ACIK` burada "belge İÇERİĞİ açıktır" DEMEK DEĞİLDİR. Belge içeriği
    # AYRI bir korkulukla (S4: `documents.origin_module` + `can_read`) ve AYRI
    # bir dilimde (AI-4) ele alınır. Bu harita **modülün** kapısıdır, o
    # korkuluğun yerine geçmez.
    "documents": IfsaSeviyesi.ACIK,
    "equipment": IfsaSeviyesi.ACIK,
    "inventory": IfsaSeviyesi.ACIK,
    "invoicing": IfsaSeviyesi.ACIK,
    "procurement": IfsaSeviyesi.ACIK,
    "progress_payments": IfsaSeviyesi.ACIK,
    "projects": IfsaSeviyesi.ACIK,
    # ⚠️ `settings` · `user_management` yönetişim denylist'indedir (S16/S17) ve
    # oraya **kapı** taşıyan araç zaten kaydedilemez. Bu satırlar o yasağın
    # yerine geçmez, onunla birlikte çalışır.
    "settings": IfsaSeviyesi.ACIK,
    "site_diary": IfsaSeviyesi.ACIK,
    "sites": IfsaSeviyesi.ACIK,
    "timesheet": IfsaSeviyesi.ACIK,
    "treasury": IfsaSeviyesi.ACIK,
    "user_management": IfsaSeviyesi.ACIK,
}


# --------------------------------------------------------------------------- #
# Alan maskesi (S5-c) — anahtar SONUCA HİÇ KONMAZ, `null` DEĞİL
# --------------------------------------------------------------------------- #

#: AI-SPEC §4.2 S5(c)'de **birebir** sayılan altı anahtar:
#: *"`tc_no/iban/sgk_no/wage_amount/address/birth_date` alan maskesi — anahtar
#: hiç bulunmaz (null DEĞİL: null 'kayıtta yok' demektir ve yalandır)"*.
S5C_ANAHTARLARI: Final[frozenset[str]] = frozenset(
    {"tc_no", "iban", "sgk_no", "wage_amount", "address", "birth_date"}
)

#: 🔴 AKRABALAR — spec'ten değil **koddan** tarandı (896 pydantic alan adı).
#: Her birinin gerekçesi ölçümdür, benzetme değil:
#:
#: * `national_id` / `customer_national_id` — TCKN'nin ta kendisi
#:   (`customers/models.py:60` yorumu: "F72 (TCKN)").
#: * `tax_number` / `customer_tax_number` — `customers`ta `national_id` ile
#:   AYNI satırda durur ve hangisinin dolu olduğu `customer_type`a bağlıdır;
#:   bir araç hangi dala düştüğünü **vaat edemez**.
#: * `tax_no` (`procurement`) / `party_tax_number` (`invoicing`) — ölçüldü,
#:   docstring birebir: *"alan TEK kolon iki kimligi tasir"*, `String(11)`
#:   yani TCKN da taşıyabilir. Tek kolon → ayrım yapılamaz → fail-closed.
#: * `party_address` / `kep_address` — `address`in kardeşleri.
#: * `phone` / `emergency_contact_phone` / `email` — doğrudan iletişim
#:   kimlikleri; `emergency_contact_phone` üstelik **üçüncü bir kişinin**
#:   verisidir (`personnel`), yani veri sahibi kullanıcı bile değildir.
#: * `ip_address` — KVKK'da kişisel veridir; hiçbir okuma aracının ona
#:   ihtiyacı yok, bulunması bir kusurdur.
AKRABA_ANAHTARLAR: Final[frozenset[str]] = frozenset(
    {
        "national_id",
        "customer_national_id",
        "tax_number",
        "customer_tax_number",
        "tax_no",
        "party_tax_number",
        "party_address",
        "kep_address",
        "phone",
        "emergency_contact_phone",
        "email",
        "ip_address",
    }
)

#: Her araçta, her derinlikte yasak. Modül seviyesinden BAĞIMSIZDIR: `ACIK` bir
#: modülün aracı da `tc_no` taşıyamaz.
YASAK_ALAN_ANAHTARLARI: Final[frozenset[str]] = S5C_ANAHTARLARI | AKRABA_ANAHTARLAR

#: 🔴 KASTEN DIŞARIDA — bu liste bir "unutulanlar" listesi DEĞİL, bir KARARDIR.
#: Hepsi `sgk`/`wage`/`iban` alt dizisini taşır ve alt dizi eşleşmesi kullansaydık
#: **hepsi** yanlışlıkla yasaklanırdı; hâlbuki yedisi tam olarak K1'in açtığı
#: "bordro dönem toplamları"dır. Bekçisi: `test_KASTEN_DISARIDA_...`.
KASTEN_DISARIDA: Final[frozenset[str]] = frozenset(
    {
        "sgk_base_total",
        "sgk_employee_pct",
        "sgk_employee_total",
        "sgk_employer_pct",
        "sgk_employer_total",
        "sgk_payable_total",
        "sgk_premium_total",
        "sgk_submitted_at",
        "subcontractor_files_own_sgk",
        "wage_type",
    }
)

#: 🔴 "Naif maske" kökleri — `KASTEN_DISARIDA`nın **niye önemsiz olmadığının**
#: ölçüsü. Bir insan alan maskesini elle yazsaydı büyük ihtimalle bu köklerle
#: yazardı; kökle eşleşen bir maske yukarıdaki **on** anahtarı da yakalar ve
#: K1'in AÇTIĞI kapıyı kapatırdı. Bekçisi `test_KASTEN_DISARIDA_...`.
NAIF_MASKE_KOKLERI: Final[tuple[str, ...]] = (
    "sgk",
    "wage",
    "iban",
    "address",
    "tax",
    "phone",
    "email",
    "birth",
    "tc_",
)

#: 🔴 GERÇEK KİŞİ ADI anahtarları — yalnız `AGREGA` modül taşıyan araçlarda
#: yasak (K1: "sıfır kişi adı"). Tüzel kişi adları (`subcontractor_name`,
#: `supplier_name`, `employer_name`, `bank_name`, `account_name`) **listede
#: DEĞİLDİR**: K1'in koşulu "bordro toplamı kimin maaşı olduğunu söylemesin"dir,
#: "hiçbir ad geçmesin" değil.
#:
#: ⚠️ Bu ayrımın karşıt kanıtı canlıda duruyor: `onay_kutum` `created_by_name`
#: taşır ve `AGREGA` bir modül BEYAN ETMEDİĞİ için geçmeye devam eder. Yasak
#: tüm araçlara uygulansaydı bugün çalışan bir araç kırılırdı — bu, kapının
#: doğru yere konduğunun ölçüsüdür.
KISI_ADI_ANAHTARLARI: Final[frozenset[str]] = frozenset(
    {
        "full_name",
        "personnel_name",
        "emergency_contact_name",
        "created_by_name",
        "decided_by_name",
        "uploaded_by_name",
        "manager_name",
        "deputy_manager_name",
        "section_manager_name",
        "site_manager_name",
        "safety_officer_name",
        "advisor_name",
        "buyer_name",
        "landowner_name",
        "shareholder_name",
        "customer_name",
        "counterparty_name",
        "party_name",
        "drawer_name",
    }
)


class IfsaIhlali(ValueError):
    """Bir `ToolSpec` KVKK korkuluğunu ihlal ediyor — araç **kaydedilemez**."""


# --------------------------------------------------------------------------- #
# Tarayıcılar
# --------------------------------------------------------------------------- #


def _ic_modeller(annotation: Any) -> Iterable[type[BaseModel]]:
    """Bir tip ifadesinin içindeki `BaseModel` alt sınıflarını verir.

    `list[X]`, `X | None`, `dict[str, X]`, `tuple[X, ...]` gibi sarmalayıcıları
    açar — iç içe modeller de taranmalıdır (K1: "iç içe modelleri de tarasın").
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        yield annotation
        return
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        for arg in typing.get_args(annotation):
            yield from _ic_modeller(arg)
        return
    for arg in typing.get_args(annotation):
        yield from _ic_modeller(arg)


def sema_anahtarlari(model: type[BaseModel]) -> set[str]:
    """Bir pydantic modelinin **tüm derinliklerdeki** alan adları.

    Döngüsel referanslar `gorulen` kümesiyle kesilir; yoksa kendine referans
    veren bir şema bu fonksiyonu sonsuz döngüye sokardı.
    """
    anahtarlar: set[str] = set()
    yigin: list[type[BaseModel]] = [model]
    gorulen: set[type[BaseModel]] = set()
    while yigin:
        su_an = yigin.pop()
        if su_an in gorulen:
            continue
        gorulen.add(su_an)
        for ad, alan in su_an.model_fields.items():
            anahtarlar.add(ad)
            yigin.extend(_ic_modeller(alan.annotation))
    return anahtarlar


def govde_anahtarlari(veri: Any) -> set[str]:
    """Çalışma anındaki gövdenin **tüm derinliklerdeki** sözlük anahtarları.

    🔴 Şema taramasının göremediği yer burasıdır: `AiPuantajHaftasi.totals`
    `dict[str, Any]`dır — anahtarları şemada YOKTUR, yalnız gövdede vardır.
    """
    anahtarlar: set[str] = set()
    yigin: list[Any] = [veri]
    while yigin:
        su_an = yigin.pop()
        if isinstance(su_an, Mapping):
            for ad, deger in su_an.items():
                if isinstance(ad, str):
                    anahtarlar.add(ad)
                yigin.append(deger)
        elif isinstance(su_an, (list, tuple, set, frozenset)):
            yigin.extend(su_an)
    return anahtarlar


def yasak_anahtarlar(veri: Any) -> list[str]:
    """Gövdede bulunan yasak anahtarlar (sıralı — hata metni deterministik)."""
    return sorted(govde_anahtarlari(veri) & YASAK_ALAN_ANAHTARLARI)


# ############################################################################ #
# DEĞER MASKESİ — anahtar değil **DEĞERİN İÇİ**
# ############################################################################ #
#
# 🔴 Yukarıdaki iki kapı da (kayıt anı ŞEMASI + çalışma anı ANAHTARI) bir şeyi
# göremez: `AiPlanGunu.text` · `AiPozKalemi.description` · `projects.name` gibi
# alanlar **serbest metindir** ve onları BAŞKA bir kullanıcı yazar. İçlerine
# konan bir TCKN/IBAN/telefon hiçbir anahtar kesişimine düşmez — `text` yasak
# bir ad değildir — ve zarf olduğu gibi üçüncü taraf sağlayıcıya gider.
#
# 🔴 **NİYE DÜŞÜRMEZ, MASKELER.** Zarfı tümden düşürmek burada yanlış araçtır:
# anahtar düzeyinde sızan bir `wage_amount` HANDLER HATASIDIR (şema onu
# taşımamalıydı), ama serbest metindeki bir numara MEŞRU bir kaydın içeriğidir.
# Düşürmek, adında sağlaması tesadüfen tutan bir numara geçen tek bir proje
# yüzünden `projeleri_listele`yi tümüyle kırardı. Maske yalnız eşleşen ALT
# DİZİYİ yutar; cevabın geri kalanı modele gider.
#
# 🔴 **NİYE SAĞLAMA DENETİMİ ZORUNLU.** "11 hane = TCKN" diyen bir maske fiş
# numarasını, sözleşme numarasını ve `String(11)` taşıyan `tax_no`yu da yutar;
# model o zaman yanlış cevap üretir. Aynı gerekçe IBAN'da mod-97'dir ve o kural
# YENİDEN YAZILMAZ: `app/core/iban.validate_iban` TEK KAYNAKTIR.
#
# ⚠️ **Kapsam dışı (bilinçli):** kişi ADI regex'lenemez ve burada aranmaz —
# K1 kararı adların gitmesine zaten izin verir (22 aracın 14'ü `name` taşır).

#: Maskelenen alt dizinin yerine konan SABİT jeton.
MASKE_JETONU: Final = "[maskelendi]"

#: TCKN ADAYI — 11 hane, ilk hane 0 olamaz. Sağlaması `_tckn_gecerli` bakar.
_TCKN_ADAYI: Final = re.compile(r"(?<![0-9])[1-9][0-9]{10}(?![0-9])")

#: TR telefon: `+90`/`0` önekli 10 hane, yaygın gruplamalar dahil. Sağlama
#: basamağı YOKTUR; önek zorunluluğu yanlış pozitifi (tutar, adet) daraltır.
_TELEFON_ADAYI: Final = re.compile(
    r"(?<![0-9+])(?:\+90|0)[ ]?\(?[0-9]{3}\)?[ ]?[0-9]{3}[ ]?[0-9]{2}[ ]?[0-9]{2}(?![0-9])"
)

#: BİTİŞİK IBAN (ISO 13616 biçimi). Doğrulama `validate_iban`ındır.
_IBAN_BITISIK: Final = re.compile(r"(?<![A-Za-z0-9])[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}(?![A-Za-z0-9])")

#: GRUPLU TR IBAN (`TR33 0006 … 26`) — kanonik 4'lü gruplama. Sabit tekrar
#: sayısı bilinçlidir: `{2,4}+` gibi esnek bir desen, IBAN'ı izleyen bir
#: BÜYÜK HARFLİ kelimeyi ya da sayıyı yutar, aday doğrulamada düşer ve IBAN
#: **maskesiz** kalırdı (sessiz yanlış negatif).
_IBAN_GRUPLU_TR: Final = re.compile(
    r"(?<![A-Za-z0-9])TR[0-9]{2}(?:[ ][0-9]{4}){5}[ ][0-9]{2}(?![0-9])"
)


def _tckn_gecerli(aday: str) -> bool:
    """T.C. Kimlik No sağlaması (10. ve 11. hane kontrol basamaklarıdır)."""
    haneler = [int(k) for k in aday]
    onuncu = ((sum(haneler[0:9:2]) * 7) - sum(haneler[1:8:2])) % 10
    return onuncu == haneler[9] and sum(haneler[:10]) % 10 == haneler[10]


def _iban_gecerli(aday: str) -> bool:
    """🔴 Kural KOPYALANMAZ: biçim + ülke uzunluğu + mod-97 tek kaynaktadır."""
    try:
        return validate_iban(aday) is not None
    except ValueError:
        return False


def _metin_maskesi(metin: str) -> str:
    """Bir dizedeki TCKN/IBAN/telefonu jetona çevirir.

    Sıra bilinçlidir: IBAN önce yutulur, yoksa 24 haneli gövdesinden bir
    telefon/TCKN adayı ayıklanmaya çalışılırdı.
    """
    sonuc = _IBAN_GRUPLU_TR.sub(
        lambda e: MASKE_JETONU if _iban_gecerli(e.group()) else e.group(), metin
    )
    sonuc = _IBAN_BITISIK.sub(
        lambda e: MASKE_JETONU if _iban_gecerli(e.group()) else e.group(), sonuc
    )
    sonuc = _TELEFON_ADAYI.sub(MASKE_JETONU, sonuc)
    return _TCKN_ADAYI.sub(lambda e: MASKE_JETONU if _tckn_gecerli(e.group()) else e.group(), sonuc)


def deger_maskesi(veri: Any) -> Any:
    """Gövdedeki **her dizenin içini** tarar; YENİ bir gövde döndürür.

    🔴 Kaynak gövde DEĞİŞTİRİLMEZ (immutability): çağıran, maskelenmiş olanı
    zarfa `dataclasses.replace` ile koyar.
    """
    if isinstance(veri, str):
        return _metin_maskesi(veri)
    if isinstance(veri, Mapping):
        return {ad: deger_maskesi(deger) for ad, deger in veri.items()}
    if isinstance(veri, list):
        return [deger_maskesi(oge) for oge in veri]
    if isinstance(veri, tuple):
        return tuple(deger_maskesi(oge) for oge in veri)
    if isinstance(veri, (set, frozenset)):
        return type(veri)(deger_maskesi(oge) for oge in veri)
    return veri


def seviye(modul: str) -> IfsaSeviyesi:
    """Modülün ifşa seviyesi. 🔴 Bilinmeyen modül **fail-closed** `KAPALI`dır.

    Harita 22 modülü ADIYLA taşır; burada bir yedeğin bulunması, haritanın
    eksik kalmasını sessizce affetmek DEĞİLDİR (küme eşitliği testi onu ayrıca
    kırmızıya çevirir) — yalnızca bir yazım hatasının **açılma** yönünde
    sonuçlanmasını imkânsız kılar.
    """
    return AI_IFSA.get(modul, IfsaSeviyesi.KAPALI)


def dogrula_spec(spec: Any) -> None:
    """Bir `ToolSpec`i kayıt anında doğrular. İhlalde `IfsaIhlali` **atar**.

    Kontroller (hepsi fail-closed):

    1. `veri_modulleri` boş olmayan bir küme değilse ve araç bir uç sarıyorsa →
       ihlal. Veri okuyan bir araç hangi modülün verisini taşıdığını **beyan
       etmek zorundadır**; türetmek ölçümle çürütüldü (aşağıda).
    2. `kapilar`daki her modül `veri_modulleri`nde de olmalı — beyanı daraltarak
       kaçış imkânsızlaşsın.
    3. `veri_modulleri`nin herhangi biri `KAPALI` → araç kaydedilemez.
    4. Yanıt şeması `YASAK_ALAN_ANAHTARLARI`ndan birini taşıyorsa → ihlal
       (modül seviyesinden bağımsız).
    5. `veri_modulleri`nin herhangi biri `AGREGA` ve yanıt şeması bir kişi adı
       anahtarı taşıyorsa → ihlal (K1'in "sıfır kişi adı" koşulu).
    6. Yönetişim denylist'i (S16/S17): `kapilar`da `user_management`,
       `settings`, `approvals` ya da `roles` varsa → ihlal. 🔴 Bu kural bugüne
       kadar **yalnız bir test dosyasında** yaşıyordu; buraya taşınması onu
       "test yazmayı unutan biri" senaryosundan çıkarır.

    🔴 **NİYE `veri_modulleri` TÜRETİLMİYOR?** Türetmenin iki adayı da ölçümle
    çürütüldü:

    * *"Ucun kapısından türet"* — `GET /dashboard/summary` yalnız
      `dashboard:view` kapısı taşır ama gövdesi `progress_payments` (portföy),
      `inventory` + `sites` (risk kartı) verisi taşır. Türeten bir sistem bu
      aracın stok ve hakediş verisi taşıdığını **göremezdi**.
    * *"`kapilar`dan türet"* — `onay_kutum`un `kapilar`ı **boştur**
      (`GET /approvals` bilinçli olarak kapısızdır). `kapilar`dan türeten bir
      sistem için o araç "hiçbir modülün verisini taşımıyor" olurdu; yani
      `personnel`i saran ve `kapilar=∅` yazan bir araç **denetimsiz geçerdi**.
      Bu tam olarak bugünkü ölçülmüş delik.
    """
    ad = getattr(spec, "ad", "<isimsiz>")
    veri_modulleri: frozenset[str] = frozenset(getattr(spec, "veri_modulleri", frozenset()))
    kapilar = {modul for modul, _ in getattr(spec, "kapilar", frozenset())}
    ucler = tuple(getattr(spec, "ucler", ()))

    if ucler and not veri_modulleri:
        raise IfsaIhlali(
            f"`{ad}` bir uç sarıyor ({ucler[0]}) ama `veri_modulleri` BOŞ. "
            "Hangi modülün verisini taşıdığı BEYAN EDİLMELİDİR — türetme "
            "ölçümle çürütüldü (`/dashboard/summary` kapısı `dashboard`, "
            "gövdesi `progress_payments` + `inventory` + `sites`)."
        )

    eksik = kapilar - veri_modulleri
    if eksik:
        raise IfsaIhlali(
            f"`{ad}` `{sorted(eksik)}` modüllerinin KAPISINI taşıyor ama "
            "`veri_modulleri`nde bildirmiyor. Beyanı daraltarak maske kaçırılamaz."
        )

    from app.modules.ai.tools.catalog import YONETISIM_DENYLIST

    yonetisim = sorted(kapilar & YONETISIM_DENYLIST)
    if yonetisim:
        raise IfsaIhlali(
            f"`{ad}` yönetişim denylist'ine KAPI taşıyor: {yonetisim}. S17: "
            "'yalnız sysadmin yazar' cümlesi, AI'ın izin matrisini yeniden "
            "yazabilmesi DEMEK DEĞİLDİR."
        )

    kapali = sorted(m for m in veri_modulleri if seviye(m) is IfsaSeviyesi.KAPALI)
    if kapali:
        raise IfsaIhlali(
            f"`{ad}` sağlayıcıya KAPALI modül verisi taşıyor: {kapali} (A1/K1). "
            "Bu araç kaydedilemez."
        )

    yanit_modeli = getattr(spec, "yanit_modeli", None)
    if yanit_modeli is None or not (
        isinstance(yanit_modeli, type) and issubclass(yanit_modeli, BaseModel)
    ):
        raise IfsaIhlali(f"`{ad}` pydantic bir `yanit_modeli` taşımıyor (B25).")

    anahtarlar = sema_anahtarlari(yanit_modeli)

    yasak = sorted(anahtarlar & YASAK_ALAN_ANAHTARLARI)
    if yasak:
        raise IfsaIhlali(
            f"`{ad}` yanıt şeması maskelenmiş alan taşıyor: {yasak} (S5-c). "
            "Anahtar sonuca HİÇ KONMAZ — `null` yazmak 'kayıtta yok' demektir "
            "ve YALANDIR."
        )

    agrega = sorted(m for m in veri_modulleri if seviye(m) is IfsaSeviyesi.AGREGA)
    if agrega:
        adlar = sorted(anahtarlar & KISI_ADI_ANAHTARLARI)
        if adlar:
            raise IfsaIhlali(
                f"`{ad}` `{agrega}` modüllerinin verisini taşıyor (AGREGA) ve "
                f"yanıt şemasında KİŞİ ADI var: {adlar}. K1'in agrega istisnası "
                "'sıfır kişi adı' koşuluna bağlıdır."
            )


__all__ = [
    "AI_IFSA",
    "AKRABA_ANAHTARLAR",
    "IfsaIhlali",
    "IfsaSeviyesi",
    "KASTEN_DISARIDA",
    "NAIF_MASKE_KOKLERI",
    "KISI_ADI_ANAHTARLARI",
    "S5C_ANAHTARLARI",
    "YASAK_ALAN_ANAHTARLARI",
    "MASKE_JETONU",
    "deger_maskesi",
    "dogrula_spec",
    "govde_anahtarlari",
    "sema_anahtarlari",
    "seviye",
    "yasak_anahtarlar",
]
