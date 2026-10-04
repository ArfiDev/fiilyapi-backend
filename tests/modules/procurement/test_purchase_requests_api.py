"""SA T2 — satın alma talebi uçları (`/purchase-requests`).

Spec: `docs/superpowers/specs/2026-08-12-sa-satinalma-design.md` §2, §3, §4.
Mockup: `Form - Satinalma Talebi.dc.html` (**FST**) · `Satınalma & Teklif.dc.html`
(**SAT**).

Bu dosyanın DONDURDUĞU kararlar:

1. **TASLAK-FARKINDALIKLI ZORUNLULUK (P6 emsali).** `draft` kaydederken YALNIZ
   proje zorunludur: ihtiyaç tarihi boş, kalem listesi boş olabilir (FST'nin
   "Taslak Kaydet" düğmesi yarım formu kaydeder). SIKI doğrulama `submit`te
   koşar ve **T3'ün işidir** — T2 yalnız kuralın kendisini
   (`validation.submit_blockers`) hazırlar.
2. **KALEM İKİ KAPILIDIR — XOR.** Kalem ya bir stok KARTINA bağlanır
   (`stock_item_id`, FST 104) ya da KATALOGSUZDUR
   (`free_text_name` + `free_text_unit`, FST "yeni malzeme tanımla"). İkisi
   birden ya da hiçbiri → **422** (biçim/kural ihlali). `quantity > 0` → 422.
3. **DURUM KODU KANONU (ST §4b).** Gövde içi VARLIK referansı (`project_id`,
   `site_id`, `section_id`, `stock_item_id`) görünmez/yok → **404**;
   biçim/kural ihlali → **422**. Yanlış durumda mutasyon → **409**.
4. **TÜREVLER KOLON DEĞİLDİR:** satır tutarı (`quantity × estimated_unit_price`),
   talebin tahmini toplamı ve **"Mevcut Stok"** (FST 75, ST bakiyesinden)
   yanıtta hesaplanır. `purchase_requests`ta tutar kolonu YOKTUR.
5. **PATCH ve DELETE yalnız `draft`ta** (spec §4); değilse **409**. `can_delete`
   bayrağı liste ve detayda döner ve AKTÖRE göredir.
6. **Numarayı istemci GÖNDEREMEZ**: `request_no` sunucu üretir (`SAT-YYYY-NNNN`).
"""

import uuid
from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core import timezone
from app.modules.procurement.models import PurchaseRequest, PurchaseRequestLine

pytestmark = pytest.mark.asyncio

_YOL = "/purchase-requests"


def _govde(project_id, **alanlar) -> dict:
    govde: dict = {"project_id": str(project_id)}
    govde.update(alanlar)
    return govde


def _kart_satiri(item_id, **alanlar) -> dict:
    satir = {"stock_item_id": str(item_id), "quantity": "15.000"}
    satir.update(alanlar)
    return satir


async def _sayimlar(session) -> tuple[int, int]:
    baslik = (await session.execute(select(func.count()).select_from(PurchaseRequest))).scalar_one()
    satir = (
        await session.execute(select(func.count()).select_from(PurchaseRequestLine))
    ).scalar_one()
    return baslik, satir


# --- Taslak-farkındalıklı oluşturma (FST "Taslak Kaydet") ---


async def test_taslak_yalniz_projeyle_kaydedilir(client, sef_headers, gorunen_proje):
    """P6 emsali: taslak GEVŞEKTİR — ihtiyaç tarihi ve kalem olmadan da kaydedilir.

    FST'de `İhtiyaç Tarihi` yıldızlıdır ama o yıldız "Onaya Gönder" içindir;
    "Taslak Kaydet" yarım formu saklayabilmelidir.
    """
    yanit = await client.post(_YOL, json=_govde(gorunen_proje.id), headers=sef_headers)

    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["status"] == "draft"
    assert govde["priority"] == "normal"
    assert govde["needed_by"] is None
    assert govde["lines"] == []
    assert govde["request_date"] == timezone.today().isoformat()


async def test_talep_fst_alanlariyla_kaydedilir(
    client, sef_headers, gorunen_proje, gorunen_santiye, gorunen_bolum, kart_fabrikasi
):
    """FST 53-58 + gerekçe + teklif son tarihi + iki kalem."""
    kart = await kart_fabrikasi("SNK-0421")

    yanit = await client.post(
        _YOL,
        json=_govde(
            gorunen_proje.id,
            request_date="2026-07-27",
            priority="urgent",
            site_id=str(gorunen_santiye.id),
            section_id=str(gorunen_bolum.id),
            needed_by="2026-08-03",
            justification="Kat 9 kolon demiri için stok kritik seviyede.",
            quote_deadline="2026-07-30",
            lines=[
                _kart_satiri(kart.id, estimated_unit_price="21500.00"),
                {
                    "free_text_name": "PP-R Boru 32mm",
                    "free_text_unit": "Metre",
                    "quantity": "200.000",
                    "estimated_unit_price": "92.00",
                },
            ],
        ),
        headers=sef_headers,
    )

    assert yanit.status_code == 201, yanit.text
    govde = yanit.json()
    assert govde["priority"] == "urgent"
    assert govde["site_id"] == str(gorunen_santiye.id)
    assert govde["section_id"] == str(gorunen_bolum.id)
    assert govde["needed_by"] == "2026-08-03"
    assert govde["quote_deadline"] == "2026-07-30"
    assert len(govde["lines"]) == 2


async def test_talep_numarasi_sunucu_uretir_istemci_gonderemez(client, sef_headers, gorunen_proje):
    """`SAT-YYYY-NNNN` (§7 S6). Gövdedeki `request_no` YOK SAYILIR."""
    yil = timezone.today().year
    ilk = await client.post(
        _YOL, json=_govde(gorunen_proje.id, request_no="SAT-1999-9999"), headers=sef_headers
    )
    assert ilk.status_code == 201, ilk.text
    assert ilk.json()["request_no"] == f"SAT-{yil}-0001"

    ikinci = await client.post(_YOL, json=_govde(gorunen_proje.id), headers=sef_headers)
    assert ikinci.json()["request_no"] == f"SAT-{yil}-0002"


async def test_durum_istemciden_gelmez(client, sef_headers, gorunen_proje):
    """Durum geçişleri T3'ün işidir; POST her zaman `draft` yazar."""
    yanit = await client.post(
        _YOL, json=_govde(gorunen_proje.id, status="ordered"), headers=sef_headers
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["status"] == "draft"


async def test_acilmayan_talep_uclari_bekcisi(client, admin_headers, gorunen_proje):
    """Bekçi: `submit`/`approve`/`reject` ve teklif alt-kaynağı T3'te AÇILDI;
    kalıcı olarak AÇILMAYANLAR ise hâlâ 404/405 vermelidir (spec §5).

    * `unsubmit`/`unapprove` — geri çekme geçişi YOKTUR (matris dört geçişlidir).
    * `deliver` — teslim damgasını stok girişi atar (§7 S4, T4).
    * `approvals` — çok adımlı onay MOTORU hiçbir dilimde açılmaz (§7 S2).
    """
    olustur = await client.post(_YOL, json=_govde(gorunen_proje.id), headers=admin_headers)
    talep_id = olustur.json()["id"]
    for yol in ("unsubmit", "unapprove", "deliver", "approvals"):
        yanit = await client.post(f"{_YOL}/{talep_id}/{yol}", json={}, headers=admin_headers)
        assert yanit.status_code == 404, f"{yol} → {yanit.status_code}"


# --- XOR + miktar (biçim/kural ihlali = 422) ---


async def test_kalem_xor_ihlalleri_422(client, sef_headers, gorunen_proje, kart_fabrikasi):
    """İkisi birden dolu ya da ikisi birden boş → 422; eksik birim → 422."""
    kart = await kart_fabrikasi("SNK-0421")

    async def _kod(satir: dict) -> int:
        yanit = await client.post(
            _YOL, json=_govde(gorunen_proje.id, lines=[satir]), headers=sef_headers
        )
        return yanit.status_code

    assert await _kod({"quantity": "1.000"}) == 422
    assert (
        await _kod(
            {
                "stock_item_id": str(kart.id),
                "free_text_name": "PP-R Boru",
                "free_text_unit": "Metre",
                "quantity": "1.000",
            }
        )
        == 422
    )
    assert await _kod({"free_text_name": "PP-R Boru", "quantity": "1.000"}) == 422
    assert await _kod({"free_text_unit": "Metre", "quantity": "1.000"}) == 422


async def test_sifir_ve_negatif_miktar_422(client, sef_headers, gorunen_proje, kart_fabrikasi):
    """`quantity > 0` — ST'nin negatif düzeltme istisnası TALEPTE YOKTUR."""
    kart = await kart_fabrikasi("SNK-0421")
    for miktar in ("0.000", "-3.000"):
        yanit = await client.post(
            _YOL,
            json=_govde(gorunen_proje.id, lines=[_kart_satiri(kart.id, quantity=miktar)]),
            headers=sef_headers,
        )
        assert yanit.status_code == 422, miktar


async def test_gerekce_tavani_422(client, sef_headers, gorunen_proje):
    """TB4 standardı: `Text` kolonlu serbest metnin tavanı ŞEMADADIR."""
    from app.core.text import FREE_TEXT_MAX_LENGTH

    yanit = await client.post(
        _YOL,
        json=_govde(gorunen_proje.id, justification="x" * (FREE_TEXT_MAX_LENGTH + 1)),
        headers=sef_headers,
    )
    assert yanit.status_code == 422


# --- DURUM KODU KANONU (ST §4b) ---


async def test_durum_kodu_kurali_govde_ici_varlik_referansi_404(
    client,
    sef_headers,
    satinalma_headers,
    gorunen_proje,
    gorunmeyen_proje,
    gorunmeyen_santiye,
    gorunen_bolum,
    kart_fabrikasi,
):
    """Kuralın TEK bekçisi: **gövde içi VARLIK referansı = 404 · biçim/kural
    ihlali = 422** (ST §4b kanonu, 2026-08-11 kullanıcı kararı).

    Dört varlık referansı da AYNI kodu döndürür — biri 422'ye kayarsa bu test
    kırılır. Son iddia KARŞI ÖRNEKTİR: biçim ihlali 422 olarak KALIR, yoksa
    kural "her şey 404" diye yozlaşırdı.
    """
    kart = await kart_fabrikasi("SNK-0421")

    async def _kod(govde: dict, headers=satinalma_headers) -> int:
        return (await client.post(_YOL, json=govde, headers=headers)).status_code

    # 1) proje YOK · 2) proje GÖRÜNMÜYOR · 3) şantiye başka projede/görünmüyor
    assert await _kod(_govde(uuid.uuid4())) == 404
    assert await _kod(_govde(gorunmeyen_proje.id)) == 404
    assert await _kod(_govde(gorunen_proje.id, site_id=str(gorunmeyen_santiye.id))) == 404
    # 4) bölüm YOK · 5) bölüm başka şantiyenin (şantiye verilmedi)
    assert await _kod(_govde(gorunen_proje.id, section_id=str(uuid.uuid4()))) == 404
    assert await _kod(_govde(gorunen_proje.id, section_id=str(gorunen_bolum.id))) == 404
    # 6) kalemin stok kartı YOK
    assert await _kod(_govde(gorunen_proje.id, lines=[_kart_satiri(uuid.uuid4())])) == 404

    # KARŞI ÖRNEK: biçim/kural ihlali 422 KALIR (XOR ihlali)
    assert await _kod(_govde(gorunen_proje.id, lines=[{"quantity": "1.000"}])) == 422
    # KARŞI ÖRNEK: geçerli gövde 201 KALIR
    assert await _kod(_govde(gorunen_proje.id, lines=[_kart_satiri(kart.id)]), sef_headers) == 201


async def test_bozuk_satir_hicbir_sey_yazmaz(
    client, sef_headers, seeded_db, gorunen_proje, kart_fabrikasi
):
    """ATOMİKLİK: ikinci satırın kartı YOK → ne başlık ne ilk satır yazılır."""
    kart = await kart_fabrikasi("SNK-0421")
    once = await _sayimlar(seeded_db)

    yanit = await client.post(
        _YOL,
        json=_govde(
            gorunen_proje.id,
            lines=[_kart_satiri(kart.id), _kart_satiri(uuid.uuid4(), quantity="3.000")],
        ),
        headers=sef_headers,
    )

    assert yanit.status_code == 404, yanit.text
    assert await _sayimlar(seeded_db) == once


# --- Türevler: satır tutarı · tahmini toplam · Mevcut Stok ---


async def test_turev_tutarlar_ve_mevcut_stok(
    client,
    sef_headers,
    gorunen_proje,
    gorunen_santiye,
    kart_fabrikasi,
    depo_fabrikasi,
    stok_girisi_fabrikasi,
):
    """FST 75 "Mevcut Stok" + satır tutarı + FST "TAHMİNİ TOPLAM".

    Katalogsuz (free_text) kalemin bakiyesi YOKTUR ve **`null`dur** — sıfır
    yazılsaydı ekran "stokta yok" ile "stok kartı bile yok"u ayırt edemezdi.
    """
    kart = await kart_fabrikasi("SNK-0421")
    depo = await depo_fabrikasi("D-1 Ambar", site=gorunen_santiye)
    await stok_girisi_fabrikasi(depo, kart, "2.400")

    olustur = await client.post(
        _YOL,
        json=_govde(
            gorunen_proje.id,
            lines=[
                _kart_satiri(kart.id, estimated_unit_price="21500.00"),
                {
                    "free_text_name": "PP-R Boru 32mm",
                    "free_text_unit": "Metre",
                    "quantity": "200.000",
                    "estimated_unit_price": "92.00",
                },
            ],
        ),
        headers=sef_headers,
    )
    assert olustur.status_code == 201, olustur.text

    detay = await client.get(f"{_YOL}/{olustur.json()['id']}", headers=sef_headers)
    assert detay.status_code == 200, detay.text
    govde = detay.json()

    # Satırlar GÖVDEDEKİ SIRAYLA döner (`sort_order`, T3'te açılan kolon): T2'de
    # sıra korunmadığı için satırlar ADA göre çözülüyordu; borç kapandı.
    assert [s["sort_order"] for s in govde["lines"]] == [0, 1]
    kart_satiri, serbest_satir = govde["lines"]
    assert kart_satiri["name"] == "Nervürlü Demir Ø12"
    assert serbest_satir["name"] == "PP-R Boru 32mm"
    assert Decimal(kart_satiri["line_total"]) == Decimal("322500.00")
    assert Decimal(kart_satiri["current_stock"]) == Decimal("2.400")
    assert kart_satiri["stock_item_code"] == "SNK-0421"
    assert kart_satiri["unit"] == "Ton"

    assert Decimal(serbest_satir["line_total"]) == Decimal("18400.00")
    assert serbest_satir["current_stock"] is None
    assert serbest_satir["unit"] == "Metre"

    assert Decimal(govde["estimated_total"]) == Decimal("340900.00")


async def test_fiyatsiz_kalem_tutari_null_toplam_sifir(
    client, sef_headers, gorunen_proje, kart_fabrikasi
):
    """Tahmini fiyat isteğe bağlıdır: satır tutarı `null`dur ve toplama GİRMEZ —
    sessizce sıfır sayılsaydı "toplam neden düşük" sorusu cevapsız kalırdı."""
    kart = await kart_fabrikasi("SNK-0421")
    olustur = await client.post(
        _YOL, json=_govde(gorunen_proje.id, lines=[_kart_satiri(kart.id)]), headers=sef_headers
    )
    govde = olustur.json()
    assert govde["lines"][0]["line_total"] is None
    assert Decimal(govde["estimated_total"]) == Decimal("0")


async def test_kalem_bakiyeleri_tek_toplu_sorgudur(
    client,
    sef_headers,
    gorunen_proje,
    gorunen_santiye,
    kart_fabrikasi,
    depo_fabrikasi,
    stok_girisi_fabrikasi,
):
    """Kalem başına bakiye sorgusu AÇILMAZ: kalem sayısı üçe katlanınca stok
    tablolarına giden sorgu sayısı DEĞİŞMEMELİDİR (ölçüm `before_cursor_execute`)."""
    from contextlib import contextmanager

    from sqlalchemy import event

    from tests.conftest import test_engine

    @contextmanager
    def _sayac():
        ifadeler: list[str] = []

        def kaydet(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
            ifadeler.append(" ".join(statement.split()))

        event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
        try:
            yield ifadeler
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)

    def _stok(ifadeler: list[str]) -> list[str]:
        return [i for i in ifadeler if "stock_entry_lines" in i or "stock_items" in i]

    depo = await depo_fabrikasi("D-1 Ambar", site=gorunen_santiye)
    kartlar = []
    for i in range(3):
        kart = await kart_fabrikasi(f"SNK-{i:04d}")
        await stok_girisi_fabrikasi(depo, kart, "5.000")
        kartlar.append(kart)

    async def _detay_id(adet: int) -> str:
        yanit = await client.post(
            _YOL,
            json=_govde(gorunen_proje.id, lines=[_kart_satiri(k.id) for k in kartlar[:adet]]),
            headers=sef_headers,
        )
        assert yanit.status_code == 201, yanit.text
        return yanit.json()["id"]

    tek = await _detay_id(1)
    uc = await _detay_id(3)

    with _sayac() as ifadeler:
        assert (await client.get(f"{_YOL}/{tek}", headers=sef_headers)).status_code == 200
    tek_sayi = len(_stok(ifadeler))

    with _sayac() as ifadeler:
        assert (await client.get(f"{_YOL}/{uc}", headers=sef_headers)).status_code == 200
    uc_sayi = len(_stok(ifadeler))

    assert uc_sayi == tek_sayi, f"N+1: {tek_sayi} → {uc_sayi}"


# --- Liste: süzgeçler + TB3 sayfalama ---


async def test_liste_suzgecleri(
    client, sef_headers, satinalma_headers, gorunen_proje, gorunen_santiye
):
    """SAT filtre çubuğu: durum · proje · öncelik · arama. Süzgeçler AND'lidir."""
    acil = await client.post(
        _YOL,
        json=_govde(gorunen_proje.id, priority="urgent", justification="Kolon demiri"),
        headers=sef_headers,
    )
    assert acil.status_code == 201, acil.text
    normal = await client.post(
        _YOL, json=_govde(gorunen_proje.id, justification="Boya işi"), headers=sef_headers
    )
    assert normal.status_code == 201, normal.text

    async def _numaralar(**sorgu) -> list[str]:
        yanit = await client.get(_YOL, params=sorgu, headers=satinalma_headers)
        assert yanit.status_code == 200, yanit.text
        return [i["request_no"] for i in yanit.json()["items"]]

    assert await _numaralar(priority="urgent") == [acil.json()["request_no"]]
    assert await _numaralar(status="draft") == sorted(
        [acil.json()["request_no"], normal.json()["request_no"]], reverse=True
    )
    assert await _numaralar(status="ordered") == []
    assert await _numaralar(project_id=str(gorunen_proje.id)) != []
    assert await _numaralar(q=acil.json()["request_no"][-4:]) == [acil.json()["request_no"]]
    assert await _numaralar(q="boya") == [normal.json()["request_no"]]


async def test_liste_sayfalama_ve_limit_tavani(client, sef_headers, gorunen_proje):
    for _ in range(3):
        assert (
            await client.post(_YOL, json=_govde(gorunen_proje.id), headers=sef_headers)
        ).status_code == 201

    yanit = await client.get(_YOL, params={"limit": 2}, headers=sef_headers)
    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert len(govde["items"]) == 2
    assert govde["total"] == 3

    assert (await client.get(_YOL, params={"limit": 201}, headers=sef_headers)).status_code == 422


async def test_liste_satirinda_tahmini_toplam_ve_can_delete(
    client, sef_headers, admin_headers, gorunen_proje, kart_fabrikasi
):
    """SAT tablosu tutar sütunu TÜREVDİR; `can_delete` liste satırında da döner.

    SIL-B1 (K4): bayrak YALNIZ Sistem Yöneticisi için `true`dur — talebi açan şef kendi taslağını
    da silemez (`false`)."""
    kart = await kart_fabrikasi("SNK-0421")
    assert (
        await client.post(
            _YOL,
            json=_govde(
                gorunen_proje.id,
                lines=[_kart_satiri(kart.id, estimated_unit_price="21500.00")],
            ),
            headers=sef_headers,
        )
    ).status_code == 201

    satir = (await client.get(_YOL, headers=sef_headers)).json()["items"][0]
    assert Decimal(satir["estimated_total"]) == Decimal("322500.00")
    assert satir["line_count"] == 1
    assert satir["can_delete"] is False
    admin_satiri = (await client.get(_YOL, headers=admin_headers)).json()["items"][0]
    assert admin_satiri["can_delete"] is True
    # Liste satırı KALEMLERİ TAŞIMAZ — tablo onları göstermez, taşımak her
    # satırda ikinci bir sorgu demek olurdu.
    assert "lines" not in satir


# --- IDOR ---


async def test_gorunmeyen_projenin_talebi_listede_yok_ve_detayda_404(
    client, admin_headers, satinalma_headers, gorunmeyen_proje
):
    """Görünmeyen projenin talebi VAR OLMAYANLA ayırt edilemez (404)."""
    olustur = await client.post(_YOL, json=_govde(gorunmeyen_proje.id), headers=admin_headers)
    assert olustur.status_code == 201, olustur.text
    talep_id = olustur.json()["id"]

    liste = await client.get(_YOL, headers=satinalma_headers)
    assert liste.json()["items"] == []
    assert liste.json()["total"] == 0

    for cagri in (
        client.get(f"{_YOL}/{talep_id}", headers=satinalma_headers),
        client.patch(f"{_YOL}/{talep_id}", json={"priority": "urgent"}, headers=satinalma_headers),
    ):
        assert (await cagri).status_code == 404
    # SIL-B1: DELETE kapısı (yalnız Sistem Yöneticisi) önce koşar → 403, varlık sızmaz.
    silme = await client.delete(f"{_YOL}/{talep_id}", headers=satinalma_headers)
    assert silme.status_code == 403


# --- PATCH: yalnız draft ---


async def test_patch_taslakta_alan_ve_kalem_degistirir(
    client, sef_headers, gorunen_proje, kart_fabrikasi
):
    """Kalemler gövdede REPLACE edilir: gönderilen liste eskisinin YERİNE geçer."""
    kart = await kart_fabrikasi("SNK-0421")
    olustur = await client.post(
        _YOL, json=_govde(gorunen_proje.id, lines=[_kart_satiri(kart.id)]), headers=sef_headers
    )
    talep_id = olustur.json()["id"]

    yanit = await client.patch(
        f"{_YOL}/{talep_id}",
        json={
            "priority": "critical",
            "needed_by": "2026-08-03",
            "lines": [
                {
                    "free_text_name": "PP-R Boru 32mm",
                    "free_text_unit": "Metre",
                    "quantity": "200.000",
                }
            ],
        },
        headers=sef_headers,
    )

    assert yanit.status_code == 200, yanit.text
    govde = yanit.json()
    assert govde["priority"] == "critical"
    assert govde["needed_by"] == "2026-08-03"
    assert len(govde["lines"]) == 1
    assert govde["lines"][0]["free_text_name"] == "PP-R Boru 32mm"


async def test_patch_kalem_gondermezse_kalemler_korunur(
    client, sef_headers, gorunen_proje, kart_fabrikasi
):
    """`lines` GÖNDERİLMEMESİ ile boş liste göndermek FARKLIDIR: ilki dokunmaz,
    ikincisi hepsini siler (`exclude_unset` dersi)."""
    kart = await kart_fabrikasi("SNK-0421")
    olustur = await client.post(
        _YOL, json=_govde(gorunen_proje.id, lines=[_kart_satiri(kart.id)]), headers=sef_headers
    )
    talep_id = olustur.json()["id"]

    dokunma = await client.patch(
        f"{_YOL}/{talep_id}", json={"priority": "urgent"}, headers=sef_headers
    )
    assert len(dokunma.json()["lines"]) == 1

    bosalt = await client.patch(f"{_YOL}/{talep_id}", json={"lines": []}, headers=sef_headers)
    assert bosalt.json()["lines"] == []


async def test_taslak_disinda_patch_ve_delete_409(
    client, sef_headers, admin_headers, seeded_db, gorunen_proje
):
    """Spec §4: PATCH/DELETE YALNIZ `draft`ta. Durum çakışması **409**tur —
    404 (yok) ya da 403 (yetki) değil: kullanıcının yetkisi VARDIR, engelleyen
    şey kaydın DURUMUDUR."""
    olustur = await client.post(_YOL, json=_govde(gorunen_proje.id), headers=sef_headers)
    talep_id = olustur.json()["id"]

    kayit = await seeded_db.get(PurchaseRequest, uuid.UUID(talep_id))
    kayit.status = "pending_approval"
    await seeded_db.flush()

    patch = await client.patch(
        f"{_YOL}/{talep_id}", json={"priority": "urgent"}, headers=sef_headers
    )
    assert patch.status_code == 409, patch.text
    sil = await client.delete(f"{_YOL}/{talep_id}", headers=admin_headers)  # SIL-B1: yalnız SisYön
    assert sil.status_code == 409, sil.text

    detay = await client.get(f"{_YOL}/{talep_id}", headers=sef_headers)
    assert detay.json()["can_delete"] is False


# --- DELETE ---


async def test_taslak_silinir_kalemler_de_gider(
    client, sef_headers, admin_headers, seeded_db, gorunen_proje, kart_fabrikasi
):
    kart = await kart_fabrikasi("SNK-0421")
    olustur = await client.post(
        _YOL, json=_govde(gorunen_proje.id, lines=[_kart_satiri(kart.id)]), headers=sef_headers
    )
    assert await _sayimlar(seeded_db) == (1, 1)

    # SIL-B1 (K4): talebi açan şef kendi taslağını da silemez; yalnız Sistem Yöneticisi siler.
    yasak = await client.delete(f"{_YOL}/{olustur.json()['id']}", headers=sef_headers)
    assert yasak.status_code == 403, yasak.text
    assert await _sayimlar(seeded_db) == (1, 1)
    yanit = await client.delete(f"{_YOL}/{olustur.json()['id']}", headers=admin_headers)
    assert yanit.status_code == 204, yanit.text
    assert await _sayimlar(seeded_db) == (0, 0)


async def test_baskasinin_taslagini_silmek_403_admin_siler(
    client, sef_headers, satinalma_headers, admin_headers, gorunen_proje
):
    """`can_delete` (`app/core/access.py`) taslak istisnası: taslağı AÇAN siler.

    `procurement` rolü `full`dür ama başkasının taslağını SİLEMEZ (403) —
    `full` silmeyi kapsamaz. `admin` koşulsuz siler.
    """
    olustur = await client.post(_YOL, json=_govde(gorunen_proje.id), headers=sef_headers)
    talep_id = olustur.json()["id"]

    yabanci = await client.get(f"{_YOL}/{talep_id}", headers=satinalma_headers)
    assert yabanci.json()["can_delete"] is False
    assert (await client.delete(f"{_YOL}/{talep_id}", headers=satinalma_headers)).status_code == 403

    assert (await client.get(f"{_YOL}/{talep_id}", headers=admin_headers)).json()["can_delete"]
    assert (await client.delete(f"{_YOL}/{talep_id}", headers=admin_headers)).status_code == 204


# --- Yetki ---


async def test_yetkisiz_rol_okumada_bile_403(client, yetkisiz_headers):
    assert (await client.get(_YOL, headers=yetkisiz_headers)).status_code == 403


# --- Submit doğrulaması (kural T2'de hazırlanır, UÇ T3'te açılır) ---


async def test_submit_engelleri_taslak_gevsekligini_tamamlar():
    """`validation.submit_blockers` — SIKI tarafın TEK kaynağı.

    T2 onu yalnız HAZIRLAR (uç T3'te açılır); kuralın burada kilitlenmesi,
    T3'ün ikinci bir kopya yazmasını engeller.
    """
    from app.modules.procurement import validation

    class _Talep:
        needed_by = None

    class _Satir:
        stock_item_id = None
        free_text_name = None
        free_text_unit = None
        estimated_unit_price = None

    eksik = validation.submit_blockers(_Talep(), [])
    assert len(eksik) == 2, eksik

    # Kaynaksız VE fiyatsız satır İKİ engel üretir (T5 bulgusu: tahmini fiyat
    # `submit`te zorunludur — ₺500K eşiği o toplamdan hesaplanır).
    _Talep.needed_by = date(2026, 8, 3)
    assert validation.submit_blockers(_Talep(), [_Satir()]) == [
        validation.LINE_SOURCE_REQUIRED,
        validation.LINE_PRICE_REQUIRED,
    ]

    _Satir.free_text_name, _Satir.free_text_unit = "PP-R Boru", "Metre"
    assert validation.submit_blockers(_Talep(), [_Satir()]) == [validation.LINE_PRICE_REQUIRED]

    _Satir.estimated_unit_price = Decimal("92.00")
    assert validation.submit_blockers(_Talep(), [_Satir()]) == []


# --- Denetim ---


async def test_mutasyonlar_denetime_yazilir(
    client, sef_headers, admin_headers, seeded_db, gorunen_proje
):
    from app.modules.audit.models import AuditLog

    olustur = await client.post(_YOL, json=_govde(gorunen_proje.id), headers=sef_headers)
    talep_id = olustur.json()["id"]
    numara = olustur.json()["request_no"]
    await client.patch(f"{_YOL}/{talep_id}", json={"priority": "urgent"}, headers=sef_headers)
    await client.delete(f"{_YOL}/{talep_id}", headers=admin_headers)

    kayitlar = (
        (await seeded_db.execute(select(AuditLog).order_by(AuditLog.occurred_at))).scalars().all()
    )
    detaylar = [k.detail for k in kayitlar if numara in (k.detail or "")]
    assert len(detaylar) == 3
