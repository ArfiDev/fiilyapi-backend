"""MU-1 T3b — yevmiye özeti + izin/denetim/kalıntı (spec §7, uç 3).

`test_mu1_journal_api.py`nin üç parçasından biri (800 satır tavanı bölmesi);
iddialar değişmeden taşındı. Kilitlediği kararlar:

* 🔴 `net_balance = ALACAK − BORÇ` ve KPI **taslağı saymaz, `reversed`ı SAYAR**
  (`POSTING_STATUSES`);
* `summary` hesap süzgeci ALMAZ ve varsayılan dönem BUGÜNÜN ayıdır;
* **Denetim:** yeni `AuditAction` üyesi AÇILMADI (`post → approve`,
  `reverse → update`) ve 🔴 **TUTAR metne GİRMEZ**; GET uçları denetim YAZMAZ;
* Silinen fişin SATIRLARI da gider (kalıntı yok).
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.core.timezone import today
from app.modules.accounting.models import JournalEntry, JournalEntryStatus
from app.modules.audit.models import AuditAction, AuditLog
from tests._silme_yardimci import sil_aile
from tests.modules.accounting._journal import YOL as _YOL
from tests.modules.accounting._journal import fis_olustur as _fis_olustur
from tests.modules.accounting._journal import iki_yaprak as _iki_yaprak

# --------------------------------------------------------------------------- #
# Uç 3 — summary (üç KPI)
# --------------------------------------------------------------------------- #


async def test_summary_UC_KPI_ve_net_ALACAK_EKSI_BORC(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """🔴 `net_balance = ALACAK − BORÇ` (E8:88 `4.120.000−3.842.600=277.400`).

    Yön burada ÖLÇÜLEBİLİR olmalıdır. Dengeli fişlerde iki toplam eşit olduğu
    için net her zaman **0** çıkar ve işaret yönü GÖRÜNMEZ. Probe bu yüzden
    SATIRLARI dengesiz bir fiştir ve durumu bilinçli olarak **`reversed`**tır:
    `reversed` `POSTING_STATUSES`e DAHİLDİR, yani KPI onu sayar. **KPI satırları
    toplar, başlığı değil** — bu testin ölçtüğü olgu tam olarak budur.

    🔴 **TB6 T2'DEN SONRA:** dengesizlik artık BAŞLIKTAN kurulamaz —
    `ck_journal_entries_posting_balanced` `posted`/`reversed` bir fişin
    `total_debit = total_credit` olmasını ZORLAR. Prob yine de kurulabilir ve
    iddiası DEĞİŞMEDİ, çünkü defter **SATIRLARI** toplar, başlığı DEĞİL:
    `header_totals` ile başlık dengeli yazılır, satırlar dengesiz bırakılır.
    (Uygulama böyle bir fiş üretemez — `apply_totals` yalnız `draft`ta koşar —
    ama ölçülecek şey `is_balanced`in SÜS OLMADIĞIDIR.)
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    await fis_fabrikasi(
        [(kasa, "100.00", "0"), (saticilar, "0", "400.00")],
        status=JournalEntryStatus.reversed,
        header_totals=("400.00", "400.00"),
    )
    resp = await client.get(f"{_YOL}/summary?year=2026&month=7", headers=muhasebe_headers)
    assert resp.status_code == 200, resp.text
    govde = resp.json()
    assert Decimal(govde["total_debit"]) == Decimal("100.00")
    assert Decimal(govde["total_credit"]) == Decimal("400.00")
    assert Decimal(govde["net_balance"]) == Decimal("300.00")


async def test_summary_taslagi_saymaz_reversedi_SAYAR(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """`POSTING_STATUSES` TEK kopyadır ve özet de onu okur."""
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    await fis_fabrikasi(
        [(kasa, "10.00", "0"), (saticilar, "0", "10.00")], status=JournalEntryStatus.draft
    )
    await fis_fabrikasi(
        [(kasa, "20.00", "0"), (saticilar, "0", "20.00")], status=JournalEntryStatus.reversed
    )
    resp = await client.get(f"{_YOL}/summary?year=2026&month=7", headers=muhasebe_headers)
    assert Decimal(resp.json()["total_debit"]) == Decimal("20.00")


async def test_summary_varsayilan_donem_BUGUNUN_ayidir(client, muhasebe_headers) -> None:
    """🔴 K6 sınır çağrısı: `timezone.today()` (`date.today()` DEĞİL)."""
    bugun = today()
    govde = (await client.get(f"{_YOL}/summary", headers=muhasebe_headers)).json()
    assert (govde["year"], govde["month"]) == (bugun.year, bugun.month)


async def test_summary_HESAP_SUZGECI_ALMAZ(
    client, muhasebe_headers, hesap_fabrikasi, fis_fabrikasi
) -> None:
    """E8:72 — KPI şeridi tablonun DIŞINDADIR ve yalnız DÖNEME bağlıdır.

    Hesap süzgeci bir PARAMETRE olarak yoktur; gönderilse bile toplamları
    DEĞİŞTİRMEZ. İddia tam olarak budur: aynı dönemde iki hesap varken tek
    hesabın kimliğini geçirmek yanıtı oynatmamalıdır.
    """
    kasa, saticilar = await _iki_yaprak(hesap_fabrikasi)
    await fis_fabrikasi([(kasa, "50.00", "0"), (saticilar, "0", "50.00")])

    suzgecsiz = await client.get(f"{_YOL}/summary?year=2026&month=7", headers=muhasebe_headers)
    suzgecli = await client.get(
        f"{_YOL}/summary?year=2026&month=7&account_id={kasa.id}", headers=muhasebe_headers
    )
    assert suzgecli.status_code == 200, suzgecli.text
    assert suzgecli.json() == suzgecsiz.json()


# --------------------------------------------------------------------------- #
# Yetki kapıları
# --------------------------------------------------------------------------- #


async def test_pm_okur_yazamaz(client, pm_headers, muhasebe_headers, hesap_fabrikasi) -> None:
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    assert (await client.get(_YOL, headers=pm_headers)).status_code == 200
    assert (
        await client.patch(f"{_YOL}/{fis['id']}", json={"description": "X"}, headers=pm_headers)
    ).status_code == 403
    assert (await client.post(f"{_YOL}/{fis['id']}/post", headers=pm_headers)).status_code == 403


async def test_yetkisiz_rol_okumada_bile_403(client, yetkisiz_headers) -> None:
    assert (await client.get(_YOL, headers=yetkisiz_headers)).status_code == 403
    assert (await client.get(f"{_YOL}/summary", headers=yetkisiz_headers)).status_code == 403


# --------------------------------------------------------------------------- #
# Denetim
# --------------------------------------------------------------------------- #


async def test_denetim_YENI_UYE_ACMAZ_ve_TUTAR_metne_girmez(
    client, muhasebe_headers, hesap_fabrikasi, seeded_db
) -> None:
    """🔴 `post → approve`, `reverse → update`; ayrım METİNDEDİR.

    Tutar metne girseydi bakiye (TÜREV, K3) günlükte donmuş bir kopya olarak
    yaşar ve ilk düzeltmede ayrışırdı (`bank_account_*` kanonu).
    """
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await client.post(f"{_YOL}/{fis['id']}/post", headers=muhasebe_headers)
    await client.post(f"{_YOL}/{fis['id']}/reverse", headers=muhasebe_headers)

    kayitlar = (
        (await seeded_db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
    )
    yevmiye = [k for k in kayitlar if "fiş" in k.detail.lower() or "Fiş" in k.detail]
    eylemler = [k.action for k in yevmiye]
    assert AuditAction.create in eylemler
    assert AuditAction.approve in eylemler  # post
    assert AuditAction.update in eylemler  # reverse
    for kayit in yevmiye:
        assert "1000" not in kayit.detail, kayit.detail


async def test_GET_uclari_denetim_YAZMAZ(
    client, muhasebe_headers, hesap_fabrikasi, seeded_db
) -> None:
    await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    once = len((await seeded_db.execute(select(AuditLog))).scalars().all())
    await client.get(_YOL, headers=muhasebe_headers)
    await client.get(f"{_YOL}/summary", headers=muhasebe_headers)
    sonra = len((await seeded_db.execute(select(AuditLog))).scalars().all())
    assert once == sonra


# --------------------------------------------------------------------------- #
# Yapısal bekçi
# --------------------------------------------------------------------------- #


async def test_silinen_fisin_satirlari_da_gider(
    client, admin_headers, muhasebe_headers, hesap_fabrikasi, seeded_db
) -> None:
    """`journal_lines.entry_id` CASCADE'tir; satırın ömrü başlığa bağlıdır."""
    fis = await _fis_olustur(client, muhasebe_headers, hesap_fabrikasi)
    await sil_aile(client, admin_headers, "journal_entry", fis["id"])
    kalan = (
        await seeded_db.execute(select(JournalEntry).where(JournalEntry.id == uuid.UUID(fis["id"])))
    ).scalar_one_or_none()
    assert kalan is None
