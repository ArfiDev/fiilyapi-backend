"""OK-1C T1 — ZİNCİR ADIMININ ONAY ROLÜ, MODÜL KAPISINI İKAME EDER.

## Bu dilim neden var

OK-1A onay zinciri motorunu canlıya aldı ama zincir FİİLEN İŞLETİLEMİYOR: bir
adım, adını taşıyan SİSTEM rolüyle geçilemiyor. Ölçülmüş üç kapalı adım:

| Aile | Adım | Sistem rolü | Modül kapısı | Bugün |
|---|---|---|---|---|
| taşeron hakedişi | 1 (`site_chief`) | `site_chief` | `progress_payments ≥ approve` | **403** |
| satınalma | 3 (`accounting`) | `accounting` | `procurement ≥ approve` | **403** |
| satınalma eşik üstü | 2 (`project_manager`) | `project_manager` | servis: `full` | **403** |

İzin matrisi (`roles/seed_data.py`): `progress_payments` satırında `site_chief`
**draft**, `procurement` satırında `accounting` **none**. Yani zincirin
tanımladığı imzacı, uçtan İÇERİ GİREMİYOR.

Kullanıcı kararı (2026-08-22): **zincir adımının onay rolü, modül kapısını
İKAME EDER.** Bir kullanıcı o adımın onay rolünü taşıyorsa, modül seviyesi
yetmese bile O ADIMI onaylayabilir.

## 🔴 108 TESTİN HİÇBİRİ BUNU DENEMİYORDU

OK-1A'nın 108 zincir testinin hiçbiri `site_chief` SİSTEM rolüyle onay
denemedi; testler boşluğun ETRAFINDAN dolaştı.
`tests/subcontractor_progress_payments/test_ok1a_chain_binding.py` docstring'i
bunu açıkça itiraf ediyor: "`site_chief` ONAY ROLÜNÜ taşıyan aktör burada
`project_manager` SİSTEM rolündedir." Bu dosya deponun o boşluk üzerindeki
İLK gerçek kırmızısıdır.

## İkamenin SINIRI (dar kapsam)

İkame yalnız **o zincir adımının kararı** içindir; aynı modülün başka hiçbir
ucunu açmaz. Sınırın kendisi kardeş dosyada bekçilenir
(`test_ok1c_dar_kapsam.py`) — burada yalnız ikamenin ÇALIŞTIĞI ve neye BAĞLI
olduğu ölçülür.

🔴 Bekçilerin beklediği hata metinleri ELLE yazılmıştır; koddan ithal edilip
kendisiyle karşılaştırılmaz — kendi ifadesini teste kopyalayan test hiçbir şey
bekçilemez (sahte-yeşilin ölçülmüş hâllerinden biri).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal

from sqlalchemy import event

from app.modules.approvals import service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from tests.conftest import test_engine
from tests.modules.approvals.conftest import adim_durumlari, proje_rolu_ver, zincir_getir

_TASERON = ApprovalDocumentType.subcontractor_progress_payment
_ISVEREN = ApprovalDocumentType.progress_payment
_SATINALMA = ApprovalDocumentType.purchase_request

_TASERON_YOL = "/subcontractor-progress-payments"
_ISVEREN_YOL = "/progress-payments"
_SATINALMA_YOL = "/purchase-requests"

_YOLLAR = {
    _TASERON: _TASERON_YOL,
    _ISVEREN: _ISVEREN_YOL,
    _SATINALMA: _SATINALMA_YOL,
}

#: 🔴 ELLE YAZILDI (`core/permissions.py:31`den ithal EDİLMEDİ).
_MODUL_KAPISI = "Bu işlem için yetkiniz yok"
#: 🔴 ELLE YAZILDI (`subcontractor .../guards.PAYMENT_MISSING`ten ithal EDİLMEDİ).
_HAKEDIS_YOK = "Hakediş bulunamadı"

_ESIK_ALTI = Decimal("100000.00")
_GEREKCE = {"reason": "Metrajlar eksik, revize edin"}


async def _zincir(seeded_db, tip, document_id, yaratan, amount=_ESIK_ALTI):
    return await service.create_chain(
        seeded_db,
        document_type=tip,
        document_id=document_id,
        amount=amount,
        created_by_user_id=yaratan.id,
    )


async def _adimlari_ilerlet(seeded_db, aktor_fabrikasi, tip, document_id, roller, *, etiket):
    """Zincirin ilk N adımını BAŞKA aktörlerle karara bağlar (görevler ayrılığı).

    Motor doğrudan çağrılır: bu adımlar testin KONUSU değil KURULUMUDUR ve
    kendi modül kapılarına takılmaları ölçülen şeyi bulandırırdı.
    """
    for sira, rol in enumerate(roller):
        vekil = await aktor_fabrikasi(
            f"{etiket}-on{sira}@ok1c.co", full_name=f"Ön İmza {sira}", approval_roles=[rol]
        )
        await service.approve_next_step(
            seeded_db, actor=vekil, document_type=tip, document_id=document_id
        )


@contextmanager
def _sorgu_sayaci() -> Iterator[list[str]]:
    """`test_ok1a_query_count.py:34-45` deseni — sürücüye giden HER ifade."""
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


# --------------------------------------------------------------------------- #
# 1. İKAME ÇALIŞIYOR — taşeron hakedişi ADIM 1
# --------------------------------------------------------------------------- #


async def test_TASERON_adim1_SITE_CHIEF_sistem_rolu_ile_ONAYLANIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Matriste `progress_payments: site_chief = draft`; uç `approve` ister.

    Zincirin 1. adımının rolü `site_chief`tir ve aktör onu TAŞIR ⇒ İKAME.
    Ara adım olduğu için evrak `pending_approval`da KALIR.
    """
    yaratan = await aktor_fabrikasi("ikame-t1-yaratan@ok1c.co", full_name="Sercan Öztürk")
    await aktor_fabrikasi(
        "ikame-t1-sef@ok1c.co",
        role_key="site_chief",
        approval_roles=[ApprovalRole.site_chief],
        full_name="Şantiye Şefi",
    )
    basliklar = await giris("ikame-t1-sef@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    zincir = await _zincir(seeded_db, _TASERON, document_id, yaratan)

    yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "pending_approval", "ara adım evrağı onaylamamalı"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, False, False]


# --------------------------------------------------------------------------- #
# 2. İKAME ÇALIŞIYOR — satınalma ADIM 3
# --------------------------------------------------------------------------- #


async def test_SATINALMA_adim3_ACCOUNTING_sistem_rolu_ile_ONAYLANIR_ve_ZINCIR_BITER(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Matriste `procurement: accounting = none` — muhasebe talebi GÖREMEZ bile.

    Zincirin SON adımı ona aittir; onayladığında zincir tamamlanır ve talep
    `quote_wait`e geçer (§3: onay ARA durum üretmez).
    """
    yaratan = await aktor_fabrikasi("ikame-t2-yaratan@ok1c.co", full_name="Talep Sahibi")
    await aktor_fabrikasi(
        "ikame-t2-muh@ok1c.co",
        role_key="accounting",
        approval_roles=[ApprovalRole.accounting],
        full_name="Muhasebe",
    )
    basliklar = await giris("ikame-t2-muh@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )
    zincir = await _zincir(seeded_db, _SATINALMA, document_id, yaratan, Decimal("10000.00"))
    await _adimlari_ilerlet(
        seeded_db,
        aktor_fabrikasi,
        _SATINALMA,
        document_id,
        [ApprovalRole.procurement, ApprovalRole.project_manager],
        etiket="ikame-t2",
    )

    yanit = await client.post(f"{_SATINALMA_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "quote_wait", "son adım talebi ilerletmeliydi"
    assert await adim_durumlari(seeded_db, zincir.id) == [True, True, True]


# --------------------------------------------------------------------------- #
# 3. RET DE İKAME EDİLİR
# --------------------------------------------------------------------------- #
#
# Onay açılıp ret açılmasaydı zincir TEK YÖNLÜ kilitlenirdi: adımın sahibi
# imzalayabilir ama "revize edilsin" diyemezdi.


async def test_RET_de_ikame_edilir_TASERON_zincir_DAMGALANIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("ikame-t3a-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "ikame-t3a-sef@ok1c.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("ikame-t3a-sef@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    yanit = await client.post(
        f"{_TASERON_YOL}/{document_id}/reject", json=_GEREKCE, headers=basliklar
    )

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "draft"
    # OKT-B1: ret zinciri SİLMEZ, damgalar (eski K2'nin yerini aldı).
    zincir = await zincir_getir(seeded_db, _TASERON, document_id)
    assert zincir is not None and zincir.rejected_at is not None


async def test_RET_de_ikame_edilir_SATINALMA_zincir_DAMGALANIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("ikame-t3b-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "ikame-t3b-muh@ok1c.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    basliklar = await giris("ikame-t3b-muh@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )
    await _zincir(seeded_db, _SATINALMA, document_id, yaratan, Decimal("10000.00"))
    await _adimlari_ilerlet(
        seeded_db,
        aktor_fabrikasi,
        _SATINALMA,
        document_id,
        [ApprovalRole.procurement, ApprovalRole.project_manager],
        etiket="ikame-t3b",
    )

    yanit = await client.post(
        f"{_SATINALMA_YOL}/{document_id}/reject", json=_GEREKCE, headers=basliklar
    )

    assert yanit.status_code == 200, yanit.text
    assert yanit.json()["status"] == "rejected"
    zincir = await zincir_getir(seeded_db, _SATINALMA, document_id)
    assert zincir is not None and zincir.rejected_at is not None


# --------------------------------------------------------------------------- #
# 4. İKAME *SIRADAKİ* ADIMA BAĞLIDIR (K2)
# --------------------------------------------------------------------------- #


async def test_ikame_SIRADAKI_adima_baglidir_rolu_TASIMAK_TEK_BASINA_YETMEZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Zincir adım 1'de beklerken (`procurement`), `accounting` rollü aktör 403.

    🔴 403'ün sebebi KALICI bir yetkisizlik DEĞİL, SIRADIR: aynı aktör aynı
    evrakta, zincir 3. adıma geldiğinde 200 alır. İki iddia birlikte olmasaydı
    "ikame hiç çalışmıyor" da aynı testi geçirirdi.
    """
    yaratan = await aktor_fabrikasi("ikame-t4-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "ikame-t4-muh@ok1c.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    basliklar = await giris("ikame-t4-muh@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )
    await _zincir(seeded_db, _SATINALMA, document_id, yaratan, Decimal("10000.00"))

    erken = await client.post(f"{_SATINALMA_YOL}/{document_id}/approve", headers=basliklar)
    assert erken.status_code == 403, erken.text
    assert erken.json()["detail"] == _MODUL_KAPISI

    await _adimlari_ilerlet(
        seeded_db,
        aktor_fabrikasi,
        _SATINALMA,
        document_id,
        [ApprovalRole.procurement, ApprovalRole.project_manager],
        etiket="ikame-t4",
    )

    sirasi_gelince = await client.post(f"{_SATINALMA_YOL}/{document_id}/approve", headers=basliklar)
    assert sirasi_gelince.status_code == 200, sirasi_gelince.text


# --------------------------------------------------------------------------- #
# 5. ZİNCİRSİZ EVRAKTA KAPI AYNEN KALIR
# --------------------------------------------------------------------------- #
#
# İkamenin dayanağı zincirin KENDİSİDİR. Zinciri olmayan (eski kayıt) bir
# evrakta ikame edilecek bir adım yoktur ⇒ modül kapısı bugünkü gibi kapalıdır.


async def test_ZINCIRSIZ_taseron_hakedisinde_modul_kapisi_403_kalir(
    client, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("ikame-t5a-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "ikame-t5a-sef@ok1c.co", role_key="site_chief", approval_roles=[ApprovalRole.site_chief]
    )
    basliklar = await giris("ikame-t5a-sef@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)

    yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == _MODUL_KAPISI


async def test_ZINCIRSIZ_satinalma_talebinde_modul_kapisi_403_kalir(
    client, aktor_fabrikasi, evrak_fabrikasi, giris
):
    yaratan = await aktor_fabrikasi("ikame-t5b-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "ikame-t5b-muh@ok1c.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )
    basliklar = await giris("ikame-t5b-muh@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )

    yanit = await client.post(f"{_SATINALMA_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == _MODUL_KAPISI


# --------------------------------------------------------------------------- #
# 6. KAPSAM İKAME EDİLMEZ → 404 (403 DEĞİL)
# --------------------------------------------------------------------------- #


async def test_BASKA_PROJENIN_adim_sahibi_onaylayamaz_403_IDOR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """IZN-B3b IDOR: adım sahipliği PROJEYE bağlıdır. Köprü'de Şantiye Şefi olan kişi
    Kule hakedişinin şef adımını onaylayamaz.

    Kapı 403 verir (ikame yalnız BELGENİN projesinde adımın sahibine açılır), adım karara
    bağlanmaz ve gövde karşı tarafın adını / tutarını SIZDIRMAZ. (Eski "kapsamı olmayan sahip
    404 alır" hâli yoktur: sahip, tanım gereği belgenin projesinin ekibindedir.)
    """
    yaratan = await aktor_fabrikasi("ikame-t6-yaratan@ok1c.co")
    document_id, _kule = await evrak_fabrikasi(
        _TASERON, creator=yaratan, subcontractor_name="Akın İnşaat"
    )
    zincir = await _zincir(seeded_db, _TASERON, document_id, yaratan)
    _diger_id, kopru = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await aktor_fabrikasi(
        "ikame-t6-sef@ok1c.co",
        role_key="site_chief",
        approval_roles=[ApprovalRole.site_chief],
        projeler=[kopru],
    )
    basliklar = await giris("ikame-t6-sef@ok1c.co")

    yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 403, yanit.text
    assert yanit.json()["detail"] == _MODUL_KAPISI
    assert "Akın" not in yanit.text, "karşı taraf adı sızdı"
    assert "100000" not in yanit.text, "tutar sızdı"
    assert await adim_durumlari(seeded_db, zincir.id) == [False, False, False]


# --------------------------------------------------------------------------- #
# 7. KUTU ⊆ EYLEM ALINABİLİR
# --------------------------------------------------------------------------- #
#
# `GET /approvals` süzgeci ile uçların kuralı AYNI kümeden türemek zorundadır.
# Türemezse ekran kullanıcıya imzalayamayacağı bir iş listeler — OK-1A'nın
# canlıdaki hâli tam olarak buydu.


async def test_KUTUDAKI_HER_SATIRIN_onay_ucu_403_VERMEZ(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Üç ailenin ilk adımının sahibi, `site_chief` SİSTEM rollü aktör.

    `progress_payments` seviyesi `draft`, `procurement` seviyesi `request` —
    ikisi de `approve`ın ALTINDA. Kutuda gördüğü ÜÇ satırın üçünde de onay ucu
    ona açık olmalıdır. IZN-B3b: bir kişi bir projede TEK rol taşır; aktör her ailenin
    projesinde O AİLENİN ilk adım rolüyle üyedir.
    """
    yaratan = await aktor_fabrikasi("ikame-t7-yaratan@ok1c.co", full_name="Evrak Sahibi")
    aktor = await aktor_fabrikasi("ikame-t7-sef@ok1c.co", role_key="site_chief", tum_projeler=False)
    basliklar = await giris("ikame-t7-sef@ok1c.co")

    taseron_id, proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await proje_rolu_ver(seeded_db, aktor, proje, "site_chief")
    await _zincir(seeded_db, _TASERON, taseron_id, yaratan)
    isveren_id, proje = await evrak_fabrikasi(_ISVEREN, creator=yaratan)
    await proje_rolu_ver(seeded_db, aktor, proje, "accounting")
    await _zincir(seeded_db, _ISVEREN, isveren_id, yaratan)
    satinalma_id, proje = await evrak_fabrikasi(
        _SATINALMA, creator=yaratan, quantity=Decimal("10"), unit_price=Decimal("1000.00")
    )
    await proje_rolu_ver(seeded_db, aktor, proje, "procurement")
    await _zincir(seeded_db, _SATINALMA, satinalma_id, yaratan, Decimal("10000.00"))

    kutu = await client.get("/approvals", headers=basliklar)
    assert kutu.status_code == 200, kutu.text
    satirlar = kutu.json()["items"]
    assert len(satirlar) >= 2, f"kutu en az iki satır göstermeliydi: {kutu.text}"

    for satir in satirlar:
        yol = f"{_YOLLAR[ApprovalDocumentType(satir['document_type'])]}/{satir['document_id']}"
        yanit = await client.post(f"{yol}/approve", headers=basliklar)
        assert yanit.status_code != 403, (
            f"kutuda görünen satır ({satir['document_type']}) onay ucunda 403 aldı: {yanit.text}"
        )


async def test_ONAY_ROLU_OLMAYAN_aktorun_kutusu_BOSTUR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """Kümenin öteki ucu: adım sahibi değilse satır da yoktur, uç da açılmaz.

    IZN-B3b: "Tüm projeler" + ANA rol `site_chief` adımın sahibidir; adım rolü OLMAYAN bir ana
    rol (`hr_manager`) aday bile değildir."""
    yaratan = await aktor_fabrikasi("ikame-t7b-yaratan@ok1c.co")
    await aktor_fabrikasi("ikame-t7b-rolsuz@ok1c.co", role_key="hr_manager")
    basliklar = await giris("ikame-t7b-rolsuz@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)

    kutu = await client.get("/approvals", headers=basliklar)

    assert kutu.status_code == 200, kutu.text
    assert kutu.json()["total"] == 0
    assert kutu.json()["items"] == []


# --------------------------------------------------------------------------- #
# 8. SICAK YOL SORGU MALİYETİ — TABAN ÖLÇÜMÜ
# --------------------------------------------------------------------------- #


async def test_MODUL_KAPISINDAN_GECEN_aktorun_sorgu_sayisi_ARTMAMALIDIR(
    client, seeded_db, aktor_fabrikasi, evrak_fabrikasi, giris
):
    """🔴 TABAN ÖLÇÜMÜ (2026-08-22, OK-1C T1) — bugün YEŞİL olması BEKLENİR.

    İkame kapısı, `require_permission`ın kendi kontrolü GEÇTİĞİNDE hiç
    koşmamalıdır: sıcak yolda +0 sorgu. Sayı SABİTLENDİ; T2'den sonra da aynı
    kalmalıdır. Artarsa ikame sıcak yola sızmış demektir.

    Aktör `project_manager` SİSTEM rolündedir (`progress_payments = approve`)
    ve zincirin 2. adımı ona aittir — yani modül kapısından ZATEN geçer.
    """
    yaratan = await aktor_fabrikasi("ikame-t8-yaratan@ok1c.co")
    await aktor_fabrikasi(
        "ikame-t8-pm@ok1c.co",
        role_key="project_manager",
        approval_roles=[ApprovalRole.project_manager],
    )
    basliklar = await giris("ikame-t8-pm@ok1c.co")
    document_id, _proje = await evrak_fabrikasi(_TASERON, creator=yaratan)
    await _zincir(seeded_db, _TASERON, document_id, yaratan)
    await _adimlari_ilerlet(
        seeded_db,
        aktor_fabrikasi,
        _TASERON,
        document_id,
        [ApprovalRole.site_chief],
        etiket="ikame-t8",
    )

    with _sorgu_sayaci() as ifadeler:
        yanit = await client.post(f"{_TASERON_YOL}/{document_id}/approve", headers=basliklar)

    assert yanit.status_code == 200, yanit.text
    # 27 → 28 (DSC-B5, 2026-10-01): hakediş router'ları `RequireUnrestricted` taşır; kapı istek
    # başına TEK disiplin okuması koşar. 28 → 25 (IZN-B3): "Tüm projeler" aktörü için kapsam kapısı
    # sorgusuz (`all_projects` → kısıtsız; −1) ve `visible_projects` tek üyelik okumasıdır (−2).
    # İkame sıcak yola sızmadı. 25 → 26 (IZN-B3 onarımı): onay kapısı isteğin PROJESİNİ çözer (yol
    # parametresindeki hakedişten proje: +1 sorgu) ve o projedeki rolle karar verir.
    assert len(ifadeler) == 26, (
        f"sıcak yol sorgu sayısı 26 iken {len(ifadeler)} oldu — ikame sıcak yola sızdı mı?"
    )
