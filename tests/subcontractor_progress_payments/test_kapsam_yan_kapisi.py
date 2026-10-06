"""Taşeron hakedişi ile kapsam maskesinin İLİŞKİSİ — ölçülmüş, ÇİVİLENMİŞ hâl.

## Bu dosya bir ONARIM değil, bir ÖLÇÜMÜN ÇİVİSİDİR

2026-09-20 devrinde şu iddia devredildi:

> `subcontractor_progress_payments` sızıntısı — `progress_payments`in BİREBİR
> İKİZİ: taşeron sözleşmesinin birim fiyatını zorunlu `Decimal` döndürür,
> routeri kapsam maskesine bağlı değil. Deseni al: etiketle → routerı bağla.

İddia koda karşı ölçüldü ve **çerçevesi YANLIŞ çıktı**. Ölçümler:

1. `progress_payments`in GERÇEK sızıntısı bir **GÖMÜLME** sızıntısıydı:
   `ProgressPaymentSummary`, kapsam kısıtlı `contracts` modülünün yanıtına
   (`EmployerContractDetail.progress_payment_summary`) gömülüydü; maske iç içe
   şemalara İNDİĞİ için oraya ULAŞIYORDU, yalnız alanlar etiketsizdi.
2. Maskeli 88 ucun yanıt ağacı özyineli gezildi: ulaşılan 113 şemanın
   **HİÇBİRİ** `subcontractor_progress_payments` paketinden değildir. Yani
   gömülme YOKTUR ve mekanizma aynı DEĞİLDİR. "Etiketle" adımının bu pakette
   hiçbir etkisi olmazdı — etiket yalnız maskenin dokunduğu ağaçta iş görür
   (`test_kapsam_capraz_sizinti.py::test_OZETIN_KENDI_UCU_ETIKETLERDEN_ETKILENMEZ`).
3. Geriye ÖLÇÜLMÜŞ ve GERÇEK olan tek şey kalıyor: bir **YAN KAPI**. Aşağıdaki
   davranış bekçisi onu ölçer.

## 🔴 YAN KAPI BİR ÜRÜN KARARIDIR — kod bu oturumda DEĞİŞTİRİLMEDİ

`subcontractor_progress_payments` routeri `require_permission("progress_payments",
…)` kullanır ve o izin anahtarı matriste 8 rolün 8'inde de `Scope.all`dır. Yani
`contracts = none` olan bir rol taşeron sözleşmesi ucundan 403 alırken AYNI
sözleşme kaleminin birim fiyatını hakediş satırında tam değeriyle okur.

Bunu kapatmanın tek yolu MATRİSİ değiştirmektir (`progress_payments` hücrelerine
kapsam kısıtı koymak) ve bu, rollerin ne göreceğini değiştiren bir ÜRÜN
kararıdır. Emsal, işveren tarafında ZATEN belgelenmiştir:
`tests/modules/test_kapsam_capraz_sizinti.py::test_OZETIN_KENDI_UCU_ETIKETLERDEN_
ETKILENMEZ` docstring'i aynı olguyu *"B maddesi … ÜRÜN KARARI"* diye kaydeder.
Bu dosya o kaydın TAŞERON İKİZİDİR — ve bir not değil, bir TESTTİR: karar
uygulandığı gün kırmızı verir ve uygulayanı `KARARLAR-BEKLEYEN.md`i okumaya
zorlar. Elle yazılmış bir devir notu bunu yapamazdı; bu depoda devir notlarının
defalarca bayat çıktığı ölçüldü.

## Neden "çivi" testleri kendi başlarına KÖRDÜR ve bu nasıl kapatıldı

Bugünkü hâli doğrulayan bir test, maske tamamen bozulduğunda da yeşil kalır
(hiçbir şey gizlenmiyorsa "gizlenmedi" iddiası da doğrudur). Bu yüzden her iki
bekçinin de POZİTİF KONTROLÜ vardır: yapısal olanı `contracts`ın GERÇEKTEN
kısıtlı olduğunu, davranışsal olanı aynı rolün sözleşme ucundan GERÇEKTEN 403
aldığını ayrıca çakar. İkisi birlikte *"kapı çalışıyor AMA yan kapı açık"* der.
"""

from decimal import Decimal

import pytest

#: `taseron_sozlesmesi_fabrikasi`nın ilk kalemine yazdığı birim fiyat.
#: Elle yazılır: fabrikadan okunsaydı test, ölçtüğü değeri kendisi üretir ve
#: fabrika bir gün `None` yazmaya başlasa bile yeşil kalırdı.
_BIRIM_FIYAT = Decimal("21500")


# --------------------------------------------------------------------------- #
# DAVRANIŞ BEKÇİSİ — gerçek rol, gerçek iki uç
# --------------------------------------------------------------------------- #


@pytest.fixture
async def yan_kapi_hakedisi(
    client, seeded_db, admin_headers, taseron_sozlesmesi_fabrikasi, kisitli_proje
):
    """`kisitli_proje`de bir taşeron sözleşmesi + satırları YÜKLÜ bir hakediş.

    Hakediş UÇTAN oluşturulur (DB'ye elle yazılmaz): satırlar sözleşme
    kalemlerinden OTOMATİK yüklenir (O66) ve testin ölçtüğü `contract_unit_price`
    tam o otomatik yüklemenin ürünüdür. Elle yazılmış bir satır, ölçülmek istenen
    yolu atlardı.
    """
    contract, _project, _site = await taseron_sozlesmesi_fabrikasi("THK-YAN", project=kisitli_proje)
    resp = await client.post(
        f"/subcontractor-contracts/{contract.id}/progress-payments",
        json={},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text
    return contract, resp.json()


async def test_SEF_sozlesme_ucunden_403_alir(client, sef_headers, yan_kapi_hakedisi):
    """🔴 POZİTİF KONTROL — asıl kapı GERÇEKTEN kapalı mı?

    Bu olmadan aşağıdaki yan kapı testi bir şey söylemezdi: her iki uç da açık
    olsaydı "yan kapı" diye bir olgu da olmazdı, ortada yalnızca yetkili bir rol
    bulunurdu.
    """
    contract, _hakedis = yan_kapi_hakedisi

    resp = await client.get(f"/subcontractor-contracts/{contract.id}", headers=sef_headers)

    assert resp.status_code == 403, (
        f"`site_chief` matriste `contracts = none` taşır; bu uç 403 vermeliydi: {resp.text}"
    )


async def test_SEF_ayni_BIRIM_FIYATI_hakedis_ucunden_artik_OKUYAMAZ(
    client, sef_headers, yan_kapi_hakedisi
):
    """YAN KAPI KAPANDI (IZN-B4b). `KARARLAR-BEKLEYEN.md` §1'in karari alan maskesiyle
    uygulandi: hakedis ucu hala 200 doner (matris dokunulmadi) ama taseron tutarlari
    `maliyet_kar` etiketlidir ve `site_chief` bu kategoriyi gizler → `null`.
    """
    _contract, hakedis = yan_kapi_hakedisi

    resp = await client.get(
        f"/subcontractor-progress-payments/{hakedis['id']}", headers=sef_headers
    )

    assert resp.status_code == 200, resp.text
    satir = resp.json()["lines"][0]
    assert satir["contract_unit_price"] is None
    assert satir["adjusted_unit_price"] is None
    assert satir["line_total"] is None
    assert resp.json()["calculation"]["gross"] is None
