"""DSC-B2 — günlük YAZMA uçları: PUT lines BİRLEŞTİRME, süzülmüş yanıtlar, audit sayıları, Ü5.

F1: aktörler patron (`_F`) rolündedir (PM günlükte `view` → 403'ü izin kapısı verirdi).
Taslak `e3` (07.05) şöyle hazırlanır (`_hazirla`): I2·S1 6 (elek) · I3·bölümsüz 1 (eşlemesiz) ·
I1·bölümsüz 2 (civil) · NULL·S1 1 (bağı kopmuş) — yani her kısıtlı yazar için hem GÖRÜNÜR hem
GİZLİ satır vardır; `created_at`/`updated_at` eskiye çekilir ki yeniden yazılan satır (onupdate
`now()`) ham SELECT'te yakalansın (aynı transaction'da `now()` sabit olduğu için aksi hâlde
`updated_at` farkı görünmezdi).
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.audit import messages
from app.modules.boq.models import BoqItem
from app.modules.site_diary.models import SiteDiaryLine
from tests._disiplin_dunyasi import GUN3, Dunya, _kimlik
from tests.discipline_scope._b2_golden_araclari import audit_kimlikleri, yeni_audit
from tests.discipline_scope._b2_yardim import SISYON_ONLY, YOK_KIMLIK, ham_satirlar, ozet

YAZANLAR = True
ESKI = datetime(2020, 1, 1, 12, 0, tzinfo=UTC)


async def _hazirla(session: AsyncSession, d: Dunya) -> uuid.UUID:
    """e3'e civil bölümsüz satırı + NULL satırı ekler; e3 satırlarının zamanlarını eskiler."""
    e3 = d.gunluk["e3"]
    session.add_all(
        [
            SiteDiaryLine(
                id=_kimlik(31, 20),
                entry_id=e3.id,
                boq_item_id=d.i["i1"].id,
                section_id=None,
                code="01.001",
                description="Beton",
                unit="m3",
                unit_price=Decimal(10),
                quantity=Decimal(2),
            ),
            SiteDiaryLine(
                id=_kimlik(31, 21),
                entry_id=e3.id,
                boq_item_id=None,
                section_id=d.s1.id,
                code="00.000",
                description="Bağı kopmuş satır",
                unit="m2",
                unit_price=Decimal(5),
                quantity=Decimal(1),
            ),
        ]
    )
    await session.flush()
    await session.execute(
        text("UPDATE site_diary_lines SET created_at=:t, updated_at=:t WHERE entry_id=:e"),
        {"t": ESKI, "e": e3.id},
    )
    return e3.id


def _satir(kalem, miktar: str, bolum=None, *, bolum_anahtari: bool = True) -> dict:
    ham: dict = {"boq_item_id": str(kalem.id), "quantity": miktar}
    if bolum_anahtari:
        ham["section_id"] = None if bolum is None else str(bolum.id)
    return ham


async def _satirlar(session: AsyncSession, entry_id: uuid.UUID) -> dict[uuid.UUID, tuple]:
    ham = await ham_satirlar(session, "site_diary_lines", "entry_id = :e", e=entry_id)
    return {satir[0]: satir for satir in ham}


def _put(client: AsyncClient, d: Dunya, baslik, satirlar: list[dict]):  # noqa: ANN202
    return client.put(f"/diary/{d.gunluk['e3'].id}/lines", headers=baslik, json={"lines": satirlar})


# ------------------------------------------------------------------ 🔴 bayt bayt bekçi


async def test_civil_put_lines_elek_ve_eslemesiz_ve_null_satirlar_bayt_bayt_ayni(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    """Gizli satırlar (elek I2·S1, eşlemesiz I3, NULL) TÜM kolonlar + id + created_at +
    updated_at ile öncesi == sonrası; civil'in kendi değişikliği uygulanır."""
    e3 = await _hazirla(seeded_db, dunya)
    once = await _satirlar(seeded_db, e3)
    gizli = {
        dunya.satir["e3_i2_s1"].id,
        dunya.satir["e3_i3_yok"].id,
        _kimlik(31, 21),
    }
    resp = await _put(client, dunya, civil_yazar, [_satir(dunya.i["i1"], "9")])
    assert resp.status_code == 200, resp.text
    sonra = await _satirlar(seeded_db, e3)
    for kimlik in gizli:
        assert sonra[kimlik] == once[kimlik], f"gizli satır DEĞİŞTİ: {kimlik}"
    assert set(sonra) == set(once)  # hiçbir satır eklenmedi/silinmedi (civil yalnız kendini yazdı)
    civil_id = _kimlik(31, 20)
    assert sonra[civil_id] != once[civil_id]
    assert Decimal(str(resp.json()["lines"][0]["quantity"])) == Decimal(9)
    assert [ln["boq_item_id"] for ln in resp.json()["lines"]] == [str(dunya.i["i1"].id)]


async def test_elek_put_lines_civil_satirlari_bayt_bayt_ayni(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, elek_yazar
) -> None:
    e3 = await _hazirla(seeded_db, dunya)
    once = await _satirlar(seeded_db, e3)
    resp = await _put(client, dunya, elek_yazar, [_satir(dunya.i["i2"], "8", dunya.s1)])
    assert resp.status_code == 200, resp.text
    sonra = await _satirlar(seeded_db, e3)
    for kimlik in (_kimlik(31, 20), dunya.satir["e3_i3_yok"].id, _kimlik(31, 21)):
        assert sonra[kimlik] == once[kimlik]
    assert sonra[dunya.satir["e3_i2_s1"].id] != once[dunya.satir["e3_i2_s1"].id]


async def test_gorunur_satir_govdede_yoksa_silinir_gizliler_kalir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    """DEĞİŞTİRME semantiği görünür küme içinde sürer: boş gövde civil'in satırını siler,
    başkalarınınkine dokunmaz (`dropped_orphan_count` 0: NULL satır düşmez)."""
    e3 = await _hazirla(seeded_db, dunya)
    resp = await _put(client, dunya, civil_yazar, [])
    assert resp.status_code == 200, resp.text
    assert resp.json()["dropped_orphan_count"] == 0
    sonra = await _satirlar(seeded_db, e3)
    assert _kimlik(31, 20) not in sonra and len(sonra) == 3


async def test_iki_gorunur_satirdan_govdede_olmayan_silinir_gizliler_kalir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    """Civil'in İKİ görünür satırı (I1 + aynı grubun I4 kalemi); gövdede yalnız biri → öteki
    SİLİNİR, gizliler (elek, eşlemesiz, NULL) bayt bayt kalır. Görünürlük kümesi yalnız gövde
    kalemlerinden hesaplanırsa silinecek I4 satırı 'gizli' sayılıp KALIRDI."""
    e3 = await _hazirla(seeded_db, dunya)
    ikinci = _kimlik(31, 22)
    kalem = BoqItem(
        id=_kimlik(11, 40),
        site_id=dunya.santiye.id,
        group_id=dunya.g["g1"].id,  # civil'in grubu → görünür
        code="01.002",
        description="Kalıp",
        unit="m2",
        quantity=Decimal(10),
        unit_price=Decimal(7),
        sort_order=2,
    )
    seeded_db.add(kalem)
    await seeded_db.flush()
    seeded_db.add(
        SiteDiaryLine(
            id=ikinci,
            entry_id=e3,
            boq_item_id=kalem.id,
            section_id=None,
            code="01.002",
            description="Kalıp",
            unit="m2",
            unit_price=Decimal(7),
            quantity=Decimal(4),
        )
    )
    await seeded_db.flush()
    once = await _satirlar(seeded_db, e3)
    resp = await _put(client, dunya, civil_yazar, [_satir(dunya.i["i1"], "9")])
    assert resp.status_code == 200, resp.text
    sonra = await _satirlar(seeded_db, e3)
    assert ikinci not in sonra
    assert set(sonra) == set(once) - {ikinci}
    for kimlik in (dunya.satir["e3_i2_s1"].id, dunya.satir["e3_i3_yok"].id, _kimlik(31, 21)):
        assert sonra[kimlik] == once[kimlik], f"gizli satır DEĞİŞTİ: {kimlik}"


async def test_atamasiz_tam_degistirme_bugunku_gibi_null_satiri_dusurur(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, atamasiz
) -> None:
    """Kontrol: kısıtsız yol `apply_lines` (S7: kısıtsızın tam değiştirmesi 'son yazan kazanır')."""
    await _hazirla(seeded_db, dunya)
    resp = await _put(client, dunya, atamasiz, [_satir(dunya.i["i2"], "1", dunya.s1)])
    assert resp.status_code == 200 and resp.json()["dropped_orphan_count"] == 1


# ------------------------------------------------------------------ 409 sızıntısı


async def test_eski_istemci_409u_yalniz_gorunur_satirlara_bakar_gizli_bolumlu_satir_sizmaz(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, atamasiz
) -> None:
    """e3'te elek'in BÖLÜMLÜ satırı (I2·S1) var; civil'in kendi satırı bölümsüzdür. Eski imzalı
    gövde (`section_id` anahtarı yok): kısıtsız 409 alır (kayıtta bölümlü satır var), civil ALMAZ —
    aksi hâlde 409, başka disiplinin bölümlü satırının varlığını sızdırırdı."""
    await _hazirla(seeded_db, dunya)
    eski = [_satir(dunya.i["i1"], "3", bolum_anahtari=False)]
    kontrol = await _put(client, dunya, atamasiz, eski)
    assert kontrol.status_code == 409, kontrol.text
    resp = await _put(client, dunya, civil_yazar, eski)
    assert resp.status_code == 200, resp.text


# ------------------------------------------------------------------ Ü7: gövde kalemleri


async def test_govdede_yabanci_eslemesiz_ve_olmayan_kalem_ayni_422(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, yazar_atamasiz
) -> None:
    await _hazirla(seeded_db, dunya)

    class _Yok:
        id = YOK_KIMLIK

    olmayan = ozet(await _put(client, dunya, civil_yazar, [_satir(_Yok, "1")]))
    assert olmayan[0] == 422
    for kalem in (dunya.i["i2"], dunya.i["i3"]):  # yabancı · eşlemesiz
        yabanci = await _put(client, dunya, civil_yazar, [_satir(kalem, "1")])
        assert ozet(yabanci) == olmayan
    pozitif = await _put(client, dunya, yazar_atamasiz, [_satir(dunya.i["i2"], "1", dunya.s1)])
    assert pozitif.status_code == 200, pozitif.text  # aynı rol, atamasız → 422 DEĞİL


# ------------------------------------------------------------------ süzülmüş yanıtlar + audit


def _yabanci_yok(govde: dict, dunya: Dunya) -> None:
    kodlar = {ln["code"] for ln in govde["lines"]}
    assert "02.001" not in kodlar and "03.001" not in kodlar and "00.000" not in kodlar
    ids = {ln["boq_item_id"] for ln in govde["lines"]}
    assert str(dunya.i["i2"].id) not in ids and str(dunya.i["i3"].id) not in ids
    assert None not in ids


async def test_yazma_yanitlari_suzulmustur_ve_toplam_gorunur_satirlardandir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, admin_kisitli
) -> None:
    await _hazirla(seeded_db, dunya)
    patch = await client.patch(
        f"/diary/{dunya.gunluk['e3'].id}", headers=civil_yazar, json={"work_done": "x"}
    )
    assert patch.status_code == 200, patch.text
    _yabanci_yok(patch.json(), dunya)
    assert Decimal(patch.json()["lines_total"]) == Decimal(2) * Decimal(10)  # yalnız I1: 2 × 10
    put = await _put(client, dunya, civil_yazar, [_satir(dunya.i["i1"], "4")])
    _yabanci_yok(put.json(), dunya)
    yeniden = await client.post(f"/diary/{dunya.gunluk['e1'].id}/reopen", headers=admin_kisitli)
    assert yeniden.status_code == 200, yeniden.text
    _yabanci_yok(yeniden.json(), dunya)
    assert {ln["boq_item_id"] for ln in yeniden.json()["lines"]} == {str(dunya.i["i1"].id)}


async def test_kisitli_gunluk_olusturur_iskelet_tum_kalemler_yanit_ve_audit_suzulur(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar
) -> None:
    once = await audit_kimlikleri(seeded_db)
    resp = await client.post(
        f"/sites/{dunya.santiye.id}/diary", headers=civil_yazar, json={"entry_date": "2026-05-12"}
    )
    assert resp.status_code == 201, resp.text  # S5: kısıtlı oluşturabilir
    assert [ln["code"] for ln in resp.json()["lines"]] == ["01.001"]
    kayit = await seeded_db.execute(
        text("SELECT count(*) FROM site_diary_lines WHERE entry_id = :e"),
        {"e": uuid.UUID(resp.json()["id"])},
    )
    assert kayit.scalar_one() == 3  # iskelet TÜM kalemler için açılır
    detay = (await yeni_audit(seeded_db, once))[0]["detail"]
    assert detay == messages.site_diary_entry_created(
        dunya.proje.name, dunya.santiye.name, date(2026, 5, 12), 1
    )


async def test_put_lines_audit_sayisi_govdedeki_satir_sayisidir(
    client: AsyncClient, seeded_db: AsyncSession, dunya: Dunya, civil_yazar, atamasiz
) -> None:
    await _hazirla(seeded_db, dunya)
    once = await audit_kimlikleri(seeded_db)
    await _put(client, dunya, civil_yazar, [_satir(dunya.i["i1"], "4")])
    detay = (await yeni_audit(seeded_db, once))[0]["detail"]
    assert detay == messages.site_diary_lines_saved(
        dunya.proje.name, dunya.santiye.name, GUN3, 1
    )  # kayıtta 4 satır var; kısıtlı yalnız kendi kapsamını görür
    once = await audit_kimlikleri(seeded_db)
    await _put(client, dunya, atamasiz, [_satir(dunya.i["i2"], "1", dunya.s1)])
    detay = (await yeni_audit(seeded_db, once))[0]["detail"]
    assert detay.endswith("· 1 poz")  # kısıtsız: kayıt satır sayısı (bugünkü gibi)


# ------------------------------------------------------------------ Ü5: silme


async def test_gunluk_silme_sadece_sistem_yoneticisi_disiplin_atanmis_olsa_da_siler(
    client: AsyncClient, dunya: Dunya, civil_yazar, yazar_atamasiz, atamasiz, admin_kisitli
) -> None:
    """SIL-B1 (K4): silme YALNIZ Sistem Yöneticisi'nindir — "kendi taslağını siler" istisnası YOK.
    Disiplin kısıtı DELETE'te uygulanmaz: disiplin atanmış Sistem Yöneticisi de siler."""
    url = f"/sites/{dunya.santiye.id}/diary"
    kendi_civil = await client.post(url, headers=civil_yazar, json={"entry_date": "2026-05-12"})
    kendi_atamasiz = await client.post(
        url, headers=yazar_atamasiz, json={"entry_date": "2026-05-13"}
    )
    for baslik, gun in ((civil_yazar, kendi_civil), (yazar_atamasiz, kendi_atamasiz)):
        resp = await client.delete(f"/diary/{gun.json()['id']}", headers=baslik)
        assert (resp.status_code, resp.json()) == (403, SISYON_ONLY)  # kendi taslağı da 403
    # SIL-B1: disiplin kısıtı DELETE'te UYGULANMAZ: disiplin atanmış Sistem Yöneticisi siler
    kisitli = await client.delete(f"/diary/{kendi_civil.json()['id']}", headers=admin_kisitli)
    assert kisitli.status_code == 204, kisitli.text
    pozitif = await client.delete(f"/diary/{kendi_atamasiz.json()['id']}", headers=atamasiz)
    assert pozitif.status_code == 204, pozitif.text
