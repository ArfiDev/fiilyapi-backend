"""OK-1C T1 — 🔴 BU DİLİMİN KALBİ: yetki genişlemesi DARDIR.

## Neden dar

İkame, zincir adımının onay rolünü modül kapısının YERİNE koyar. Geniş
yorumlansaydı — "onay rolünü taşıyan, o modülde ne isterse yapar" — zincirin
VARLIK SEBEBİ boşa çıkardı: muhasebe, satınalma talebini onaylayabilsin diye
talep AÇMA, DÜZENLEME ve SİLME yetkisi de kazanırdı; şantiye şefi hakedişi
imzalayabilsin diye ÖDEME İŞARETLEME ve ONAY GERİ ALMA yetkisi kazanırdı.
Bunların hiçbiri kullanıcı kararında yoktur; karar tek cümledir:

> Bir kullanıcı o adımın onay rolünü taşıyorsa, modül seviyesi yetmese bile
> **O ADIMI** onaylayabilir.

Yani genişleme üç eksende birden dardır:

| Eksen | İkame EDİLİR | İkame EDİLMEZ |
|---|---|---|
| **UÇ** | `/approve` · `/reject` | liste · detay · yazma · `submit` · `mark-paid` · `unapprove` |
| **EVRAK** | adımı bekleyen O evrak | aynı ailenin başka evrağı |
| **ADIM** | zincirin SIRADAKİ adımı | ileride/geride kalan adımlar |

## İki katmanda bekçilenir

1. **YAPISAL** (`test_ikame_kapisi_TAM_OLARAK_ALTI_operasyonda`) — rota
   tablosundan ÜRETİLİR, elle liste yazılmaz. Yeni bir uca ikame kapısı
   bağlanırsa test kırılır, kimse fark etmesin diye beklemez.
2. **DAVRANIŞSAL** — aynı aktör, aynı oturumda: `/approve` 200, komşu uçlar 403.
   Yapısal bekçi kapının BAĞLANDIĞI yeri ölçer; davranışsal bekçi kapının NE
   YAPTIĞINI.

🔴 Beklenen hata metinleri ELLE yazılmıştır (koddan ithal edilmemiştir).
"""

from decimal import Decimal

from fastapi.routing import APIRoute

from app.main import app
from app.modules.approvals.models import ApprovalRole
from tests.modules.approvals.test_ok1c_ikame import (
    _ISVEREN,
    _SATINALMA,
    _TASERON,
    _adimlari_ilerlet,
    _zincir,
)

#: 🔴 ELLE YAZILDI (`core/permissions.py:31`den ithal EDİLMEDİ).
_MODUL_KAPISI = "Bu işlem için yetkiniz yok"

#: İkame kapısının bağlanacağı TAM küme. Path parametresi adı iki ailede
#: `payment_id`, satınalmada `request_id`dir — fabrika `document_id_param` ile
#: çözer, yani kümenin kendisi yol biçiminden bağımsızdır.
_IKAME_UCLARI = frozenset(
    {
        ("POST", "/subcontractor-progress-payments/{payment_id}/approve"),
        ("POST", "/subcontractor-progress-payments/{payment_id}/reject"),
        ("POST", "/progress-payments/{payment_id}/approve"),
        ("POST", "/progress-payments/{payment_id}/reject"),
        ("POST", "/purchase-requests/{request_id}/approve"),
        ("POST", "/purchase-requests/{request_id}/reject"),
    }
)

#: 🔴 BUGÜN ÖLÇÜLDÜ (2026-08-22, taban `d888591`): iki modülün izin kapısı
#: taşıyan operasyon sayıları. İkame kapısı `require_permission`ın YERİNE
#: geçtiği için bu sayılar T2'den sonra da AYNI kalmalıdır.
#: IZN-B2: `mark-paid` ×2 ve `select-and-order` onay eylemi olarak sayfa ONAYLAR kapısına
#: (`require_pages`) taşındı; MODÜL kapısı taşıyan uç sayısı 28→26 ve 23→22 oldu (zincir
#: ikamesi aynı iki ucu — approve/reject — taşımaya devam eder).
#: SIL-B1: DELETE uçları (hakediş ×2, talep, teklif) `require_system_admin` kapısına taşındı (MODÜL
#: kapısı taşımaz): 26→24 ve 22→20.
#: IZN-B5b: hakediş YAZMA uçları (işveren 5 + taşeron 5) sayfa Düzenler kapısına (`require_pages`)
#: taşındı → MODÜL kapısı taşıyan uç sayısı 24→14.
_MODUL_OPERASYON_SAYISI = {"progress_payments": 14, "procurement": 20}

#: Ölçümün o günkü DÖKÜMÜ — yalnız hata mesajında farkı basmak için tutulur;
#: iddia SAYIYA yapılır (aşağıda), kümeye değil.
_OLCULEN_UCLAR: dict[str, frozenset[tuple[str, str]]] = {
    "progress_payments": frozenset(
        {
            ("GET", "/progress-payments"),
            ("GET", "/progress-payments/{payment_id}"),
            ("GET", "/projects/{project_id}/progress-payments/diary-suggestion"),
            ("GET", "/projects/{project_id}/progress-payments/summary"),
            ("GET", "/subcontractor-contracts/{contract_id}/progress-payments/diary-suggestion"),
            ("GET", "/subcontractor-progress-payments"),
            ("GET", "/subcontractor-progress-payments/summary"),
            ("GET", "/subcontractor-progress-payments/{payment_id}"),
            ("PATCH", "/progress-payments/{payment_id}"),
            ("PATCH", "/subcontractor-progress-payments/{payment_id}"),
            ("POST", "/progress-payments/{payment_id}/approve"),
            ("POST", "/progress-payments/{payment_id}/refresh-prices"),
            ("POST", "/progress-payments/{payment_id}/reject"),
            ("POST", "/progress-payments/{payment_id}/submit"),
            ("POST", "/progress-payments/{payment_id}/unapprove"),
            ("POST", "/projects/{project_id}/progress-payments"),
            ("POST", "/subcontractor-contracts/{contract_id}/progress-payments"),
            ("POST", "/subcontractor-progress-payments/{payment_id}/approve"),
            ("POST", "/subcontractor-progress-payments/{payment_id}/refresh-prices"),
            ("POST", "/subcontractor-progress-payments/{payment_id}/reject"),
            ("POST", "/subcontractor-progress-payments/{payment_id}/submit"),
            ("POST", "/subcontractor-progress-payments/{payment_id}/unapprove"),
            ("PUT", "/progress-payments/{payment_id}/lines"),
            ("PUT", "/subcontractor-progress-payments/{payment_id}/lines"),
        }
    ),
    "procurement": frozenset(
        {
            ("GET", "/purchase-orders"),
            ("GET", "/purchase-orders/{order_id}"),
            ("GET", "/purchase-requests"),
            ("GET", "/purchase-requests/{request_id}"),
            ("GET", "/purchase-requests/{request_id}/quotes"),
            ("GET", "/purchase-requests/{request_id}/quotes/export.xlsx"),
            ("GET", "/purchasing/summary"),
            ("GET", "/suppliers"),
            ("GET", "/suppliers/{supplier_id}"),
            ("PATCH", "/purchase-orders/{order_id}"),
            ("PATCH", "/purchase-requests/{request_id}"),
            ("PATCH", "/purchase-requests/{request_id}/quotes/{quote_id}"),
            ("PATCH", "/suppliers/{supplier_id}"),
            ("POST", "/purchase-orders"),
            ("POST", "/purchase-requests"),
            ("POST", "/purchase-requests/{request_id}/approve"),
            ("POST", "/purchase-requests/{request_id}/quotes"),
            ("POST", "/purchase-requests/{request_id}/reject"),
            ("POST", "/purchase-requests/{request_id}/submit"),
            ("POST", "/suppliers"),
        }
    ),
}


def _api_rotalari(rotalar) -> list[APIRoute]:
    """🔴 FastAPI 0.141'de `app.routes` doğrudan `APIRoute` VERMEZ.

    Ara katman `_IncludedRouter`dır ve `.original_router.routes` ile açılır;
    özyineleme bu yüzden gereklidir (kanıtlandı, 2026-08-22).
    """
    cikti: list[APIRoute] = []
    for rota in rotalar:
        if isinstance(rota, APIRoute):
            cikti.append(rota)
        elif type(rota).__name__ == "_IncludedRouter":
            cikti.extend(_api_rotalari(rota.original_router.routes))
        elif hasattr(rota, "routes"):
            cikti.extend(_api_rotalari(rota.routes))
    return cikti


def _kapilar(rota: APIRoute) -> list[dict[str, object]]:
    """Rotanın taşıdığı izin kapıları — CLOSURE serbest değişkenlerinden.

    🔴 Ayrım FONKSİYON ADINDAN yapılmaz: `require_permission` da
    `require_permission_or_chain_step` de içeride `_check` adında bir kapanış
    döndürebilir. Ölçülen şey kapanışın TAŞIDIĞI değerlerdir:
    `module_key` + `min_level` ⇒ izin kapısı; bunlara EK olarak
    `document_type` + `document_id_param` ⇒ İKAME kapısı.
    """
    bulunan: list[dict[str, object]] = []
    for bagimlilik in rota.dependant.dependencies:
        cagri = bagimlilik.call
        kod = getattr(cagri, "__code__", None)
        kapanis = getattr(cagri, "__closure__", None)
        if kod is None or not kapanis:
            continue
        serbest = {
            ad: hucre.cell_contents for ad, hucre in zip(kod.co_freevars, kapanis, strict=True)
        }
        if "module_key" in serbest and "min_level" in serbest:
            bulunan.append(serbest)
    return bulunan


def _ikame_kapisi_mi(serbest: dict[str, object]) -> bool:
    return "document_type" in serbest and "document_id_param" in serbest


def _operasyonlar() -> list[tuple[str, str, list[dict[str, object]]]]:
    """(metot, yol, kapılar) üçlüleri — rota tablosundan ÜRETİLİR."""
    cikti: list[tuple[str, str, list[dict[str, object]]]] = []
    for rota in _api_rotalari(app.routes):
        kapilar = _kapilar(rota)
        for metot in sorted(rota.methods - {"HEAD", "OPTIONS"}):
            cikti.append((metot, rota.path, kapilar))
    return cikti


def _modul_operasyonlari(modul: str) -> set[tuple[str, str]]:
    return {
        (metot, yol)
        for metot, yol, kapilar in _operasyonlar()
        if any(kapi["module_key"] == modul for kapi in kapilar)
    }


def _ikame_operasyonlari() -> set[tuple[str, str]]:
    return {
        (metot, yol)
        for metot, yol, kapilar in _operasyonlar()
        if any(_ikame_kapisi_mi(kapi) for kapi in kapilar)
    }


# --------------------------------------------------------------------------- #
# 9. YAPISAL BEKÇİ — rota tablosundan üretilir
# --------------------------------------------------------------------------- #


async def test_iki_modulun_operasyon_sayisi_DEGISMEDI():
    """İkame kapısı `require_permission`ın YERİNE geçer, ÜSTÜNE değil.

    Sayılar bugün ölçüldü (2026-08-22): `progress_payments` 28 ·
    `procurement` 23. T2 kapıyı EKLESEYDİ (yerine koymak yerine) sayılar
    değişmezdi ama bu test yine de bir tabandır: 29. uç eklendiğinde farkı
    mesajda görürüz.
    """
    for modul, beklenen_sayi in _MODUL_OPERASYON_SAYISI.items():
        olculen = _modul_operasyonlari(modul)
        fark = olculen ^ _OLCULEN_UCLAR[modul]
        assert len(olculen) == beklenen_sayi, (
            f"{modul}: {beklenen_sayi} operasyon bekleniyordu, {len(olculen)} bulundu. "
            f"Fark: {sorted(fark)}"
        )


async def test_ikame_kapisi_TAM_OLARAK_ALTI_operasyonda():
    """🔴 T1'de `app/modules/approvals/gate.py` HENÜZ YOKTUR — bu test KIRMIZI.

    Kapı tespiti `gate.py` İTHAL EDİLMEDEN, yalnız closure serbest
    değişkenlerinden yapılır; böylece hata çıplak bir `ImportError` değil
    "ikame kapısı hiçbir uçta bulunamadı" cümlesidir.
    """
    olculen = _ikame_operasyonlari()

    assert olculen, (
        "İkame kapısı HİÇBİR UÇTA bulunamadı — `require_permission_or_chain_step` "
        "henüz hiçbir rotaya bağlanmamış (T1'de beklenen durum budur). "
        f"Bağlanması gereken küme: {sorted(_IKAME_UCLARI)}"
    )
    assert olculen == set(_IKAME_UCLARI), (
        f"ikame kapısı YANLIŞ kümede. Fazla: {sorted(olculen - _IKAME_UCLARI)} · "
        f"Eksik: {sorted(_IKAME_UCLARI - olculen)}"
    )


async def test_KALAN_KIRK_BES_operasyon_ikame_kapisi_TASIMAZ():
    """Dar kapsamın yapısal karşılığı: iki modülün geri kalanı DOKUNULMAMIŞTIR."""
    tum_modul_uclari = _modul_operasyonlari("progress_payments") | _modul_operasyonlari(
        "procurement"
    )
    kalan = tum_modul_uclari - _IKAME_UCLARI

    assert len(kalan) == 28, f"kalan operasyon sayısı 28 değil {len(kalan)}: {sorted(kalan)}"
    sizanlar = kalan & _ikame_operasyonlari()
    assert not sizanlar, f"ikame kapısı onay/ret DIŞINDAKİ uçlara sızmış: {sorted(sizanlar)}"


async def test_ikame_kapisi_BASKA_MODULE_baglanmamis():
    """İkame yalnız iki evrak modülünündür; üçüncü bir modüle bulaşamaz."""
    izinli = {"progress_payments", "procurement"}
    yabancilar = {
        (metot, yol, kapi["module_key"])
        for metot, yol, kapilar in _operasyonlar()
        for kapi in kapilar
        if _ikame_kapisi_mi(kapi) and kapi["module_key"] not in izinli
    }

    assert not yabancilar, f"ikame kapısı yabancı modüle bağlanmış: {sorted(yabancilar)}"


# --------------------------------------------------------------------------- #
# 10. DAR KAPSAM — DAVRANIŞ (`procurement` × `accounting`)
# --------------------------------------------------------------------------- #


async def test_MUHASEBE_satinalmada_YALNIZ_onay_ucunu_acar(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Matriste `procurement: accounting = none` — muhasebe hiçbir ucu göremez.

    Zincirin 3. adımı ona ait olduğunda AÇILAN TEK ŞEY `/approve`tır. Komşu
    yedi uç 403 kalmalıdır; kalmazsa ikame bir modül yetkisine dönüşmüştür.

    🔴 Kapının GERÇEKTEN açıldığı aynı testte gösterilir: 403'lerin hepsi
    "ikame hiç çalışmıyor" dünyasında da geçerdi.
    """
    yaratan = await aktor_fabrikasi("dar-t10-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "dar-t10-muh@ok1c.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    basliklar = await giris("dar-t10-muh@ok1c.co")
    document_id, proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )
    await _zincir(seeded_db, _SATINALMA, document_id, yaratan, Decimal("10000.00"))
    await _adimlari_ilerlet(
        seeded_db,
        aktor_fabrikasi,
        _SATINALMA,
        document_id,
        [ApprovalRole.procurement, ApprovalRole.project_manager],
        etiket="dar-t10",
    )

    kapali = [
        ("GET", "/purchase-requests", None),
        ("GET", f"/purchase-requests/{document_id}", None),
        ("POST", "/purchase-requests", {"project_id": str(proje.id)}),
        ("PATCH", f"/purchase-requests/{document_id}", {"priority": "normal"}),
        ("POST", f"/purchase-requests/{document_id}/submit", None),
        ("GET", f"/purchase-requests/{document_id}/quotes", None),
    ]
    for metot, yol, govde in kapali:
        yanit = await client.request(metot, yol, json=govde, headers=basliklar)
        assert yanit.status_code == 403, f"{metot} {yol} → {yanit.status_code}: {yanit.text}"
        assert yanit.json()["detail"] == _MODUL_KAPISI, f"{metot} {yol}: {yanit.text}"

    # SIL-B1: DELETE modül kapısı değil Sistem Yöneticisi kapısıdır (farklı 403 gövdesi).
    silme = await client.delete(f"/purchase-requests/{document_id}", headers=basliklar)
    assert silme.status_code == 403
    assert silme.json()["detail"] == "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"

    acik = await client.post(f"/purchase-requests/{document_id}/approve", headers=basliklar)
    assert acik.status_code == 200, acik.text


# --------------------------------------------------------------------------- #
# 11. DAR KAPSAM — DAVRANIŞ (`progress_payments` × `site_chief`)
# --------------------------------------------------------------------------- #


async def test_SEFIN_hakedis_ailesinde_YALNIZ_KENDI_ADIMI_acilir(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 ÖLÇÜLMÜŞ DÜZELTME: `progress_payments: site_chief = DRAFT`, `none` DEĞİL.

    Yani listeleme/detay/oluşturma/düzenleme/silme bu role BUGÜN ZATEN AÇIKTIR
    ve onları "403 olmalı" diye test etmek YANLIŞ olurdu. İkamenin AÇMADIĞINI
    kanıtlayacak uçlar `draft`ın ÜSTÜNDEKİLERDİR (`mark-paid` → `approve`,
    `unapprove` → `admin`).

    İki eksen birlikte ölçülür:
      * **UÇ ekseni** — zinciri OLAN evrakta bile `mark-paid`/`unapprove` 403;
        ikame "bu evrakta her şeyi yapabilir" DEMEK DEĞİLDİR.
      * **EVRAK ekseni** — aktörün adımı OLMAYAN ikinci hakedişte `/approve`
        403; ikame EVRAK bazındadır, rol bazında değil.
    """
    yaratan = await aktor_fabrikasi("dar-t11-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "dar-t11-sef@ok1c.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("dar-t11-sef@ok1c.co")

    # (A) Aktörün adımını BEKLEYEN taşeron hakedişi.
    kendi_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, kendi_id, yaratan)
    # (B) İşveren hakedişi — zinciri var ama ilk adımı `accounting`tir.
    isveren_id, _ = await evrak_fabrikasi(_ISVEREN, creator=yaratan)
    await _zincir(seeded_db, _ISVEREN, isveren_id, yaratan)
    # (C) Adımı İLERLEMİŞ ikinci taşeron hakedişi (sıradaki adım PM'in).
    baska_id, _ = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, baska_id, yaratan)
    await _adimlari_ilerlet(
        seeded_db,
        aktor_fabrikasi,
        _TASERON,
        baska_id,
        [ApprovalRole.site_chief],
        etiket="dar-t11",
    )

    kapali = [
        f"/subcontractor-progress-payments/{kendi_id}/mark-paid",
        f"/subcontractor-progress-payments/{kendi_id}/unapprove",
        f"/progress-payments/{isveren_id}/mark-paid",
        f"/progress-payments/{isveren_id}/unapprove",
        f"/subcontractor-progress-payments/{baska_id}/approve",
    ]
    for yol in kapali:
        yanit = await client.post(yol, headers=basliklar)
        assert yanit.status_code == 403, f"POST {yol} → {yanit.status_code}: {yanit.text}"
        assert yanit.json()["detail"] == _MODUL_KAPISI, f"POST {yol}: {yanit.text}"

    acik = await client.post(
        f"/subcontractor-progress-payments/{kendi_id}/approve", headers=basliklar
    )
    assert acik.status_code == 200, acik.text
