"""B9 — **META-TEST**: katalogdaki HER araç için kapsam iddiası.

| Bekçi | Mutasyon (KIRMIZI olmalı) | Pozitif kontrol (YEŞİL kalmalı) |
|---|---|---|
| B9 | Bir aracın `ucler` yolunu doğrudan servise bağla | Kapsamı OLAN aktör veriyi **GÖRÜR** |

## 🔴 SKIP YOK — ama "her araca aynı iddia" da YANLIŞ

İlk hâlim her araca aynı cümleyi kuruyordu ("kapsamsız aktör `ScopedEmpty`
alır") ve **üç araçta yanlış olduğu ölçüldü**:

* `onay_kutum` kapsamlı bir küme değil, **aktörün kendi kuyruğunu** döndürür;
  boş olması kapsam dışılık DEĞİLDİR ve `ScopedEmpty` yazmak "yetkin dar" ima
  ederdi.
* `gosterge_ozeti` bir LİSTE değil bir KART döndürür; kapsam farkı zarf
  **hâlinde** değil, kartın **içindeki sayıda** görünür.
* `yetkilerim` / `navigate_to` hiç kapsamlı veri okumaz.

Bu yüzden her araç için **adlandırılmış, bilinçli** bir beklenti yazılır ve
`BEKLENTILER` sözlüğünün katalogla **küme eşitliği** ayrıca bekçilenir: yeni bir
araç eklenip beklentisi yazılmazsa test SKIP olmaz, **KIRMIZI** olur.

🔴 Bu dosya "bekçi kendi ölçtüğü yolu kurmaz" kuralına uyar: rotalar
`build_read_plane`in kurduğu rotalardır, testin eklediği rota YOKTUR.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest

from app.modules.ai import audit as ai_audit
from app.modules.ai.registry import ToolRegistry
from app.modules.ai.result import Empty, NotFound, Ok, ScopedEmpty, ToolError
from app.modules.ai.tools.catalog import CATALOG, READ_TOOLS
from app.modules.users.models import ProjectMember

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _denetim_sussun(monkeypatch):
    """Denetim yazımı B6/B6b'nin işidir; burada ölçülen şey KAPSAM.

    (Gerçek yazım ayrı session açar ve testin savepoint'i dışına düşerek işçi
    veritabanına sızardı.)
    """

    async def _sahte(**kwargs):
        return None

    monkeypatch.setattr(ai_audit, "record_tool_call", _sahte)


@pytest.fixture
async def kapsam_kurulumu(seeded_db, user_factory, project_factory):
    """İki aktör + iki proje + iki şantiye + POZİTİF KONTROL tohumları.

    * `iceriden` — A projesine erişimi VAR.
    * `disaridan` — hiçbir projeye erişimi YOK ama **AYNI ROL, AYNI İZİN
      SEVİYELERİ**. 🔴 Fark YETKİDE değil KAPSAMDA: testin ölçtüğü şey
      `require_permission` değil `visible_*` zinciridir.

    🔴 **ROL `site_chief` → `patron` OLDU (AI-2b/2d) ve gerekçesi ÖLÇÜLDÜ.**
    Katalog 6'dan 22 araca çıktı; `site_chief` matriste `contracts=none`
    taşıyor (ölçüldü, `seed_data.MATRIX`), yani `sozlesmeler` ve `taseronlar`
    kataloğa HİÇ girmez ve B9 onları `ToolError` olarak görürdü — bekçi
    "kapsam çalışıyor"u değil "araç yok"u ölçmüş olurdu.

    Tohumlu roller tarandı; **22 aracın 22 kapısını da taşıyan tek rol
    `patron`**. Ve `patron` bu iş için GÜVENLİDİR: `projects=full`tur,
    `admin` DEĞİL — `visible_projects` yalnız `projects=admin` iken süzgeci
    atlar (`projects/service.py`), yani kapsam farkı ölçülebilir kalır. Bu
    satır düşerse (patron `admin`e çıkarsa) `_bekle_scoped_empty` iddiaları
    kırmızı olur ve rol seçimi yeniden düşünülür.

    🔴 **TOHUMLAR POZİTİF KONTROL İÇİNDİR.** Tohumsuz bir B9, on araçta "ikisi
    de boş" diyerek yeşil kalır ve **hiçbir şey ölçmezdi**. Her tohum bir
    aracın "içeriden GÖRÜNÜR" yarısını mümkün kılar.
    """
    from app.modules.contracts.models import Subcontractor, SubcontractorContract
    from app.modules.equipment.models import Equipment, EquipmentCategory
    from app.modules.progress_payments.models import ProgressPayment
    from app.modules.projects.models import ProjectContract, ProjectLandShare
    from app.modules.sites.models import Site, SiteStatus
    from app.modules.subcontractor_progress_payments.models import (
        SubcontractorProgressPayment,
    )

    a_projesi = await project_factory(code="AI-A", name="A Projesi")
    b_projesi = await project_factory(code="AI-B", name="B Projesi")

    a_santiye = Site(
        project_id=a_projesi.id,
        code="AI-A-S1",
        name="A Şantiyesi",
        status=SiteStatus.active,
        start_date=date(2026, 1, 1),
    )
    b_santiye = Site(
        project_id=b_projesi.id,
        code="AI-B-S1",
        name="B Şantiyesi",
        status=SiteStatus.active,
        start_date=date(2026, 1, 1),
    )
    seeded_db.add_all([a_santiye, b_santiye])

    iceriden = await user_factory("iceriden@fiil.example.com", "Sifre1234!", "patron")
    disaridan = await user_factory("disaridan@fiil.example.com", "Sifre1234!", "patron")
    seeded_db.add(
        ProjectMember(user_id=iceriden.id, project_id=a_projesi.id, role_id=iceriden.role_id)
    )
    await seeded_db.flush()

    taseron = Subcontractor(name="Kapsamsiz Taseron")
    # 🔴 PASİF taşeron: ucun `active_only` VARSAYILANI `True`dur ve devralınsaydı
    # bu satır hiçbir araç yanıtında görünmezdi.
    pasif_taseron = Subcontractor(name="Pasif Taseron", is_active=False)
    # 🔴 DEPODAKİ makine: `site_id IS NULL` → `equipment.repository.scope`un
    # OR dalı gereği KAPSAM SÜZGECİNE TABİ DEĞİL, yani DIŞARIDAKİ de görür.
    # Bu satır `SIRKET_GENELI` beyanının pozitif kontrolüdür.
    depo_makinesi = Equipment(name="Depo Makinesi", category=EquipmentCategory.machinery)
    # Şantiyeli makine: YALNIZ içeriden görünür → iki dal da ölçülmüş olur.
    a_makinesi = Equipment(
        name="Sahadaki Makine", category=EquipmentCategory.machinery, site_id=a_santiye.id
    )
    land_share = ProjectLandShare(
        project_id=a_projesi.id,
        landowner_name="Arsa Sahibi",
        our_share_pct=Decimal("50.00"),
        owner_share_pct=Decimal("50.00"),
    )
    isveren_sozlesmesi = ProjectContract(project_id=a_projesi.id, contract_no="ISV-1")
    isveren_hakedisi = ProgressPayment(
        project_id=a_projesi.id,
        sequence_no=1,
        vat_pct=Decimal("20.00"),
        advance_pct=Decimal("0.00"),
        retainage_pct=Decimal("0.00"),
        created_by=iceriden.id,
    )
    taseron_sozlesmesi = SubcontractorContract(
        project_id=a_projesi.id,
        subcontractor_name="Kapsamsiz Taseron",
        contract_no="TAS-1",
        created_by=iceriden.id,
    )
    # 🔴 SIRA ÖNEMLİ: `progress_payments.project_id` FK'si `projects`e DEĞİL
    # **`project_contracts`**e bakar (ölçüldü: `progress_payments_project_id_fkey`).
    # Aynı flush'a konursa SQLAlchemy sırayı çıkaramaz ve FK ihlali gelir.
    seeded_db.add_all(
        [
            taseron,
            pasif_taseron,
            depo_makinesi,
            a_makinesi,
            land_share,
            isveren_sozlesmesi,
            taseron_sozlesmesi,
        ]
    )
    await seeded_db.flush()
    seeded_db.add(isveren_hakedisi)
    seeded_db.add(
        SubcontractorProgressPayment(
            contract_id=taseron_sozlesmesi.id,
            project_id=a_projesi.id,
            sequence_no=1,
            vat_pct=Decimal("20.00"),
            advance_pct=Decimal("0.00"),
            retainage_pct=Decimal("0.00"),
            created_by=iceriden.id,
        )
    )
    await seeded_db.flush()

    # 🔴 KİMLİK HARİTASINDAN ÇIKAR — `populate_existing` kanonunun bu testteki
    # yüzü. Okuma düzlemi bu testte AYNI session'ı kullanır ve
    # `get_current_user` `session.get(User, ..., options=[joinedload(User.role)])`
    # çağırır; nesne haritada ROL YÜKLENMEDEN dururken `options` SESSİZCE YOK
    # SAYILIR ve `User.role` (`lazy="raise"`) patlar. Üretimde her istek kendi
    # session'ını alır, dolayısıyla bu satır testin kurgusunun bedelidir —
    # bir ürün kusuru DEĞİL. (`expire_all()` denendi ve `MissingGreenlet`
    # ürettiği ÖLÇÜLDÜ: expire edilmiş öznitelik senkron erişimde IO ister.)
    seeded_db.expunge(iceriden)
    seeded_db.expunge(disaridan)

    return {
        "iceriden": iceriden,
        "disaridan": disaridan,
        "a_projesi_id": a_projesi.id,
        "b_projesi_id": b_projesi.id,
        "a_santiye_id": a_santiye.id,
        "b_santiye_id": b_santiye.id,
    }


#: Yol parametresi / zorunlu sorgu parametresi isteyen araçların argümanları.
#: 🔴 Beş uç zorunlu SORGU parametresi bildirir (ölçüldü: `required=True`);
#: eksik gönderilirse araç `ust_kaynak_hatasi` döner ve B9 "kapsam" yerine
#: "422" ölçer.
_ARGUMANLAR = {
    "puantaj_haftasi": lambda k: {
        "site_id": str(k["a_santiye_id"]),
        "iso_year": 2026,
        "iso_week": 30,
    },
    "navigate_to": lambda k: {"ekran": "projeler"},
    "proje_detayi": lambda k: {"project_id": str(k["a_projesi_id"])},
    "arsa_payi": lambda k: {"project_id": str(k["a_projesi_id"])},
    "santiye_detayi": lambda k: {"site_id": str(k["a_santiye_id"])},
    "is_kalemleri": lambda k: {"site_id": str(k["a_santiye_id"])},
    "gunluk_kayit": lambda k: {"site_id": str(k["a_santiye_id"])},
    "puantaj": lambda k: {"site_id": str(k["a_santiye_id"]), "year": 2026, "month": 7},
    "gun_plani": lambda k: {"site_id": str(k["a_santiye_id"]), "start": "2026-07-01"},
    "sozlesmeler": lambda k: {"contract_type": "employer"},
    "makine_calisma": lambda k: {"year": 2026, "month": 7},
    "makine_yakit": lambda k: {"year": 2026, "month": 7},
}


def _argumanlar(arac_adi: str, kurulum) -> dict:
    yapici = _ARGUMANLAR.get(arac_adi)
    return yapici(kurulum) if yapici else {}


async def _cagir(arac_adi, user, kurulum, transport_factory, actor_factory, seeded_db, **ek):
    kayit = ToolRegistry(READ_TOOLS)
    return await kayit.invoke(
        arac_adi=arac_adi,
        argumanlar={**_argumanlar(arac_adi, kurulum), **ek},
        actor=await actor_factory(user),
        transport=transport_factory(user),
    )


# --------------------------------------------------------------------------- #
# Araç başına BİLİNÇLİ beklenti
# --------------------------------------------------------------------------- #


def _bekle_scoped_empty(dis, ic) -> None:
    assert isinstance(dis, ScopedEmpty), f"kapsamsız aktör {type(dis).__name__} aldı"
    assert isinstance(ic, Ok) and ic.row_count >= 1
    # 🔴 `Empty` DEĞİL: ikisi ayrılmazsa AI "hiç proje yok" der ve YALAN söyler.
    assert "kapsamınızda değil" in dis.mesaj()
    assert "hiç kayıt yok" not in dis.mesaj()


def _bekle_notfound(dis, ic) -> None:
    assert isinstance(dis, NotFound), f"kapsamsız aktör {type(dis).__name__} aldı"
    assert isinstance(ic, Ok)


def _bekle_kart_farki(dis, ic) -> None:
    """Kart döndüren araç: zarf hâli AYNI, ama kartın SAYISI farklı."""
    assert isinstance(dis, Ok) and isinstance(ic, Ok)
    assert ic.data["gorunur_proje_sayisi"] > dis.data["gorunur_proje_sayisi"] == 0


def _bekle_kendi_kuyrugu(dis, ic) -> None:
    """`onay_kutum`: aktörün KENDİ kuyruğu. Kimseye imza düşmediği için ikisi de
    boştur ve bu **kapsam dışılık DEĞİLDİR** — `ScopedEmpty` yazmak yanlış olurdu."""
    assert isinstance(dis, Ok) and isinstance(ic, Ok)
    assert dis.row_count == ic.row_count == 0
    assert dis.data["total"] == ic.data["total"] == 0
    assert "my_approval_roles" not in dis.data  # IZN-B3b: alan kalkti (rol artik projeye bagli)


def _bekle_kapsamsiz(dis, ic) -> None:
    """Kapsamlı veri OKUMAYAN araç: iki aktör aynı ŞEKLİ alır."""
    assert isinstance(dis, Ok) and isinstance(ic, Ok)
    assert set(dis.data) == set(ic.data)


def _bekle_notfound_vs_bos(dis, ic) -> None:
    """🔴 GÖRÜNMEYEN ŞANTİYE ile BOŞ LİSTE aynı cevap DEĞİLDİR.

    Dışarıdaki **404** alır (`NotFound` — "erişebildiğin kapsamda böyle bir
    kayıt yok"); içerideki 200 + boş alır ve zarf **`Empty`** olur: *"bu
    şantiyede günlük kaydı girilmemiş."*

    🔴 **`ScopedEmpty` DEĞİL — ve bu bir DÜZELTMEDİR.** İlk hâlim
    `ScopedEmpty` bekliyordu, yani handler'ın *"hiçbiri sizin kapsamınızda
    değil"* demesini KANON yapıyordu. Ölçüldü ve YALAN çıktı: uç kapsam
    kararını şantiye üzerinden verir (`site_diary/read.py::list_entries` →
    `service.visible_site`) ve görünmeyen şantiye **404** alır — yani kapsam
    dışılık 200+boş dalına HİÇ ULAŞAMAZ. Bir bekçinin yanlış bir cümleyi
    kilitlemesi, cümleyi hiç bekçilememekten daha kötüdür.

    ⚠️ DÜRÜST NOT: bu araç için VERİLİ bir pozitif kontrol yoktur (fikstür
    günlük kaydı tohumlamıyor). Ölçülen şey **iki zarf hâlinin ayrıştığı**dır;
    "veri geliyor" iddiası burada KURULMAZ.
    """
    assert isinstance(dis, NotFound), f"kapsamsız aktör {type(dis).__name__} aldı"
    assert isinstance(ic, Empty), f"içerideki {type(ic).__name__} aldı"
    # 🔴 Ve cümleler AYRIŞIR: "kapsamınızda değil" ibaresi BASILMAZ.
    assert "kapsamınızda değil" not in ic.mesaj()
    assert "hiç kayıt yok" in ic.mesaj()


def _bekle_sirket_geneli_ayni(dis, ic) -> None:
    """🔴 `SIRKET_GENELI` aracın iddiası: kapsam farkı SONUCU DEĞİŞTİRMEZ.

    `_bekle_scoped_empty`in TERSİ bir iddiadır ve bilinçlidir: `taseronlar`ın
    kapsam süzgeci **hiç yoktur** (uç `user` parametresi bile almaz), ötekiler
    ise iki dallı OR taşır ve tohumlanan satır kapsamsız dala düşer.

    ⚠️ **DÜRÜST NOT — üç aracın ikisinde VERİLİ pozitif kontrol YOKTUR.**
    `taseronlar` gerçekten tohumludur (bir `Subcontractor` var, ikisi de
    GÖRÜR): iddia hem doludur hem eşittir. Ama `makine_yakit` ve `makine_kira`
    için fikstür satır tohumlamaz (yakıt fişi / kira faturası yok), yani orada
    ölçülen şey "iki boş küme eşit"tir. Bunu gizlemek yerine yazıyorum:
    o iki araç için asıl kanıt `test_ai2bd_araclar.py`deki **kaynak zinciri**
    ölçümüdür (OR dalı hâlâ kodda mı), bu satır değil.
    """
    assert dis.govde() == ic.govde(), (
        "ŞİRKET GENELİ beyan eden araç kapsamla DEĞİŞTİ — `kume` yeniden ölçülmeli"
    )


def _iki_dal(dis, ic, adlar) -> None:
    """🔴 İKİ DALLI OR'un İKİ DALI DA ÖLÇÜLÜR.

    * DEPODAKİ makine (`site_id IS NULL`) kapsam süzgecine TABİ DEĞİL →
      hiçbir projeye erişimi olmayan aktör bile onu GÖRÜR. `SIRKET_GENELI`
      beyanının pozitif kontrolü budur.
    * Şantiyeli makine YALNIZ içerideki için görünür → süzgecin fiilen
      çalıştığının kanıtı.

    İkisi birlikte olmasaydı: yalnız ilki "süzgeç hiç çalışmıyor"u da
    geçirirdi, yalnız ikincisi `SIRKET_GENELI` beyanını kanıtlamazdı.
    """
    assert isinstance(dis, Ok) and isinstance(ic, Ok)
    assert adlar(dis) == {"Depo Makinesi"}, adlar(dis)
    assert adlar(ic) == {"Depo Makinesi", "Sahadaki Makine"}, adlar(ic)


def _bekle_depo_dali_gorunur(dis, ic) -> None:
    """`makine_listesi` — satır kümesi doğrudan `repository.scope`tan gelir."""
    _iki_dal(dis, ic, lambda z: {m["name"] for m in z.data})


def _bekle_calisma_ozeti_iki_dal(dis, ic) -> None:
    """🔴 ÖLÇÜMLE BULUNDU: `work-summary` bir AGREGAT ama kapsam DUYARLIDIR.

    İlk hâlim bu araca `_bekle_sirket_geneli_ayni` yazmıştı ("agregat, kapsam
    farkı görünmez") ve test **KIRMIZI** oldu: uç, saati SIFIR olan makineler
    için de satır üretir, yani satır kümesi görünür ekipman kümesidir. Aktör
    farkı doğrudan `rows`ta konuşur.

    Ders kayda geçsin: "agregat = kapsamsız" ÇIKARIMI YANLIŞTIR. Toplamlar
    aynı (ikisi de 0) ama küme farklı — beyan yine `SIRKET_GENELI`dir çünkü
    DEPO dalı süzgeci atlar.
    """
    _iki_dal(dis, ic, lambda z: {r["equipment_name"] for r in z.data["rows"]})


#: 🔴 Her araç için ADLANDIRILMIŞ beklenti. Kümesi katalogla eşitlenir.
BEKLENTILER = {
    # --- AI-0b -------------------------------------------------------------
    "projeleri_listele": _bekle_scoped_empty,
    "puantaj_haftasi": _bekle_notfound,
    "gosterge_ozeti": _bekle_kart_farki,
    "onay_kutum": _bekle_kendi_kuyrugu,
    "yetkilerim": _bekle_kapsamsiz,
    "navigate_to": _bekle_kapsamsiz,
    # --- AI-2b: kimlikle çağrılanlar → görünmeyen kayıt 404 (S14) ----------
    "proje_detayi": _bekle_notfound,
    "santiye_detayi": _bekle_notfound,
    "is_kalemleri": _bekle_notfound,
    "arsa_payi": _bekle_notfound,
    "puantaj": _bekle_notfound,
    "gun_plani": _bekle_notfound,
    # --- AI-2b: kapsam süzgeçli listeler → 200 + BOŞ (403 DEĞİL) -----------
    "santiyeleri_listele": _bekle_scoped_empty,
    "isveren_hakedisleri": _bekle_scoped_empty,
    "taseron_hakedisleri": _bekle_scoped_empty,
    "sozlesmeler": _bekle_scoped_empty,
    # --- AI-2b: iki hâl AYRIŞIR ------------------------------------------- #
    "gunluk_kayit": _bekle_notfound_vs_bos,
    # --- ŞİRKET GENELİ: kapsam farkı sonucu DEĞİŞTİRMEZ -------------------- #
    "taseronlar": _bekle_sirket_geneli_ayni,
    "makine_calisma": _bekle_calisma_ozeti_iki_dal,
    "makine_yakit": _bekle_sirket_geneli_ayni,
    "makine_kira": _bekle_sirket_geneli_ayni,
    # --- ŞİRKET GENELİ ama İKİ DAL de ölçülür ------------------------------ #
    "makine_listesi": _bekle_depo_dali_gorunur,
}


def test_B9_BEKLENTILER_katalogla_KUME_ESITTIR() -> None:
    """Yeni bir araç eklenip beklentisi yazılmazsa SKIP değil KIRMIZI."""
    assert set(BEKLENTILER) == {s.ad for s in CATALOG}


# --------------------------------------------------------------------------- #
# B9 — HER araç için parametrize
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.ad)
async def test_B9_kapsam_farki_HER_ARAC_ICIN_olculur(
    spec, kapsam_kurulumu, transport_factory, actor_factory, seeded_db
):
    dis = await _cagir(
        spec.ad,
        kapsam_kurulumu["disaridan"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
    )
    ic = await _cagir(
        spec.ad,
        kapsam_kurulumu["iceriden"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
    )
    assert not isinstance(dis, ToolError), f"{spec.ad} (dışarıdan): {dis}"
    # 🔴 POZİTİF KONTROL: bu satır olmadan test "kapsam çalışıyor"u değil
    # "hiçbir şey dönmüyor"u kanıtlardı.
    assert not isinstance(ic, ToolError), f"{spec.ad} (içeriden): {ic}"
    BEKLENTILER[spec.ad](dis, ic)


@pytest.mark.parametrize("spec", CATALOG, ids=lambda s: s.ad)
async def test_B9_kapsam_disi_zarfta_A_PROJESININ_izi_YOKTUR(
    spec, kapsam_kurulumu, transport_factory, actor_factory, seeded_db
):
    """Her araca uygulanabilen TEK tekdüze iddia: kapsam dışı aktörün zarfında
    içerideki varlıkların adı ya da kimliği **hiç geçmez**."""
    dis = await _cagir(
        spec.ad,
        kapsam_kurulumu["disaridan"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
    )
    metin = str(dis.govde())
    for iz in (
        "A Projesi",
        "AI-A",
        "A Şantiyesi",
        str(kapsam_kurulumu["a_projesi_id"]),
        str(kapsam_kurulumu["a_santiye_id"]),
    ):
        assert iz not in metin, f"`{spec.ad}` kapsam dışı zarfta {iz!r} sızdırdı"


async def test_B9_POZITIF_KONTROL_iceridekinin_zarfinda_A_PROJESI_GORUNUR(
    kapsam_kurulumu, transport_factory, actor_factory, seeded_db
):
    """Yukarıdaki 'iz yok' iddiası, iz **görülebilir olduğu için** anlamlıdır."""
    ic = await _cagir(
        "projeleri_listele",
        kapsam_kurulumu["iceriden"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
    )
    assert "A Projesi" in str(ic.govde())
    assert "B Projesi" not in str(ic.govde())


# --------------------------------------------------------------------------- #
# AI-0 kabul ölçütü 5 + S14
# --------------------------------------------------------------------------- #


async def test_TASERONLAR_PASIF_kayitlari_da_getirir_active_only_DEVRALINMAZ(
    kapsam_kurulumu, transport_factory, actor_factory, seeded_db
):
    """🔴 GİZLİ VARSAYILAN — ÜÇ YERDE BİRDEN YALAN ÜRETİYORDU.

    `GET /subcontractors` `active_only: bool = True` bildirir. Handler
    parametresiz çağırsaydı repository `WHERE is_active IS TRUE` koyardı ve:

      (a) katalog açıklaması *"şirketin tüm taşeronlarını içerir"* derken küme
          SÜZÜLMÜŞ olurdu,
      (b) `AiTaseron.is_active` yapısal olarak HEP `True` olurdu — alan bilgi
          taşımaz, `presenters` içindeki "pasif" dalı ÖLÜ KOD olurdu,
      (c) boş küme "kayıt yok" derdi, doğrusu "AKTİF kayıt yok"tur.

    Bu test üçünü birden kapatır: pasif kayıt **dönmeli** ve `is_active`
    alanı **gerçekten iki değer** taşımalı. Bekçi yapısal değil DAVRANIŞSALDIR;
    params bekçisi `active_only` gönderildiğini görür ama ETKİSİNİ göremez.
    """
    sonuc = await _cagir(
        "taseronlar",
        kapsam_kurulumu["iceriden"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
    )
    assert isinstance(sonuc, Ok), sonuc
    adlar = {t["name"] for t in sonuc.data}
    assert "Kapsamsiz Taseron" in adlar
    assert "Pasif Taseron" in adlar, (
        "Pasif taşeron DÖNMEDİ — `active_only` varsayılanı devralınmış olabilir."
    )
    # 🔴 Alan gerçekten İKİ DEĞER taşıyor: "pasif" dalı ölü kod değil.
    assert {t["is_active"] for t in sonuc.data} == {True, False}


async def test_B9_KABUL_OLCUTU_5_baska_projenin_santiyesi_NotFound_A_ile_GORUNUR(
    kapsam_kurulumu, transport_factory, actor_factory, seeded_db
):
    gorunmeyen = await _cagir(
        "puantaj_haftasi",
        kapsam_kurulumu["iceriden"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
        site_id=str(kapsam_kurulumu["b_santiye_id"]),
    )
    assert isinstance(gorunmeyen, NotFound), gorunmeyen

    gorunen = await _cagir(
        "puantaj_haftasi",
        kapsam_kurulumu["iceriden"],
        kapsam_kurulumu,
        transport_factory,
        actor_factory,
        seeded_db,
    )
    assert isinstance(gorunen, Ok), gorunen
    assert gorunen.data["site_name"] == "A Şantiyesi"


async def test_S14_gorunmeyen_var_olan_ile_var_olmayan_BAYT_BAYT_AYNI(
    kapsam_kurulumu, transport_factory, actor_factory, seeded_db
):
    async def _sonda(site_id) -> dict:
        sonuc = await _cagir(
            "puantaj_haftasi",
            kapsam_kurulumu["iceriden"],
            kapsam_kurulumu,
            transport_factory,
            actor_factory,
            seeded_db,
            site_id=str(site_id),
        )
        return sonuc.govde()

    assert await _sonda(kapsam_kurulumu["b_santiye_id"]) == await _sonda(uuid.uuid4())


# --------------------------------------------------------------------------- #
# B9 MUTASYONU — aracın ucu yerine SERVİSE bağlanması
# --------------------------------------------------------------------------- #


def test_B9_MUTASYON_servise_baglanan_arac_KAPSAMI_KAYBEDER() -> None:
    """🔴 #1 mimarisini öldüren ölçümün birebir yeniden üretimi.

    `timesheet/week.py::build(...)` **aktör ALMAZ**; kapsam kapısı router'ın
    çağırdığı `service.visible_site(session, user, site_id)`tir. Bir araç ucu
    değil servisi sarsaydı, `timesheet:view` olan HERKES erişimi olmayan
    projelerin puantajını okurdu. Bu test o olguyu kilitler: ölçüm bir gün
    değişirse (servis aktör almaya başlarsa) burası kırmızı olur ve T2 kararı
    yeniden düşünülür.
    """
    import inspect

    from app.modules.timesheet import router as timesheet_router
    from app.modules.timesheet import service as timesheet_service
    from app.modules.timesheet import week as timesheet_week

    imza = inspect.signature(timesheet_week.build)
    assert not ({"actor", "user"} & set(imza.parameters)), (
        f"`timesheet.week.build` artık aktör alıyor: {list(imza.parameters)}. "
        "#1'i çürüten ölçüm yeniden yapılmalı."
    )
    assert hasattr(timesheet_service, "visible_site")
    assert "visible_site" in inspect.getsource(timesheet_router)
