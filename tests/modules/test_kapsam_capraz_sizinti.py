"""ÇAPRAZ MODÜL SIZINTISI — kısıtlı bir modülün yanıtına GÖMÜLÜ, BAŞKA modülde
tanımlı şemanın para/ilerleme alanları gerçekten maskeleniyor mu?

## Neden ayrı bir dosya

`field_scope.maskele()` iç içe `BaseModel`lere İNER (`_deger`), yani maske
`contracts` yanıtının içindeki `progress_payments` şemasına ULAŞIR. Ulaşmak
yetmez: o şemanın alanları ETİKETSİZSE hepsi `kimlik` sayılır (fail-OPEN,
gerekçe `core/field_scope.py`) ve HİÇBİR kapsamda gizlenmez. 2026-09-19
denetiminde ölçülen canlı hâl tam buydu: `EmployerContractDetail.amount`
`limited` kapsamda `null` dönerken, AYNI sözleşme bedeli gömülü
`progress_payment_summary.contract_amount` alanında AÇIKTA kalıyordu.

## Kardeş bekçilerle işbölümü — ÇAKIŞMAZ

* `tests/core/test_para_alani_siniflandirmasi.py` YAPIYI ölçer: maskeli
  uçlardan ulaşılan her şemada etiket VAR MI? (Etiketi olmayan alanı yakalar.)
* Bu dosya DAVRANIŞI ölçer: gerçek rol · gerçek uç · gerçek yanıt. Etiket doğru
  KOVAYA konmuş mu, köprü (`kapsam_kapisi`) → rota sınıfı (`kapsam_rotasi`) →
  maske zinciri gömülü şemaya kadar KOPMADAN gidiyor mu?

Üç parçanın (etiket · köprü · rota sınıfı) üçü de tek tek yeşilken zincirin
gömülü şemada kopması mümkündür — yapı testi onu göremez, çünkü yapı testi
şemanın imzasına bakar, dönen GÖVDEYE değil.

## Kapsam DEĞİŞKENİ testte kurulur, seed'den OKUNMAZ

Üç testin tek farkı rolün `contracts` KAPSAMIDIR; seviye (`view`) sabittir.
Seed'e bağlansaydı, matris bir gün değiştiğinde test sessizce ANLAMSIZLAŞIR
(hep aynı kapsamı ölçen üç kopya olurdu) — kırmızı vermeden. Şimdi ölçülen şey
maskenin kendisidir ve deneyin tek değişkeni kapsamdır (`modul_duzeyi_yaz`
deseni; kapsam = `tum_tutarlar` bayrağı).
"""

import uuid
from decimal import Decimal

from app.core.access import AccessLevel
from app.core.sayfalar import HiddenCategory
from app.modules.contracts.models import EmployerContractGroup, EmployerContractItem
from app.modules.progress_payments.models import (
    ProgressPayment,
    ProgressPaymentLine,
    ProgressPaymentStatus,
)
from app.modules.projects.models import ProjectContract
from app.modules.sites.models import Site
from tests._hassas_alan import rol_gizle
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz

from ._boq import _auth, _login_with_access

#: Ölçülen senaryonun sayıları. Hepsi BİRBİRİNDEN FARKLI seçildi: eşit sayılar
#: kullanılsaydı "yanlış alanı okuyan" bir assert de yeşil kalırdı.
_BEDEL = Decimal("11200000.00")
_MIKTAR = Decimal("21000.000")
_BIRIM_FIYAT = Decimal("100.00")
#: brüt = 21.000 × ₺100 = 2.100.000 · avans %20 = 420.000 · teminat %5 = 105.000
#: net = 2.100.000 − 420.000 − 105.000 = 1.575.000 · kalan = 11.200.000 − 2.100.000
#: ilerleme = 2.100.000 / 11.200.000 × 100 = %18,75
_BRUT = "2100000.00"
_AVANS = "420000.00"
_TEMINAT = "105000.00"
_NET = "1575000.00"
_KALAN = "9100000.00"
_ILERLEME = "18.75"


async def _hakedisli_proje(db_session, project_factory, olusturan_id: uuid.UUID) -> uuid.UUID:
    """Bedeli olan bir sözleşme + ONAYLANMIŞ tek hakediş.

    Hakediş durum GEÇİŞ uçlarıyla kurulmaz (`_fixtures_summary._ozet_ortami`
    emsali): kurulum testin ÖLÇTÜĞÜ şey değil ÖN KOŞULUDUR ve uçlardan kurmak
    testi ölçmediği kurallara (D8 tek açık hakediş) bağımlı yapardı.
    """
    project = await project_factory("KPS-CPR", name="Çapraz Sızıntı Projesi")
    contract = ProjectContract(
        project_id=project.id,
        contract_no="SZL-KPS-CPR",
        amount=_BEDEL,
        advance_pct=Decimal("20"),
        retainage_pct=Decimal("5"),
        vat_pct=Decimal("20"),
    )
    db_session.add(contract)
    await db_session.flush()

    site = Site(project_id=project.id, code="SNT-KPS", name="Çapraz Şantiyesi")
    group = EmployerContractGroup(project_id=project.id, name="Çapraz Grubu", sort_order=1)
    db_session.add_all([site, group])
    await db_session.flush()

    item = EmployerContractItem(
        project_id=project.id,
        group_id=group.id,
        code="03.001",
        description="Çapraz pozu",
        unit="m³",
        quantity=Decimal("100000"),
        unit_price=_BIRIM_FIYAT,
        sort_order=1,
    )
    db_session.add(item)
    await db_session.flush()

    payment = ProgressPayment(
        project_id=project.id,
        sequence_no=1,
        status=ProgressPaymentStatus.approved,
        period_year=2026,
        period_month=9,
        vat_pct=contract.vat_pct,
        advance_pct=contract.advance_pct,
        retainage_pct=contract.retainage_pct,
        created_by=olusturan_id,
    )
    payment.lines = [
        ProgressPaymentLine(
            contract_item_id=item.id,
            site_id=site.id,
            code=item.code,
            description=item.description,
            unit=item.unit,
            contract_unit_price=_BIRIM_FIYAT,
            coefficient=Decimal("1.000"),
            quantity=_MIKTAR,
            group_name=group.name,
        )
    ]
    db_session.add(payment)
    await db_session.flush()
    return project.id


async def _sozlesme_detayi(
    client, db_session, user_factory, project_factory, gizli: bool, eposta: str
) -> dict:
    """`GET /projects/{id}/contract` — `contracts` `tum_tutarlar` bayrağı = `gizli`."""
    olusturan = await user_factory(
        email=f"kurucu-{eposta}", password="parola1234", role_key="system_admin"
    )
    project_id = await _hakedisli_proje(db_session, project_factory, olusturan.id)
    token = await _login_with_access(client, db_session, user_factory, "project_manager", eposta)
    await modul_duzeyi_yaz(
        db_session,
        "project_manager",
        "contracts",
        AccessLevel.view,
        tum_tutarlar=gizli,
    )

    resp = await client.get(f"/projects/{project_id}/contract", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_ALL_kapsamda_GOMULU_ozetin_HER_SAYISI_GORUNUR(
    client, db_session, user_factory, project_factory
):
    """🔴 POZİTİF KONTROL — maske "her şeyi gizle" hâline gelirse burası kırmızı.

    Bu test olmasaydı, gömülü özeti KOŞULSUZ `None`a çeken bir kusur diğer iki
    testi de yeşil bırakırdı ve E14 "Hakediş Özeti" kartı HERKES için boşalırdı.
    """
    govde = await _sozlesme_detayi(
        client, db_session, user_factory, project_factory, False, "all@capraz.co"
    )
    ozet = govde["progress_payment_summary"]

    assert govde["amount"] == str(_BEDEL)
    assert ozet["contract_amount"] == str(_BEDEL)
    assert ozet["cumulative_gross"] == _BRUT
    assert ozet["advance_deduction_total"] == _AVANS
    assert ozet["retention_total"] == _TEMINAT
    assert ozet["net_total"] == _NET
    assert ozet["remaining"] == _KALAN
    assert ozet["progress_pct"] == _ILERLEME


async def test_LIMITED_kapsamda_GOMULU_ozetin_PARASI_da_GIZLENIR(
    client, db_session, user_factory, project_factory
):
    """`contracts = view/limited` → PARA gizli.

    🔴 Ölçülen kusur BUYDU: `amount` gizlenirken gömülü `contract_amount`
    AÇIKTAYDI — yani maske ikinci bir kapıdan AYNI sayıyı yayınlıyordu.
    `net_total`/`remaining` de türevdir; gizlenmeselerdi bedel
    `net_total + avans + teminat` ya da `cumulative_gross + remaining` ile geri
    hesaplanırdı.
    """
    govde = await _sozlesme_detayi(
        client, db_session, user_factory, project_factory, True, "lim@capraz.co"
    )
    ozet = govde["progress_payment_summary"]

    assert govde["amount"] is None, "SÖZLEŞME BEDELİ SIZDI"
    assert ozet["contract_amount"] is None, "GÖMÜLÜ ÖZETTEN BEDEL SIZDI"
    assert ozet["cumulative_gross"] is None, "KÜMÜLATİF BRÜT SIZDI"
    assert ozet["advance_deduction_total"] is None, "AVANS MAHSUBU SIZDI"
    assert ozet["retention_total"] is None, "TEMİNAT KESİNTİSİ SIZDI"
    assert ozet["net_total"] is None, "NET ÖDEME SIZDI"
    assert ozet["remaining"] is None, "KALAN BEDEL SIZDI"
    # IZN-B4: `progress_pct` bedelden TÜREYEN orandır (`paid / contract_amount`) → `sozlesme_fiyat`
    # etiketli; bedeli gizleyen rol oranı da görmez (yoksa bedel oran × tutardan geri hesaplanırdı).
    assert ozet["progress_pct"] is None, "TÜREV İLERLEME ORANI SIZDI"
    assert govde["contract_no"] == "SZL-KPS-CPR", "KİMLİK GİZLENDİ"


async def test_OZETIN_KENDI_UCU_DA_MASKELENIR_K2(client, db_session, user_factory, project_factory):
    """IZN-B4a K2: aynı şema KENDİ ucundan (`/projects/{id}/progress-payments/summary`) dönerken
    de maskelenir.

    🔴 Eski sürümde bu uç BİLİNÇLİ maskesizdi (router `kapsam_rotasi`ya bağlı değildi) ve bu test
    onu belgeliyordu; opus çürütücü bunu SIZINTI olarak ölçtü: `sozlesme_fiyat` gizli bir rol
    sözleşme bedelini ve türevlerini (hakediş toplamı, ilerleme, kesintiler, net) bu uçtan
    okuyabiliyordu. Router artık `MaskeRotasi` taşır; bayraksız rol ise HER ŞEYİ görür (pozitif
    kontrol) ve sayaçlar (`payment_count`) gizli rolde de durur.
    """
    olusturan = await user_factory(
        email="kurucu@ozet.co", password="parola1234", role_key="system_admin"
    )
    project_id = await _hakedisli_proje(db_session, project_factory, olusturan.id)
    token = await _login_with_access(
        client, db_session, user_factory, "project_manager", "ozet@capraz.co"
    )
    yol = f"/projects/{project_id}/progress-payments/summary"

    await rol_gizle(db_session, "project_manager")  # hiçbir kategori gizli değil
    acik = await client.get(yol, headers=_auth(token))
    assert acik.status_code == 200, acik.text
    assert acik.json()["contract_amount"] == str(_BEDEL)  # POZİTİF KONTROL
    assert acik.json()["cumulative_gross"] == _BRUT
    assert acik.json()["net_total"] == _NET
    assert acik.json()["progress_pct"] == _ILERLEME

    await rol_gizle(db_session, "project_manager", HiddenCategory.sozlesme_fiyat)
    gizli = await client.get(yol, headers=_auth(token))
    assert gizli.status_code == 200, gizli.text
    ozet = gizli.json()
    for alan in (
        "contract_amount",
        "cumulative_gross",
        "progress_pct",
        "advance_deduction_total",
        "retention_total",
        "net_total",
    ):
        assert ozet[alan] is None, alan
    assert ozet["payment_count"] == acik.json()["payment_count"]  # sayaç gizlenmez
