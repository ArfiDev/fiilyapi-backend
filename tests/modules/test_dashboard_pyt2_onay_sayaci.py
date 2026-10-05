"""P-YT2 — gösterge panelindeki "Onay Bekleyenler" rozeti ARTIK GERÇEK SAYIDIR.

🔴 SAHTE-YEŞİL UYARISI. Yer tutucudan gerçek değere geçişte zarfın ŞEKLİ
değişmez (`available`/`count`/`items` alanları zaten vardı) — dolayısıyla
"alan var mı" tipi bir iddia bu dilimde HİÇBİR ŞEY bekçilemez ve geçiş
öncesinde de sonrasında da yeşil kalır. Bu dosyadaki her iddia bu yüzden
**elle yazılmış bir SAYIYI** çakar; beklenen değerler kurulumdan türetilmez.

🔴 K3 — AYNI FORMÜL İKİ YERDE YAŞAMAZ. Kutunun süzgeci
`approvals/repository.py:_pending_filter`ta **dört koşul** uygular (adım rolü ·
kendi evrağı · görevler ayrılığı · proje kapsamı). Panel o kuralı KOPYALAMAZ,
`approvals.service.pending_for_user`ı ÇAĞIRIR. Aşağıdaki dört bekçi dördünü
ayrı ayrı ölçer; `test_panel_sayaci_ile_kutu_TOPLAMI_BIREBIR_AYNIDIR` ise
dördü aynı anda ısırırken iki yüzeyin AYNI sayıyı ürettiğini çakar — bir kopya
tam orada ayrışırdı.
"""

import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.approvals import service as approvals_service
from app.modules.approvals.models import ApprovalDocumentType, ApprovalRole
from app.modules.dashboard.service import build_summary
from app.modules.projects.models import Project
from app.modules.users.models import User
from tests.conftest import test_engine
from tests.modules.approvals.conftest import (
    onay_rolu_ver,
    proje_rolu_ver,
    rol_sahipleri_dolgusu,  # noqa: F401  (fixture)
    taseron_evraki,
)

pytestmark = pytest.mark.usefixtures("rol_sahipleri_dolgusu")

PAROLA = "parola1234"
_TASERON = ApprovalDocumentType.subcontractor_progress_payment

#: Zincir tutarı eşiğin (₺500.000) ALTINDA seçildi: `patron` adımı EKLENMESİN.
#: Eklenseydi zincirin SIRADAKİ adımı yine `site_chief` olurdu (patron SONA
#: eklenir), ama kurulumun neden bu tutarı taşıdığı okunmaz kalırdı.
_ESIK_ALTI = Decimal("100000.00")


@contextmanager
def _sorgu_sayaci() -> Iterator[list[str]]:
    """Sürücüye giden HER ifadeyi sayar (`test_ok1a_query_count.py` deseni)."""
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


@pytest.fixture
def aktor(seeded_db: AsyncSession, user_factory):
    """Sistem rolü · onay rolleri · PROJE KAPSAMI üçü de AYRI eksendir (OK-1A K1).

    `projeler=None` ⇒ `all_projects=True`; `projeler=[]` ⇒ erişim satırı HİÇ
    açılmaz (kapsamsız aktör — IDOR bekçisinin kurulumu).
    """

    async def _kur(
        email: str,
        *,
        role_key: str = "site_chief",
        approval_roles: Sequence[ApprovalRole] = (),
        projeler: Sequence[Project] | None = None,
    ) -> User:
        user = await user_factory(email=email, password=PAROLA, role_key=role_key)
        if projeler is None:
            # Onay rolü varsa PROJE rolü (ekip kişisi, `all_projects=False`; "Tüm projeler"
            # kişide ekip satırı yok sayılır), yoksa "Tüm projeler" + ana rol.
            user.all_projects = not approval_roles
            await onay_rolu_ver(seeded_db, user, *approval_roles)
        else:
            # IZN-B3b: verilen projelerde ekip üyesi — onay rolü varsa O rolle (proje rolü).
            uye_rolu = approval_roles[0].value if approval_roles else role_key
            for proje in projeler:
                await proje_rolu_ver(seeded_db, user, proje, uye_rolu)
        await seeded_db.flush()
        return user

    return _kur


async def _zincir(session: AsyncSession, project: Project, yaratan: User) -> uuid.UUID:
    """Taşeron hakedişi + ona bağlı AÇIK zincir; `document_id` döner.

    Zincirin 1. adımı `site_chief`tir (`approvals/definitions.py:CHAIN_DEFINITIONS`).
    """
    document_id = await taseron_evraki(session, project, yaratan)
    await approvals_service.create_chain(
        session,
        document_type=_TASERON,
        document_id=document_id,
        amount=_ESIK_ALTI,
        created_by_user_id=yaratan.id,
    )
    return document_id


async def _zincirler(session: AsyncSession, project_factory, yaratan: User, kodlar: Sequence[str]):
    """Her zincire KENDİ projesi. Zorunluluk ölçüldü: `taseron_evraki`
    sözleşmeyi `contract_no = f"{project.code}-TSZ"` ile açar ve o kolon
    GLOBAL tekildir (`uq_subcontractor_contracts_contract_no`) — aynı projede
    iki evrak kurulumun kendisini patlatır, ürünü değil."""
    for kod in kodlar:
        proje = await project_factory(kod, name=f"Proje {kod}")
        await _zincir(session, proje, yaratan)


async def _giris(client: AsyncClient, email: str) -> dict[str, str]:
    resp = await client.post("/auth/login", json={"email": email, "password": PAROLA})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# (A) BAĞLANDI — rozet gerçek sayıyı basar
# --------------------------------------------------------------------------- #


async def test_rozet_UC_bekleyen_icin_UC_basar(seeded_db, aktor, project_factory):
    """Beklenen sayı ELLE yazıldı: üç evrak kuruldu, rozet `3` olmalı.

    Yer tutucu hâli `0` basardı; kurulumdan türetilen bir beklenti (`len(...)`)
    ise yer tutucuyu da yeşil geçirirdi."""
    yaratan = await aktor("pyt2-yaratan@d.co", approval_roles=())
    sef = await aktor("pyt2-sef@d.co", approval_roles=[ApprovalRole.site_chief])
    await _zincirler(seeded_db, project_factory, yaratan, ["PYT2-A1", "PYT2-A2", "PYT2-A3"])

    ozet = await build_summary(seeded_db, sef)

    assert ozet.pending_approvals.count == 3, "üç açık zincirin üçü de şefe düşmeli"
    assert ozet.pending_approvals.available is True, (
        "kaynak CANLI: kart artık 'veri yok' demiyor (OK-1A/OK-1C)"
    )
    assert ozet.pending_approvals.pending_module == "approvals", (
        "anahtar KALIR — artık 'bekleyen' değil BESLEYEN modülü işaret eder"
    )


async def test_rozet_SIFIR_ama_available_TRUE__gercek_sifir_bilinmiyor_DEGIL(
    seeded_db, aktor, project_factory
):
    """🔴 K2 — "gerçek 0" ile "bilinmiyor" ARTIK AYRI ŞEYLERDİR.

    Onay rolü olmayan aktörün gerçekten bekleyen imzası YOKTUR; motor bunu
    yetkiyle döner. `available=False` bırakmak, canlı bir kaynağın verdiği
    otoriter sıfırı "bilinmiyor" gibi göstermek olurdu."""
    yaratan = await aktor("pyt2-y2@d.co", approval_roles=())
    proje = await project_factory("PYT2-B", name="Güneşkent B")
    await _zincir(seeded_db, proje, yaratan)
    rolsuz = await aktor("pyt2-rolsuz@d.co", role_key="hr_manager", approval_roles=())

    ozet = await build_summary(seeded_db, rolsuz)

    assert ozet.pending_approvals.count == 0
    assert ozet.pending_approvals.available is True, (
        "onay rolü olmayan aktörün sıfırı GERÇEK bir sıfırdır, 'veri yok' değil"
    )


async def test_items_BOS_KALIR__zarf_satiri_tasiyamaz(seeded_db, aktor, project_factory):
    """Mockup satırı DÖRT olgu taşır (başlık · tutar · göreli zaman · aciliyet
    çipi); `items: list[str]` bir tanesini taşır. Rozet bağlandı, LİSTE hâlâ
    yer tutucudur — bu bilinçlidir ve boş kalması bir sonuçtur."""
    yaratan = await aktor("pyt2-y3@d.co", approval_roles=())
    proje = await project_factory("PYT2-C", name="Güneşkent C")
    await _zincir(seeded_db, proje, yaratan)
    sef = await aktor("pyt2-sef3@d.co", approval_roles=[ApprovalRole.site_chief])

    ozet = await build_summary(seeded_db, sef)

    assert ozet.pending_approvals.count == 1
    assert ozet.pending_approvals.items == [], (
        "liste zarfı doldurulmadı: satır dört olgu ister, list[str] birini taşır"
    )


# --------------------------------------------------------------------------- #
# K5 — KAPSAM AYNEN KORUNUR: dört süzgeç koşulu ayrı ayrı
# --------------------------------------------------------------------------- #


async def test_BASKA_rolun_adimi_SAYILMAZ(seeded_db, aktor, project_factory):
    """Bekçi: adım rolü. Zincirin 1. adımı `site_chief`tir; `accounting`
    rolündeki aktör onu GÖRMEZ (kendi adımı 3. sıradadır ve henüz açık değil)."""
    yaratan = await aktor("pyt2-y4@d.co", approval_roles=())
    proje = await project_factory("PYT2-D", name="Güneşkent D")
    await _zincir(seeded_db, proje, yaratan)
    muhasebe = await aktor(
        "pyt2-muh@d.co", role_key="accounting", approval_roles=[ApprovalRole.accounting]
    )

    ozet = await build_summary(seeded_db, muhasebe)

    assert ozet.pending_approvals.count == 0, "sıradaki adım şefin, muhasebenin DEĞİL"


async def test_KENDI_evragi_SAYILMAZ(seeded_db, aktor, project_factory):
    """Bekçi 5 — kendi evrağı. Aktör hem yaratan hem 1. adımın rolüdür;
    admin istisnası yoktur (`site_chief`: `progress_payments` = draft)."""
    proje = await project_factory("PYT2-E", name="Güneşkent E")
    sef = await aktor("pyt2-sef5@d.co", approval_roles=[ApprovalRole.site_chief])
    await _zincir(seeded_db, proje, sef)

    ozet = await build_summary(seeded_db, sef)

    assert ozet.pending_approvals.count == 0, "kendi açtığı evrak kendi rozetini şişirmez"


async def test_GORUNMEYEN_projenin_evragi_SAYILMAZ(seeded_db, aktor, project_factory):
    """🔴 IDOR — proje kapsamı. Aktör YALNIZ A projesini görür; zincir B
    projesindedir. Kapsam SQL'dedir, bu yüzden sayım da ondan türer."""
    yaratan = await aktor("pyt2-y6@d.co", approval_roles=())
    gorunen = await project_factory("PYT2-F1", name="Görünen")
    gizli = await project_factory("PYT2-F2", name="Gizli")
    await _zincir(seeded_db, gizli, yaratan)
    sef = await aktor(
        "pyt2-sef6@d.co", approval_roles=[ApprovalRole.site_chief], projeler=[gorunen]
    )

    ozet = await build_summary(seeded_db, sef)

    assert ozet.pending_approvals.count == 0, "görülmeyen projenin evrağı rozette SAYILMAZ"


async def test_GOREVLER_AYRILIGI__ayni_zincirde_karar_vermis_aktor_SAYMAZ(
    seeded_db, aktor, project_factory, client
):
    """Bekçi 6 — görevler ayrılığı. 1. adımı şef olarak onaylayan aktör, projedeki ROLÜ PM'e
    değişince zincirin 2. adımı ona düşse bile rozet ARTMAZ (IZN-B3b: tek kişi tek projede
    tek rol; ayrılık rol değişiminde ısırır)."""
    yaratan = await aktor("pyt2-y7@d.co", approval_roles=())
    proje = await project_factory("PYT2-G", name="Güneşkent G")
    document_id = await taseron_evraki(seeded_db, proje, yaratan)
    await approvals_service.create_chain(
        seeded_db,
        document_type=_TASERON,
        document_id=document_id,
        amount=_ESIK_ALTI,
        created_by_user_id=yaratan.id,
    )
    ikili = await aktor(
        "pyt2-ikili@d.co",
        role_key="system_admin",
        approval_roles=[ApprovalRole.site_chief],
    )
    once = await build_summary(seeded_db, ikili)
    assert once.pending_approvals.count == 1, "kurulum kontrolü: karardan ÖNCE bir satır düşüyor"

    await approvals_service.approve_next_step(
        seeded_db, actor=ikili, document_type=_TASERON, document_id=document_id
    )

    await proje_rolu_ver(seeded_db, ikili, proje, "project_manager")
    ara = await build_summary(seeded_db, ikili)
    assert ara.pending_approvals.count == 0, "2. adım PM'indir ama ona kapalı (ayrılık)"
    sonra = await build_summary(seeded_db, ikili)

    assert sonra.pending_approvals.count == 0, (
        "aynı zincirde karar vermiş aktöre ikinci adım DÜŞMEZ (görevler ayrılığı)"
    )


# --------------------------------------------------------------------------- #
# K3 — YAPISAL BEKÇİ: panel ile kutu AYNI kuraldan türer
# --------------------------------------------------------------------------- #


async def test_panel_sayaci_ile_kutu_TOPLAMI_BIREBIR_AYNIDIR(
    seeded_db, aktor, project_factory, client
):
    """🔴 K3 KOPYA BEKÇİSİ. Dört süzgeç koşulu AYNI ANDA ısırır:

    | zincir | koşul | kutuda | rozette |
    |---|---|---|---|
    | görünen projede, BAŞKASININ evrağı | — | ✓ | ✓ |
    | görünen projede, AKTÖRÜN evrağı    | bekçi 5 | ✗ | ✗ |
    | GİZLİ projede, başkasının evrağı   | kapsam  | ✗ | ✗ |

    Panel süzgeci kopyalasaydı bu kurulumda ayrışırdı; eşitlik iddiası
    kopyanın DOĞRU olmasını değil, HİÇ OLMAMASINI bekçiler.
    """
    yaratan = await aktor("pyt2-y8@d.co", approval_roles=())
    baskasinin = await project_factory("PYT2-H1", name="Görünen · başkasının")
    kendi = await project_factory("PYT2-H2", name="Görünen · kendi evrağı")
    gizli = await project_factory("PYT2-H3", name="Gizli")
    sef = await aktor(
        "pyt2-sef8@d.co",
        approval_roles=[ApprovalRole.site_chief],
        projeler=[baskasinin, kendi],
    )
    await _zincir(seeded_db, baskasinin, yaratan)
    await _zincir(seeded_db, kendi, sef)
    await _zincir(seeded_db, gizli, yaratan)

    ozet = await build_summary(seeded_db, sef)
    basliklar = await _giris(client, "pyt2-sef8@d.co")
    kutu = await client.get("/approvals", headers=basliklar)

    assert kutu.status_code == 200, kutu.text
    assert ozet.pending_approvals.count == 1, "üç zincirden YALNIZ biri şefe düşer"
    assert kutu.json()["total"] == 1, "kutu da aynı biri döndürmeli"
    assert ozet.pending_approvals.count == kutu.json()["total"], (
        "panel rozeti ile kutu toplamı AYRIŞTI — süzgeç kopyalanmış olmalı (K3)"
    )
    assert len(kutu.json()["items"]) == 1, "gövde ile sayım aynı süzgeçten türer"


# --------------------------------------------------------------------------- #
# K5 — SORGU MALİYETİ: panel sıcak yoldur
# --------------------------------------------------------------------------- #


async def test_panelin_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ(seeded_db, aktor, project_factory):
    """🔴 N+1 YOK. Bir bekleyen ile ON bekleyen AYNI sorgu sayısını üretmeli.

    Eşitlik iddiası mutlak bir tavandan güçlüdür: tavan dilim büyüdükçe sessizce
    gevşetilebilir, eşitlik satır başına ek sorgu eklendiği anda kırılır."""
    yaratan = await aktor("pyt2-y9@d.co", approval_roles=())
    sef = await aktor("pyt2-sef9@d.co", approval_roles=[ApprovalRole.site_chief])

    await _zincirler(seeded_db, project_factory, yaratan, ["PYT2-I00"])
    with _sorgu_sayaci() as tek:
        bir = await build_summary(seeded_db, sef)
    assert bir.pending_approvals.count == 1

    await _zincirler(seeded_db, project_factory, yaratan, [f"PYT2-I{n:02d}" for n in range(1, 10)])
    with _sorgu_sayaci() as on:
        onlu = await build_summary(seeded_db, sef)
    assert onlu.pending_approvals.count == 10

    assert len(on) == len(tek), (
        f"bekleyen sayısı 1→10 olunca panelin sorgu sayısı {len(tek)}→{len(on)} oldu — N+1"
    )


async def test_onay_rolu_OLAN_aktorun_panel_MALIYETI_CAKILDI(seeded_db, aktor, project_factory):
    """🔴 MUTASYON TURUNUN ACTIGI DELIK. `limit=0` -> `limit=50` mutasyonu
    DAVRANISI HIC DEGISTIRMEZ (`total` ayni sayidir) ama satir
    zenginlestirmesini (adimlar · adlar · UC EVRAK AILESI) bosuna kostururur.
    N+1 bekcisi bunu GOREMEZ: o iki olcumu AYNI `limit` ile karsilastirir ve
    sabit bir carpani sabit birakir.

    Bu yuzden burada MUTLAK bir tavan cakilir. Sayi elle yazilmistir:
      8  — panelin `limit=0` oncesi olculmus tabani
      +1 — onay rolu sorgusu
      +2 — `_admin_document_types` (izin modulu basina; bugun iki)
      +3 — `visible_projects` (izin + proje + iliskiler)
      +7 — proje `selectin` bagintilarinin motor tarafindaki TEKRARI
      +2 — sayim + (bos) sayfa
      +10 — DASH-1 `portfolio` BAGLANDI (asagida kalem kalem)
    Tavanin gevsemesi bir SEBEP ister; sessizce gevsememeli.

    🔴 DASH-1'de ONBIR degil ON eklendi ve fark OLCULDU:
      +1 `can_read("progress_payments")` — ALAN kapisi (K4)
      +1 `visible_projects` izin okumasi (`projects: admin` atlamasi icin)
      +2 `user_project_access` + `projects`
      +5 projenin `selectin` bagintilari (sozlesme · yatirim · santiyeler ·
         arsa payi · hissedarlar)
      +1 `cumulative_gross_by_projects` hakedis okumasi
    ⚠️ ONBIRINCI sorgu (hakedis `lines` `selectin`i) bu kurulumda HIC ACILMAZ:
    burada ISVEREN hakedisi yoktur, ust sorgu sifir satir doner. Kurulum bir
    gun tamamlanmis isveren hakedisi tasirsa tavan 47 olur — ve bu, tavanin
    sessizce degil SEBEPLE gevsemesi gereken tam o durumdur.

    🔴 RISK-1: **`risks` bağlandı → +15** ve on beşi de ÖLÇÜLDÜ (statement
    dökümü, `before_cursor_execute`):
      +3  `can_read` — kartın ÜÇ AYRI izin kapısı (`inventory` ·
          `progress_payments` · `sites`). Kart kısmî dolar, bu yüzden kapılar
          da ayrıdır; tek bir kapı kartın ancak üçte birini yönetirdi.
      +8  `visible_projects` — izin okuması + `user_project_access` + `projects`
          + projenin BEŞ `selectin` bağıntısı.
          ⚠️ Bu, `portfolio`nun ödediği kapsam okumasının TEKRARIDIR ve
          bilerek bırakıldı: iki kart kapsamı birbirine GEÇİRSE, biri izin
          kapısında erken dönerken öteki onun listesini kullanırdı ve kapsam
          tanımı iki yerde yaşamaya başlardı (`_pending_approvals`ın aynı
          gerekçeyle taşıdığı 7 tekrarın kardeşi).
      +2  stok — kritik satırlar + EŞİKSİZ kalem sayacı (K2: "bilinmiyor" hâli
          sessiz kalamaz).
      +1  gecikmiş taşeron hakedişi.
      +1  planlanan bitişinden önce tamamlanmış bölüm.
    Üçü de SATIR SAYISINDAN bağımsızdır (bekçisi
    `test_risk1_uyari_akisi.py::test_kart_sorgu_sayisi_SATIR_SAYISINDAN_BAGIMSIZ`);
    izni olmayan aktör o kaynağın sorgusunu HİÇ ödemez.

    🔴 **K4 KAPISI: +1 (46 -> 47, 34 -> 35).** Tavan SESSİZCE gevşemedi, SEBEBİ
    şudur: proje kartları (`code` · `name` · `status` · **`budget`** ·
    `progress_pct`) hiçbir ALAN kapısından geçmiyordu — tek kapı ucun
    `dashboard:view` kapısıydı, oysa AYNI VERİ `GET /projects` ucundan
    `require_permission("projects", ...)` ile korunuyor. `projects` hücresi
    çalışma anında `none` yapılan bir rol şirketin proje BÜTÇELERİNİ panelden
    okumaya devam ederdi (bekçisi:
    `test_dashboard_api.py::test_projects_izni_KAPALIYKEN_panel_proje_karti_BASMAZ`).
    Eklenen sorgu ÖLÇÜLDÜ ve TEKTİR: `can_read(..., "projects")`in
    `role_permissions` okuması (aynı kurulumda 6 -> 7 izin okuması, toplam
    34 -> 35). Satır sayısından BAĞIMSIZDIR — N+1 bekçisi (`:335`) dokunulmadan
    yeşil kalır.
    """
    yaratan = await aktor("pyt2-y10@d.co", approval_roles=())
    # "Tüm projeler" + ANA rol `site_chief` (üretimdeki gerçek durum; ekip satırı yok).
    sef = await aktor("pyt2-sef10@d.co", approval_roles=())
    await _zincirler(seeded_db, project_factory, yaratan, ["PYT2-K1", "PYT2-K2"])

    with _sorgu_sayaci() as sorgular:
        ozet = await build_summary(seeded_db, sef)

    assert ozet.pending_approvals.count == 2
    # IZN-B3: 47 → 41 (`visible_projects` artık 1 üyelik okuması: −2 × 3 çağrı). IZN-B3 onarımı:
    # 41 → 49 (+8): alan kapısı PROJE BAŞINA (portföy + risk modülleri: ekip rolü + hücre okuması),
    # `restricted_project_ids` (+1 disiplin okuması × 2), gelen kutusu evrak modülü başına proje
    # süzgeci. Hepsi PROJE SAYISINDAN bağımsızdır (N+1 yok).
    assert len(sorgular) == 49, (
        f"onay rolu tasiyan aktorun panel maliyeti {len(sorgular)} sorgu oldu (beklenen 49) — "
        "rozet icin sayfa GOVDESI de cekiliyor olabilir (`limit` degisti mi?)"
    )


async def test_onay_rolu_YOKSA_panel_TEK_ek_sorgu_oder(seeded_db, aktor, project_factory):
    """⚠️ Maliyet ölçümü kanona bağlandı: onay rolü OLMAYAN aktör (çoğunluk)
    yalnız BİR ek sorgu öder — motor rol kümesi boşken erkenden döner.

    Sayı TAVAN olarak çakılır: rol sorgusundan sonra ikinci bir sorgu açan bir
    değişiklik (örneğin kapsamı rolden ÖNCE çözmek) tam burada kırılır.

    🔴 DASH-1: taban 8 + onay rolü 1 + **`portfolio` bağlandı 10** = 19.
    🔴 RISK-1: + **`risks` bağlandı 15** = 34.
    🔴 K4 KAPISI: + **proje kartlarının `can_read("projects")` kapısı 1** = 35.
    Her üç kalemin dökümü `test_onay_rolu_OLAN_aktorun_panel_MALIYETI_CAKILDI`
    docstring'inde tek tek yazılıdır; onay rolünden BAĞIMSIZ oldukları için iki
    tavana da AYNI sayıyla girerler."""
    rolsuz = await aktor("pyt2-rolsuz2@d.co", role_key="hr_manager", approval_roles=())
    await project_factory("PYT2-J", name="Güneşkent J")

    with _sorgu_sayaci() as sorgular:
        ozet = await build_summary(seeded_db, rolsuz)

    assert ozet.pending_approvals.count == 0
    # IZN-B2: 35 → 37. Proje kartı izni artık İKİ okumadır (seviye: sayfa hücreleri `gate_ok`;
    # kapsam: eski satır / `tum_tutarlar` hibriti `actor_scope`) + rolün tek seferlik okunması.
    # IZN-B3: 37 → 33 (`visible_projects` −2 × iki çağrı: portföy + risk); onarım: 33 → 35 (+2:
    # `restricted_project_ids` + proje başına alan kapısı toplu okuması).
    # IZN-B3b: 35 → 31. Aktör artık ana rolü `patron` OLAN "Tüm projeler" kişi DEĞİL (o, `patron`
    # adımının sahibidir ve "rolsüz" sayılamaz) ama ana rolü hiçbir adım rolü olmayan `hr_manager`:
    # `actor_is_candidate` (1 sorgu) hâlâ TEK ek sorgudur; fark, rolün daha az izin okumasıdır.
    assert len(sorgular) == 31, (
        f"rolsüz aktörün panel maliyeti {len(sorgular)} sorgu — "
        "taban 8 + onay rolü 1 + portföy 10 + risk 15 + proje kartı izin kapısı 3 (IZN-B2)"
    )
