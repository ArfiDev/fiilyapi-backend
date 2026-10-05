"""Eşzamanlılık (yarış) testlerinin ORTAK zaman tavanı — FIX-B3.

Tavan bir BEKLEME DEĞİLDİR: test yalnız bir şey ASILI kalırsa bu kadar bekler; normal
koşuda görev/olay çok daha önce tamamlanır. Bu yüzden geniş tutmak bekçiyi ZAYIFLATMAZ
(kilit alınmazsa ya da sıra bozulursa iddia yine düşer), yalnız yavaş makinede SAHTE
KIRMIZIYI önler. Dar tavanın (5 sn) CI yükünde yetmediği ölçüldü: MU-3A bariyeri
(EXPORT-XLSX turu) ve FIX-B3 (#130).

"Hâlâ bloke mi?" iddiaları (TimeoutError = başarı) bu sabiti KULLANMAZ — onlar kısa
kalmalıdır (`_BLOKE_TAVANI`), yoksa her koşu tavan kadar uzar.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

YARIS_TAVANI_SN = 30.0


async def kilitte_bekleyen_sorgu(
    engine: AsyncEngine,
    gorev: asyncio.Task,
    *,
    mesaj: str,
    tavan: float = YARIS_TAVANI_SN,
    sorgu_oneki: str | None = None,
) -> str:
    """`gorev`in oturumu bir KİLİTTE bekleyene kadar yoklar; bekleyen SORGUNUN metnini döner.

    TEST-B1: `asyncio.sleep(0.3); assert not task.done()` kalıbının yerine. O kalıp yük
    altında SAHTE YEŞİLDİ: ikinci görev 0,3 sn'de kilide hiç VARMAMIŞSA da "bitmedi"
    geçer; birinci commit edince ikinci taze veriyi okur ve KİLİTSİZ mutant da yeşil kalır
    (her dosyada ikinci görevi 0,5 sn geciktirerek yeniden üretildi).

    * Her yoklama YENİ bağlantıda (yeni transaction) koşar — `pg_stat_activity` görüntüsü
      transaction başına önbelleklenir (FIX-B3).
    * `datname = current_database()`: xdist işçisinin KENDİ veritabanı (`<taban>_gwN`).
    * Görev beklemeden BİTERSE sonucu/istisnasıyla düşer (kilitsiz mutantın imzası).
    * Çağıran dönen metinde DOĞRU kilidi iddia eder (ör. tablo + `FOR UPDATE`): kilitsiz
      mutantta ikinci görev yine BAŞKA bir yerde (ör. `UPDATE`in satır kilidinde) bekleyebilir.
    * `sorgu_oneki`: yalnız bu önekle başlayan bekleyen sorgular sayılır. Yoklama işçi DB'sindeki
      HER oturumu görür; CI'da (`-n 4 --dist loadfile`) aynı işçide koşmuş başka bir dosyadan
      kalan, alakasız bir kilitte bekleyen oturum (ör. `SELECT users …`) yakalanıp bekçiyi
      SAHTE KIRMIZI yapıyordu (PR #174). Önek verilince alakasız bekleme yok sayılır; görevin
      KENDİSİ beklemeden biterse yine düşer (kilitsiz mutant imzası korunur).
    """
    sql = text(
        "SELECT query FROM pg_stat_activity WHERE datname = current_database() "
        "AND pid <> pg_backend_pid() AND wait_event_type = 'Lock' "
        "AND (CAST(:onek AS text) IS NULL OR query LIKE CAST(:onek AS text) || '%')"
    )
    loop = asyncio.get_running_loop()
    son = loop.time() + tavan
    while loop.time() < son:
        async with engine.connect() as conn:
            satirlar = (await conn.execute(sql, {"onek": sorgu_oneki})).scalars().all()
        if satirlar:
            assert len(satirlar) == 1, f"{mesaj} — birden fazla bekleyen: {satirlar}"
            return " ".join(satirlar[0].split())
        if gorev.done():
            hata = None if gorev.cancelled() else gorev.exception()
            sonuc = "iptal" if gorev.cancelled() else repr(hata) if hata else repr(gorev.result())
            raise AssertionError(f"{mesaj} — görev kilitte BEKLEMEDEN bitti: {sonuc}") from hata
        await asyncio.sleep(0.05)
    raise AssertionError(f"{mesaj} — {tavan} sn içinde kilit beklemesi GÖRÜLMEDİ")
