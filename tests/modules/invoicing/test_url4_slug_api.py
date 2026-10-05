"""URL-4 — `GET /invoices/{invoice_id}` fatura numarasıyla açılır.

KÖK OLAY (kullanıcı, 2026-09-05): *"şu link işi sadece projeler kısmı için
düzeldi diğer sayfalar hâlâ tuhaf linklerde bunu da düzelt"*. URL-2 yalnız
`projects`/`sites`/`sections` için slug açmıştı.

## Bu dosyanın kilitlediği üç karar

1. **Anahtar `invoice_no`dur, AYRI BİR SLUG KOLONU AÇILMADI.** Kolon NOT
   NULL'dır ve yön başına tekildir; ikinci bir kolon aynı bilgiyi iki yerde
   tutar ve migration doğururdu.
2. 🔴 **BELİRSİZLİKTE SESSİZ SEÇİM YOKTUR** (yönetim kararı 2026-09-05).
   `uq_invoices_no_direction` (`direction`, `invoice_no`) yalnız YÖN BAŞINA
   tekildir; aynı numara bir gelen ve bir giden faturada birlikte bulunabilir.
   **0 isabet -> 404 · 1 isabet -> döner · 2 isabet -> 409.**
3. 🔴 **GÖRÜNÜRLÜK SÜZGECİ SAYMADAN ÖNCE KOŞAR** ve bu sıra bir GÜVENLİK
   gereğidir: önce sayılsaydı, kullanıcının GÖREMEDİĞİ bir faturanın varlığı
   409'un kendisiyle sızardı. Bu ayrımı ölçen test
   `test_gorunmeyen_ikiz_409_URETMEZ_gorunen_TEK_faturayi_ACAR`tır ve tam
   olarak o mutasyonu (süzgeci saymadan sonraya almak) kırmızıya çevirir.

🔴 **K-IKIZ1**: kapıyı ölçen tek şey kapıya ÇARPAN istektir; her negatif
iddianın yanında KARŞIT KANIT taşıyan bir pozitif kontrol vardır — yoksa
"her istek 404" diye kırılmış bir uç da yeşil kalırdı.
"""

import uuid

from app.modules.invoicing.guards import INVOICE_MISSING, INVOICE_NO_AMBIGUOUS
from app.modules.invoicing.models import InvoiceDirection
from tests._silme_yardimci import sil_aile

_YOL = "/invoices"


# =========================================================================== #
# 1. ÇÖZÜMLEME — UUID **ve** numara (URL-2 kararı 2)
# =========================================================================== #


async def test_uuid_ve_numara_AYNI_govdeyi_doner(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """Eski UUID bağlantısı ÇALIŞMAYA DEVAM EDER (URL-2 kararı 2)."""
    fatura = await fatura_fabrikasi(project=gorunen_proje, invoice_no="FIL20260001")

    by_uuid = await client.get(f"{_YOL}/{fatura.id}", headers=muhasebe_headers)
    by_no = await client.get(f"{_YOL}/FIL20260001", headers=muhasebe_headers)

    assert by_uuid.status_code == by_no.status_code == 200, by_no.text
    assert by_uuid.json() == by_no.json()
    assert by_no.json()["id"] == str(fatura.id)


async def test_slug_alani_NUMARANIN_KENDISIDIR_ve_LISTEDE_de_bulunur(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """🔴 Sözleşmede yayınlanan anahtar ile çözümleyicinin aradığı değer AYNI.

    Ayrıca `slug` LİSTE şemasında da bulunur: URL-2'de `SiteOptionListResponse`e
    slug EKLENMEDİĞİ için seçici slug üretememişti (`routes.ts:34-45` kuralı) —
    aynı yarım göç tekrarlanmaz. `InvoiceResponse` hem liste satırı hem detay
    başlığı olduğu için burada tuzak YAPISAL olarak imkânsızdır; test yine de
    ikisini de OKUR, çünkü şema bir gün ayrılabilir.
    """
    await fatura_fabrikasi(project=gorunen_proje, invoice_no="FIL20260042")

    detay = await client.get(f"{_YOL}/FIL20260042", headers=muhasebe_headers)
    assert detay.json()["slug"] == "FIL20260042"

    liste = await client.get(_YOL, headers=muhasebe_headers)
    assert liste.status_code == 200, liste.text
    satir = next(k for k in liste.json()["items"] if k["invoice_no"] == "FIL20260042")
    assert satir["slug"] == "FIL20260042"

    # Ve yayınlanan anahtar GERÇEKTEN o kaydı açar (karşıt kanıt).
    assert (await client.get(f"{_YOL}/{satir['slug']}", headers=muhasebe_headers)).json()[
        "id"
    ] == satir["id"]


async def test_YOL_SEGMENTINE_giremeyen_numara_slugu_NULL_birakir(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """🔴 GELEN faturada `invoice_no` SERBEST METİNDİR — `2026/0001` yazılabilir.

    Böyle bir numara `/faturalar/2026/0001` üretirdi ve Next.js dinamik segmenti
    bunu EŞLEŞTİREMEZDİ. Yüzde-kodlama BFF + FastAPI arasındaki iki katmanda
    güvenilir olmadığı için KODLAMA değil ELEME seçildi: `slug` NULL döner,
    istemci `slug ?? id` ile UUID'ye düşer.
    """
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, invoice_no="2026/0001"
    )

    detay = await client.get(f"{_YOL}/{fatura.id}", headers=muhasebe_headers)
    assert detay.status_code == 200, detay.text
    assert detay.json()["invoice_no"] == "2026/0001"
    assert detay.json()["slug"] is None

    # POZİTİF KONTROL (karşıt kanıt): AYNI fatura, URL-güvenli bir numarayla
    # slug'ını ALIR — yani `None` "slug hep boş" kusuru DEĞİL, eleme kuralıdır.
    guvenli = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, invoice_no="A-2026.0001"
    )
    ikinci = await client.get(f"{_YOL}/{guvenli.id}", headers=muhasebe_headers)
    assert ikinci.json()["slug"] == "A-2026.0001"


async def test_olmayan_numara_404(client, muhasebe_headers) -> None:
    resp = await client.get(f"{_YOL}/HIC-BOYLE-BIR-NUMARA-YOK", headers=muhasebe_headers)
    assert resp.status_code == 404
    assert resp.json() == {"detail": INVOICE_MISSING}


# =========================================================================== #
# 2. 🔴 BELİRSİZLİK — 0/1/2 -> 404/döner/409 (yönetim kararı)
# =========================================================================== #


async def test_ayni_numara_IKI_YONDE_de_varsa_409_SESSIZCE_SECILMEZ(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """🔴 BEKÇİNİN ASIL ÖLÇÜTÜ — iki yönde aynı numaralı İKİ fatura kurulur.

    `uq_invoices_no_direction` bunu ENGELLEMEZ (yön başına tekil), yani bu
    durum ÜRETİMDE gerçekten oluşabilir. Sessizce biri seçilseydi kullanıcı
    hangi faturayı açtığını bilemez ve seçim satır sırası gibi TANIMSIZ bir
    şeye bağlı olurdu.
    """
    giden = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.outgoing, invoice_no="IKIZ20260001"
    )
    gelen = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.incoming, invoice_no="IKIZ20260001"
    )
    assert giden.id != gelen.id

    resp = await client.get(f"{_YOL}/IKIZ20260001", headers=muhasebe_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": INVOICE_NO_AMBIGUOUS}

    # 🔴 POZİTİF KONTROL (K-IKIZ1) — İKİSİ DE UUID'siyle AÇILIR: 409 "fatura
    # okunamıyor" değil "hangisi olduğu belirsiz" demektir. Bu iddia olmasaydı
    # uç tamamen kırılmış olsa da yukarıdaki 409 yeşil kalabilirdi.
    for fatura in (giden, gelen):
        tekil = await client.get(f"{_YOL}/{fatura.id}", headers=muhasebe_headers)
        assert tekil.status_code == 200, tekil.text
        assert tekil.json()["id"] == str(fatura.id)


async def test_TEK_isabet_GECER_belirsizlik_yoksa_409_YOKTUR(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """POZİTİF KONTROL: bekçi "numaralı her istek 409" diye kırılamaz."""
    fatura = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.outgoing, invoice_no="TEKIL20260001"
    )
    resp = await client.get(f"{_YOL}/TEKIL20260001", headers=muhasebe_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(fatura.id)


async def test_AYNI_YONDE_ikinci_kez_ayni_numara_ZATEN_YAZILAMAZ(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje, db_session
) -> None:
    """Belirsizliğin ÜST SINIRI İKİDİR ve bu YAPISALDIR (yön sayısı ikidir).

    Aynı yönde ikinci bir aynı numaralı fatura `uq_invoices_no_direction`a
    çarpar — yani 409'un "üç isabet" hâli DB tarafından imkânsız kılınmıştır ve
    çözümleyicinin `> 1` eşiği tam olarak doğru eşiktir.
    """
    await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.outgoing, invoice_no="UQ20260001"
    )
    with_error = False
    try:
        await fatura_fabrikasi(
            project=gorunen_proje, direction=InvoiceDirection.outgoing, invoice_no="UQ20260001"
        )
    except Exception:  # IntegrityError — asıl iddia "yazılamaz"dır
        with_error = True
        await db_session.rollback()
    assert with_error, "aynı yönde aynı numara YAZILABİLDİ — UQ kısıtı düşmüş"


# =========================================================================== #
# 3. 🔴 GÖRÜNÜRLÜK SÜZGECİ ANAHTARLA DELİNMEZ (IDOR)
# =========================================================================== #


async def test_gorunmeyen_projenin_faturasi_NUMARAYLA_da_404(
    client, muhasebe_headers, fatura_fabrikasi, gorunmeyen_proje
) -> None:
    """Numara TAHMİN EDİLEBİLİR (`FIL<yıl><sıra>` üretilir), UUID değil.

    Görünmeyen projedeki gerçek fatura, var OLMAYAN numarayla BİREBİR AYNI 404
    gövdesini alır — 403 verilseydi kullanıcı kaydın var olduğunu öğrenirdi.
    """
    fatura = await fatura_fabrikasi(project=gorunmeyen_proje, invoice_no="GIZLI20260001")

    numarayla = await client.get(f"{_YOL}/GIZLI20260001", headers=muhasebe_headers)
    uuid_ile = await client.get(f"{_YOL}/{fatura.id}", headers=muhasebe_headers)
    olmayan = await client.get(f"{_YOL}/YOK20269999", headers=muhasebe_headers)

    assert numarayla.status_code == uuid_ile.status_code == olmayan.status_code == 404
    assert numarayla.json() == uuid_ile.json() == olmayan.json() == {"detail": INVOICE_MISSING}


async def test_gorunen_kullaniciya_AYNI_fatura_ACILIR_POZITIF_KONTROL(
    client, admin_headers, fatura_fabrikasi, gorunmeyen_proje
) -> None:
    """🔴 POZİTİF KONTROL: yukarıdaki 404'ler numaranın çalışmamasından DEĞİL,
    GÖRÜNÜRLÜKTEN gelir. Bu iddia olmasaydı bekçi "numara hiç çözülmüyor" diye
    kırılabilir ve önceki test yine yeşil kalırdı (eşdeğer mutant)."""
    fatura = await fatura_fabrikasi(project=gorunmeyen_proje, invoice_no="GIZLI20260002")

    resp = await client.get(f"{_YOL}/GIZLI20260002", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(fatura.id)


async def test_gorunmeyen_ikiz_409_URETMEZ_gorunen_TEK_faturayi_ACAR(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje, gorunmeyen_proje
) -> None:
    """🔴🔴 SIRA BEKÇİSİ — süzgeç SAYMADAN ÖNCE koşar.

    Kurulum: aynı numara iki yönde de var, ama biri kullanıcının GÖREMEDİĞİ
    projede. Doğru davranış: kullanıcı için ORTADA BELİRSİZLİK YOKTUR, kendi
    faturasını 200 ile açar.

    Mutasyon: `visible_invoice` içinde süzgeci saymadan SONRAYA almak
    (`if len(adaylar) > 1: 409` yazmak) bu testi 409 ile KIRAR — ve o kırılma
    tam olarak sızıntının kendisidir: kullanıcı, göremediği bir faturanın var
    olduğunu 409'dan öğrenirdi.
    """
    gorunen = await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.outgoing, invoice_no="KARMA20260001"
    )
    gizli = await fatura_fabrikasi(
        project=gorunmeyen_proje, direction=InvoiceDirection.incoming, invoice_no="KARMA20260001"
    )

    resp = await client.get(f"{_YOL}/KARMA20260001", headers=muhasebe_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(gorunen.id)

    # KARŞIT KANIT: hepsini gören aktör için belirsizlik GERÇEKTEN vardır -> 409.
    # Yani 200 "409 hiç ateşlenmiyor"dan değil, KAPSAMDAN geliyor.
    assert gizli.id is not None


async def test_hepsini_goren_aktor_AYNI_kurulumda_409_ALIR(
    client, admin_headers, fatura_fabrikasi, gorunen_proje, gorunmeyen_proje
) -> None:
    """Bir önceki testin KARŞIT KANITI — aynı iki fatura, farklı aktör."""
    await fatura_fabrikasi(
        project=gorunen_proje, direction=InvoiceDirection.outgoing, invoice_no="KARMA20260002"
    )
    await fatura_fabrikasi(
        project=gorunmeyen_proje, direction=InvoiceDirection.incoming, invoice_no="KARMA20260002"
    )

    resp = await client.get(f"{_YOL}/KARMA20260002", headers=admin_headers)
    assert resp.status_code == 409, resp.text
    assert resp.json() == {"detail": INVOICE_NO_AMBIGUOUS}


# =========================================================================== #
# 4. YAZMA UÇLARI ANAHTAR KABUL ETMEZ (URL-2 kararı 3)
# =========================================================================== #


async def test_PATCH_numara_kabul_ETMEZ_422(
    client, muhasebe_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    """Yazma yüzeyi tahmin edilebilir bir anahtara AÇILMAZ."""
    await fatura_fabrikasi(project=gorunen_proje, invoice_no="YAZMA20260001")

    resp = await client.patch(
        f"{_YOL}/YAZMA20260001", headers=muhasebe_headers, json={"note": "deneme"}
    )
    assert resp.status_code == 422, resp.text


async def test_DELETE_numara_kabul_ETMEZ_422(
    client, admin_headers, fatura_fabrikasi, gorunen_proje
) -> None:
    await fatura_fabrikasi(project=gorunen_proje, invoice_no="YAZMA20260002")
    resp = await client.delete(f"{_YOL}/YAZMA20260002", headers=admin_headers)
    assert resp.status_code == 422, resp.text

    # POZİTİF KONTROL: UUID ile AYNI uç çalışır (422 "silme bozuk" demek değil).
    fatura = await fatura_fabrikasi(project=gorunen_proje, invoice_no="YAZMA20260003")
    assert (await sil_aile(client, admin_headers, "invoice", fatura.id)).status_code in (200, 204)


async def test_bozuk_deger_artik_422_DEGIL_404(client, muhasebe_headers) -> None:
    """URL-2 kararı 6'nın kabul edilmiş YAN ETKİSİ — burada da geçerlidir.

    Slug uzayı tam olarak "UUID olmayan metinler"dir; ikisi aynı yol
    parametresinde birlikte yaşayamaz.
    """
    resp = await client.get(f"{_YOL}/kesinlikle-uuid-degil", headers=muhasebe_headers)
    assert resp.status_code == 404
    # Ve gerçek bir UUID hâlâ UUID gibi çözülür (karşıt kanıt).
    assert (await client.get(f"{_YOL}/{uuid.uuid4()}", headers=muhasebe_headers)).status_code == 404
