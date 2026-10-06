"""Muhasebe RAPOR yanıt şemaları (MU-2 T4 mizan · T5 KDV beyanı).

`schemas.py` 415 satırdır ve BÜYÜTÜLMEZ (800 tavanı, MU-1 kanonu); rapor
şemaları — dönem şemalarının `periods_schemas.py`de durması gibi — kendi
dosyasında toplanır. T5'in `/vat-return` yanıtı da BURAYA gelir: iki rapor da
yevmiyeden TÜRETİLİR, hiçbiri saklanmaz ve ikisi de aynı `Decimal` sözleşmesini
paylaşır.

🔴 **İSTEK GÖVDESİ YOKTUR.** Mizan bir OKUMA ucudur; dönem seçimi sorgu
parametresidir (`year`/`month`), gövde değil.

## 🔴 Altı para alanının hiçbiri HESAPTA `None` olmaz

(IZN-B4b: yanıtta yalnız rol bu tutarları GİZLİYSE `null` döner — `MaliTutar`.)
Boş taraf **`0`** basar. Mockup'ın `—` işareti (satır 84, 88, …) bir SUNUM
kararıdır ve frontend'e aittir: `null` dönseydi ekranın her aritmetiği ve
tfoot'un GENEL TOPLAM satırı `null` yayardı. `Decimal`dir; kayan nokta hiçbir
aşamada devreye girmez.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, PrivateAttr

from app.core.field_mask import Hassas
from app.modules.accounting.bordro_hesaplari import bordro_ek_kategori
from app.modules.accounting.schemas import MaliTutar

__all__ = [
    "BalanceSheetLine",
    "BalanceSheetResponse",
    "BalanceSheetSection",
    "BalanceSheetSide",
    "CashFlowStatementLine",
    "CashFlowStatementResponse",
    "CashFlowStatementSection",
    "IncomeStatementLine",
    "IncomeStatementResponse",
    "IncomeStatementSection",
    "MonthlyCashPoint",
    "TrialBalanceResponse",
    "TrialBalanceRow",
    "TrialBalanceTotals",
    "VatDeductionRow",
    "VatReturnResponse",
    "VatTaxableRow",
]


def _satir_kodlari(satirlar):  # noqa: ANN001, ANN202
    return [kod for satir in satirlar for kod in satir.account_codes]


def _bolum_kodlari(bolumler):  # noqa: ANN001, ANN202
    return [kod for bolum in bolumler for kod in _satir_kodlari(bolum.lines)]


class TrialBalanceTotals(BaseModel):
    """tfoot `GENEL TOPLAM` (mockup satır 161-171) — altı kolonun AYRI toplamı.

    🔴 K15: mockup'ın tfoot RAKAMLARI kendi satırlarıyla çelişir (göstermelik);
    SATIRLAR kazanır, tfoot'tan yalnız YAPI alınır — yani "altı ayrı toplam +
    iki kapanış toplamının eşit basıldığı denge iddiası".

    Toplam TÜM kümeyi kapsar; sayfalama olsaydı bu sayı bir SAYFANIN toplamı
    olurdu ve `GENEL TOPLAM` adı yalan söylerdi (bkz. `TrialBalanceResponse`).
    """

    opening_debit: MaliTutar
    opening_credit: MaliTutar
    period_debit: MaliTutar
    period_credit: MaliTutar
    closing_debit: MaliTutar
    closing_credit: MaliTutar

    #: IZN-B5a (21a): toplam, bordro beslenen (`335`/`361`/`730`) bir satiri ICERIYOR mu?
    #: Servis doldurur. YANIT ALANI DEGILDIR (OpenAPI degismez): `model_copy` ozel ozniteligi
    #: tasir, `KATEGORI_COZ` onu okur. Icerirse genel toplam `maas_kisisel` ile de gizlenir
    #: (aksi hâlde "toplam − gorunen satirlar" bordroyu verirdi).
    _bordro_dahil: bool = PrivateAttr(default=False)

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "TrialBalanceTotals", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return etiketler | {Hassas.maas_kisisel} if model._bordro_dahil else etiketler


class TrialBalanceRow(BaseModel):
    """Mizanın bir hesap satırı — mockup'ın 8 kolonu birebir (satır 63-77).

    Üç grup AYNI ŞEY DEĞİLDİR ve bu ayrım şemada da görünür:

    | Grup | Nicelik | Kaç taraf dolu |
    |---|---|---|
    | `opening_*` | **NET** | en fazla BİRİ |
    | `period_*`  | **BRÜT** (`Σdebit` ve `Σcredit` ayrı ayrı) | **İKİSİ BİRDEN** |
    | `closing_*` | **NET** | en fazla BİRİ |

    Mockup satır 85-86 (Kasa dönem `2.640.000` **ve** `2.535.200`) brütlüğün
    kanıtıdır; satır 87-88 (kapanış `284.800` / `—`) netliğin.
    """

    account_id: uuid.UUID
    account_code: str
    account_name: str
    opening_debit: MaliTutar
    opening_credit: MaliTutar
    period_debit: MaliTutar
    period_credit: MaliTutar
    closing_debit: MaliTutar
    closing_credit: MaliTutar

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "TrialBalanceRow", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori([model.account_code], etiketler)


class TrialBalanceResponse(BaseModel):
    """Mizanın tamamı — 🔴 **K7 SAYFALAMA ZARFI YOKTUR** (bilinçli sapma).

    Gerekçe: `totals` GENEL TOPLAMDIR ve `is_balanced` onun üzerinden kurulur;
    sayfalanmış bir mizanda ikisi de anlamsızlaşır (2. sayfanın "toplam borç =
    toplam alacak" iddiası hiçbir şey ifade etmez). Küme SINIRLIDIR: tekdüzen
    hesap planı ~200 satırdır ve `include_empty=false` hareketsizleri zaten
    eler. `items`/`total`/`limit`/`offset` yerine `rows`/`totals` adları tam da
    bu farkı görünür kılmak için seçilmiştir.

    `year`/`month` yanıtta TEKRARLANIR: mockup satır 45 (`Ocak–Temmuz 2026`)
    başlığı buradan kurulur ve istemci hangi dönemi gördüğünü kendi isteğinden
    değil SUNUCUNUN cevabından okur.

    `is_balanced` = `totals.closing_debit == totals.closing_credit` (mockup
    satır 54-57 kontrol banner'ı).
    """

    year: int
    month: int
    is_balanced: bool
    rows: list[TrialBalanceRow]
    totals: TrialBalanceTotals


class VatTaxableRow(BaseModel):
    """`Tablo 1 — Matrah ve Vergi`nin bir ORAN satırı (mockup satır 84-89).

    🔴 `rate = 0` satırları BURAYA GİRMEZ: mockup istisnayı ayrı, italik/gri bir
    satır olarak çizer (satır 90-95) ve vergisi tanımı gereği `0`dır. Listeye
    konsaydı `Vergi` kolonu hep `0` olan sahte bir "oran" satırı doğardı.
    `VatReturnResponse.exempt_base` onun yeridir.
    """

    rate: Annotated[Decimal, Hassas.yok]  # KDV ORANI (yüzde): tutar değil
    base: MaliTutar
    vat: MaliTutar


class VatDeductionRow(BaseModel):
    """`İndirimler` tablosunun bir satırı (mockup satır 116-125).

    🔴 Mockup İKİ satır çizer (`Mal Alışları` / `Hizmet Alımları`) ama bu ayrımın
    veri modelinde karşılığı YOKTUR (ölçüldü: `item_type`/`is_service`/
    `product_type` sıfır eşleşme, kalemin stok bağı yok). Sınıflandırıcı
    UYDURULMADI; tek satır `Alışlar` döner ve boşluk açık borçtur. Liste tipi
    olması, sınıflandırma bir gün gerçekten modellendiğinde şemanın KIRILMADAN
    büyümesi içindir.
    """

    source: str
    base: MaliTutar
    vat: MaliTutar


class VatReturnResponse(BaseModel):
    """KDV Beyannamesinin tamamı — 🔴 sayfalama YOKTUR (mizanla aynı gerekçe).

    `year`/`month` yanıtta TEKRARLANIR: istemci hangi dönemi gördüğünü kendi
    isteğinden değil SUNUCUNUN cevabından okur (mockup satır 45 başlığı).

    🔴 **`payable` ve `carried_forward` AYNI ANDA sıfırdan büyük OLAMAZ.**
    `fark = calculated_vat − deductible_vat`; `payable = max(fark, 0)` ve
    `carried_forward = max(−fark, 0)`. Negatif fark "ödenecek" DEĞİL DEVREDEN
    KDV'dir — tek alan açılıp negatif basılsaydı ekran devlete borç yerine
    alacak yazardı. Mockup yalnız `Ödenecek` çizer (satır 65-69, 134-143); alan
    yine de açılır, sunum kararı frontend'indir.

    Para alanlarının hiçbiri `None` OLMAZ; boş dönem her yeri `0` basar ve
    `due_date` YİNE doludur (vade fatura verisine değil TAKVİME bağlıdır).
    """

    year: int
    month: int
    due_date: date
    calculated_vat: MaliTutar
    deductible_vat: MaliTutar
    payable: MaliTutar
    carried_forward: MaliTutar
    taxable_rows: list[VatTaxableRow]
    exempt_base: MaliTutar
    deductions: list[VatDeductionRow]


# --------------------------------------------------------------------------- #
# MT-1 T4 — Bilanço (mockup `Mali Tablo - Bilanço.dc.html`)
# --------------------------------------------------------------------------- #


class BalanceSheetLine(BaseModel):
    """Bilançonun bir KALEMİ — mockup'ın tek bir satırı (ör. BL:51).

    `amount` **YUVARLANMAZ** (MT-K2): `Numeric(18,2)` kuruşuyla döner ve
    yuvarlama bir GÖSTERİM kararıdır. Uç yuvarlasaydı ara toplamlar
    bileşenlerinden 1 TL sapar ve `is_balanced` sahte biçimde `False` çıkardı.

    İşaret: kalem, ait olduğu tarafta POZİTİF basar (mockup'ın 13 satırının
    hepsi pozitiftir). `320` Satıcılar `2.184.000` gösterir, `−2.184.000`
    değil — ham `net` `SIGN[tür]` ile çevrilir. **Kontra hesap ise DÜŞÜLÜR**
    (`is_contra`, MT-K1: BL:57 `Maddi Duran Varlıklar (net)`). Bir kalem yine de
    NEGATİF çıkabilir: geçmiş yıl zararı ya da dönem zararı gerçek bir sonuçtur
    ve `0`a kırpılsaydı `AKTİF ≠ PASİF` olurdu.

    🔴 `account_codes` / `group_codes` mockup'ta BASILMIYOR ama yine de döner:
    bir kalemin İÇİNE bakmanın (drill-down) tek yolu budur ve asıl işlevi
    "Diğer …" kalemlerini ŞEFFAF kılmaktır — haritaya girmeyen bir hesabın
    NEREYE düştüğü kullanıcıya ancak buradan görünür. Alan açmak ucuzdur,
    sonradan eklemek kırıcıdır. Frontend basmayabilir.
    """

    key: str
    label: str
    amount: MaliTutar
    account_codes: list[str]
    group_codes: list[str]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "BalanceSheetLine", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori(model.account_codes, etiketler)


class BalanceSheetSection(BaseModel):
    """Bölüm bandı + kalemleri + ara toplam (mockup BL:50-55 kalıbı).

    `subtotal` kalemlerinden HESAPLANIR, mockup'tan kopyalanmaz (K15: mockup'ın
    toplamları satırlarıyla çelişebilir ve o bir SUNUM göstermeliğidir).
    """

    key: str
    title: str
    subtotal_label: str
    subtotal: MaliTutar
    lines: list[BalanceSheetLine]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "BalanceSheetSection", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        # Ara toplam, bordro iceren bir kalemi KAPSIYORSA gizlenir (cikarma yoluyla turetme).
        return bordro_ek_kategori(
            (kod for kalem in model.lines for kod in kalem.account_codes), etiketler
        )


class BalanceSheetSide(BaseModel):
    """Bilançonun bir TARAFI — AKTİF (BL:44-63) ya da PASİF (BL:66-88).

    İki taraf ayrı nesnelerdir çünkü mockup onları AYRI KARTLARDA çizer (BL:42
    iki sütunlu ızgara) ve `total` her tarafın kendi genel toplamıdır.
    """

    key: str
    title: str
    total_label: str
    total: MaliTutar
    sections: list[BalanceSheetSection]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "BalanceSheetSide", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori(
            (
                kod
                for bolum in model.sections
                for kalem in bolum.lines
                for kod in kalem.account_codes
            ),
            etiketler,
        )


class BalanceSheetResponse(BaseModel):
    """Bilançonun tamamı — 🔴 **K7 SAYFALAMA ZARFI YOKTUR** (mizan/KDV emsali).

    Gerekçe: `total` GENEL TOPLAMDIR ve `is_balanced` onun üzerinden kurulur;
    sayfalanmış bir bilançoda ikisi de anlamsızlaşır. Küme zaten SABİTTİR: 13
    kalem, iki taraf.

    `as_of` yanıtta TEKRARLANIR: mockup BL:37 seçicisinin başlığı buradan
    kurulur ve istemci hangi ANI gördüğünü kendi isteğinden değil SUNUCUNUN
    cevabından okur.

    🔴 **`is_balanced` ÖLÇÜLÜR, `True` VARSAYILMAZ.** Gerekçe TB6 T2'de
    DEĞİŞTİ, sonuç DEĞİŞMEDİ: eski `ck_journal_entries_posted_balanced` yalnız
    `posted`ı bağlıyordu ve dengesiz bir `reversed` fiş DB'ye girebiliyordu —
    **o borç KAPANDI** (`ck_journal_entries_posting_balanced` deftere girenlerin
    HEPSİNİ bağlar). Ama kısıt **BAŞLIK** toplamlarını bağlar, bilanço ise
    `journal_lines`ı toplar — başlığı dengeli, satırları dengesiz bir fiş HÂLÂ
    kurulabilir. Gösterge ayrıca `is_contra` veri hatalarını da yakalar: kontra
    işaretlenmemiş bir `257` iki katı tutar kaydırır ve burada görünür.

    🔴 **Dönem kilidi rozeti YOKTUR** (MT-K8): bilanço salt-okumadır, kapalı
    dönemin bilançosu ile açığınki arasında fark yoktur ve mockup rozet
    çizmemiştir. **Karşılaştırma (önceki dönem) sütunu da YOKTUR** (MT-K6):
    mockup tabloları 2 sütunludur, BL:37'deki `31 Aralık 2025` seçeneği bir
    karşılaştırma sütunu DEĞİL ayrı bir sorgudur.
    """

    as_of: date
    is_balanced: bool
    assets: BalanceSheetSide
    liabilities: BalanceSheetSide


# --------------------------------------------------------------------------- #
# MT-1 T5 — Nakit Akış Tablosu (mockup `Mali Tablo - Nakit Akışı.dc.html`)
# --------------------------------------------------------------------------- #


class CashFlowStatementLine(BaseModel):
    """Nakit akışının bir KALEMİ (ör. NA:71 `Müşterilerden Tahsilat`).

    🔴 `amount` **İŞARETLİDİR**: giriş `+`, çıkış `−` (mockup NA:71-75 `+`/`−`
    önekleri). Mutlak değer basıp yönü etikete gömen bir uç, `Ekipman Alımı`
    satırında bir ekipman SATIŞINI ayırt edemezdi — mockup B bölümünde TEK
    kalem çizer ve satış da oraya düşer (K15: kalem sayısı bağlayıcı).

    `account_codes` mockup'ta basılmıyor ama döner: bir kalemin İÇİNE bakmanın
    tek yolu budur ve `Diğer Nakit Çıkışları` kovasını ŞEFFAF kılar.
    """

    key: str
    label: str
    amount: MaliTutar
    account_codes: list[str]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "CashFlowStatementLine", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori(model.account_codes, etiketler)


class CashFlowStatementSection(BaseModel):
    """`A`/`B`/`C` bölümü + kalemleri + ara toplam (mockup NA:68-79 kalıbı).

    `subtotal` kalemlerinden HESAPLANIR. 🔴 K15: mockup'ın A bölümü satırları
    `5.842.000` toplarken ara toplam `6.842.000` basıyor (NA:71-78, 1.000.000
    fark) — SATIRLAR kazanır, tfoot bir SUNUM göstermeliğidir.
    """

    key: str
    code: str
    title: str
    subtotal_label: str
    subtotal: MaliTutar
    lines: list[CashFlowStatementLine]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "CashFlowStatementSection", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori(_satir_kodlari(model.lines), etiketler)


class MonthlyCashPoint(BaseModel):
    """`Aylık Nakit Pozisyonu` grafiğinin bir noktası (mockup NA:108-131).

    🔴 **BAKİYE, akış DEĞİL:** grafiğin adı "Pozisyon"dur ve nokta o ayın
    SONUNDAKİ nakit bakiyesidir (açılış nakdi dâhil). Aylık akış basan bir
    uygulama aynı veriyle bambaşka bir eğri çizer ve son noktası
    `closing_cash`e denk GELMEZDİ.
    """

    year: int
    month: int
    closing_cash: MaliTutar

    #: IZN-B5a: noktanin bakiyesi bordro iceren bir donem akisini kapsiyor mu (servis doldurur;
    #: yanit alani DEGILDIR, OpenAPI degismez).
    _bordro_dahil: bool = PrivateAttr(default=False)

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "MonthlyCashPoint", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return etiketler | {Hassas.maas_kisisel} if model._bordro_dahil else etiketler


class CashFlowStatementResponse(BaseModel):
    """Nakit Akış Tablosunun tamamı — yevmiyeden türer (KK-2).

    🔴 **`/treasury/cash-flow` İLE AYNI ŞEY DEĞİLDİR.** O uç
    `payments`+`invoices`ten türeyen GÜNLÜK giriş/çıkış serisidir (F-HZ ekranı);
    bu uç yevmiyeden türeyen işletme/yatırım/finansman tablosudur. İkisi farklı
    sayı basar ve bu bir kusur değildir — ayrım her iki modül docstring'inde de
    yazılıdır.

    🔴 **DÖRT ALAN BİRDEN DÖNER** ve gerekçesi ölçülmüştür: mockup'ın alt bandı
    `DÖNEM SONU NAKİT (A+B+C)` **diyor** ama değeri `4.249.500`, yani
    Bilanço'daki `Kasa ve Bankalar` (BL:51) ile BİREBİR aynı — bu KAPANIŞ
    NAKDİDİR. A+B+C ise `4.802.000`dir (NA:58). İkisi AYRI ŞEYDİR ve mockup'ta
    **DÖNEM BAŞI NAKİT satırı EKSİKTİR** (türetilen açılış `−552.500` çıkar,
    imkânsız). Uç dördünü de döndürür; hangisinin basılacağına frontend kendi
    diliminde karar verir (MU-2'nin `carried_forward`ı emsal).

    Kimlik: `closing_cash == opening_cash + net_change`.

    `year`/`month` yanıtta TEKRARLANIR: mockup NA:37 (`Ocak–Temmuz 2026`)
    başlığı buradan kurulur.

    Kapsam dışı: `3 Aylık Projeksiyon` kartı (NA:134-150) — ileriye dönük
    tahmin, algoritması mockup'ta YOK ve açıklama metinleri (`"Hakediş +
    bordro"`) serbest metin. İCAT EDİLMEZ; frontend devre-dışı + gerekçeyle
    basar (F-TH kanonu).
    """

    year: int
    month: int
    sections: list[CashFlowStatementSection]
    net_change: MaliTutar
    opening_cash: MaliTutar
    closing_cash: MaliTutar
    monthly_cash: list[MonthlyCashPoint]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "CashFlowStatementResponse", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        # Acilis nakdi onceki donemlerin birikimidir; net degisim/kapanis bu donemin akisini icerir.
        if alan_adi == "opening_cash":
            return etiketler
        return bordro_ek_kategori(_bolum_kodlari(model.sections), etiketler)


# --------------------------------------------------------------------------- #
# MT-2 — Gelir Tablosu (mockup `Ekran 11 - Mali Tablo.dc.html`, GT:86-147)
# --------------------------------------------------------------------------- #


class IncomeStatementLine(BaseModel):
    """Gelir tablosunun bir KALEMİ — mockup'ın tek bir satırı (ör. GT:98).

    🔴 `amount` **POZİTİF sözleşmelidir**: gelir kalemleri `Σ(alacak − borç)`,
    gider kalemleri `Σ(borç − alacak)` basar ve doğru işlenmiş bir defterde
    ikisi de pozitiftir (mockup'ın altı satırının hepsi pozitif; `Toplam Gider`
    kırmızıdır ama işaretsizdir, GT:137). Gider kalemini negatif basan bir uç,
    `DÖNEM KARI = Toplam Gelir − Toplam Gider` mockup aritmetiğini bozardı.
    Kalem yine de NEGATİF çıkabilir — `İş Hasılatı` satışın altında bir iade
    hacminde (`61` grubu) eksiye döner ve bu GERÇEK bir sonuçtur, `0`a
    kırpılsaydı toplam yalan söylerdi.

    🔴 **YUVARLANMAZ** (MT-K2): `Numeric(18,2)` kuruşuyla döner. Yüzde/marj
    (`%49,9`) ve trend (`↑ %8,3`) BURADA YOKTUR — oran bir GÖSTERİM kararıdır
    (`0` gelirde `ZeroDivisionError` üretirdi) ve trend önceki dönem
    karşılaştırması ister; mockup hangi dönem olduğunu SÖYLEMİYOR ve algoritma
    İCAT EDİLMEZ (nakit akışının `3 Aylık Projeksiyon`u ile aynı gerekçe).

    `account_codes` mockup'ta basılmıyor ama **ZORUNLUDUR**: bir kalemin
    hangi hesaplardan geldiğinin tek kanıtı budur ve `Genel Giderler` kovasını
    (on grup) ŞEFFAF kılar. 🔴 Gider kalemlerinde MALİYET AKTARIM hesapları
    listede YOKTUR çünkü tutara da girmezler — çiftin **alacak** bacağı
    (`711`, `741`, …, K7) **ve borç** bacağı (`700`, `799`, K7-b) birlikte
    dışlanır; satır böylece BRÜT gideri gösterir.
    """

    key: str
    label: str
    amount: MaliTutar
    account_codes: list[str]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "IncomeStatementLine", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori(model.account_codes, etiketler)


class IncomeStatementSection(BaseModel):
    """`GELİRLER` (GT:95) ya da `GİDERLER` (GT:113) bölümü + ara toplamı.

    `subtotal` kalemlerinden HESAPLANIR, mockup'tan KOPYALANMAZ (K15). Mockup'ın
    aritmetiği bu tabloda TEMİZDİR (24.870.500+124.200 = 24.994.700 ·
    12.480.000+5.840.000+3.120.000+42.000 = 21.482.000) ama kural rakamın doğru
    çıkmasına bağlı değildir.
    """

    key: str
    title: str
    subtotal_label: str
    subtotal: MaliTutar
    lines: list[IncomeStatementLine]

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "IncomeStatementSection", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        return bordro_ek_kategori(_satir_kodlari(model.lines), etiketler)


class IncomeStatementResponse(BaseModel):
    """Gelir Tablosunun tamamı — 🔴 **K7 SAYFALAMA ZARFI YOKTUR** (bilanço emsali).

    Küme SABİTTİR: **2 bölüm · 6 kalem · 2 ara toplam · 1 genel toplam.**
    TDHP'nin `Brüt Satış Kârı` / `Faaliyet Kârı` basamakları YAZILMAZ — mockup
    onları çizmiyor ve icat edilmiş bir kalem tasarım otoritesini aşardı.

    🔴 **`period_profit` hiçbir kalemden toplanmaz.** Değeri
    `statement_map.period_profit()`ten gelir ve Bilanço'nun `Dönem Net Kârı`
    kalemi (BL:83) ile **BİREBİR AYNI** fonksiyondur — iki uç ayrışamaz.
    `total_revenue − total_expense` ise KALEMLERDEN toplanır.

    🔴 **İkisi AYRIŞABİLİR ve bu bilinçlidir (K7 + K7-b):** gider kalemleri
    maliyet aktarım hesaplarının İKİ bacağını da dışlar (satır BRÜT gideri
    gösterir), `period_profit()` ise ikisini de sayar — orada birbirlerini
    götürürler. Aktarım fişi ATILMIŞ bir defterde
    `total_revenue − total_expense ≠ period_profit` olur. Üç alanın da
    dönmesinin sebebi budur: fark GÖRÜNÜR kalsın, sessizce bir tarafa
    yazılmasın (`CashFlowStatementResponse`un dört alanı emsal).

    `year`/`month` yanıtta TEKRARLANIR: mockup GT:90 (`Ocak – Temmuz 2026`)
    başlığı buradan kurulur ve istemci hangi dönemi gördüğünü kendi isteğinden
    değil SUNUCUNUN cevabından okur.

    Kapsam dışı (bilinçli): trend kolonu (GT:99 `↑ %8,3`) · oran/marj kolonu
    (GT:117 `%49,9`, GT:142 `%14,1`) · proje süzgeci (GT:81 — üç muhasebe
    tablosunda da `project_id`/`site_id` YOKTUR) · dönem kilidi rozeti
    (salt-okuma ucu).
    """

    year: int
    month: int
    sections: list[IncomeStatementSection]
    total_revenue: MaliTutar
    total_expense: MaliTutar
    profit_label: str
    period_profit: MaliTutar

    @staticmethod
    def KATEGORI_COZ(  # noqa: N802 — `field_mask.KATEGORI_COZ_OZNITELIGI` sözleşmesi
        model: "IncomeStatementResponse", alan_adi: str, etiketler: frozenset[Hassas]
    ) -> frozenset[Hassas]:
        # Gider ve donem kari bordro iceren bir satiri kapsar; gelir toplami bagimsizdir.
        if alan_adi == "total_revenue":
            return etiketler
        return bordro_ek_kategori(_bolum_kodlari(model.sections), etiketler)
