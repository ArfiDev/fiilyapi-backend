"""MU-1 T3b — `GET /journal` koşan bakiyesi (spec §6d, §6e · E8:101-106).

E8'in tablosu **SATIR bazlıdır**, fiş bazlı değil; altıncı sütunu (`Bakiye`)
mockup'ta göstermeliktir (hiçbir aritmetiği tutmaz) ve kural bu yüzden
YAPIDAN okunur.

## Bu dosyanın kilitlediği kararlar

1. 🔴 **`carried_balance` ŞARTTIR** — pencere ÖNCESİNDEKİ satırların toplamı.
   Olmasaydı ay değişince ya da sayfa atlanınca seri sıfırdan başlar ve
   anlamsız bir bakiye kolonu doğardı.
2. 🔴 **Birikim ESKİDEN YENİYE, gösterim YENİDEN ESKİYE.** Pencere fonksiyonu
   ASC koşar, yanıt DESC döner (E8 tarih DESC'tir).
3. 🔴 **Pencere fonksiyonu ALT SORGUDA, `LIMIT` DIŞTA** (R9). Ters olsaydı her
   sayfa sıfırdan başlar ve 2. sayfanın bakiyesi yalan olurdu.
4. 🔴 **Sıralama DÖRT parçalıdır** ve sonuncusu `jl.id`dir (R8): `func.now()`
   işlem başına SABİTTİR, aynı işlemde yazılan fişlerin `created_at`i EŞİTTİR
   → `jl.id` olmadan sayfalama satır tekrarlar/atlar.
5. **İşaret HAM `net`tir** (borç `+`, alacak `−`) ve türe göre ÇEVRİLMEZ: hesap
   süzgeci opsiyoneldir (E8:96 `Tüm Hesaplar`), karışık kümede tür-bazlı işaret
   tanımsızdır.
6. **`POSTING_STATUSES` burada da geçerlidir**: `draft` deftere GİRMEZ.
"""

from datetime import date
from decimal import Decimal

from app.core.timezone import today
from app.modules.accounting.models import ChartAccountType, JournalEntryStatus

_YOL = "/journal"


async def _hesaplar(hesap_fabrikasi):
    kasa = await hesap_fabrikasi("100", name="Kasa", account_type=ChartAccountType.asset)
    saticilar = await hesap_fabrikasi(
        "320", name="Satıcılar", account_type=ChartAccountType.liability
    )
    return kasa, saticilar


async def _temmuz_senaryosu(hesap_fabrikasi, fis_fabrikasi):
    """Haziran'da bir devir + Temmuz'da iki hareket.

    Kasa açısından: devir `+1000`, `05.07` `+200`, `10.07` `−50`.
    """
    kasa, saticilar = await _hesaplar(hesap_fabrikasi)
    await fis_fabrikasi(
        [(kasa, "1000.00", "0"), (saticilar, "0", "1000.00")],
        entry_date=date(2026, 6, 20),
        description="Haziran devri",
    )
    await fis_fabrikasi(
        [(kasa, "200.00", "0"), (saticilar, "0", "200.00")],
        entry_date=date(2026, 7, 5),
        description="Temmuz tahsilat",
    )
    await fis_fabrikasi(
        [(kasa, "0", "50.00"), (saticilar, "50.00", "0")],
        entry_date=date(2026, 7, 10),
        description="Temmuz ödeme",
    )
    return kasa, saticilar


# --------------------------------------------------------------------------- #
# Devir + koşan bakiye
# --------------------------------------------------------------------------- #


async def test_devir_pencere_ONCESINDEN_gelir_ve_kosan_bakiye_UZERINE_BIRIKIR(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 §6d'nin tamamı TEK iddiada: devir + ASC birikim + DESC gösterim."""
    kasa, _ = await _temmuz_senaryosu(hesap_fabrikasi, fis_fabrikasi)

    resp = await client.get(
        f"{_YOL}?year=2026&month=7&account_id={kasa.id}", headers=muhasebe_headers
    )
    assert resp.status_code == 200, resp.text
    govde = resp.json()

    assert set(govde) == {"items", "total", "limit", "offset", "carried_balance"}
    assert Decimal(govde["carried_balance"]) == Decimal("1000.00")
    assert govde["total"] == 2

    # Gösterim tarih DESC: önce 10.07, sonra 05.07.
    assert [s["entry_date"] for s in govde["items"]] == ["2026-07-10", "2026-07-05"]
    # Birikim ASC: 1000 → 1200 → 1150. DESC listede 1150 üstte, 1200 altta.
    assert [Decimal(s["running_balance"]) for s in govde["items"]] == [
        Decimal("1150.00"),
        Decimal("1200.00"),
    ]


async def test_devir_YOKKEN_sifirdan_baslar(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """Satırı olmayan pencere öncesinde `SUM()` **NULL** döner, `0` değil —
    `COALESCE` olmasaydı devir alanı BOŞ basardı (R12)."""
    kasa, saticilar = await _hesaplar(hesap_fabrikasi)
    await fis_fabrikasi(
        [(kasa, "40.00", "0"), (saticilar, "0", "40.00")], entry_date=date(2026, 7, 2)
    )
    govde = (
        await client.get(f"{_YOL}?year=2026&month=7&account_id={kasa.id}", headers=muhasebe_headers)
    ).json()
    assert Decimal(govde["carried_balance"]) == Decimal("0")
    assert Decimal(govde["items"][0]["running_balance"]) == Decimal("40.00")


async def test_isaret_HAM_nettir_ture_gore_cevrilmez(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 `320` PASİFTİR ve hesap planı kartında `+50` basar (K3 işareti), ama
    DEFTERDE ham `net` görünür: alacak `−`dir.

    Çevrilseydi `Tüm Hesaplar` seçiliyken karışık türler tek bir sütunda
    toplanamaz ve koşan bakiye tanımsız kalırdı.
    """
    _, saticilar = await _hesaplar(hesap_fabrikasi)
    kasa2 = await hesap_fabrikasi("102", name="Bankalar")
    await fis_fabrikasi(
        [(kasa2, "50.00", "0"), (saticilar, "0", "50.00")], entry_date=date(2026, 7, 3)
    )
    govde = (
        await client.get(
            f"{_YOL}?year=2026&month=7&account_id={saticilar.id}", headers=muhasebe_headers
        )
    ).json()
    assert Decimal(govde["items"][0]["running_balance"]) == Decimal("-50.00")


async def test_satir_alanlari_hesap_KODU_ve_ADINI_tasir(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """E8:102 `Hesap Kodu` + E8:113'ün iki satırlı açıklama hücresi."""
    kasa, _ = await _temmuz_senaryosu(hesap_fabrikasi, fis_fabrikasi)
    satir = (
        await client.get(f"{_YOL}?year=2026&month=7&account_id={kasa.id}", headers=muhasebe_headers)
    ).json()["items"][0]
    assert set(satir) == {
        "entry_id",
        "entry_date",
        "entry_status",
        "account_id",
        "account_code",
        "account_name",
        "description",
        "detail_note",
        "debit",
        "credit",
        "running_balance",
        "source_type",  # IZN-B4b onarımı (eklemeli): bordro satırı `maas_kisisel` ile de gizlenir
    }
    assert satir["account_code"] == "100"
    assert satir["account_name"] == "Kasa"


# --------------------------------------------------------------------------- #
# 🔴 R8 — sayfalama determinizmi
# --------------------------------------------------------------------------- #


async def test_ayni_islemdeki_UC_FIS_sayfa_sinirinda_TEKRARLAMAZ_ATLAMAZ(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 R8'in doğrudan ölçümü.

    Üç fiş AYNI işlemde ve AYNI tarihle yazılır: `func.now()` işlem başına
    SABİT olduğu için üçünün de `created_at`i EŞİTTİR. Sıralamanın son parçası
    `jl.id` olmasaydı Postgres satırları keyfî sırada döndürür, `LIMIT/OFFSET`
    ile sayfalanan küme satır TEKRARLAR ya da ATLARDI — ve koşan bakiye
    sayfadan sayfaya oynardı.

    İddia hem KAPSAMI (altı satırın hepsi) hem TEKİLLİĞİ (tekrar yok) ölçer.
    """
    kasa, saticilar = await _hesaplar(hesap_fabrikasi)
    for sira in range(3):
        await fis_fabrikasi(
            [(kasa, "10.00", "0"), (saticilar, "0", "10.00")],
            entry_date=date(2026, 7, 8),
            description=f"Aynı gün {sira}",
        )

    toplanan: list[tuple[str, str]] = []
    for offset in (0, 2, 4):
        govde = (
            await client.get(
                f"{_YOL}?year=2026&month=7&limit=2&offset={offset}", headers=muhasebe_headers
            )
        ).json()
        assert govde["total"] == 6
        toplanan.extend((s["entry_id"], s["account_code"]) for s in govde["items"])

    assert len(toplanan) == 6
    assert len(set(toplanan)) == 6


async def test_kosan_bakiye_SAYFADAN_SAYFAYA_oynamaz(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 R9: pencere fonksiyonu `LIMIT`ten SONRA hesaplansaydı her sayfa
    sıfırdan başlardı. Sayfalanmış seri, sayfalanmamış seriyle BİREBİR aynı
    olmalıdır."""
    kasa, _ = await _temmuz_senaryosu(hesap_fabrikasi, fis_fabrikasi)
    tek_sayfa = (
        await client.get(f"{_YOL}?year=2026&month=7&account_id={kasa.id}", headers=muhasebe_headers)
    ).json()["items"]

    parcali = []
    for offset in (0, 1):
        parcali.extend(
            (
                await client.get(
                    f"{_YOL}?year=2026&month=7&account_id={kasa.id}&limit=1&offset={offset}",
                    headers=muhasebe_headers,
                )
            ).json()["items"]
        )
    assert [s["running_balance"] for s in parcali] == [s["running_balance"] for s in tek_sayfa]


# --------------------------------------------------------------------------- #
# Süzgeçler
# --------------------------------------------------------------------------- #


async def test_taslak_fis_DEFTERE_girmez(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    kasa, saticilar = await _hesaplar(hesap_fabrikasi)
    await fis_fabrikasi(
        [(kasa, "70.00", "0"), (saticilar, "0", "70.00")],
        entry_date=date(2026, 7, 4),
        status=JournalEntryStatus.draft,
    )
    govde = (await client.get(f"{_YOL}?year=2026&month=7", headers=muhasebe_headers)).json()
    assert govde["total"] == 0


async def test_status_suzgeci_tek_durumu_daraltir(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    kasa, saticilar = await _hesaplar(hesap_fabrikasi)
    await fis_fabrikasi(
        [(kasa, "10.00", "0"), (saticilar, "0", "10.00")], entry_date=date(2026, 7, 6)
    )
    await fis_fabrikasi(
        [(kasa, "20.00", "0"), (saticilar, "0", "20.00")],
        entry_date=date(2026, 7, 7),
        status=JournalEntryStatus.reversed,
    )
    hepsi = (await client.get(f"{_YOL}?year=2026&month=7", headers=muhasebe_headers)).json()
    assert hepsi["total"] == 4

    yalniz = (
        await client.get(f"{_YOL}?year=2026&month=7&status=reversed", headers=muhasebe_headers)
    ).json()
    assert yalniz["total"] == 2


async def test_varsayilan_donem_BUGUNUN_ayidir(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 K6 sınır çağrısı: varsayılan `timezone.today()`nin ayıdır.

    `date.today()` sunucunun yerel saatini (Railway'de UTC) okurdu ve ayın son
    gecesi defter YANLIŞ aya bakardı.
    """
    bugun = today()
    kasa, saticilar = await _hesaplar(hesap_fabrikasi)
    await fis_fabrikasi([(kasa, "5.00", "0"), (saticilar, "0", "5.00")], entry_date=bugun)

    govde = (await client.get(_YOL, headers=muhasebe_headers)).json()
    assert govde["total"] == 2


async def test_limit_tavani_KIRPILMAZ_422dir(client, muhasebe_headers) -> None:
    assert (await client.get(f"{_YOL}?limit=200", headers=muhasebe_headers)).status_code == 200
    assert (await client.get(f"{_YOL}?limit=201", headers=muhasebe_headers)).status_code == 422


async def test_yetkisiz_rol_403(client, yetkisiz_headers) -> None:
    assert (await client.get(_YOL, headers=yetkisiz_headers)).status_code == 403
