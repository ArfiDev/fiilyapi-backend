"""P-KK — kat karşılığı paylaşım özeti + denge şemaları (spec: üç mockup).

## Neden ayrı dosya

`projects/schemas.py` 589 satırdır; bu dilimin ~170 satırı oraya eklenseydi
dosya 800 tavanına yaklaşırdı (WORKFLOW §4: "yeni kodu BAŞINDAN pakete böl").
Aynı gerekçeyle hesap çekirdeği `land_share_balance.py`, uç gövdesi
`land_share.py` olarak ayrıdır — `costs.py` / `cost_summary.py` ayrımının
aynısı.

## İki denge AYRI şemadır (K2)

Tek bir "denge" alanı YOKTUR. Bir proje ADET olarak dengede olup DEĞER olarak
sapabilir (23 küçük daire ≠ %55 değer), bu yüzden `count_balance` ile
`value_balance` iki ayrı karttır ve tek sayıya indirgenmez.
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel

from app.core.field_mask import Hassas
from app.modules.units.models import UnitKind, UnitOwnerSide, UnitSalesStatus

__all__ = [
    "LandShareContract",
    "LandShareCountBalance",
    "LandShareOwnerSide",
    "LandShareOurSide",
    "LandSharePartition",
    "LandShareShareholderRow",
    "LandShareSummaryResponse",
    "LandShareUnitListResponse",
    "LandShareUnitRow",
    "LandShareValueBalance",
]


class LandShareContract(BaseModel):
    """`Proje - Kat Karşılığı` sözleşme kartı — YALNIZ modelde GERÇEKTEN VAR OLAN
    kolonlar (K5).

    T1 ölçümü: mockup'ın çizdiği yedi alanın (sözleşme no · noter tarihi · arsa
    alanı · inşaat alanı · teslim tarihi · gecikme cezası · teminat) YEDİSİ DE
    `ProjectLandShare`de kolon olarak vardır. Uydurulan alan yoktur ve bu dilim
    kolon AÇMAZ.

    Hepsi `None` olabilir (`landowner_name` ve iki oran hariç): sözleşme
    kademeli girilir ve boş alan "bilinmiyor"dur — uydurma değer üretilmez.
    """

    landowner_name: str
    our_share_pct: Annotated[Decimal, Hassas.yok]
    owner_share_pct: Annotated[Decimal, Hassas.yok]
    contract_no: str | None
    notary_date: date | None
    land_area_m2: Annotated[Decimal | None, Hassas.yok]
    construction_area_m2: Annotated[Decimal | None, Hassas.yok]
    delivery_date: date | None
    daily_penalty: Annotated[Decimal | None, Hassas.sozlesme_fiyat]
    guarantee_amount: Annotated[Decimal | None, Hassas.sozlesme_fiyat]


class LandSharePartition(BaseModel):
    """Bir kümenin (arsa sahibi payı · atanmamış) adet + değer toplamı."""

    unit_count: int
    value_total: Annotated[Decimal | None, Hassas.satis_alici]


class LandShareOwnerSide(LandSharePartition):
    """`ARSA SAHİBİ PAYI` kartı. Satış sayaçları YOKTUR ve eklenmeyecektir:
    arsa sahibi ünitelerini KENDİSİ satar, bizim satış sistemimize dahil
    değildir (mockup `Proje - Kat Karşılığı`)."""


class LandShareOurSide(LandSharePartition):
    """`BİZİM PAY` kartı — satış kırılımı yalnız BU tarafta anlamlıdır.

    `available_count` YALNIZ `listed` üniteleri sayar; `closed` (satışa kapalı)
    ve `sales_status` NULL olan satırlar HİÇBİR sayaca girmez — uydurulmuş bir
    durum atanmaz. Bu yüzden üç sayacın toplamı `unit_count`a eşit OLMAYABİLİR.
    `remaining_value` ise `value_total − sold_value` türevidir ve tüm satılmamış
    stoku kapsar (mockup: 23 ünite − 8 satılan = 15 "Kalan Stok").
    """

    sold_count: int
    reserved_count: int
    available_count: int
    sold_value: Annotated[Decimal | None, Hassas.satis_alici]
    remaining_value: Annotated[Decimal | None, Hassas.satis_alici]


class LandShareShareholderRow(BaseModel):
    """`Hissedar Dağılımı` satırı (mockup: "Ahmet Yılmaz (%50) · 10 ünite").

    `share_pct` OLDUĞU GİBİ basılır; oranlar toplamı 100 değilse uç düzeltmez
    (K2 kanonu: ekranın görevi toplamı olduğu gibi basmaktır). Ünite sayımı
    YALNIZ `owner_side = landowner` VE bu hissedara atanmış satırları sayar —
    çelişkili veri (bkz. `land_share.py`) hissedar dağılımına sızmaz.
    """

    shareholder_id: uuid.UUID
    name: str
    share_pct: Annotated[Decimal, Hassas.yok]
    unit_count: int
    value_total: Annotated[Decimal | None, Hassas.satis_alici]


class LandShareCountBalance(BaseModel):
    """`Ünite Sayısı Dengesi` kartı (mockup `Form - Paylasim Girisi`).

    `*_missing_count` İŞARETLİDİR: artı = eksik atama, eksi = fazla atama.
    Mutlak değere indirgemek "3 eksik" ile "3 fazla"yı aynı sayı yapardı.

    Beklenen adetler TEK yuvarlamadan türer (`owner = toplam − our`), bu yüzden
    `our_expected_count + owner_expected_count == total_unit_count` her zaman
    doğrudur — ayrı yuvarlama 42 üniteyi 23+20=43 yapardı.
    """

    total_unit_count: int
    our_expected_count: int
    owner_expected_count: int
    our_assigned_count: int
    owner_assigned_count: int
    unassigned_count: int
    our_missing_count: int
    owner_missing_count: int


class LandShareValueBalance(BaseModel):
    """`Değer Dengesi (Rayiç)` kartı — adet dengesinden AYRI hesaplanır (K2).

    Payda ATANMIŞ değerdir (atanmamış üniteler girmez): henüz paylaşılmamış bir
    ünitenin rayici gerçekleşen oranı seyreltemez.

    Dört alan `None` olabilir ve bu "HESAPLANAMAZ"dır, "sıfır" değil: atanmış
    rayiç değer toplamı 0 ise (rayiç girilmemiş proje) sapma tanımsızdır ve `0`
    dönmek ekrana "✓ denge uygun" bastırırdı. `tolerance_pct` bu durumda BİLE
    döner — frontend eşiği kopyalamak zorunda kalmasın diye (bir eşik iki yerde
    yaşarsa ayrışır).
    """

    our_value: Annotated[Decimal | None, Hassas.satis_alici]
    owner_value: Annotated[Decimal | None, Hassas.satis_alici]
    assigned_value_total: Annotated[Decimal | None, Hassas.satis_alici]
    our_actual_pct: Annotated[Decimal | None, Hassas.satis_alici]
    owner_actual_pct: Annotated[Decimal | None, Hassas.satis_alici]
    deviation_pct: Annotated[Decimal | None, Hassas.satis_alici]
    tolerance_pct: Annotated[Decimal, Hassas.yok]
    is_within_tolerance: bool | None


class LandShareBalance(BaseModel):
    """İki denge YAN YANA döner; tek "dengede mi" bayrağına indirgenmez (K2)."""

    count_balance: LandShareCountBalance
    value_balance: LandShareValueBalance


class LandShareSummaryResponse(BaseModel):
    """`GET /projects/{id}/land-share/summary`.

    Kat karşılığı OLMAYAN proje (kayıt yok) burada 404 alır, BOŞ ÖZET DEĞİL:
    boş özet ekrana "%0/%0 paylaşım" bastırır ve kullanıcı veriyi kaybettiğini
    sanardı.
    """

    project_id: uuid.UUID
    project_name: str
    contract: LandShareContract
    totals: LandSharePartition
    our_side: LandShareOurSide
    owner_side: LandShareOwnerSide
    shareholders: list[LandShareShareholderRow]
    unassigned: LandSharePartition
    balance: LandShareBalance


class LandShareUnitRow(BaseModel):
    """`Kat Karşılığı - Paylaşım` orta tablosunun bir satırı.

    `shareholder_name` ve `buyer_name` YANITTA gelir (mockup "Hissedar / Alıcı"
    sütununda arsa satırında hissedarı, bizim satırda alıcıyı basar) — frontend
    satır başına ikinci bir istek atmaya zorlanmaz (N+1 yasağı).
    """

    unit_id: uuid.UUID
    block_id: uuid.UUID
    block_name: str
    unit_no: str
    unit_kind: UnitKind
    layout: str | None
    floor: str | None
    gross_area_m2: Annotated[Decimal | None, Hassas.yok]
    appraisal_value: Annotated[Decimal | None, Hassas.satis_alici]
    owner_side: UnitOwnerSide | None
    shareholder_id: uuid.UUID | None
    shareholder_name: str | None
    buyer_name: str | None
    sales_status: UnitSalesStatus | None


class LandShareUnitListResponse(BaseModel):
    """SAYFALI: 42 ünite bugün küçük, 400 ünite yarın değil.

    `total` SÜZGEÇLENMİŞ kümenin boyutudur (sayfalamadan ÖNCE) — sayfa çubuğu
    buradan çıkar (`ProjectListResponse.total` ile aynı sözleşme).
    """

    items: list[LandShareUnitRow]
    total: int
    limit: int
    offset: int
