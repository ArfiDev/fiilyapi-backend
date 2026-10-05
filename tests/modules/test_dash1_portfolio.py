"""DASH-1 — gosterge panelindeki "Portfoy · Toplam Hakedis" kartinin bekcileri.

NE CAKILIR
----------
Kart artik yer tutucu DEGIL: **isveren** hakedislerinin `approved` + `paid`
brut toplamini basar. Tanimin her parcasi ayri bir bekci ister, cunku "bir sayi
cikti" iddiasi TEK basina hicbir seyi kanitlamaz (sahte-yesilin 8. hâli: ozet
servisin arkasindaki KUME bekcisizdir).

K-IKIZ1 geregi olumlu kontrolun (1) yaninda DORT KARSIT KANIT durur:

1. DOLU     — gorunur projede `approved`+`paid` -> `available=true`, carpimi
              testte acikca yazilmis bir sayi.
2. KAPSAM   — erisilmeyen projenin hakedisi toplama GIRMEZ (ve toplam, ikisinin
              toplamindan KESIN OLARAK KUCUKTUR: suzgeci silen mutasyon yakalanir).
3. KUME     — `draft` / `pending_approval` eklemek sayiyi OYNATMAZ.
4. BOS      — gorunur projesi olmayan aktor: `available=false` + modul adi
              (soru hic sorulmadi), UYDURMA `0.00` DEGIL.
5. IZIN     — `hr_manager` (dashboard=view/KAPSAM `all`, progress_payments=none):
              `available=false` + `pending_module is None` (`restricted()`) ve
              hucre ACILINCA sayinin GELDIGI — ayni aktorde CIFT YON.

Tum iddialar `GET /dashboard/summary` HTTP ucundan gecer: bekci KULLANICININ
gordugunu olcmelidir.
"""

from decimal import Decimal

from app.core.access import AccessLevel, Scope
from app.modules.progress_payments.models import ProgressPaymentStatus
from app.modules.users.models import ProjectMember

from . import _ilr
from ._boq import _set_permission

#: Zarf tek fiyatla kurulur ki testteki carpim GOZLE dogrulanabilsin.
_BIRIM = "1000.00"


async def _login_kapsamli(client, session, user_factory, role_key, email, *, projeler=None):
    """`_ilr.login`in DAR hâli: `all_projects=True` yerine SECILI projeler.

    Kapsam bekcisi (test 2) tam da bu farkla ayakta durur — paylasilan yardimci
    her aktore tum projeleri acar ve sizinti hic gorunmezdi.
    """
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    if projeler is None:
        user.all_projects = True
    else:
        for proje in projeler:
            session.add(ProjectMember(user_id=user.id, project_id=proje.id, role_id=user.role_id))
    await session.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _portfoy(client, headers: dict) -> dict:
    resp = await client.get("/dashboard/summary", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["portfolio"]


async def test_portfoy_isveren_hakedisinin_approved_ve_paid_brutunu_toplar(
    client, db_session, user_factory, project_factory
):
    """(1) OLUMLU KONTROL — dolu zarf ve BAGIMSIZ aritmetik.

    Zarfin UCU birden cakilir: yalnizca `value` cakilsaydi, `available=false`
    donen bir zarf bile (deger yaninda) yesil gecebilirdi.
    """
    yazan = await _ilr.aktor(db_session, user_factory, "pf-yazan@dash1.co")
    project = await project_factory(code="D1-PF", project_type="taahhut")
    site = await _ilr.santiye(db_session, project)
    kalem = await _ilr.isveren_kalemi(db_session, project, quantity="10000", unit_price=_BIRIM)
    await _ilr.isveren_hakedisi(
        db_session,
        project,
        yazan,
        kalem,
        site,
        quantity="300",
        status=ProgressPaymentStatus.approved,
        sequence_no=1,
    )
    await _ilr.isveren_hakedisi(
        db_session,
        project,
        yazan,
        kalem,
        site,
        quantity="400",
        status=ProgressPaymentStatus.paid,
        sequence_no=2,
    )
    headers = await _login_kapsamli(client, db_session, user_factory, "patron", "pf@d1.co")

    portfoy = await _portfoy(client, headers)

    # BRUT = Σ(miktar × sozlesme birim fiyati); KDV brute DAHIL DEGILDIR
    # (`calculations.vat_amount` ayri bir kalemdir, `gross_total` yalnizca
    # `line_total` toplar). 300 × 1000.00 + 400 × 1000.00 = 700000.00
    assert Decimal(portfoy["value"]) == Decimal("700000.00")
    assert portfoy["available"] is True
    assert portfoy["pending_module"] is None


async def test_portfoy_ERISILMEYEN_projenin_hakedisini_TOPLAMAZ(
    client, db_session, user_factory, project_factory
):
    """(2) KARSIT KANIT — K4 sizinti bekcisi.

    Iki projede de hakedis var, aktor YALNIZ birini goruyor. Toplamin
    "erisilebilen projeninki"ne esit olmasi tek basina yetmez; ayrica IKISININ
    TOPLAMINDAN KESIN OLARAK KUCUK oldugu cakilir — kapsam suzgecini silen bir
    mutasyon o zaman yakalanir.
    """
    yazan = await _ilr.aktor(db_session, user_factory, "pf2-yazan@dash1.co")
    gorunur = await project_factory(code="D1-PF-GOR", project_type="taahhut")
    gizli = await project_factory(code="D1-PF-GIZ", project_type="taahhut")
    for proje, miktar, kod in ((gorunur, "300", "15.150.1002"), (gizli, "900", "15.150.2003")):
        site = await _ilr.santiye(db_session, proje, code=f"S-{proje.code}")
        kalem = await _ilr.isveren_kalemi(
            db_session, proje, quantity="10000", unit_price=_BIRIM, code=kod
        )
        await _ilr.isveren_hakedisi(
            db_session,
            proje,
            yazan,
            kalem,
            site,
            quantity=miktar,
            status=ProgressPaymentStatus.approved,
            sequence_no=1,
        )
    headers = await _login_kapsamli(
        client, db_session, user_factory, "patron", "pf2@d1.co", projeler=[gorunur]
    )

    portfoy = await _portfoy(client, headers)

    # YALNIZ gorunur proje: 300 × 1000.00 = 300000.00
    gorunur_toplam = Decimal("300000.00")
    # Iki proje birden olsaydi: (300 + 900) × 1000.00 = 1200000.00
    ikisi_birden = Decimal("1200000.00")
    assert Decimal(portfoy["value"]) == gorunur_toplam
    assert Decimal(portfoy["value"]) < ikisi_birden, (
        "kapsam suzgeci dustu: gorulmeyen projenin hakedisi portfoye sizdi (K4)"
    )


async def test_portfoy_TASLAK_ve_ONAY_BEKLEYEN_hakedisi_SAYMAZ(
    client, db_session, user_factory, project_factory
):
    """(3) KARSIT KANIT — KUME bekcisi (`test_dash1_spent_parity.py:92` deseni).

    Once olculur, sonra kumeye GIRMEMESI gereken iki durum EKLENIR, yeniden
    olculur: sayi KIMILDAMAMALIDIR. Durum suzgecini genisleten bir mutasyon
    ancak boyle yakalanir — "bir sayi cikti" iddiasi genis kumede de yesildir.
    """
    yazan = await _ilr.aktor(db_session, user_factory, "pf3-yazan@dash1.co")
    project = await project_factory(code="D1-PF-KUM", project_type="taahhut")
    site = await _ilr.santiye(db_session, project)
    kalem = await _ilr.isveren_kalemi(db_session, project, quantity="100000", unit_price=_BIRIM)
    await _ilr.isveren_hakedisi(
        db_session,
        project,
        yazan,
        kalem,
        site,
        quantity="300",
        status=ProgressPaymentStatus.approved,
        sequence_no=1,
    )
    headers = await _login_kapsamli(client, db_session, user_factory, "patron", "pf3@d1.co")

    once = await _portfoy(client, headers)
    # 300 × 1000.00 = 300000.00
    assert Decimal(once["value"]) == Decimal("300000.00")

    for sira, durum in (
        (2, ProgressPaymentStatus.draft),
        (3, ProgressPaymentStatus.pending_approval),
    ):
        await _ilr.isveren_hakedisi(
            db_session,
            project,
            yazan,
            kalem,
            site,
            quantity="5000",
            status=durum,
            sequence_no=sira,
        )

    sonra = await _portfoy(client, headers)

    assert sonra == once, "taslak/onay bekleyen hakedis portfoy toplamini OYNATTI"


async def test_portfoy_gorunur_proje_YOKKEN_bos_zarf_doner_uydurma_sifir_DEGIL(
    client, db_session, user_factory, project_factory
):
    """(4) KARSIT KANIT — 2. hâl: soru HIC SORULMADI.

    Aktorun izni var ama gorunur projesi yok. `0.00` basmak "hicbir hakedisin
    yok" derdi; dogrusu "sana gorunen bir portfoy yok"tur (K2).
    """
    await project_factory(code="D1-PF-BOS", project_type="taahhut")
    headers = await _login_kapsamli(
        client, db_session, user_factory, "patron", "pf4@d1.co", projeler=[]
    )

    portfoy = await _portfoy(client, headers)

    assert portfoy["available"] is False
    assert portfoy["value"] is None
    assert portfoy["pending_module"] == "progress_payments", (
        "bos zarf kaynagini bildirmelidir; uydurma bir sifir basilamaz"
    )


async def test_portfoy_hakedis_izni_OLMAYAN_role_SAYIYI_SIZDIRMAZ(
    client, db_session, user_factory, project_factory
):
    """(5) KARSIT KANIT — K4 ALAN kapisi + ILR-1/2 UCUNCU hâl.

    OLCULDU (`roles/seed_data.py` MATRIX): paneli acabilen ama
    `progress_payments = none` olan TEK rol `hr_manager`dir
    (`dashboard` satirinda `_LIM`, `progress_payments` satirinda `_N`).
    Kapi olmasaydi bu rol sirketin TUM hasilat toplamini okurdu.

    🔴 `pending_module` BOS gelir: "bu modul daha yazilmadi" ile "bunu gormeye
    yetkin yok" farkli iki durumdur (kullanici karari 2026-08-27); ikincisine
    modul adi yazmak ekrani YALANCI yapardi.
    """
    yazan = await _ilr.aktor(db_session, user_factory, "pf5-yazan@dash1.co")
    project = await project_factory(code="D1-PF-IZN", project_type="taahhut")
    site = await _ilr.santiye(db_session, project)
    kalem = await _ilr.isveren_kalemi(db_session, project, quantity="10000", unit_price=_BIRIM)
    # Kapi olmasaydi 300000.00 okunacakti — bekci "veri yoktu" ile karismasin.
    await _ilr.isveren_hakedisi(
        db_session,
        project,
        yazan,
        kalem,
        site,
        quantity="300",
        status=ProgressPaymentStatus.approved,
        sequence_no=1,
    )
    # 🔴 Bu bekci K4 ALAN KAPISINI olcer, KAPSAM MASKESINI DEGIL. `hr_manager`
    #    seed'de `dashboard = _LIM` (limited) tasir ve `portfolio` PARA kovasinda
    #    etiketlidir (`dashboard/schemas.py:171`) — maske TEK BASINA asagidaki
    #    `(False, None, None)` uclusunu uretir, yani K4 kapisi TAMAMEN kaldirilsa
    #    bile test YESIL kalirdi. OLCULDU (2026-09-19): `dashboard/service.py`
    #    icindeki `can_read` kapisi silinip test kosuldu, YESIL kaldi. Kapsam
    #    ACIKCA `all`a cekilir ki deneyin tek degiskeni izin hucresi olsun;
    #    hucrenin KENDISI de seed'e birakilmaz (`_set_permission` kanonu: matris
    #    degistiginde test sessizce anlamsizlasmasin).
    await _set_permission(db_session, "hr_manager", "dashboard", AccessLevel.view, Scope.all)
    await _set_permission(db_session, "hr_manager", "progress_payments", AccessLevel.none)
    headers = await _login_kapsamli(client, db_session, user_factory, "hr_manager", "pf5@d1.co")

    kisitli = await _portfoy(client, headers)

    assert kisitli["available"] is False
    assert kisitli["value"] is None
    assert kisitli["pending_module"] is None, (
        "izin yoklugu `restricted()` ile anlatilir; `pending_module` IZIN anlamiyla yuklenmez"
    )

    # 🔴 AYNI AKTORDE OLUMLU KONTROL (K-IKIZ1): hucre CALISMA ANINDA acilir,
    #    uc acik kalir ve sayi GELIR. Iki isi birden yapar: (a) "her seyi
    #    kisitla" mutasyonunu yakalar, (b) kapsamin gercekten `all` oldugunu
    #    KANITLAR — maske hâlâ isleseydi izin acilsa bile deger `None` kalirdi,
    #    yani yukaridaki kapsam satiri curuse bu yari KIRILIR.
    await _set_permission(db_session, "hr_manager", "progress_payments", AccessLevel.view)

    acik = await _portfoy(client, headers)

    assert acik["available"] is True, acik
    # 300 × 1000.00 — kapinin tuttugu sayinin TA KENDISI.
    assert Decimal(acik["value"]) == Decimal("300000.00"), acik
