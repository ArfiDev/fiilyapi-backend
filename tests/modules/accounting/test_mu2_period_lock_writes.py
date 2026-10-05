"""MU-2 T3 — 🔴 KAPALI DÖNEM YAZMAYA KAPALIDIR: ALTI giriş noktasının HEPSİ.

Kusurun her biçimi ayrı ayrı kilitlenir; "bir yerde denetim var" YETMEZ, çünkü
kapatılmayan TEK yol bütün yasağı anlamsız kılar:

| # | Uç | Servis |
|---|---|---|
| 1 | `POST /journal-entries` | `service.create_entry` |
| 2 | `PATCH /journal-entries/{id}` | `service.update_entry` |
| 3 | `DELETE /journal-entries/{id}` | silme motoru (`silme/service.sil`): dönem kilidi ATLANIR |
| 4 | `PUT /journal-entries/{id}/lines` | `service.replace_lines` |
| 5 | `POST /journal-entries/{id}/post` | `state_service.perform_transition` |
| 6 | `POST /journal-entries/{id}/reverse` | `state_service.perform_transition` |

## 🔴 İki özel durum — yarım kapatılan delikler

**`update` ÇİFT KONTROL ister.** `entry_date` değişiyorsa HEM ESKİ HEM YENİ
dönem denetlenir. Yalnız birine bakmak deliği yarım kapatır:
* yalnız YENİ döneme bakılsaydı, kapalı bir dönemdeki fiş açık bir aya
  TAŞINABİLİRDİ — mali iz kapalı dönemden sessizce boşalırdı;
* yalnız ESKİ döneme bakılsaydı, açık bir dönemdeki fiş KAPALI bir aya
  SOKULABİLİRDİ — kapanmış mizan geçmişe dönük değişirdi.

**`reverse` İKİ döneme dokunur.** Orijinalin dönemi VE stornonun kendi dönemi
(`_build_reversal` `timezone.today()` kullanır → BUGÜNÜN dönemi). İkisi de
409 üretir; istisna YOKTUR.

## 🔴 NEGATİF KONTROL

`test_ACIK_donemde_alti_islem_de_CALISIR` olmadan bu dosya, "her şeyi
reddeden" bir denetimle de yemyeşil kalırdı.
"""

from datetime import date
from decimal import Decimal

import pytest
from httpx import AsyncClient

from app.core.timezone import today
from app.modules.accounting.models import (
    AccountingPeriodStatus,
    ChartAccountType,
    JournalEntryStatus,
)
from tests._silme_yardimci import sil_aile

from ._journal import YOL, govde, iki_yaprak, satir

#: Kapalı dönemin ayı. `fis_fabrikasi` varsayılan tarihi (2026-07-17) ile aynı
#: dönemdir — testler fişi orada kurar, dönemi kapatır ve kapıyı yoklar.
KAPALI_TARIH = date(2026, 7, 17)
KAPALI_YIL, KAPALI_AY = KAPALI_TARIH.year, KAPALI_TARIH.month

#: Kapalı dönemin BİTİŞİĞİNDEKİ açık ay — `update`in "kapalıya sokma" yönü için.
ACIK_TARIH = date(2026, 9, 4)


async def _fis(fis_fabrikasi, hesap_fabrikasi, *, status, entry_date, sira=0):  # noqa: ANN001, ANN202
    kasa, saticilar = await iki_yaprak(hesap_fabrikasi, sira)
    return await fis_fabrikasi(
        [(kasa, "1000.00", "0"), (saticilar, "0", "1000.00")],
        status=status,
        entry_date=entry_date,
    )


# --------------------------------------------------------------------------- #
# 1 — POST /journal-entries
# --------------------------------------------------------------------------- #


async def test_1_OLUSTURMA_kapali_doneme_409(
    client: AsyncClient, muhasebe_headers: dict[str, str], hesap_fabrikasi, donem_fabrikasi
) -> None:
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    kasa, saticilar = await iki_yaprak(hesap_fabrikasi)
    resp = await client.post(
        YOL,
        json=govde(kasa, saticilar, entry_date=KAPALI_TARIH.isoformat()),
        headers=muhasebe_headers,
    )
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# 2 — PATCH /journal-entries/{id} — İKİ YÖN
# --------------------------------------------------------------------------- #


async def test_2a_GUNCELLEME_kapali_donemdeki_fiste_409(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    """ESKİ dönem kapalı — açıklama düzeltmesi bile geçmez."""
    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.draft, entry_date=KAPALI_TARIH
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await client.patch(
        f"{YOL}/{fis.id}", json={"description": "Yeni açıklama"}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text


async def test_2b_GUNCELLEME_kapali_donemden_CIKARAMAZ(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    """🔴 ESKİ dönem denetimi: kapalı aydaki fiş açık aya TAŞINAMAZ.

    Yalnız YENİ döneme bakan bir uygulama bu testte kırmızıya döner — mali iz
    kapalı dönemden sessizce boşalırdı.
    """
    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.draft, entry_date=KAPALI_TARIH
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await client.patch(
        f"{YOL}/{fis.id}", json={"entry_date": ACIK_TARIH.isoformat()}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text


async def test_2c_GUNCELLEME_kapali_doneme_SOKAMAZ(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    """🔴 YENİ dönem denetimi: açık aydaki fiş kapalı aya SOKULAMAZ.

    Yalnız ESKİ döneme bakan bir uygulama bu testte kırmızıya döner — kapanmış
    mizan geçmişe dönük değişirdi.
    """
    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.draft, entry_date=ACIK_TARIH
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await client.patch(
        f"{YOL}/{fis.id}", json={"entry_date": KAPALI_TARIH.isoformat()}, headers=muhasebe_headers
    )
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# 3 — DELETE /journal-entries/{id}
# --------------------------------------------------------------------------- #


async def test_3_SILME_kapali_doneme_de_calisir_SIL_B2(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    """SIL-B2 (K2): kapalı dönem silmeyi DURDURMAZ. Dönem kilidi yalnız silme yolunda atlanır;
    fiş gider, önizleme kapalı dönemi bildirir, denetim satırı "dönem kilidi atlandı" yazar."""
    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.draft, entry_date=KAPALI_TARIH
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await sil_aile(client, admin_headers, "journal_entry", fis.id)
    assert resp.status_code == 204, resp.text


# --------------------------------------------------------------------------- #
# 4 — PUT /journal-entries/{id}/lines
# --------------------------------------------------------------------------- #


async def test_4_BACAK_DEGISTIRME_kapali_doneme_409(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    kasa = await hesap_fabrikasi("100", name="Kasa", account_type=ChartAccountType.asset)
    saticilar = await hesap_fabrikasi(
        "320", name="Satıcılar", account_type=ChartAccountType.liability
    )
    fis = await fis_fabrikasi(
        [(kasa, "1000.00", "0"), (saticilar, "0", "1000.00")],
        status=JournalEntryStatus.draft,
        entry_date=KAPALI_TARIH,
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await client.put(
        f"{YOL}/{fis.id}/lines",
        json={"lines": [satir(kasa.id, debit="5.00"), satir(saticilar.id, credit="5.00")]},
        headers=muhasebe_headers,
    )
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# 5 — POST /journal-entries/{id}/post
# --------------------------------------------------------------------------- #


async def test_5_KAYITLASTIRMA_kapali_doneme_409(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.draft, entry_date=KAPALI_TARIH
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await client.post(f"{YOL}/{fis.id}/post", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# 6 — POST /journal-entries/{id}/reverse — İKİ DÖNEM
# --------------------------------------------------------------------------- #


async def test_6a_STORNO_ORIJINALIN_donemi_kapaliysa_409(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.posted, entry_date=KAPALI_TARIH
    )
    await donem_fabrikasi(KAPALI_YIL, KAPALI_AY)
    resp = await client.post(f"{YOL}/{fis.id}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text


async def test_6b_STORNONUN_KENDI_donemi_kapaliysa_409(
    client: AsyncClient,
    muhasebe_headers: dict[str, str],
    hesap_fabrikasi,
    fis_fabrikasi,
    donem_fabrikasi,
) -> None:
    """🔴 Orijinal AÇIK bir dönemdedir; kapalı olan BUGÜNÜN dönemidir.

    `_build_reversal` stornoyu `timezone.today()`ye yazar. Yalnız orijinalin
    dönemine bakan bir uygulama bu testte kırmızıya döner: kapalı bir aya
    taptaze bir `posted` fiş doğardı.
    """
    bugun = today()
    orijinal_tarih = date(2020, 1, 15)
    assert (orijinal_tarih.year, orijinal_tarih.month) != (bugun.year, bugun.month)

    fis = await _fis(
        fis_fabrikasi, hesap_fabrikasi, status=JournalEntryStatus.posted, entry_date=orijinal_tarih
    )
    await donem_fabrikasi(bugun.year, bugun.month)
    resp = await client.post(f"{YOL}/{fis.id}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text


# --------------------------------------------------------------------------- #
# 🔴 NEGATİF KONTROL — yasak her şeyi kapatmıyor
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("donem_var", [False, True])
async def test_ACIK_donemde_alti_islem_de_CALISIR(
    client: AsyncClient,
    admin_headers: dict[str, str],
    hesap_fabrikasi,
    donem_fabrikasi,
    donem_var: bool,
) -> None:
    """Altı yol da 409 ALMAZ — hem dönem satırı YOKKEN hem AÇIK satır varken.

    Satır yokken de geçmesi ŞARTTIR: dönem kayıtları proaktif açılmaz (YAGNI)
    ve "satır yok" hâli AÇIK sayılır. `donem_var` iki hâli de kilitler.
    """
    bugun = today()
    if donem_var:
        await donem_fabrikasi(bugun.year, bugun.month, status=AccountingPeriodStatus.open)

    kasa, saticilar = await iki_yaprak(hesap_fabrikasi)
    # 1 — oluştur
    resp = await client.post(
        YOL, json=govde(kasa, saticilar, entry_date=bugun.isoformat()), headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    fis_id = resp.json()["id"]

    # 2 — güncelle
    resp = await client.patch(
        f"{YOL}/{fis_id}", json={"description": "Düzeltildi"}, headers=admin_headers
    )
    assert resp.status_code == 200, resp.text

    # 4 — bacakları değiştir
    resp = await client.put(
        f"{YOL}/{fis_id}/lines",
        json={"lines": [satir(kasa.id, debit="7.00"), satir(saticilar.id, credit="7.00")]},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    assert Decimal(resp.json()["total_debit"]) == Decimal("7.00")

    # 5 — kayıtlaştır
    resp = await client.post(f"{YOL}/{fis_id}/post", headers=admin_headers)
    assert resp.status_code == 200, resp.text

    # 6 — storno
    resp = await client.post(f"{YOL}/{fis_id}/reverse", headers=admin_headers)
    assert resp.status_code == 201, resp.text

    # 3 — sil (yeni bir TASLAK üzerinde; `posted` fiş zaten silinemez)
    kasa2, saticilar2 = await iki_yaprak(hesap_fabrikasi, sira=1)
    resp = await client.post(
        YOL, json=govde(kasa2, saticilar2, entry_date=bugun.isoformat()), headers=admin_headers
    )
    assert resp.status_code == 201, resp.text
    resp = await sil_aile(client, admin_headers, "journal_entry", resp.json()["id"])
    assert resp.status_code == 204, resp.text
