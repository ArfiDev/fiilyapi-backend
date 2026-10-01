"""TB-AUDIT — `audit/messages` bölünmesinin ANLIK GÖRÜNTÜ bekçisi.

## Neden bu dosya var

`app/modules/audit/messages` 1655 satırlık TEK dosyaydı (tavan 800) ve denetim
mesajı ekleyen HER dilim ona dokunuyordu. Dosya alt modüllere bölündü.

🔴 **Bölmenin gerçek riski testlerin GÖRMEDİĞİ bir risktir.** Ölçüldü: 164 mesaj
önekinin **119'u** hiçbir testte literal olarak geçmiyor — mevcut testlerin çoğu
beklenen metni `messages.X(...)` çağırarak kuruyor, yani *üretim ifadesini üretim
ifadesiyle* karşılaştırıyor. Böyle bir test bir metin sessizce değişirse
**yeşil kalır**: kendi ifadesini kendisiyle karşılaştıran test hiçbir şey
bekçilemez (kanon).

👉 Bu yüzden bölmenin güvencesi mevcut testler DEĞİL, bu dosyadır: bölmeden
**önce** dondurulmuş bir referansla, bölmeden **sonra** üretilen metinlerin
BİREBİR aynı olduğunu kanıtlar.

## Referans dürüst müdür?

`_ANLIK_GORUNTU` dosyası **bölmeden ÖNCEKİ ağaçta** üretildi ve o hâliyle
commit'lendi (`chore/tbaudit-messages-bolme` ilk commit'i). Bölmeden sonra
YENİDEN ÜRETİLMEDİ — yalnız karşılaştırıldı. Referansı yeniden üretmek bu
bekçiyi hiçliğe çevirir; bir metni bilerek değiştiren dilim referansı
`python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazeler ve
**farkı incelemede görünür kılar** — sessizce değil.

## 🔴 REFERANSA EKLENEN TEK SEMBOL (PUAN-SAAT, 2026-08-28)

`timesheet_week_saved` — puantaj aylıktan haftalığa geçti ve kaydetme olayı
artık HAFTA özetidir. Referans `python -m tests.test_tbaudit_denetim_metni_anlik_goruntu`
ile tazelendi; farkta **KAYIP 0**, yalnız bu fonksiyonun dört çağrısı EKLENDİ
(`git diff` ile gözle doğrulandı). `timesheet_saved` KALDI: aylık okuma yüzeyi
(Excel/arşiv) duruyor ve eski denetim satırları o metni taşıyor.

## 🔴 REFERANSA EKLENEN SEMBOL (BLF-B1, 2026-10-01)

`section_type_created` — yeni bölüm tipi kaydı için eklendi. Referans
`python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazelendi; farkta
**KAYIP 0**, yalnız bu fonksiyonun 1 çağrısı EKLENDİ (`diff` ile doğrulandı).

## 🔴 REFERANSA EKLENEN SEMBOLLER (TKL-B2.2, 2026-10-01)

`work_item_created`, `work_item_updated` — çekirdek `/catalog/items` uçları için eklendi.
Referans `python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazelendi; farkta
**KAYIP 0**, yalnız bu iki fonksiyonun birer çağrısı EKLENDİ (`diff` ile doğrulandı).

## 🔴 REFERANSA EKLENEN SEMBOLLER (TKL-B2.3, 2026-10-01)

`work_item_price_updated` (+ ozel yardimcisi `_price_text`) — `ref_price` DEGISEN kalem
guncellemesinin denetim metni (`eski → yeni` fiyat). Referans
`python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazelendi; farkta **KAYIP 0**,
yalniz bu iki sembolun cagrilari EKLENDI (`diff` ile dogrulandi; `work_item_updated` metni
DEGISMEDI).

## 🔴 REFERANSA EKLENEN SEMBOLLER (TKL-B3.1, 2026-10-01)

`employer_contract_items_bulk_created` (+ sabiti `BULK_AUDIT_CODES_SHOWN`) — toplu poz
ekleme ucunun TEK denetim satırı. Referans
`python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazelendi; farkta **KAYIP 0**,
yalnız bu iki sembolün satırları EKLENDİ (`diff` ile doğrulandı); sayaçlar 211→213 sembol,
199→200 fonksiyon.

## 🔴 REFERANSA EKLENEN SEMBOL (TKL-B4.1, 2026-10-02)

`offer_settings_updated` — `PUT /offers/settings` denetim satırı. Referans
`python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazelendi; farkta **KAYIP 0**,
yalnız bu fonksiyonun 2 çağrısı EKLENDİ (`diff` ile doğrulandı); sayaçlar 213→214 sembol,
200→201 fonksiyon.

## 🔴 REFERANSA EKLENEN SEMBOLLER (TKL-B4.2, 2026-10-02)

`offer_created`, `offer_updated`, `offer_conditions_updated`, `offer_deleted`,
`offer_revision_created`, `offer_status_changed`, `offer_items_bulk_created`
(+ sabiti `OFFER_BULK_POZ_SHOWN`) — teklif uçlarının denetim satırları (`messages/offers.py`).
Referans `python -m tests.test_tbaudit_denetim_metni_anlik_goruntu` ile tazelendi; farkta
**KAYIP 0**, yalnız bu sembollerin satırları EKLENDİ (`diff` ile doğrulandı); sayaçlar
214→222 sembol, 201→208 fonksiyon.

`offer_setting_pct_changed`, `offer_setting_days_changed`, `offer_setting_terms_changed`,
`offer_settings_changed` (+ `OFFER_TERMS_SHOWN`, `_short_terms`) — `PUT /offers/settings` denetim
satırının `eski → yeni` metni (E8; yalnız DEĞİŞEN alanlar). Farkta **KAYIP 0**; sayaçlar
222→228 sembol, 208→213 fonksiyon. `offer_settings_updated` (B4.1) KORUNDU ama artık ÇAĞRILMIYOR
(silinirse bu referanstaki satırları KAYBOLUR — bilinçli bir sonraki turun işi).

## Kapsam

Modülün TÜM sembolleri (özel `_` adları DÂHİL) ve her fonksiyon için birden çok
çağrı: her parametre AYRI bir değer alır (argüman sırası takası fark edilsin),
`| None` alanların iki hâli de, `bool` alanların iki hâli de üretilir.

`datetime` değeri BİLEREK 21:30 UTC'dir: TR'de ertesi günün 00:30'udur, yani
`_damga`nın saat dilimi çevirisini (TB5 §1 kusur sınıfı) fiilen koşturur — ham
`strftime`e dönen bir kopya bu dosyada KIRMIZI verir.
"""

from __future__ import annotations

import ast
import inspect
import types
from collections import defaultdict
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args, get_origin

from app.core.access import AccessLevel
from app.modules.audit import messages

_ANLIK_GORUNTU = Path(__file__).with_name("tbaudit_denetim_metni_anlik_goruntu.txt")

#: TR'de ertesi güne taşan bir an — `_damga`nın çevirisini fiilen koşturur.
_AN = datetime(2026, 3, 7, 21, 30, tzinfo=UTC)


def _paket_dosyalari() -> list[Path]:
    """`messages` modülünü oluşturan .py dosyaları (tek dosya da paket de olur)."""
    kaynak = Path(inspect.getfile(messages))
    if kaynak.name == "__init__.py":
        return sorted(p for p in kaynak.parent.glob("*.py") if p.name != "__init__.py")
    return [kaynak]


def _tanimlar() -> dict[str, list[str]]:
    """AST ile: sembol adı -> onu TANIMLAYAN yerler (`dosya:satır`).

    🔴 `grep` DEĞİL AST: docstring'lerde ve yorumlarda sembol adları geçiyor
    (ör. `APPROVAL_ON_BEHALF_MARK` iki üretim dosyasının docstring'inde) ve
    metinsel arama onları TANIM sanardı.
    """
    bulunan: dict[str, list[str]] = defaultdict(list)
    for yol in _paket_dosyalari():
        agac = ast.parse(yol.read_text(encoding="utf-8"))
        for dugum in agac.body:
            adlar: list[str] = []
            if isinstance(dugum, ast.FunctionDef | ast.AsyncFunctionDef):
                adlar = [dugum.name]
            elif isinstance(dugum, ast.Assign):
                adlar = [t.id for t in dugum.targets if isinstance(t, ast.Name)]
            elif isinstance(dugum, ast.AnnAssign) and isinstance(dugum.target, ast.Name):
                adlar = [dugum.target.id]
            for ad in adlar:
                bulunan[ad].append(f"{yol.name}:{dugum.lineno}")
    return dict(bulunan)


def _deger(annotation: Any, ad: str, sira: int) -> Any:
    """Anotasyondan DETERMİNİSTİK bir temsilî değer.

    Değer parametre SIRASINA bağlıdır: iki parametre takas edilirse üretilen
    metin değişir ve bekçi bunu görür. Aynı türden iki parametreye aynı değer
    verilseydi takas GÖRÜNMEZ olurdu.
    """
    if annotation is str:
        return f"<{ad}#{sira}>"
    if annotation is int:
        return 100 + sira
    if annotation is bool:
        return True
    if annotation is date:
        return date(2026, 1 + sira % 12, 1 + sira % 28)
    if annotation is datetime:
        return _AN
    if annotation is Decimal:
        return Decimal(f"{1000 + sira}.55")
    if annotation is AccessLevel:
        return AccessLevel.approve
    if annotation is object:
        # Üretimde buraya `date` (ödeme vadesi) ve `Decimal` (toplam) geçiyor.
        return date(2026, 6, 15) if "date" in ad else Decimal(f"{2000 + sira}.75")
    if get_origin(annotation) in (types.UnionType,):
        ic = [a for a in get_args(annotation) if a is not type(None)]
        return _deger(ic[0], ad, sira)
    if get_origin(annotation) is list:
        (icerik,) = get_args(annotation) or (str,)
        if get_origin(icerik) is tuple:
            return [(15000, Decimal("0.15"), Decimal("2250")), (30000, Decimal("0.20"), None)]
        return [f"<{ad}#{sira}a>", f"<{ad}#{sira}b>"]
    if get_origin(annotation) is dict:
        return {"sgk_isci": Decimal("0.14"), "issizlik": Decimal("0.01"), "kaynak": "resmî"}
    raise AssertionError(f"anotasyon karsiligi TANIMSIZ: {annotation!r} ({ad})")


def _cagri_matrisi(fn: Any) -> list[dict[str, Any]]:
    """Bir fonksiyon için çağrı kümesi: taban + her DALLI parametrenin varyantı.

    `| None` ve `bool` parametreler metnin AYRI dallarını üretir (ör. `BILINMIYOR`
    yedeği, "vekâleten" eki). Taban çağrı tek başına o dalları hiç koşturmazdı.
    """
    imza = inspect.signature(fn)
    taban: dict[str, Any] = {}
    dalli: list[tuple[str, Any]] = []
    for sira, (ad, p) in enumerate(imza.parameters.items()):
        taban[ad] = _deger(p.annotation, ad, sira)
        if p.annotation is bool:
            dalli.append((ad, False))
        elif p.annotation is int:
            # `int` de BAYRAK olabilir: `units_imported(skipped=0)` ve
            # `unit_allocation_updated(shareholder_count=0)` sifirda AYRI bir
            # cumle kuruyor. Yalniz sifirdan farkli bir deger uretilseydi o iki
            # metin anlik goruntude HIC gorunmez, yani DONMAMIS olurdu.
            dalli.append((ad, 0))
        elif get_origin(p.annotation) is types.UnionType and type(None) in get_args(p.annotation):
            dalli.append((ad, None))
        elif get_origin(p.annotation) is list:
            dalli.append((ad, []))
    return [taban] + [{**taban, ad: deger} for ad, deger in dalli]


def _uret() -> str:
    """Modülün ürettiği TÜM metinlerin kanonik, sıralı dökümü."""
    satirlar: list[str] = []
    for ad in sorted(_tanimlar()):
        nesne = getattr(messages, ad)
        if not callable(nesne):
            satirlar.append(f"{ad} = {nesne!r}")
            continue
        for kwargs in _cagri_matrisi(nesne):
            gosterim = ", ".join(f"{k}={v!r}" for k, v in kwargs.items())
            satirlar.append(f"{ad}({gosterim}) = {nesne(**kwargs)!r}")
    return "\n".join(satirlar) + "\n"


def test_denetim_metinleri_bolme_oncesi_referansla_birebir_ayni() -> None:
    """🔴 Bu dilimin TEK gerçek güvencesi.

    Referans bölmeden ÖNCE donduruldu; bölme bir metni sessizce değiştirseydi
    (bir gerekçe kısaltılırken bir f-string'e dokunmak yeterdi) burada KIRMIZI
    olurdu — mevcut testlerin 119 önek için görmediği şey tam budur.
    """
    beklenen = _ANLIK_GORUNTU.read_text(encoding="utf-8")
    assert beklenen.strip(), "referans anlık görüntü BOŞ — bekçi hiçbir şey ölçmüyor olurdu"

    uretilen = _uret()

    beklenen_satirlar = beklenen.splitlines()
    uretilen_satirlar = uretilen.splitlines()
    eksik = set(beklenen_satirlar) - set(uretilen_satirlar)
    fazla = set(uretilen_satirlar) - set(beklenen_satirlar)
    assert not eksik and not fazla, (
        f"denetim metni DEĞİŞTİ.\n  kaybolan {len(eksik)}: {sorted(eksik)[:5]}\n"
        f"  yeni {len(fazla)}: {sorted(fazla)[:5]}"
    )
    assert uretilen == beklenen


def test_anlik_goruntu_bos_degil_ve_tum_sembolleri_kapsiyor() -> None:
    """Bekçinin girdisinin BOŞ OLMADIĞI ayrıca kanıtlanır.

    Referans yanlışlıkla boşalsa ya da sembollerin yarısı düşse üstteki test
    yine yeşil kalabilirdi ("hiçbir şeyi hiçbir şeyle karşılaştırmak").
    """
    tanimlar = _tanimlar()
    assert len(tanimlar) == 228, f"sembol sayısı 228 olmalı, {len(tanimlar)} bulundu"
    fonksiyonlar = [a for a in tanimlar if callable(getattr(messages, a))]
    assert len(fonksiyonlar) == 213, f"fonksiyon sayısı 213 olmalı, {len(fonksiyonlar)} bulundu"

    satirlar = _ANLIK_GORUNTU.read_text(encoding="utf-8").splitlines()
    assert len(satirlar) >= 240, f"anlık görüntü çok kısa: {len(satirlar)} satır"
    for ad in tanimlar:
        assert any(s.startswith(f"{ad}(") or s.startswith(f"{ad} = ") for s in satirlar), (
            f"`{ad}` anlık görüntüde HİÇ geçmiyor — bekçi onu kapsamıyor"
        )


def test_paylasilan_yardimcilarin_tek_kopyasi_var() -> None:
    """🔴 K2 — hiçbir sembol İKİ KEZ tanımlı olmamalı.

    Bölme sırasında bir yardımcıyı iki alt modüle KOPYALAMAK en sinsi bozulmadır:
    bugün iki kopya aynı metni üretir, yarın biri düzeltilir öteki kalır
    (`_damga`nın TR saat dilimi düzeltmesi tam bu sınıftandır) ve *"tek kopyayı
    çağır"* diyen bir sonraki dilim İKİ ADAY bulur.
    """
    coklu = {ad: yerler for ad, yerler in _tanimlar().items() if len(yerler) > 1}
    assert not coklu, f"AYNI sembol birden çok yerde tanımlı: {coklu}"


if __name__ == "__main__":  # pragma: no cover - referansı elle tazelemek için
    _ANLIK_GORUNTU.write_text(_uret(), encoding="utf-8")
    print(f"referans yazildi: {_ANLIK_GORUNTU}")
