"""Muhasebe RAPOR uçları (MU-2) — `GET /trial-balance` (Mizan) · T5: `/vat-return`.

Kapı **`accounting`** iznidir (`guards.PERMISSION_MODULE`) — 🔴 **yeni izin
modülü AÇILMADI, matris DEĞİŞMEDİ, izin migration'ı YOKTUR.** Mizan bir
OKUMADIR, dolayısıyla `view` yeter: PM · muhasebe · patron · sysadmin görür.

## 🔴 `prefix` YOKTUR — bilinçli

Router bir KÖK taşımaz, iki AYRI birinci-seviye yol taşıyacaktır
(`/trial-balance` bugün, `/vat-return` T5'te). Ortak bir önek uydurmak
(`/accounting-reports/...`) mockup'ın sidebar'ıyla (satır 33, 36 — `Mizan` ve
`KDV Beyanı` kardeş girdilerdir) ve öteki muhasebe köklerinin düz adlandırmasıyla
(`/journal`, `/chart-of-accounts`) çelişirdi. T5 buraya YENİ BİR `@router.get`
ekler; dosya bunun için hazırdır.

## ROTA SIRASI

`/trial-balance` TEK segmentli ve LİTERALDİR. `grep` ile doğrulandı: repoda
`trial-balance` geçen başka hiçbir yol yoktu ve uygulamanın KÖK seviyesinde
`"/{param}"` biçiminde hiçbir rota yoktur (tek `"/{...}"` rotası `/equipment`
öneki altındadır). Dolayısıyla bu yolun UUID sanılması yapısal olarak imkânsızdır
ve `include_router` sırası serbesttir (MK-2 dersinin uygulanamadığı hâl).

## 🔴 `year`/`month` ZORUNLU — sunucunun "bugün"ü HİÇ okunmaz

`ledger.default_period()` gibi bir varsayılan AÇILMADI. Gerekçe TB5'in üretimde
kanıtlı kusurudur: sunucunun yerel takvimini okuyan bir varsayılan, TR gecesinde
bir gün (ve ayın ilk gecesinde bir AY) geride kalır ve kullanıcı hangi mizana
baktığını bilmez. İki parametre de zorunlu tutulunca kusur bu uçta **yapısal
olarak imkânsızdır**; frontend dönemi zaten mockup satır 44-46'daki `‹ Ocak–Temmuz
2026 ›` gezicisinden bilir.

`GET` uçları `record_audit` ÇAĞIRMAZ (WORKFLOW kuralı — okumalar denetlenmez).
"""

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from app.core import http
from app.core.access import AccessLevel
from app.core.db import DbSession
from app.core.deps import get_current_user
from app.core.openapi import COMMON_ERROR_RESPONSES
from app.core.permissions import require_permission
from app.modules.accounting import (
    balance_sheet,
    cash_flow_statement,
    export,
    guards,
    income_statement,
    trial_balance,
    vat_return,
)
from app.modules.accounting.reports_schemas import (
    BalanceSheetResponse,
    CashFlowStatementResponse,
    IncomeStatementResponse,
    TrialBalanceResponse,
    VatReturnResponse,
)
from app.modules.users.models import User

router = APIRouter(tags=["accounting"], responses=COMMON_ERROR_RESPONSES)

_VIEW = require_permission(guards.PERMISSION_MODULE, AccessLevel.view)

# 🔴 Bantlar `accounting_periods`in CHECK'leri ve `periods_router` ile BİREBİR —
# aynı takvim iki uçta farklı aralık kabul etseydi bir dönem kapatılabilir ama
# mizanı görüntülenemez olurdu.
_YEAR = Annotated[int, Query(ge=2000, le=2100)]
_MONTH = Annotated[int, Query(ge=1, le=12)]

# 🔴 Bilançonun penceresi bir TARİHTİR (nokta-zaman), yıl+ay çifti DEĞİL —
# `_YEAR`/`_MONTH` bantları burada YETMEZ. Bant yine de `accounting_periods`in
# yıl CHECK'iyle (2000-2100) tutarlıdır: takvimin iki uçta farklı aralık kabul
# etmesi, kapatılabilen ama bilançosu alınamayan bir dönem üretirdi.
# Sınırlar KAPALIDIR (`ge`/`le`): `lt`/`gt` yazılsaydı `2100` yılının bilançosu
# hiç alınamazdı.
_AS_OF = Annotated[date, Query(ge=date(2000, 1, 1), le=date(2100, 12, 31))]


@router.get("/trial-balance", response_model=TrialBalanceResponse, dependencies=[_VIEW])
async def trial_balance_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    include_empty: bool = False,
) -> TrialBalanceResponse:
    """Mizan — açılış (NET) · dönem hareketi (**BRÜT**) · kapanış (NET).

    Dönem BİRİKİMLİ bir ARALIKTIR (mockup satır 45 `Ocak–Temmuz 2026`): yılın
    Ocak ayından `month`un SON GÜNÜNE kadar. Ayrıntı ve aritmetik
    `trial_balance.py` modül docstring'indedir.

    🔴 **Sayfalama YOKTUR** (K7 zarfı kullanılmaz): `totals` GENEL TOPLAMDIR ve
    `is_balanced` onun üzerinden kurulur — sayfalanmış bir mizanda ikisi de
    anlamsızlaşırdı. Küme sınırlıdır (tekdüzen hesap planı ~200 satır).

    `include_empty=false` (varsayılan): üç pencerenin HİÇBİRİNDE hareketi
    olmayan hesap listelenmez — mockup'ın 8 satırının hepsi hareketlidir (satır
    80-159) ve kullanılmayan yüzlerce hesap tabloyu okunamaz hâle getirirdi.
    `true` ile hareketsizler de gelir, altı kolonu da `0` basar.

    Sıralama `code` ARTAN — mizan hesap planının sırasını izler.
    """
    return await trial_balance.build_trial_balance(
        session, year=year, month=month, include_empty=include_empty
    )


@router.get(
    "/trial-balance/export.xlsx",
    dependencies=[_VIEW],
    response_class=Response,
    responses={200: {"content": {export.XLSX_MEDIA_TYPE: {}}, "description": "Excel dosyasi"}},
)
async def trial_balance_export_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
    include_empty: bool = False,
) -> Response:
    """Mizanın Excel çıktısı (mockup satır 48 `Excel`).

    🔴 Zarf **`build_trial_balance` ile — EKRANIN UCUYLA AYNI ÇAĞRIDAN** gelir:
    parametreler, bantlar, `include_empty` varsayılanı ve satır sırası birebir
    aynıdır. İkinci bir sorgu/süzgeç yolu AÇILMAZ (`units`/`payroll` emsali:
    bir kere kur, iki kere bas) — ayrışsalardı ekran ile dosya aynı dönem için
    farklı bir mizan gösterir ve hangisinin doğru olduğu tartışılırdı.

    Sayfalama YOKTUR ve BURADA DA yoktur: `build_trial_balance` zaten kümenin
    tamamını döner, dolayısıyla sessiz kırpma yapısal olarak imkânsızdır.

    `year`/`month` burada da ZORUNLUDUR (sunucunun "bugün"ü hiç okunmaz) —
    aksi hâlde dosya adı, kullanıcının hangi dönemi indirdiğini yalan söylerdi.

    Okuma ucudur: `record_audit` ÇAĞIRMAZ ve `Request` parametresi bile ALMAZ.
    """
    report = await trial_balance.build_trial_balance(
        session, year=year, month=month, include_empty=include_empty
    )
    return Response(
        content=export.build_trial_balance_workbook(report).getvalue(),
        media_type=export.XLSX_MEDIA_TYPE,
        headers={
            "Content-Disposition": http.content_disposition(
                export.trial_balance_filename(year, month)
            )
        },
    )


@router.get("/vat-return", response_model=VatReturnResponse, dependencies=[_VIEW])
async def vat_return_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
) -> VatReturnResponse:
    """KDV Beyannamesi — hesaplanan · indirilecek · ödenecek/devreden.

    🔴 **Pencere TEK AYDIR** (mockup satır 45 `Haziran 2026`), mizanın birikimli
    aralığından FARKLI. Faturanın `issue_date`i esas alınır; sınırlar kapalıdır.
    Sayılan faturalar, oran gruplaması, istisna tanımı ve vade aritmetiği
    `vat_return.py` modül docstring'indedir.

    🔴 Beyanname faturanın parasını YENİDEN YAZMAZ: her fatura için
    `invoicing/amounts.py` yeniden çalıştırılır, matrah avans/teminat düşülmüş
    `tax_base`tir. Aynı formülün ikinci bir kopyası bu uçta yapısal olarak
    yasaktır (kaynak metni testle denetlenir).

    Mockup'ın `XML İndir` / `GİB'e Gönder` düğmeleri (satır 48-49) KAPSAM
    DIŞIDIR: e-Fatura/GİB tümüyle ertelenmiştir, uç AÇILMAMIŞTIR.

    🔴 **Sayfalama YOKTUR**: küme oran sayısıyla (bir avuç) sınırlıdır ve
    `calculated_vat` satırların GENEL toplamıdır — sayfalanmış bir beyanda
    anlamsızlaşırdı.
    """
    return await vat_return.build_vat_return(session, year=year, month=month)


@router.get("/balance-sheet", response_model=BalanceSheetResponse, dependencies=[_VIEW])
async def balance_sheet_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    as_of: _AS_OF,
) -> BalanceSheetResponse:
    """Bilanço — AKTİF/PASİF, üç seviye (bölüm bandı → kalem → ara toplam).

    🔴 **Dönem modeli NOKTA-ZAMANDIR**, mizanın birikimli aralığından FARKLI:
    mockup BL:37 üç ayrı **tek gün** sunar (`31 Temmuz 2026` / `30 Haziran 2026`
    / `31 Aralık 2025`). Gövde `entry_date <= as_of` kümülatif nettir; tek
    istisna `Dönem Net Kârı` kalemidir ve penceresi `{as_of.year}-01-01` ile
    `as_of` arasıdır (yılbaşından bugüne). Aritmetik ve kontra netlemesi
    `balance_sheet.py` modül docstring'indedir.

    🔴 **`is_balanced` ÖLÇÜLÜR, `True` VARSAYILMAZ**: gerekçe TB6 T2'de
    DEĞİŞTİ, sonuç DEĞİŞMEDİ. Dengesiz bir `reversed` BAŞLIK artık yazılamaz
    (`ck_journal_entries_posting_balanced` deftere girenlerin HEPSİNİ bağlar),
    ama kısıt BAŞLIK toplamlarını bağlar, bilanço ise `journal_lines`ı toplar —
    başlığı dengeli, satırları dengesiz bir fiş HÂLÂ kurulabilir; ayrıca
    `is_contra` işaretlenmemiş bir `257` iki katı tutar kaydırır. Sabit `True`
    basan bir bilanço sessizce yalan söylerdi.

    🔴 **Sayfalama YOKTUR** (K7 zarfı kullanılmaz): `total` GENEL TOPLAMDIR ve
    `is_balanced` onun üzerinden kurulur — sayfalanmış bir bilançoda ikisi de
    anlamsızlaşırdı. Küme SABİTTİR: iki taraf, 13 kalem.

    Kapsam dışı (bilinçli): proje/şantiye süzgeci (üç muhasebe tablosunda da
    `project_id`/`site_id` YOKTUR ve mockup süzgeç çizmiyor) · karşılaştırma
    sütunu (mockup tabloları 2 sütunlu, BL:48-62) · `PDF` düğmesi (BL:38 — düğme
    dışında hiçbir şey söylemiyor) · dönem kilidi rozeti (salt-okuma ucu).
    """
    return await balance_sheet.build_balance_sheet(session, as_of=as_of)


@router.get("/cash-flow-statement", response_model=CashFlowStatementResponse, dependencies=[_VIEW])
async def cash_flow_statement_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
) -> CashFlowStatementResponse:
    """Nakit Akış Tablosu — `A` işletme · `B` yatırım · `C` finansman.

    🔴 **`/treasury/cash-flow` İLE AYNI ŞEY DEĞİLDİR ve yol adı bilinçli olarak
    ayrıdır.** O uç `payments`+`invoices`ten türeyen **GÜNLÜK giriş/çıkış
    serisidir** (F-HZ hazine paneli, E9:90-106); bu uç **yevmiyeden** türeyen
    işletme/yatırım/finansman tablosudur (KK-2). İkisi farklı sayı basar ve bu
    bir kusur DEĞİLDİR — ayrım her iki modül docstring'inde de yazılıdır.
    Bilanço ile bu tablo TEK tabandan gelir, bu yüzden `Kasa ve Bankalar`
    (BL:51) ile `closing_cash` birebir aynıdır.

    🔴 **Pencere BİRİKİMLİDİR** (mockup NA:37 `Ocak–Temmuz 2026`): yılın Ocak
    ayından `month`un SON GÜNÜNE kadar — mizanla AYNI semantik. `year`/`month`
    ZORUNLUDUR; `/treasury/cash-flow`un aksine içinde bulunulan aya DÜŞMEZ
    (sunucunun "bugün"ü hiç okunmaz).

    🔴 **DÖRT ALAN BİRDEN DÖNER:** `net_change` (A+B+C) · `opening_cash` ·
    `closing_cash` · bölüm ara toplamları. Mockup'ın alt bandı
    `DÖNEM SONU NAKİT (A+B+C)` **diyor** ama değeri kapanış nakdidir (NA:100 =
    BL:51) — ikisi ayrı şeydir ve mockup'ta `DÖNEM BAŞI NAKİT` satırı EKSİKTİR.
    Hangisinin basılacağına frontend kendi diliminde karar verir.

    `monthly_cash[]` = `Aylık Nakit Pozisyonu` grafiği (NA:108-131): Ocak'tan
    seçilen aya kadar **ay sonu nakit BAKİYESİ** (akış değil).

    Kapsam dışı (bilinçli): `3 Aylık Projeksiyon` kartı (NA:134-150) — ileriye
    dönük tahmin, algoritması mockup'ta YOK, açıklama metinleri serbest metin;
    İCAT EDİLMEZ · `PDF` düğmesi (NA:38) · proje/şantiye süzgeci (üç muhasebe
    tablosunda da kolon yok, mockup süzgeç çizmiyor).
    """
    return await cash_flow_statement.build_cash_flow_statement(session, year=year, month=month)


@router.get("/income-statement", response_model=IncomeStatementResponse, dependencies=[_VIEW])
async def income_statement_endpoint(
    user: Annotated[User, Depends(get_current_user)],
    session: DbSession,
    year: _YEAR,
    month: _MONTH,
) -> IncomeStatementResponse:
    """Gelir Tablosu — `GELİRLER` / `GİDERLER` + `DÖNEM KARI` (mockup GT:86-147).

    🔴 **Pencere BİRİKİMLİDİR** (mockup GT:90 `Ocak – Temmuz 2026`): yılın Ocak
    ayından `month`un SON GÜNÜNE kadar — mizan ve nakit akışıyla AYNI semantik.
    Bilançonun `as_of` NOKTA-ZAMANI burada YANLIŞ olurdu: gelir tablosu bir AKIŞ
    tablosudur ve kümülatif bir pencere geçmiş yılların hasılatını bu yılın
    cirosuna eklerdi. `year`/`month` ZORUNLUDUR; sunucunun "bugün"ü HİÇ okunmaz
    (TB5'in yerel-takvim kusuru bu uçta yapısal olarak imkânsız).

    🔴 **Yapı SABİTTİR: 2 bölüm · 6 kalem · 2 ara toplam · 1 genel toplam** (K1).
    TDHP'nin `Brüt Satış Kârı` / `Faaliyet Kârı` basamakları YAZILMAZ — mockup
    onları çizmiyor ve icat edilmiş bir kalem tasarım otoritesini aşardı.
    `Taşeron Ödemeleri` grup `74 Hizmet Üretim Maliyeti`nden gelir: TDHP'de
    "taşeron" grubu YOKTUR (`101 Alınan Çekler` tuzağının kardeşi) ve satırı boş
    bırakıp `0` bastırmak İKİ ANLAMLI bir `0` üretirdi.

    🔴 **`period_profit` hiçbir kalemden toplanmaz:** Bilanço'nun `Dönem Net
    Kârı` kalemiyle (BL:83) **AYNI FONKSİYONDAN** gelir (`statement_map.
    period_profit()`, TEK KOPYA) — `/income-statement?year=Y&month=12` ile
    `/balance-sheet?as_of=Y-12-31` ayrışamaz. `total_revenue − total_expense`
    ise KALEMLERDEN toplanır ve ikisi AYRIŞABİLİR: gider kalemleri 7/A yansıtma
    hesaplarını dışlar (satır BRÜT gideri gösterir, K7), `period_profit()` ise
    onları sayar. Üç alan da döner ki fark GÖRÜNÜR kalsın.

    🔴 **Sayfalama YOKTUR** (K7 zarfı kullanılmaz): küme SABİTTİR (6 kalem) ve
    `period_profit` GENEL sonuçtur — sayfalanmış bir gelir tablosunda
    anlamsızlaşırdı.

    Kapsam dışı (bilinçli): **trend kolonu** (GT:99 `↑ %8,3`) — önceki dönem
    karşılaştırması demektir, mockup hangi dönem olduğunu SÖYLEMİYOR ve
    algoritma İCAT EDİLMEZ (nakit akışının `3 Aylık Projeksiyon`u aynı
    gerekçeyle dışlandı) · **oran/marj kolonu** (GT:117 `%49,9`, GT:142 `%14,1`)
    — yanıtta `total_revenue` ve satır tutarı zaten var; bölme bir GÖSTERİM
    kararıdır ve `0` gelirde `ZeroDivisionError` üretirdi, frontend hesaplar ·
    **proje süzgeci** (GT:81) — üç muhasebe tablosunda da `project_id`/`site_id`
    kolonu YOKTUR (bilanço ve nakit akışı aynı gerekçeyle dışladı) · **dönem
    kilidi rozeti** — salt-okuma ucu, dönem kilidi HİÇ okunmaz.
    """
    return await income_statement.build_income_statement(session, year=year, month=month)
