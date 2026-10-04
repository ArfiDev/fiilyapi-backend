"""Task C12 — DELETE uçları + silme korkulukları (spec §7).

Kapsam: `DELETE /subcontractors/{id}`, `DELETE /subcontractor-contracts/{id}`,
`DELETE /subcontractor-contracts/items/{id}`,
`DELETE /contracts/employer/groups/{id}`, `DELETE /contracts/employer/items/{id}`
ve `sites` silme yolundaki dördüncü korkuluk (`SITE_HAS_CONTRACTS`).

Ayrıca (C11 incelemesinden devredilen ek iş): `POST
/subcontractor-contracts/{id}/items` gövdesindeki `source_contract_item_id`
doğrulaması — başka projenin işveren kalemine bağlanamaz.
"""

import uuid
from decimal import Decimal

import pytest

from app.modules.boq.models import BoqGroup, BoqItem
from app.modules.contracts.models import (
    EmployerContractGroup,
    EmployerContractItem,
    Subcontractor,
    SubcontractorContract,
)
from app.modules.projects.models import ProjectContract
from app.modules.sites.models import Site

GRUP_ADI = "A — Betonarme İşleri"


async def _employer_contract(session, project_id, **kwargs) -> ProjectContract:
    defaults = {
        "contract_no": "SZL-2026-DEL",
        "amount": Decimal("11200000"),
        "advance_pct": Decimal("20"),
    }
    defaults.update(kwargs)
    contract = ProjectContract(project_id=project_id, **defaults)
    session.add(contract)
    await session.flush()
    return contract


async def _group(session, project_id, **kwargs) -> EmployerContractGroup:
    defaults = {"name": GRUP_ADI, "sort_order": 0}
    defaults.update(kwargs)
    group = EmployerContractGroup(project_id=project_id, **defaults)
    session.add(group)
    await session.flush()
    return group


async def _item(session, project_id, group_id, **kwargs) -> EmployerContractItem:
    defaults = {
        "code": "04.001",
        "description": "Demir donatı",
        "unit": "Ton",
        "quantity": Decimal("200"),
        "unit_price": Decimal("21500"),
    }
    defaults.update(kwargs)
    item = EmployerContractItem(project_id=project_id, group_id=group_id, **defaults)
    session.add(item)
    await session.flush()
    return item


async def _site(session, project_id, code, name) -> Site:
    site = Site(project_id=project_id, code=code, name=name)
    session.add(site)
    await session.flush()
    return site


# --- Fixture'lar ---


@pytest.fixture
async def dagitimli_proje(seeded_db, project_factory) -> uuid.UUID:
    """Sözleşme + poz grubu + kalem + kaleme bağlı bir BOQ satırı (spec §6.3).

    `test_distribution_write.py._kurulum`'un aynısı, dosyalar arası paylaşılan
    fixture yok (her modül kendi test dosyasında kurar).
    """
    project = await project_factory(code="CL-DEL-01", name="Silme Testi Projesi")
    await _employer_contract(seeded_db, project.id)
    site = await _site(seeded_db, project.id, "SNT-DEL-01", "Ana Şantiye")
    group = await _group(seeded_db, project.id)
    item = await _item(seeded_db, project.id, group.id)

    boq_group = BoqGroup(site_id=site.id, name=GRUP_ADI)
    seeded_db.add(boq_group)
    await seeded_db.flush()
    seeded_db.add(
        BoqItem(
            site_id=site.id,
            group_id=boq_group.id,
            contract_item_id=item.id,
            code="04.001",
            description="Demir donatı",
            unit="Ton",
            quantity=Decimal("120"),
            unit_price=Decimal("21500"),
        )
    )
    await seeded_db.flush()
    return {"project_id": project.id, "site_id": site.id, "item_id": item.id}


@pytest.fixture
async def santiye_boq(dagitimli_proje) -> uuid.UUID:
    return dagitimli_proje["site_id"]


@pytest.fixture
async def sozlesme_kalemi(dagitimli_proje) -> uuid.UUID:
    return dagitimli_proje["item_id"]


@pytest.fixture
async def dagitimli_proje_id(dagitimli_proje) -> uuid.UUID:
    return dagitimli_proje["project_id"]


@pytest.fixture
async def grup_dolu(seeded_db, project_factory) -> uuid.UUID:
    """Sözleşme poz grubu, içinde bir kalem var — silme 409 dönmeli."""
    project = await project_factory(code="CL-DEL-02", name="Dolu Grup Projesi")
    await _employer_contract(seeded_db, project.id, contract_no="SZL-2026-DEL2")
    group = await _group(seeded_db, project.id)
    await _item(seeded_db, project.id, group.id)
    return group.id


@pytest.fixture
async def bos_grup(seeded_db, project_factory) -> uuid.UUID:
    """Kalemsiz grup — silme serbest olmalı (409 tetiklenmemeli)."""
    project = await project_factory(code="CL-DEL-03", name="Boş Grup Projesi")
    await _employer_contract(seeded_db, project.id, contract_no="SZL-2026-DEL3")
    group = await _group(seeded_db, project.id, name="B — Kalemsiz Grup")
    return group.id


@pytest.fixture
async def taseron_projesi(seeded_db, project_factory) -> uuid.UUID:
    project = await project_factory(code="CL-DEL-04", name="Taşeron Sözleşmesi Projesi")
    return project.id


@pytest.fixture
async def taseron(seeded_db) -> uuid.UUID:
    subcontractor = Subcontractor(name="Akın İnşaat Ltd. Şti.", category="Betonarme")
    seeded_db.add(subcontractor)
    await seeded_db.flush()
    return subcontractor.id


@pytest.fixture
async def bagsiz_taseron(seeded_db) -> uuid.UUID:
    """Hiçbir sözleşmeye bağlı olmayan taşeron — silme serbest olmalı."""
    subcontractor = Subcontractor(name="Serbest Taşeron A.Ş.", category="Sıva")
    seeded_db.add(subcontractor)
    await seeded_db.flush()
    return subcontractor.id


@pytest.fixture
async def diger_kullanici(user_factory):
    """`created_by` FK RESTRICT'tir (`users.id`) — rastgele bir UUID FK ihlali

    üretir, gerçek bir kullanıcı gerekir. Taslak sahibi OLMAYAN senaryolar
    (yayındaki sözleşme, başkasının taslağı) bu kullanıcıyı sahibi yapar.
    """
    return await user_factory(
        email="diger@contracts-delete.co", password="parola1234", role_key="system_admin"
    )


@pytest.fixture
async def taseron_sozlesmesi(seeded_db, taseron_projesi, taseron, diger_kullanici) -> uuid.UUID:
    """Yayında (taslak olmayan), sistem yöneticisi tarafından oluşturulmuş bir

    taşeron sözleşmesi — proje müdürü SİLEMEMELİ (taslak istisnası burada
    geçerli DEĞİL).
    """
    contract = SubcontractorContract(
        project_id=taseron_projesi,
        subcontractor_id=taseron,
        subcontractor_name="Akın İnşaat Ltd. Şti.",
        work_category="Betonarme",
        contract_no="TSZ-2026-DEL",
        is_draft=False,
        created_by=diger_kullanici.id,
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return contract.id


@pytest.fixture
async def kendi_taslagi(seeded_db, taseron_projesi, project_manager) -> uuid.UUID:
    """Proje müdürünün KENDİ açtığı, hâlâ taslak bir sözleşme — `can_delete`

    taslak istisnası burada geçerli olmalı (spec §5.0).
    """
    contract = SubcontractorContract(
        project_id=taseron_projesi,
        is_draft=True,
        created_by=project_manager.id,
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return contract.id


@pytest.fixture
async def baskasinin_taslagi(seeded_db, taseron_projesi, diger_kullanici) -> uuid.UUID:
    """Başka bir kullanıcının taslağı — taslak istisnası sahibi OLMAYANA

    uygulanmaz.
    """
    contract = SubcontractorContract(
        project_id=taseron_projesi,
        is_draft=True,
        created_by=diger_kullanici.id,
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return contract.id


@pytest.fixture
async def sozlesme_kalemi_taseron(seeded_db, taseron_sozlesmesi) -> uuid.UUID:
    from app.modules.contracts.models import SubcontractorContractItem

    item = SubcontractorContractItem(
        contract_id=taseron_sozlesmesi,
        code="03.001",
        description="Beton",
        unit="m³",
        quantity=Decimal("100"),
        unit_price=Decimal("1200"),
    )
    seeded_db.add(item)
    await seeded_db.flush()
    return item.id


@pytest.fixture
async def santiye_sozlesmesi(
    seeded_db, project_factory, diger_kullanici
) -> tuple[uuid.UUID, uuid.UUID]:
    """Bir şantiye + o şantiyeye bağlı bir taşeron sözleşmesi."""
    project = await project_factory(code="CL-DEL-05", name="Şantiye Sözleşmesi Projesi")
    site = await _site(seeded_db, project.id, "SNT-DEL-05", "Sözleşmeli Şantiye")
    contract = SubcontractorContract(
        project_id=project.id,
        site_id=site.id,
        is_draft=True,
        created_by=diger_kullanici.id,
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return project.id, site.id


@pytest.fixture
async def santiye(santiye_sozlesmesi) -> uuid.UUID:
    return santiye_sozlesmesi[1]


# --- DELETE /subcontractor-contracts/{id} — kapı: yalnız Sistem Yöneticisi (SIL-B1) ---


@pytest.mark.asyncio
async def test_proje_muduru_silemez(client, project_manager_headers, taseron_sozlesmesi):
    """Kalıcı karar 2: full silmeyi KAPSAMAZ. Bu BEKLENEN davranış."""
    yanit = await client.delete(
        f"/subcontractor-contracts/{taseron_sozlesmesi}", headers=project_manager_headers
    )
    assert yanit.status_code == 403


@pytest.mark.asyncio
async def test_kendi_taslagini_silemez_istisna_yok(client, project_manager_headers, kendi_taslagi):
    """SIL-B1 (K4): eski `can_delete` taslak istisnası KALDIRILDI — kendi taslağı da 403."""
    yanit = await client.delete(
        f"/subcontractor-contracts/{kendi_taslagi}", headers=project_manager_headers
    )
    assert yanit.status_code == 403


@pytest.mark.asyncio
async def test_baskasinin_taslagini_silemez(client, project_manager_headers, baskasinin_taslagi):
    """Taslak istisnası yok (SIL-B1): başkasının taslağı da 403."""
    yanit = await client.delete(
        f"/subcontractor-contracts/{baskasinin_taslagi}", headers=project_manager_headers
    )
    assert yanit.status_code == 403


@pytest.mark.asyncio
async def test_admin_yayindaki_sozlesmeyi_siler(client, admin_headers, taseron_sozlesmesi):
    yanit = await client.delete(
        f"/subcontractor-contracts/{taseron_sozlesmesi}", headers=admin_headers
    )
    assert yanit.status_code == 204


@pytest.mark.asyncio
async def test_gorunmeyen_sozlesme_ayni_403(client, kisitli_headers, taseron_sozlesmesi):
    """SIL-B1: kapı (Sistem Yöneticisi) handler'dan ÖNCE koşar; kapsam dışı sözleşme ile var
    olmayan kimlik AYNI 403 gövdesini alır (varlık sızmaz, IDOR)."""
    gercek = await client.delete(
        f"/subcontractor-contracts/{taseron_sozlesmesi}", headers=kisitli_headers
    )
    yok = await client.delete(f"/subcontractor-contracts/{uuid.uuid4()}", headers=kisitli_headers)
    assert gercek.status_code == 403
    assert yok.status_code == 403
    assert gercek.json()["detail"] == yok.json()["detail"]


# --- DELETE /subcontractor-contracts/items/{item_id} ---


@pytest.mark.asyncio
async def test_taseron_kalemi_admin_siler(client, admin_headers, sozlesme_kalemi_taseron):
    yanit = await client.delete(
        f"/subcontractor-contracts/items/{sozlesme_kalemi_taseron}", headers=admin_headers
    )
    assert yanit.status_code == 204


@pytest.mark.asyncio
async def test_taseron_kalemi_proje_muduru_silemez(
    client, project_manager_headers, sozlesme_kalemi_taseron
):
    yanit = await client.delete(
        f"/subcontractor-contracts/items/{sozlesme_kalemi_taseron}",
        headers=project_manager_headers,
    )
    assert yanit.status_code == 403


# --- DELETE /subcontractors/{id} ---


@pytest.mark.asyncio
async def test_sozlesmesi_olan_taseron_silinemez(
    client, admin_headers, taseron, taseron_sozlesmesi
):
    yanit = await client.delete(f"/subcontractors/{taseron}", headers=admin_headers)
    assert yanit.status_code == 409
    assert "önce sözleşmeleri silin" in yanit.json()["detail"]


@pytest.mark.asyncio
async def test_bagsiz_taseron_silinir(client, admin_headers, bagsiz_taseron):
    yanit = await client.delete(f"/subcontractors/{bagsiz_taseron}", headers=admin_headers)
    assert yanit.status_code == 204


@pytest.mark.asyncio
async def test_taseron_proje_muduru_silemez(client, project_manager_headers, bagsiz_taseron):
    yanit = await client.delete(
        f"/subcontractors/{bagsiz_taseron}", headers=project_manager_headers
    )
    assert yanit.status_code == 403


# --- DELETE /contracts/employer/groups/{id} ---


@pytest.mark.asyncio
async def test_dolu_grup_silinemez(client, admin_headers, grup_dolu):
    yanit = await client.delete(f"/contracts/employer/groups/{grup_dolu}", headers=admin_headers)
    assert yanit.status_code == 409
    assert "önce pozları silin" in yanit.json()["detail"]


@pytest.mark.asyncio
async def test_bos_grup_silinir(client, admin_headers, bos_grup):
    yanit = await client.delete(f"/contracts/employer/groups/{bos_grup}", headers=admin_headers)
    assert yanit.status_code == 204


# --- DELETE /contracts/employer/items/{id} — engel yok, BOQ SET NULL ---


@pytest.mark.asyncio
async def test_sozlesme_kalemi_silinince_boq_satiri_kalir(
    client, admin_headers, seeded_db, dagitimli_proje_id, santiye_boq, sozlesme_kalemi
):
    """`BoqItemResponse` `contract_item_id` TAŞIMAZ (spec dışı alan) — bağ

    koptuğu DB'den doğrulanır, GET yanıtından DEĞİL. Yanıt seviyesinde
    doğrulanabilir olan tek şey satırın hâlâ VAR OLMASI (silinmediği).
    """
    from sqlalchemy import select as sa_select

    from app.modules.boq.models import BoqItem

    silme = await client.delete(
        f"/contracts/employer/items/{sozlesme_kalemi}", headers=admin_headers
    )
    assert silme.status_code == 204
    boq = (await client.get(f"/sites/{santiye_boq}/boq", headers=admin_headers)).json()
    assert len(boq["groups"][0]["items"]) == 1

    boq_item = (
        await seeded_db.execute(sa_select(BoqItem).where(BoqItem.site_id == santiye_boq))
    ).scalar_one()
    assert boq_item.contract_item_id is None


# --- sites: dördüncü silme korkuluğu (SITE_HAS_CONTRACTS) ---


@pytest.mark.asyncio
async def test_sozlesmeli_santiye_silinir_sozlesme_birlikte_gider(
    client, admin_headers, santiye_sozlesmesi, seeded_db
):
    """SIL-B1: eski SITE_HAS_CONTRACTS 409 korkuluğu Sistem Yöneticisi için BYPASS edilir;
    taşeron sözleşmesi (`site_id` RESTRICT) önce, şantiye sonra silinir."""
    from sqlalchemy import func
    from sqlalchemy import select as sa_select

    from tests._silme_yardimci import sil_aile

    _, site_id = santiye_sozlesmesi
    yanit = await sil_aile(client, admin_headers, "site", site_id)
    assert yanit.status_code == 204
    kalan = await seeded_db.execute(
        sa_select(func.count())
        .select_from(SubcontractorContract)
        .where(SubcontractorContract.site_id == site_id)
    )
    assert kalan.scalar_one() == 0


# --- Ek iş: source_contract_item_id IDOR/doğrulama (C11 incelemesinden) ---


@pytest.fixture
async def baska_projenin_kalemi(seeded_db, project_factory) -> uuid.UUID:
    """Başka bir projenin işveren sözleşmesi kalemi — taşeron kalemi buna

    bağlanamamalı (bilgi sızıntısı: yanıtta o projenin grup adı sızabilir).
    """
    project = await project_factory(code="CL-DEL-06", name="Gizli Proje")
    await _employer_contract(seeded_db, project.id, contract_no="SZL-2026-DEL6")
    group = await _group(seeded_db, project.id, name="Gizli Grup")
    item = await _item(seeded_db, project.id, group.id, code="99.001")
    return item.id


@pytest.mark.asyncio
async def test_source_item_baska_projeden_baglanamaz(
    client, admin_headers, taseron_sozlesmesi, baska_projenin_kalemi
):
    yanit = await client.post(
        f"/subcontractor-contracts/{taseron_sozlesmesi}/items",
        json={
            "code": "77.001",
            "description": "Sızıntı denemesi",
            "unit": "m³",
            "quantity": 10,
            "source_contract_item_id": str(baska_projenin_kalemi),
        },
        headers=admin_headers,
    )
    assert yanit.status_code == 404


@pytest.fixture
async def ayni_projenin_taseron_sozlesmesi(
    seeded_db, dagitimli_proje_id, diger_kullanici
) -> uuid.UUID:
    """`dagitimli_proje_id` ile AYNI projeye bağlı taşeron sözleşmesi —

    `sozlesme_kalemi` de bu projeye ait, dolayısıyla bağlama BAŞARILI olmalı
    (doğru-yol davranışı, IDOR testinin tersi).
    """
    contract = SubcontractorContract(
        project_id=dagitimli_proje_id,
        is_draft=True,
        created_by=diger_kullanici.id,
    )
    seeded_db.add(contract)
    await seeded_db.flush()
    return contract.id


@pytest.mark.asyncio
async def test_source_item_ayni_projeden_baglanir(
    client, admin_headers, ayni_projenin_taseron_sozlesmesi, sozlesme_kalemi
):
    yanit = await client.post(
        f"/subcontractor-contracts/{ayni_projenin_taseron_sozlesmesi}/items",
        json={
            "code": "77.002",
            "description": "Doğru yol denemesi",
            "unit": "Ton",
            "quantity": 10,
            "source_contract_item_id": str(sozlesme_kalemi),
        },
        headers=admin_headers,
    )
    assert yanit.status_code == 201, yanit.text
    assert yanit.json()["source_contract_item_id"] == str(sozlesme_kalemi)
