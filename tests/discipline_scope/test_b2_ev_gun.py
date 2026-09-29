"""DSC-B2 — EV günlük dağıtım YAZMASI (PUT …/days/{day}/allocation): kısıtlı BİRLEŞTİRME.

F1: aktörler patron (`_F`) rolünde. Gün 06.05 (GUN2): puantaj Ali 5 sa · Veli 4 sa, henüz dağıtım
YOK (05.05'te var). k1 = I1·S1 (civil/KAB), k2 = I2·S1 (elek/DUV).
🔴 Bayt bekçisi: elek yazar → civil yazar → elek'in kod/satır/hücre/not satırları ham SELECT ile
öncesi == sonrası; `ev_day_notes.updated_at` önceden geçmişe çekilir ki onupdate yakalansın.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.earned_value import audit_messages as msg
from app.modules.personnel.models import Personnel
from app.modules.timesheet.models import TimesheetEntry
from tests._disiplin_dunyasi import GUN2, Dunya
from tests.discipline_scope._b2_golden_araclari import audit_kimlikleri, yeni_audit
from tests.discipline_scope._b2_yardim import ozet

YAZANLAR = True
ESKI = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)
D = Decimal


def _url(d: Dunya) -> str:
    return f"/sites/{d.santiye.id}/earned-value/days/{GUN2.isoformat()}/allocation"


async def _kisi(session: AsyncSession, ad: str) -> uuid.UUID:
    return (
        await session.execute(select(Personnel.id).where(Personnel.full_name == ad))
    ).scalar_one()


def _hucre(kisi: uuid.UUID, node: str, saat: str) -> dict:
    return {"row": {"kind": "personnel", "ref_id": str(kisi)}, "node_id": node, "hours": saat}


def _k(d: Dunya) -> tuple[str, str]:
    return f"l:{d.i['i1'].id}:{d.s1.id}", f"l:{d.i['i2'].id}:{d.s1.id}"


async def _ham(session: AsyncSession, sql: str, **kw: object) -> list[tuple]:
    return [tuple(r) for r in (await session.execute(text(sql), kw)).all()]


async def _elek_kesiti(session: AsyncSession, d: Dunya) -> dict[str, list[tuple]]:
    """Elek'in dünyası: kendi kodu, TÜM satırlar (paylaşılan dahil), kendi hücreleri, not."""
    _, k2 = _k(d)
    s, g = d.santiye.id, GUN2
    return {
        "kodlar": await _ham(
            session,
            "SELECT * FROM ev_day_codes WHERE site_id=:s AND day=:g AND node_id=:n ORDER BY 1,2,3",
            s=s,
            g=g,
            n=k2,
        ),
        "satirlar": await _ham(
            session, "SELECT * FROM ev_day_rows WHERE site_id=:s AND day=:g ORDER BY id", s=s, g=g
        ),
        "hucreler": await _ham(
            session,
            "SELECT c.* FROM ev_day_cells c JOIN ev_day_rows r ON r.id=c.row_id "
            "WHERE r.site_id=:s AND r.day=:g AND c.node_id=:n ORDER BY 1,2",
            s=s,
            g=g,
            n=k2,
        ),
        "not": await _ham(
            session, "SELECT * FROM ev_day_notes WHERE site_id=:s AND day=:g", s=s, g=g
        ),
    }


async def _elek_yaz(client: AsyncClient, session: AsyncSession, d: Dunya, elek_yazar) -> None:
    _, k2 = _k(d)
    ali, veli = await _kisi(session, "Ali Usta"), await _kisi(session, "Veli Usta")
    resp = await client.put(
        _url(d),
        headers=elek_yazar,
        json={
            "codes": [{"node_id": k2, "rule": "direct"}],
            "cells": [_hucre(ali, k2, "2"), _hucre(veli, k2, "4")],
            "unallocated_reason": "Elek gerekçesi",
        },
    )
    assert resp.status_code == 200, resp.text
    await session.execute(
        text("UPDATE ev_day_notes SET updated_at=:t WHERE site_id=:s AND day=:g"),
        {"t": ESKI, "s": d.santiye.id, "g": GUN2},
    )


# ------------------------------------------------------------------ 🔴 bayt bayt bekçi


async def test_civil_dagitim_yazar_elegin_kod_satir_hucre_not_satirlari_bayt_bayt_ayni(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, elek_yazar
) -> None:
    """Ali paylaşılan satırdır (elek 2 sa · civil 3 sa). Elek'in tüm satırları + paylaşılan
    satırın id'si korunur; civil'in değişikliği uygulanır."""
    k1, _ = _k(dunya)
    await _elek_yaz(client, seeded_db, dunya, elek_yazar)
    once = await _elek_kesiti(seeded_db, dunya)
    assert len(once["satirlar"]) == 2 and len(once["hucreler"]) == 2 and once["not"]
    ali = await _kisi(seeded_db, "Ali Usta")
    ali_satir_once = await _ham(
        seeded_db, "SELECT id FROM ev_day_rows WHERE personnel_id=:p AND day=:g", p=ali, g=GUN2
    )

    resp = await client.put(
        _url(dunya),
        headers=civil_yazar,
        json={"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]},
    )
    assert resp.status_code == 200, resp.text

    sonra = await _elek_kesiti(seeded_db, dunya)
    for ad in ("kodlar", "satirlar", "hucreler", "not"):
        assert sonra[ad] == once[ad], f"elek'in {ad} satırları DEĞİŞTİ"
    ali_satir_sonra = await _ham(
        seeded_db, "SELECT id FROM ev_day_rows WHERE personnel_id=:p AND day=:g", p=ali, g=GUN2
    )
    assert ali_satir_sonra == ali_satir_once  # paylaşılan satırın id'si KORUNDU
    # civil'in değişikliği uygulandı
    kodlar = await _ham(
        seeded_db,
        "SELECT node_id FROM ev_day_codes WHERE site_id=:s AND day=:g ORDER BY 1",
        s=dunya.santiye.id,
        g=GUN2,
    )
    assert sorted(x[0] for x in kodlar) == sorted(_k(dunya))
    ali_hucreler = await _ham(
        seeded_db,
        "SELECT node_id, hours FROM ev_day_cells WHERE row_id=:r ORDER BY 1",
        r=ali_satir_sonra[0][0],
    )
    assert {n: h for n, h in ali_hucreler} == {k1: D(3), _k(dunya)[1]: D(2)}


async def test_civil_yaniti_yalniz_kendi_kod_ve_hucrelerini_gosterir_toplam_suzulmez(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, elek_yazar
) -> None:
    k1, k2 = _k(dunya)
    await _elek_yaz(client, seeded_db, dunya, elek_yazar)
    ali = await _kisi(seeded_db, "Ali Usta")
    resp = await client.put(
        _url(dunya),
        headers=civil_yazar,
        json={"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]},
    )
    govde = resp.json()
    assert [c["node_id"] for c in govde["codes"]] == [k1]
    assert [c["node_id"] for c in govde["cells"]] == [k1]
    assert k2 not in resp.text and str(dunya.i["i2"].id) not in resp.text
    # K4: totals şantiye geneli (Ali 3+2, Veli 4 = 9 dağıtılan / 9 kaynak)
    assert (D(govde["totals"]["source_hours"]), D(govde["totals"]["allocated_hours"])) == (9, 9)


# ------------------------------------------------------------------ S3 · S4


async def test_s3_kisitli_kayit_paylasilan_satirin_source_hoursini_canliya_yeniler(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, elek_yazar
) -> None:
    k1, _ = _k(dunya)
    await _elek_yaz(client, seeded_db, dunya, elek_yazar)
    ali = await _kisi(seeded_db, "Ali Usta")
    ts = (
        await seeded_db.execute(
            select(TimesheetEntry).where(
                TimesheetEntry.personnel_id == ali, TimesheetEntry.work_date == GUN2
            )
        )
    ).scalar_one()
    ts.hours = D(7)
    await seeded_db.flush()
    resp = await client.put(
        _url(dunya),
        headers=civil_yazar,
        json={"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]},
    )
    assert resp.status_code == 200, resp.text
    (saat,) = (
        await _ham(
            seeded_db,
            "SELECT source_hours FROM ev_day_rows WHERE personnel_id=:p AND day=:g",
            p=ali,
            g=GUN2,
        )
    )[0]
    assert saat == D(7)


async def test_s4_kisitlida_govdede_yoksa_not_dokunulmaz_varsa_yazilir_kisitsizda_temizlenir(
    client: AsyncClient,
    seeded_db: AsyncSession,
    dunya: Dunya,
    civil_yazar,
    elek_yazar,
    yazar_atamasiz,
) -> None:
    k1, k2 = _k(dunya)
    await _elek_yaz(client, seeded_db, dunya, elek_yazar)
    once = await _ham(
        seeded_db,
        "SELECT * FROM ev_day_notes WHERE site_id=:s AND day=:g",
        s=dunya.santiye.id,
        g=GUN2,
    )
    ali = await _kisi(seeded_db, "Ali Usta")
    govde = {"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]}
    assert (await client.put(_url(dunya), headers=civil_yazar, json=govde)).status_code == 200
    sonra = await _ham(
        seeded_db,
        "SELECT * FROM ev_day_notes WHERE site_id=:s AND day=:g",
        s=dunya.santiye.id,
        g=GUN2,
    )
    assert sonra == once and once[0][2] == "Elek gerekçesi"  # ESKİ tarih de korunur
    # gövdede VARSA ortak alan yazılır (S7: son yazan kazanır)
    resp = await client.put(
        _url(dunya), headers=civil_yazar, json={**govde, "unallocated_reason": "Civil gerekçesi"}
    )
    assert resp.json()["unallocated_reason"] == "Civil gerekçesi"
    # KISITSIZ: gövdede yoksa bugünkü "yoksa temizle" (atamasız eş, tam değiştirme)
    tam = {"codes": [], "cells": []}
    resp = await client.put(_url(dunya), headers=yazar_atamasiz, json=tam)
    assert resp.status_code == 200, resp.text
    assert resp.json()["unallocated_reason"] is None


# ------------------------------------------------------------------ Ü7 · audit


def _normalle(govde: object, *dugumler: str) -> str:
    metin = str(govde)
    for n in dugumler:
        metin = metin.replace(n, "<DUGUM>")
    return metin


async def test_u7_yabanci_kod_olmayan_kodla_ayni_422_ve_yazma_olmaz(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    _, k2 = _k(dunya)
    yok = f"l:{uuid.UUID(int=777)}:{dunya.s1.id}"
    ali = await _kisi(seeded_db, "Ali Usta")

    def govde(node: str) -> dict:
        return {"codes": [{"node_id": node, "rule": "direct"}], "cells": [_hucre(ali, node, "1")]}

    yabanci = await client.put(_url(dunya), headers=civil_yazar, json=govde(k2))
    olmayan = await client.put(_url(dunya), headers=civil_yazar, json=govde(yok))
    a, b = ozet(yabanci), ozet(olmayan)
    assert a[0] == b[0] == 422 and a[1] == b[1]
    assert _normalle(a[2], k2) == _normalle(b[2], yok)
    # pozitif kontrol: atamasız eş aynı yabancı kodu KABUL eder
    ok = await client.put(_url(dunya), headers=yazar_atamasiz, json=govde(k2))
    assert ok.status_code == 200, ok.text
    assert await _ham(seeded_db, "SELECT 1 FROM ev_day_codes WHERE node_id=:n", n=yok) == []


async def test_audit_sayisi_govdedeki_kendi_hucre_sayisidir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, elek_yazar
) -> None:
    k1, _ = _k(dunya)
    await _elek_yaz(client, seeded_db, dunya, elek_yazar)
    once = await audit_kimlikleri(seeded_db)
    ali = await _kisi(seeded_db, "Ali Usta")
    resp = await client.put(
        _url(dunya),
        headers=civil_yazar,
        json={"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]},
    )
    assert resp.status_code == 200, resp.text
    yeni = await yeni_audit(seeded_db, once)
    assert len(yeni) == 1
    assert yeni[0]["detail"] == msg.day_allocation_saved(
        dunya.proje.name, dunya.santiye.name, GUN2, 1
    )


# ------------------------------------------------------------------ canlıdan düşmüş satır


async def _puantaj_sil(session: AsyncSession, kisi: uuid.UUID) -> None:
    await session.execute(
        text("DELETE FROM timesheet_entries WHERE personnel_id=:p AND work_date=:g"),
        {"p": kisi, "g": GUN2},
    )


async def _hucreler(session: AsyncSession, d: Dunya, kisi: uuid.UUID) -> dict[str, Decimal]:
    satirlar = await _ham(
        session,
        "SELECT c.node_id, c.hours FROM ev_day_cells c JOIN ev_day_rows r ON r.id=c.row_id "
        "WHERE r.site_id=:s AND r.day=:g AND r.personnel_id=:p",
        s=d.santiye.id,
        g=GUN2,
        p=kisi,
    )
    return {n: h for n, h in satirlar}


async def test_dusmus_satir_gizli_hucre_kalir_kisitlinin_kendi_hucresi_silinir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, elek_yazar
) -> None:
    """Veli'nin puantajı silinir: satırında elek hücresi (gizli) KALIR → satır KALIR; civil'in
    kendi eski Veli hücresi (k1) SİLİNİR (yetim kalıp GET'te görünüp silinemez olmaz)."""
    k1, k2 = _k(dunya)
    await _elek_yaz(client, seeded_db, dunya, elek_yazar)
    ali, veli = await _kisi(seeded_db, "Ali Usta"), await _kisi(seeded_db, "Veli Usta")
    ilk = {
        "codes": [{"node_id": k1, "rule": "direct"}],
        "cells": [_hucre(veli, k1, "1"), _hucre(ali, k1, "3")],
    }
    assert (await client.put(_url(dunya), headers=civil_yazar, json=ilk)).status_code == 200
    assert await _hucreler(seeded_db, dunya, veli) == {k1: D(1), k2: D(4)}
    await _puantaj_sil(seeded_db, veli)
    ikinci = {"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]}
    resp = await client.put(_url(dunya), headers=civil_yazar, json=ikinci)
    assert resp.status_code == 200, resp.text
    assert await _hucreler(seeded_db, dunya, veli) == {k2: D(4)}  # elek KALDI, civil silindi
    assert all(c["node_id"] != k1 or c["ref_id"] != str(veli) for c in resp.json()["cells"])


async def test_dusmus_satir_gizli_hucresi_yoksa_silinir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    """Canlıda olmayan satır, GİZLİ hücresi yoksa silinir (yalnız civil hücreli satır)."""
    k1, _ = _k(dunya)
    ali, veli = await _kisi(seeded_db, "Ali Usta"), await _kisi(seeded_db, "Veli Usta")
    ilk = {
        "codes": [{"node_id": k1, "rule": "direct"}],
        "cells": [_hucre(veli, k1, "1"), _hucre(ali, k1, "3")],
    }
    assert (await client.put(_url(dunya), headers=civil_yazar, json=ilk)).status_code == 200
    await _puantaj_sil(seeded_db, veli)
    ikinci = {"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]}
    assert (await client.put(_url(dunya), headers=civil_yazar, json=ikinci)).status_code == 200
    assert await _hucreler(seeded_db, dunya, veli) == {}
    kalan = await _ham(
        seeded_db, "SELECT 1 FROM ev_day_rows WHERE personnel_id=:p AND day=:g", p=veli, g=GUN2
    )
    assert kalan == []


# ------------------------------------------------------------------ S7


async def test_s7_kisitli_ekler_sonra_kisitsiz_tam_put_govdede_olmayani_siler(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    """Son yazan kazanır (belgelenmiş karar): kısıtsız TAM DEĞİŞTİRME, gövdesinde olmayan
    civil kod/hücresini siler."""
    k1, k2 = _k(dunya)
    ali, veli = await _kisi(seeded_db, "Ali Usta"), await _kisi(seeded_db, "Veli Usta")
    ilk = {"codes": [{"node_id": k1, "rule": "direct"}], "cells": [_hucre(ali, k1, "3")]}
    assert (await client.put(_url(dunya), headers=civil_yazar, json=ilk)).status_code == 200
    assert await _hucreler(seeded_db, dunya, ali) == {k1: D(3)}
    tam = {"codes": [{"node_id": k2, "rule": "direct"}], "cells": [_hucre(veli, k2, "4")]}
    assert (await client.put(_url(dunya), headers=yazar_atamasiz, json=tam)).status_code == 200
    assert await _hucreler(seeded_db, dunya, ali) == {}
    kodlar = await _ham(
        seeded_db,
        "SELECT node_id FROM ev_day_codes WHERE site_id=:s AND day=:g",
        s=dunya.santiye.id,
        g=GUN2,
    )
    assert [x[0] for x in kodlar] == [k2]
