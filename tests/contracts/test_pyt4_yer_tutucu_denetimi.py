"""P-YT4 — `contracts` yer tutucularının DENETİM bekçileri (2026-08-23).

P-YT1 (`sites`) · P-YT2 (`sites`+`dashboard`) · P-YT3 (`boq`·`sales`·`inventory`)
turlarının dördüncüsü. Denetimin bu dosyada çakılan ÜÇ bulgusu:

1. 🔑 **BAĞLANDI** — `ContractListItem.progress_pct` taşeron sekmesinde artık
   `None` DEĞİL. Eski gerekçe ("taşeron hakedişi AYRI dilim, spec §1.2") ÖLÇÜLDÜ
   ve BAYAT çıktı: dilim TH ile yazıldı, `summary.cumulative_gross_by_contracts`
   CANLI ve `contracts.service.list_contracts` onu ZATEN çağırıyordu — yalnız
   `items`a geçirmiyordu. Bağlama EK SORGU AÇMAZ (aşağıdaki N+1 bekçisi).
2. **ANAHTAR FOSİLİ** — `EmployerContractDetail.pending_modules` `"project_schedule"`
   diyordu; bu ad depoda BAŞKA HİÇBİR YERDE geçmez (ne izin modülü, ne paket, ne
   dosya). Gerçek sahip `sites` (`section_milestones`) ve CANLI.
3. **BAĞ YOK, MODÜL VAR** — `documents` (BC) ve `subcontractor_progress_payments`
   (TH) modüllerinin İKİSİ DE canlı; eksik olan sözleşme→belge BAĞI ve
   `progress_payment_summary`in ŞEKLİ (mockup kendi etiketiyle çelişiyor).

Bekçilerin ayrışımı:
* `test_baglandi_*`   — yeni davranış (uçtan uca)
* `test_taban_*`      — hangi statüler paya girer (P-YT3'ün "taban" dersi)
* `test_N1_*`         — bağlama sorgu sayısını ARTIRMADI
* `test_gerekce_*`    — kalan yer tutucuların gerekçeleri hâlâ doğru mu
"""

import uuid
from decimal import Decimal

from sqlalchemy import select

from app.core.sayfalar import MODUL_ANAHTARLARI
from app.modules.contracts.models import (
    ContractStatus,
    Subcontractor,
    SubcontractorContract,
    SubcontractorContractItem,
)
from app.modules.contracts.schemas import EmployerContractDetail, SubcontractorContractDetail
from app.modules.documents.models import Document, DocumentFolder
from app.modules.projects.models import Project
from app.modules.sites.models import SectionMilestone
from app.modules.subcontractor_progress_payments.models import (
    SubcontractorPaymentStatus,
    SubcontractorProgressPayment,
    SubcontractorProgressPaymentLine,
)
from app.modules.users.models import User

# `asyncio_mode = "auto"` (pyproject) — async testler ayrıca işaretlenmez;
# modül düzeyi `pytestmark` senkron bekçilere de iner ve uyarı üretirdi.
_BIRIM = Decimal("1000.00")


async def _admin_id(session) -> uuid.UUID:
    """`created_by` icin HERHANGI bir kullanici — testin konusu yetki degil veri.

    Sabit e-postaya baglanmaz: bazi testler `admin_headers` yerine
    `kisitli_headers` ile kosar ve o kullanici hic yaratilmamis olur.
    """
    user = (await session.execute(select(User).limit(1))).scalars().first()
    assert user is not None, "Testte hic kullanici yok."
    return user.id


async def _sozlesme(
    session, project: Project, *, contract_no: str, miktar: Decimal
) -> SubcontractorContract:
    """Tek kalemli taşeron sözleşmesi. Bedel = `miktar × 1.000,00`."""
    sub = Subcontractor(name=f"Taşeron {contract_no}")
    session.add(sub)
    await session.flush()
    contract = SubcontractorContract(
        project_id=project.id,
        subcontractor_id=sub.id,
        subcontractor_name=sub.name,
        contract_no=contract_no,
        status=ContractStatus.active,
        created_by=await _admin_id(session),
    )
    session.add(contract)
    await session.flush()
    session.add(
        SubcontractorContractItem(
            contract_id=contract.id,
            code="01.001",
            description="Kazı",
            unit="m3",
            quantity=miktar,
            unit_price=_BIRIM,
            sort_order=0,
        )
    )
    await session.flush()
    await session.refresh(contract)
    return contract


async def _hakedis(
    session,
    contract: SubcontractorContract,
    *,
    sequence_no: int,
    status: SubcontractorPaymentStatus,
    miktar: Decimal,
) -> None:
    """Sözleşmenin TEK kalemine `miktar` yazan bir hakediş. Brüt = miktar × 1.000."""
    item = contract.items[0]
    payment = SubcontractorProgressPayment(
        contract_id=contract.id,
        project_id=contract.project_id,
        sequence_no=sequence_no,
        period_year=2026,
        period_month=7,
        status=status,
        vat_pct=Decimal("20.00"),
        advance_pct=Decimal("0.00"),
        retainage_pct=Decimal("0.00"),
        created_by=await _admin_id(session),
    )
    payment.lines = [
        SubcontractorProgressPaymentLine(
            contract_item_id=item.id,
            code=item.code,
            description=item.description,
            unit=item.unit,
            contract_unit_price=item.unit_price,
            coefficient=Decimal("1.000"),
            quantity=miktar,
            sort_order=0,
        )
    ]
    session.add(payment)
    await session.flush()


def _satir(govde: dict, contract_id: uuid.UUID) -> dict:
    return next(row for row in govde["items"] if row["id"] == str(contract_id))


# --- 1. BAĞLANDI: taşeron satırının İlerleme sütunu ---


async def test_baglandi_taseron_satiri_ILERLEME_yuzdesi_dondurur(
    client, admin_headers, ornek_proje, seeded_db
):
    """🔑 SZL 49 "İlerleme" sütunu taşeron sekmesinde de GERÇEK sayı basar.

    Bedel 10 × 1.000 = 10.000,00; onaylı hakedişin brütü 4 × 1.000 = 4.000,00
    ⇒ %40,00. Eski davranış `None`'dı (mockup'ta sütun "—" görünürdü).
    """
    contract = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-P40", miktar=Decimal("10"))
    await _hakedis(
        seeded_db,
        contract,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        miktar=Decimal("4"),
    )

    yanit = await client.get("/contracts?type=subcontractor", headers=admin_headers)

    assert yanit.status_code == 200, yanit.text
    satir = _satir(yanit.json(), contract.id)
    assert satir["amount"] == "10000.00"
    assert satir["progress_pct"] == "40.00"


async def test_baglandi_hakedissiz_sozlesmede_SIFIR_doner_bilinmeyen_degil(
    client, admin_headers, ornek_proje, seeded_db
):
    """Hakedişi olmayan sözleşme `%0,00`dır — `None` (bilinmiyor) DEĞİL.

    İşveren dalının davranışıyla BİREBİR aynı (`cumulative.get(id, 0.00)`).
    """
    contract = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-P00", miktar=Decimal("5"))

    yanit = await client.get("/contracts?type=subcontractor", headers=admin_headers)

    assert _satir(yanit.json(), contract.id)["progress_pct"] == "0.00"


async def test_baglandi_bedelsiz_sozlesmede_None_kalir_sahte_yuzde_YOK(
    client, admin_headers, ornek_proje, seeded_db
):
    """Fiyatsız (bedeli 0) sözleşmede bölme YAPILMAZ: `None`.

    `summary.progress_pct` sözleşmesi (`amount <= 0 ⇒ None`) taşeron dalında da
    aynen geçerlidir — sahte `%0` bir bilgi iddiasıdır.
    """
    sub = Subcontractor(name="Fiyatsız Taşeron")
    seeded_db.add(sub)
    await seeded_db.flush()
    contract = SubcontractorContract(
        project_id=ornek_proje.id,
        subcontractor_id=sub.id,
        subcontractor_name=sub.name,
        contract_no="TSD-P-BOS",
        status=ContractStatus.active,
        created_by=await _admin_id(seeded_db),
    )
    seeded_db.add(contract)
    await seeded_db.flush()

    yanit = await client.get("/contracts?type=subcontractor", headers=admin_headers)

    satir = _satir(yanit.json(), contract.id)
    assert satir["amount"] == "0.00"
    assert satir["progress_pct"] is None


# --- 2. TABAN: hangi statüler paya girer ---


async def test_taban_yalniz_approved_ve_paid_paya_girer(
    client, admin_headers, ornek_proje, seeded_db
):
    """🔴 TABAN BEKÇİSİ — taslak ve onay bekleyen hakediş İLERLEMEYE GİRMEZ.

    Bu sütunun tabanı, aynı satırdaki `summary.progress_payment_total` KPI'ı ve
    işveren sekmesindeki aynı sütunla AYNI olmak ZORUNDA (`COMPLETED_STATUSES`).
    Bu depoda İKİNCİ bir "ilerleme" formülü daha var — `cost_summary._row`
    YALNIZ `paid` sayar (mockup aritmetiğinden okundu, KY 209-251/KK 213-246) —
    ve iki formül karışırsa aynı ada sahip iki sütun farklı şey ölçer.

    Kurulum: bedel 20.000,00 · approved 4.000 · paid 3.000 · pending 5.000 ·
    draft 6.000 ⇒ pay 7.000 ⇒ %35,00 (rakip formüller %20 / %60 / %90 ederdi).
    """
    contract = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-TAB", miktar=Decimal("20"))
    for sira, (durum, miktar) in enumerate(
        (
            (SubcontractorPaymentStatus.approved, Decimal("4")),
            (SubcontractorPaymentStatus.paid, Decimal("3")),
            (SubcontractorPaymentStatus.pending_approval, Decimal("5")),
            (SubcontractorPaymentStatus.draft, Decimal("6")),
        ),
        start=1,
    ):
        await _hakedis(seeded_db, contract, sequence_no=sira, status=durum, miktar=miktar)

    yanit = await client.get("/contracts?type=subcontractor", headers=admin_headers)

    satir = _satir(yanit.json(), contract.id)
    assert satir["progress_pct"] == "35.00", (
        "Taban kaymış: yalnız `approved|paid` paya girmeli (COMPLETED_STATUSES)."
    )


async def test_taban_satir_yuzdesi_ile_KPI_toplami_ayni_kumeden_okur(
    client, admin_headers, ornek_proje, seeded_db
):
    """Satır yüzdesi ile şerit KPI'ı TEK kümülatif sözlükten beslenir.

    İki ayrı okuma olsaydı biri ötekinden sessizce sapardı (bu depoda `spent`
    alanında bir kez yaşandı).
    """
    a = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-KPI-A", miktar=Decimal("10"))
    b = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-KPI-B", miktar=Decimal("10"))
    await _hakedis(
        seeded_db, a, sequence_no=1, status=SubcontractorPaymentStatus.approved, miktar=Decimal("2")
    )
    await _hakedis(
        seeded_db, b, sequence_no=1, status=SubcontractorPaymentStatus.paid, miktar=Decimal("3")
    )

    govde = (await client.get("/contracts?type=subcontractor", headers=admin_headers)).json()

    assert _satir(govde, a.id)["progress_pct"] == "20.00"
    assert _satir(govde, b.id)["progress_pct"] == "30.00"
    # 2.000 + 3.000 = 5.000,00
    assert Decimal(govde["summary"]["progress_payment_total"]) == Decimal("5000.00")


async def test_taban_gorunmeyen_projenin_sozlesmesi_ne_satirda_ne_KPIda(
    client, kisitli_headers, ornek_proje, gorunmeyen_proje, seeded_db
):
    """Kapsam süzgeci bağlamadan SONRA da SQL'de: gizli sözleşme hiç çekilmez."""
    gizli = await _sozlesme(
        seeded_db, gorunmeyen_proje, contract_no="TSD-GIZLI", miktar=Decimal("10")
    )
    acik = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-ACIK", miktar=Decimal("10"))
    await _hakedis(
        seeded_db,
        gizli,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        miktar=Decimal("9"),
    )
    await _hakedis(
        seeded_db,
        acik,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        miktar=Decimal("1"),
    )

    govde = (await client.get("/contracts?type=subcontractor", headers=kisitli_headers)).json()

    assert all(row["id"] != str(gizli.id) for row in govde["items"])
    assert _satir(govde, acik.id)["progress_pct"] == "10.00"
    assert Decimal(govde["summary"]["progress_payment_total"]) == Decimal("1000.00")


# --- 3. N+1 ---


async def test_N1_ilerleme_baglamasi_sozlesme_basina_SORGU_ACMAZ(
    client, admin_headers, ornek_proje, seeded_db
):
    """🔴 Bağlama EK SORGU AÇMADI: kümülatif sözlük ZATEN okunuyordu.

    DÖRT sözleşme + dört hakedişle koşulan sorgu sayısı, TEK sözleşmeliyle AYNI
    olmalı. Sözleşme başına `cumulative_gross_by_contracts` çağıran naif bağlama
    (bu bekçi yazılırken mutasyonla denendi) burada kırmızıya döner.
    """
    from sqlalchemy import event

    ifadeler: list[str] = []

    def _kaydet(conn, cursor, statement, parameters, context, executemany):
        ifadeler.append(statement)

    tek = await _sozlesme(seeded_db, ornek_proje, contract_no="TSD-N1-1", miktar=Decimal("10"))
    await _hakedis(
        seeded_db,
        tek,
        sequence_no=1,
        status=SubcontractorPaymentStatus.approved,
        miktar=Decimal("1"),
    )
    bind = seeded_db.get_bind()
    event.listen(bind, "before_cursor_execute", _kaydet)
    try:
        await client.get("/contracts?type=subcontractor", headers=admin_headers)
        tek_sozlesme = len(ifadeler)

        for i in (2, 3, 4):
            c = await _sozlesme(
                seeded_db, ornek_proje, contract_no=f"TSD-N1-{i}", miktar=Decimal("10")
            )
            await _hakedis(
                seeded_db,
                c,
                sequence_no=1,
                status=SubcontractorPaymentStatus.approved,
                miktar=Decimal("1"),
            )
        ifadeler.clear()
        await client.get("/contracts?type=subcontractor", headers=admin_headers)
        dort_sozlesme = len(ifadeler)
    finally:
        event.remove(bind, "before_cursor_execute", _kaydet)

    assert dort_sozlesme == tek_sozlesme, (
        f"Sözleşme sayısı 1→4 olunca sorgu {tek_sozlesme}→{dort_sozlesme} çıktı: "
        "ilerleme bağlaması N+1 açmış."
    )


# --- 4. GEREKÇE ÇÜRÜME BEKÇİLERİ ---

# Her `pending_modules` anahtarı CANLI bir artefaktı adlandırmak ZORUNDA.
# `project_schedule` bu bekçi yazılana kadar bir FOSİLDİ: depoda o ada sahip
# hiçbir izin modülü, paket ya da dosya yoktu.
#
# 🔴 Harita DEPO GENELİDİR, tek modülün değil: aşağıdaki bekçi `pending_modules`
# alanı taşıyan HER şemayı gezer. Yeni bir anahtar eklendiğinde buraya "hangi
# canlı artefaktı gösteriyor" satırı yazılmadan test geçmez.
#
# Anahtarların hepsi izin modülü DEĞİLDİR ve bu bilinçlidir (P-YT3'ün
# `tests/sales/...::test_pending_modules_etiketlerinin_HEPSI_CANLI_bir_izin_
# modulunu_adlandirir` bekçisi yalnız `sales`in anahtarları için geçerlidir):
# `project_costs` bir DOSYA (`projects/costs.py`),
# `subcontractor_progress_payments` bir PAKET ama izin modülü değil.
_ANAHTAR_ARTEFAKTI = {
    "documents": "app/modules/documents",
    "invoicing": "app/modules/invoicing",
    "project_costs": "app/modules/projects/costs.py",
    "sites": "app/modules/sites",
    "subcontractor_progress_payments": "app/modules/subcontractor_progress_payments",
}


def _pending_modules_semalari() -> dict[str, list[str]]:
    """`pending_modules` alanı taşıyan TÜM şemalar → varsayılan anahtar listesi.

    `app.main` import edildiği anda her modülün şemaları `sys.modules`tadır;
    bekçi bu yüzden elle bir şema listesi TAŞIMAZ (taşısaydı yeni bir şema
    sessizce denetim dışında kalırdı — bu turda bulunan fosil de tam olarak
    "arama dar tutuldu" yüzünden ikinci bir yerde hayatta kalmıştı).
    """
    import sys

    from pydantic import BaseModel

    # Yan etki KASITLI: `app.main` tüm modüllerin şemalarını `sys.modules`a yükler.
    import app.main  # noqa: F401

    bulunan: dict[str, list[str]] = {}
    for ad, modul in list(sys.modules.items()):
        if not ad.startswith("app."):
            continue
        for nesne_adi in dir(modul):
            nesne = getattr(modul, nesne_adi, None)
            if not (isinstance(nesne, type) and issubclass(nesne, BaseModel)):
                continue
            alan = nesne.model_fields.get("pending_modules")
            if alan is None or alan.default_factory is None:
                continue
            bulunan[f"{nesne.__module__}.{nesne.__name__}"] = list(alan.default_factory())  # type: ignore[call-arg]
    return bulunan


def test_gerekce_pending_modules_anahtarlari_FOSIL_OLAMAZ() -> None:
    """🔴 Anahtar, ADI OLAN bir şeyi göstermek zorundadır — DEPO GENELİNDE.

    P-YT1 kanonu: `pending_module` artık "modül yok" DEMİYOR, "veri hangi modülün
    mülkiyetinde" diyor. Bu ancak anahtar GERÇEK bir şeyi adlandırırsa anlamlıdır.
    Bekçi iki yoldan da doğrular: ya 21 izin modülünden biri, ya depoda var olan
    canlı bir paket/dosya.
    """
    import pathlib

    kok = pathlib.Path(__file__).resolve().parents[2]
    izin_modulleri = set(MODUL_ANAHTARLARI)
    semalar = _pending_modules_semalari()

    # Denetimin bildiği İKİ sözleşme şeması mutlaka taranmış olmalı — bekçi
    # kendi girdisini kaybederse sessizce boş küme üzerinde yeşil kalırdı.
    for sema in (EmployerContractDetail, SubcontractorContractDetail):
        anahtar_adi = f"{sema.__module__}.{sema.__name__}"
        assert anahtar_adi in semalar, f"{anahtar_adi} taranamadı — bekçi kör."

    for sema_adi, anahtarlar in sorted(semalar.items()):
        for anahtar in anahtarlar:
            yol = _ANAHTAR_ARTEFAKTI.get(anahtar)
            assert yol is not None, (
                f"{sema_adi}: {anahtar!r} bu bekçinin haritasında YOK — yeni bir "
                "anahtar eklenmiş ve hangi CANLI artefaktı gösterdiği yazılmamış."
            )
            assert (kok / yol).exists(), (
                f"{sema_adi}: {anahtar!r} → {yol} artefaktı YOK (fosil anahtar)."
            )
            assert anahtar in izin_modulleri or (kok / yol).exists(), (
                f"{sema_adi}: {anahtar!r} ne izin modülü ne de canlı bir artefakt."
            )


def test_gerekce_belge_tablosunda_SOZLESME_BAGI_YOKTUR() -> None:
    """`documents` CANLI ama sözleşmeye BAĞLANAMAZ — yer tutucunun GERÇEK engeli.

    Eski gerekçe "belgeler modülü yazılmadı" diyordu; BC dilimi 20. izin modülünü
    açtı. Kalan engel BAĞ eksikliğidir: künyede yalnız `project_id`/`site_id`/
    `folder_id` var. Bu bekçi kolonun eklendiği anda kırmızıya döner ve gerekçeyi
    yeniden okumaya zorlar.

    İddia ŞEMA METADATA'sıdır, veri değil — bu yüzden DB fixture'ı ALMAZ
    (kardeş `test_gerekce_milestone_*` ile aynı desen).
    """
    kolonlar = set(Document.__table__.columns.keys())
    assert "contract_id" not in kolonlar and "subcontractor_contract_id" not in kolonlar, (
        "Belge künyesine sözleşme bağı eklenmiş: `pending_modules` gerekçesi "
        "(contracts/schemas.py) yeniden karara bağlanmalı."
    )
    assert set(DocumentFolder.__table__.columns.keys()) & {"contract_id"} == set()
    # Davranış tarafı: kapsam gerçekten proje/şantiyedir.
    assert "project_id" in kolonlar


def test_gerekce_milestone_SOZLESMEYE_degil_BOLUME_baglidir() -> None:
    """E14 100-120 "Milestone Takvimi" — engel ŞEKİL ve KAPSAM, modül değil.

    `section_milestones` CANLIDIR (`sites`), ama (a) sözleşmeye değil BÖLÜME
    bağlıdır — işveren sözleşmesi proje düzeyindedir ve bir projede N şantiye ×
    N bölüm vardır; (b) mockup AY ARALIĞI ("Nis–Tem 2025") ve ÜÇ durum
    (Tamamlandı/Devam Ediyor/Planlandı) ister, model TEK `milestone_date` taşır
    ve durumu bilinçli olarak TÜREV bırakır (`sites/models.py` sınıf notu).
    """
    kolonlar = set(SectionMilestone.__table__.columns.keys())
    assert "section_id" in kolonlar
    assert "contract_id" not in kolonlar, (
        "Milestone'a sözleşme bağı eklenmiş: `EmployerContractDetail.pending_modules` "
        "gerekçesi yeniden okunmalı."
    )
    assert "start_date" not in kolonlar and "end_date" not in kolonlar, (
        "Milestone'a tarih ARALIĞI eklenmiş: E14'ün 'Nis–Tem 2025' şekli artık "
        "karşılanabilir olabilir, gerekçe yeniden karara bağlanmalı."
    )
    assert "status" not in kolonlar


def test_gerekce_taseron_detayinin_hakedis_ozeti_hala_None_tipindedir() -> None:
    """`progress_payment_summary` BAĞLANMADI — ve sebebi ARTIK "modül yok" değil.

    TH dilimi canlıdır ve `summary.get_summary` ile tam bir özet üretebilir.
    Bağlamayı bekleten şey TSD mockup'ının KENDİ ÇELİŞKİSİDİR: L74 etiketi
    "Ödenen Hakediş ₺2.936.000" der ama sayı üç hakedişin TOPLAMIDIR
    (1.240.000 "Onay Bekliyor" + 960.000 + 736.000) — yani etiket ÖDENEN, sayı
    TÜM STATÜLER. `paid` bağlansaydı 1.696.000 çıkardı ve mockup'ı tutmazdı;
    hepsi bağlansaydı etiket YALAN olurdu. Şeklin kendisi de kırıcıdır (bugün
    sözleşmede `type: "null"`).
    """
    alan = SubcontractorContractDetail.model_fields["progress_payment_summary"]
    assert alan.annotation is type(None), (
        "`progress_payment_summary` bağlanmış: contracts/schemas.py'deki mockup "
        "çelişkisi notu okunup güncellenmeli (ve bu satır silinmeli)."
    )
    assert SubcontractorContractDetail.model_fields["documents"].annotation is type(None)
