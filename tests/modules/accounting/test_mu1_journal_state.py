"""MU-1 T3b — yevmiye fişi DURUM MAKİNESİ uçları (spec §7, uçlar 6/8/9).

`test_mu1_journal_api.py`nin üç parçasından biri (800 satır tavanı bölmesi);
iddialar değişmeden taşındı. Kilitlediği kararlar:

* 🔴 **`posted` fişte DELETE → 409, 403 DEĞİL:** yetki VARDIR, engelleyen kaydın
  DURUMUDUR;
* 🔴 **`DELETE` düz `admin`:** `full` (muhasebe) 403 alır — ön koşullu testtir,
  aynı kullanıcı PATCH'i GEÇER;
* 🔴 **K1 kapısı `POST /post`ta YENİDEN koşar** (bilinçli tekrar): fiş taslakken
  yaprak hesabın altına çocuk açılırsa kayıtlaştırma anında ısırır;
* 🔴 **Storno YENİ BİR FİŞTİR:** tarihi `timezone.today()`dir, açıklaması
  `REVERSAL_PREFIX`li, bacakları TAKASLIDIR ve orijinal `reversed` olur;
* 🔴 **K3 uçtan uca:** orijinal + storno → hesabın bakiyesi **TAM SIFIR**.
"""

import uuid
from decimal import Decimal

from app.core.timezone import today
from app.modules.accounting import guards, validation
from tests._silme_yardimci import sil_aile
from tests.modules.accounting._journal import YOL as _YOL
from tests.modules.accounting._journal import fis_olustur as _fis_olustur
from tests.modules.accounting._journal import govde as _govde
from tests.modules.accounting._journal import iki_yaprak as _iki_yaprak

# --------------------------------------------------------------------------- #
# Uç 6 — DELETE
# --------------------------------------------------------------------------- #


async def test_delete_yalniz_admin_204_full_403(
    client, admin_headers, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 `full` silmeyi KAPSAMAZ. Ön koşul: aynı kullanıcı PATCH'i GEÇER, yani
    403 gerçekten YETKİ SEVİYESİNDEN gelir, kaydın durumundan değil."""
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    assert (
        await client.patch(
            f"{_YOL}/{fis['id']}", json={"description": "Ön koşul"}, headers=muhasebe_headers
        )
    ).status_code == 200

    assert (await client.delete(f"{_YOL}/{fis['id']}", headers=muhasebe_headers)).status_code == 403
    assert (await sil_aile(client, admin_headers, "journal_entry", fis["id"])).status_code == 204
    assert (await client.get(f"{_YOL}/{fis['id']}", headers=muhasebe_headers)).status_code == 404


async def test_delete_kayitli_fis_de_silinir_sisyon_her_kosulda(
    client, admin_headers, muhasebe_headers, hesap_fabrikasi
) -> None:
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    resp = await sil_aile(client, admin_headers, "journal_entry", fis["id"])
    assert resp.status_code == 204, resp.text  # SIL-B2: "yalnız taslak" kuralı silme yolunda kalktı
    assert (await client.get(f"{_YOL}/{fis['id']}", headers=muhasebe_headers)).status_code == 404


async def test_delete_olmayan_fis_404(client, admin_headers) -> None:
    assert (await sil_aile(client, admin_headers, "journal_entry", uuid.uuid4())).status_code == 404


# --------------------------------------------------------------------------- #
# Uç 8 — post (kayıtlaştır)
# --------------------------------------------------------------------------- #


async def test_post_ucu_taslagi_kayitlastirir_ve_ikincisi_409(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    resp = await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "posted"

    tekrar = await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    assert tekrar.status_code == 409, tekrar.text
    assert tekrar.json()["detail"] == guards.INVALID_TRANSITION


async def test_post_ucu_K1_kapisini_YENIDEN_kosar(
    client, muhasebe_headers, hesap_fabrikasi, seeded_db
) -> None:
    """🔴 Kapı kayıtlaştırma anında TEKRAR ısırır (spec §4).

    Senaryo gerçektir: fiş taslakken `100` YAPRAKTI; sonra altına `100.01`
    açılırsa (uç K-Ş3 ile 409 verir, ama veri başka bir yoldan da doğabilir) o
    fiş artık üst hesaba kesilmiş sayılır ve kayıtlaştırılmamalıdır. Kapı yalnız
    yazma anında koşsaydı bu fiş sessizce deftere girer ve MU-2 mizanı çift
    sayardı.
    """
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await hesap_fabrikasi("100.01", name="Merkez Kasa")
    await seeded_db.flush()

    resp = await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    assert resp.status_code == 422, resp.text
    assert validation.NON_LEAF_ACCOUNT in resp.json()["detail"]


async def test_post_ucu_olmayan_fiste_404(client, muhasebe_headers) -> None:
    resp = await client.post(f"{_YOL}/{uuid.uuid4()}/post", headers=muhasebe_headers)
    assert resp.status_code == 404, resp.text


# --------------------------------------------------------------------------- #
# Uç 9 — reverse (storno)
# --------------------------------------------------------------------------- #


async def test_reverse_YENI_FIS_uretir_bacaklar_TAKASLI(
    client, muhasebe_headers, hesap_fabrikasi
) -> None:
    """🔴 Storno bir alan ya da bayrak DEĞİL, YENİ BİR FİŞTİR (spec §5).

    * `status` doğrudan `posted` — storno taslak değildir;
    * 🔴 `entry_date` **`timezone.today()`**dir: orijinalin tarihi kullanılsaydı
      storno MU-2'nin kilitleyeceği KAPALI bir döneme düşerdi (K6 sınır çağrısı);
    * `description` `REVERSAL_PREFIX`lidir ve önek `guards.py`de TEK kopyadır;
    * `detail_note` kopyadır (dayanak aynıdır);
    * bacaklar `debit ↔ credit` TAKASLIDIR, `sort_order` KORUNUR.
    """
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)

    resp = await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 201, resp.text
    storno = resp.json()

    bugun = today()
    assert storno["status"] == "posted"
    assert storno["entry_date"] == bugun.isoformat()
    assert (storno["period_year"], storno["period_month"]) == (bugun.year, bugun.month)
    assert storno["description"] == f"{guards.REVERSAL_PREFIX}Kasa tahsilatı"
    assert storno["detail_note"] == "Ziraat Bank · TRF-20260717"
    assert storno["reversal_of_id"] == fis["id"]

    asil = {(s["account_code"], s["sort_order"]): (s["debit"], s["credit"]) for s in fis["lines"]}
    for satir in storno["lines"]:
        borc, alacak = asil[(satir["account_code"], satir["sort_order"])]
        assert (Decimal(satir["debit"]), Decimal(satir["credit"])) == (
            Decimal(alacak),
            Decimal(borc),
        )

    orijinal = await client.get(f"{_YOL}/{fis['id']}", headers=muhasebe_headers)
    assert orijinal.json()["status"] == "reversed"


async def test_reverse_K3_net_TAM_SIFIR(client, muhasebe_headers, hesap_fabrikasi) -> None:
    """🔴 R6 uçtan uca: `POSTING_STATUSES`e `reversed` DAHİL olduğu için
    orijinal + storno hesabın bakiyesini TAM SIFIRA götürür.

    Yalnız `posted` sayılsaydı orijinal defterden düşer, storno ters
    bacaklarıyla eklenir ve net **−orijinal** çıkardı (ÇİFT ters kayıt).
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    resp = await client.post(_YOL, json=_govde(kasa, saticilar), headers=muhasebe_headers)
    assert resp.status_code == 201, resp.text
    fis = resp.json()
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)

    ara = await client.get(f"/chart-of-accounts/{kasa.id}", headers=muhasebe_headers)
    assert Decimal(ara.json()["balance"]) == Decimal("1000.00")

    await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)

    for hesap in (kasa, saticilar):
        son = await client.get(f"/chart-of-accounts/{hesap.id}", headers=muhasebe_headers)
        assert Decimal(son.json()["balance"]) == Decimal("0"), hesap.code


async def test_ayni_fis_IKI_KEZ_terslenemez_409(client, muhasebe_headers, hesap_fabrikasi) -> None:
    """🔴 Normal yolda ısıran kapı **MATRİSTİR**, `ENTRY_ALREADY_REVERSED` değil.

    Sıra bilinçlidir ve EŞİK = KİLİT'in parçasıdır: (1) kilit, (2) matris,
    (3) stornoya özel kapılar. İlk storno orijinali `reversed` DAMGALAR ve
    `reversed` TERMİNALDİR — ikinci istek matrisi hiç geçemez. Bu iddia
    ayrıca sıranın kendisini kilitler: stornoya özel kapı öne alınsaydı burada
    başka bir cümle dönerdi.

    `ENTRY_ALREADY_REVERSED` kapısı ölü DEĞİLDİR; damgası atılmamış ama stornosu
    yazılmış bir fişi (UNIQUE'in servis karşılığı) kapatır ve
    `test_damgasiz_stornosu_olan_fis_409` onu DOĞRUDAN ölçer.
    """
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)

    resp = await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.INVALID_TRANSITION


async def test_damgasiz_stornosu_olan_fis_409(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 `uq_journal_entries_reversal_of`un SERVİS karşılığı — DOĞRUDAN ölçüm.

    Matrisin GÖREMEDİĞİ hâl: fiş hâlâ `posted`tır (damgası atılmamış) ama ona
    işaret eden bir storno VARDIR. Bu durum normal yoldan doğmaz; ileride
    fiş üreten başka bir yol (MU-3 entegrasyonu) ya da elle bir düzeltme onu
    üretebilir. Kapı olmasaydı ikinci storno UNIQUE'e düşer ve kullanıcı
    ayrımsız bir "Veri bütünlüğü hatası" alırdı — hangi fişin zaten terslendiğini
    öğrenemezdi.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    orijinal = await fis_fabrikasi([(kasa, "10.00", "0"), (saticilar, "0", "10.00")])
    await fis_fabrikasi([(kasa, "0", "10.00"), (saticilar, "10.00", "0")], reversal_of=orijinal)

    resp = await client.post(f"{_YOL}/{orijinal.id}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.ENTRY_ALREADY_REVERSED


async def test_STORNO_terslenemez_409(client, muhasebe_headers, hesap_fabrikasi) -> None:
    """Stornonun stornosu sonsuz zincir açardı, mali anlamı yoktur (spec §5)."""
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    storno = (await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)).json()

    resp = await client.post(f"{_YOL}/{storno['id']}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.REVERSAL_NOT_REVERSIBLE


async def test_taslak_fis_terslenemez_409(client, muhasebe_headers, hesap_fabrikasi) -> None:
    """Matris dışı çift — `(draft, reverse)` tabloda YOKTUR."""
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    resp = await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == guards.INVALID_TRANSITION
