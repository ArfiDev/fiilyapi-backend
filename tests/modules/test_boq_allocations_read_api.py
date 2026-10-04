"""BOQ-ALLOC — pozun bolum tahsislerini OKUMA ucu (`GET /boq/items/{id}/allocations`).

NEDEN VAR: `PUT .../allocations` **tam kume degistirmedir** — govdedeki liste
tahsisin TAMAMIDIR ve gonderilmeyen bolum SILINIR. Kismi gorusu olan bir ekran
o PUT'a yazarsa gormedigi bolumlerin paylarini sessizce siler. Kanon: *tam kume
degistirme ucu, kumenin tamamini okuyan bir uc olmadan yazmaya acilamaz.*

🔴 Bu dosyanin en kritik iki testi:
  * `test_N1_YOK_...` — "gozle bakip N+1 yok" kanit degildir; sorgular SAYILIR.
  * `test_yuvarlak_yolculuk_...` — GET'in ciktisi PUT'a aynen verilince kume
    DEGISMEMELIDIR; iki ucun sekli ayrisirsa oku->degistir->yaz dongusu bozulur.
"""

import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import event, func, select
from sqlalchemy.engine import Engine

from app.core.access import AccessLevel, Scope
from app.modules.audit.models import AuditLog
from app.modules.boq.models import BoqGroup, BoqItem, BoqItemSectionAllocation
from app.modules.roles.models import Module, Role, RolePermission
from app.modules.sites.models import Section, Site
from app.modules.users.models import UserProjectAccess
from tests._legacy_permission_yardimcisi import sync_page_cells

# --- Kurulum yardimcilari (test_boq_allocations_api.py deseniyle birebir) ----


async def _set_permission(
    session, role_key: str, module_key: str, level: AccessLevel, scope: Scope = Scope.all
) -> None:
    role_id = (await session.execute(select(Role.id).where(Role.key == role_key))).scalar_one()
    module_id = (
        await session.execute(select(Module.id).where(Module.key == module_key))
    ).scalar_one()
    permission = (
        await session.execute(
            select(RolePermission).where(
                RolePermission.role_id == role_id, RolePermission.module_id == module_id
            )
        )
    ).scalar_one()
    permission.access_level = level
    permission.scope = scope
    await session.flush()
    await sync_page_cells(session, permission.role_id)


async def _login(
    client, session, user_factory, role_key: str, email: str, project=None, all_projects=True
) -> str:
    user = await user_factory(email=email, password="parola1234", role_key=role_key)
    session.add(
        UserProjectAccess(
            user_id=user.id,
            project_id=None if project is None else project.id,
            all_projects=all_projects,
        )
    )
    await session.flush()
    resp = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    return resp.json()["access_token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _site(session, project, code: str = "A-BLOK") -> Site:
    site = Site(project_id=project.id, code=code, name=f"{code} Şantiyesi")
    session.add(site)
    await session.flush()
    return site


async def _section(session, site, name: str, code: str | None = None) -> Section:
    section = Section(site_id=site.id, name=name, code=code)
    session.add(section)
    await session.flush()
    return section


async def _group(session, site, name: str = "TOPRAK VE TEMEL İŞLERİ"):
    group = BoqGroup(site_id=site.id, name=name, sort_order=0)
    session.add(group)
    await session.flush()
    return group


async def _item(session, site, group, code: str = "01.001", **kwargs) -> BoqItem:
    defaults = {
        "description": "Beton Dökümü C30",
        "unit": "m³",
        "quantity": Decimal("1200.000"),
        "unit_price": Decimal("100.00"),
    }
    defaults.update(kwargs)
    item = BoqItem(site_id=site.id, group_id=group.id, code=code, **defaults)
    session.add(item)
    await session.flush()
    return item


async def _allocation(
    session, item, section, quantity: str, created_at: datetime | None = None
) -> BoqItemSectionAllocation:
    """🔴 `created_at` ÖLÇÜLMÜŞ BİR ZORUNLULUKTUR (sıralama testi için).

    Kolonun `server_default`u `func.now()`dur ve PostgreSQL'de bu **transaction
    başlangıç zamanıdır** — tek bir test transaction'ında eklenen TÜM satırlar
    BİREBİR AYNI `created_at`i alır. O zaman `ORDER BY created_at, id` fiilen
    rastgele `uuid4()`e düşer ve sıralama iddiası **yazı tura** olur (ölçüldü:
    aynı test ardışık koşularda iki farklı sıra verdi). Sıra iddia eden test
    zamanları AÇIKÇA verir.
    """
    row = BoqItemSectionAllocation(
        boq_item_id=item.id, section_id=section.id, quantity=Decimal(quantity)
    )
    if created_at is not None:
        row.created_at = created_at
    session.add(row)
    await session.flush()
    return row


def _url(item_id) -> str:
    return f"/boq/items/{item_id}/allocations"


@contextlib.contextmanager
def _sorgu_sayaci():
    """Gercekten SQL sayar — `Engine` sinif duzeyinde dinlenir, motor ornegi degil.

    Async motor senkron `Engine`i sarmaladigi icin `before_cursor_execute` async
    oturumda da tetiklenir. Sayac testin kendisi tarafindan "sifirdan buyuk"
    oldugu iddia edilerek dogrulanir: sayamayan bir sayac N+1'i de goremez.
    """
    ifadeler: list[str] = []

    def _kaydet(conn, cursor, statement, parameters, context, executemany):  # noqa: ANN001
        ifadeler.append(statement)

    event.listen(Engine, "before_cursor_execute", _kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(Engine, "before_cursor_execute", _kaydet)


# --- İddia 1: tahsisli poz --------------------------------------------------


async def test_tahsisli_poz_200_ve_satirlar_created_at_sirasinda(
    client, db_session, user_factory, project_factory
):
    """Satir sayisi + `section_name` + `quantity` dogru, sira `created_at`."""
    project = await project_factory("BOQALLOC-1")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    kat_a = await _section(db_session, site, "Kat 6-10")
    kat_b = await _section(db_session, site, "Kat 11-15")
    # Sıra bilinçli olarak hem ALFABETİK hem EKLEME sırasıyla ÇELİŞİR ve
    # zamanlar AÇIKÇA verilir: `created_at` sıralaması ancak böyle ayrışır.
    once = datetime(2026, 8, 1, 9, 0, tzinfo=UTC)
    await _allocation(db_session, item, kat_b, "300.000", created_at=once)
    await _allocation(db_session, item, kat_a, "400.000", created_at=once + timedelta(hours=1))
    token = await _login(client, db_session, user_factory, "system_admin", "r1@boqalloc.co")

    resp = await client.get(_url(item.id), headers=_auth(token))

    assert resp.status_code == 200
    govde = resp.json()
    assert [s["section_name"] for s in govde["allocations"]] == ["Kat 11-15", "Kat 6-10"]
    assert [s["quantity"] for s in govde["allocations"]] == ["300.000", "400.000"]
    assert [s["section_id"] for s in govde["allocations"]] == [str(kat_b.id), str(kat_a.id)]
    # K4: `item` gövdesi PUT'un cevabıyla BİREBİR aynı şekilde ve
    # `allocated_quantity` tahsis TOPLAMINDAN türer.
    assert govde["item"]["id"] == str(item.id)
    assert govde["item"]["code"] == "01.001"
    assert govde["item"]["quantity"] == "1200.000"
    assert govde["item"]["allocated_quantity"] == "700.000"
    assert govde["item"]["unallocated_quantity"] == "500.000"


async def test_cevap_sekli_PUTun_cevabiyla_BIREBIR_ayni(
    client, db_session, user_factory, project_factory
):
    """K4 — frontend'in oku->degistir->yaz dongusunde IKI sekil ayristirmasi olmamali."""
    project = await project_factory("BOQALLOC-2")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    kat = await _section(db_session, site, "Kat 6-10")
    token = await _login(client, db_session, user_factory, "system_admin", "r2@boqalloc.co")

    put_resp = await client.put(
        _url(item.id),
        json={"allocations": [{"section_id": str(kat.id), "quantity": "250.000"}]},
        headers=_auth(token),
    )
    get_resp = await client.get(_url(item.id), headers=_auth(token))

    assert put_resp.status_code == 200
    assert get_resp.status_code == 200
    assert get_resp.json() == put_resp.json()


# --- İddia 2: tahsissiz poz -------------------------------------------------


async def test_tahsissiz_poz_200_ve_BOS_LISTE_404_degil(
    client, db_session, user_factory, project_factory
):
    """K6 — poz varsa ve görünüyorsa boş küme GEÇERLİ bir cevaptır.

    PUT'un `[]` kabulüyle simetriktir: "tahsis yok" bir hata durumu değildir.
    """
    project = await project_factory("BOQALLOC-3")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    token = await _login(client, db_session, user_factory, "system_admin", "r3@boqalloc.co")

    resp = await client.get(_url(item.id), headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["allocations"] == []
    # Sıfır tahsis PUT'un `[]` cevabıyla AYNI biçimde döner (K4) — biçim değil
    # DEĞER iddia edilir, aksi hâlde iki uç ölçek farkıyla ayrışabilir.
    assert Decimal(resp.json()["item"]["allocated_quantity"]) == Decimal("0")
    assert Decimal(resp.json()["item"]["unallocated_quantity"]) == Decimal("1200.000")


# --- İddia 3: IDOR ----------------------------------------------------------


async def test_baska_projenin_pozu_404_bos_liste_DEGIL(
    client, db_session, user_factory, project_factory
):
    """K2 — gorunmeyen kalem 404; 403 DEĞİL, boş liste DEĞİL.

    Boş liste dönmek en sinsi kaçaktır: uç "200" der, ekran "tahsis yok" gösterir
    ve kullanıcı PUT'a basınca YABANCI projenin paylarını siler.
    """
    gorunen = await project_factory("BOQALLOC-4A")
    gizli = await project_factory("BOQALLOC-4B")
    site = await _site(db_session, gizli)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    kat = await _section(db_session, site, "Kat 6-10")
    await _allocation(db_session, item, kat, "100.000")
    token = await _login(
        client,
        db_session,
        user_factory,
        "project_manager",
        "r4@boqalloc.co",
        project=gorunen,
        all_projects=False,
    )

    gizli_resp = await client.get(_url(item.id), headers=_auth(token))
    hayali_resp = await client.get(_url(uuid.uuid4()), headers=_auth(token))

    assert gizli_resp.status_code == 404
    # 🔴 Var olmayan kimlikle AYIRT EDİLEMEZ olmalı — aksi hâlde uç, başka
    # projenin poz kimliklerinin VARLIĞINI sızdırır.
    assert hayali_resp.status_code == 404
    assert gizli_resp.json() == hayali_resp.json()


# --- İddia 4: kapı `_VIEW` --------------------------------------------------


async def test_view_seviyeli_kullanici_200_alir(client, db_session, user_factory, project_factory):
    """K1 — okuma ucudur, kapisi `_VIEW`. `_FULL` olsaydi burasi 403 verirdi."""
    project = await project_factory("BOQALLOC-5")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    kat = await _section(db_session, site, "Kat 6-10")
    await _allocation(db_session, item, kat, "120.000")
    await _set_permission(db_session, "site_chief", "boq", AccessLevel.view)
    token = await _login(client, db_session, user_factory, "site_chief", "r5@boqalloc.co")

    resp = await client.get(_url(item.id), headers=_auth(token))

    assert resp.status_code == 200
    assert resp.json()["allocations"][0]["section_name"] == "Kat 6-10"


# --- İddia 5: denetim günlüğü ----------------------------------------------


async def test_okuma_denetim_gunlugune_YAZMAZ(client, db_session, user_factory, project_factory):
    """K3 (T7 kurali) — okumalar `record_audit` cagirmaz; `export_boq_endpoint` emsali."""
    project = await project_factory("BOQALLOC-6")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    kat = await _section(db_session, site, "Kat 6-10")
    await _allocation(db_session, item, kat, "50.000")
    token = await _login(client, db_session, user_factory, "system_admin", "r6@boqalloc.co")

    once = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()
    resp = await client.get(_url(item.id), headers=_auth(token))
    sonra = (await db_session.execute(select(func.count()).select_from(AuditLog))).scalar_one()

    assert resp.status_code == 200
    assert sonra == once


# --- İddia 6: N+1 YOK (ÖLÇÜLÜR) --------------------------------------------


async def test_N1_YOK_sorgu_sayisi_bolum_sayisiyla_ARTMAZ(
    client, db_session, user_factory, project_factory
):
    """🔴 K5 — uçtan uca ölçüm: bölüm sayısı 1→3 olurken SQL sayısı SABİT kalmalı.

    Aynı uç iki kez çağrılır ve çalıştırılan SQL ifadeleri SAYILIR; "gözle bakıp
    N+1 yok" kanıt DEĞİLDİR. Sayacın kendisi `> 0` ile doğrulanır — hiçbir şey
    saymayan bir sayaç her zaman "eşit" der.

    🔴 AMA BU TEST TEK BAŞINA YETMEZ, ÖLÇÜLDÜ: `_visible_item` → `_visible_site`
    zinciri `session.get(Site, ...)` yapar ve `Site.sections` `lazy="selectin"`
    olduğu için ŞANTİYENİN TÜM BÖLÜMLERİ kimlik haritasına önden dolar. Bundan
    sonra bölüm başına `session.get(Section, ...)` yazan bir mutasyon **HİÇ SQL
    ATMAZ** ve bu test YEŞİL kalır (mutasyon turunda fiilen oldu). İki katman
    birbirini maskeliyor → alt katmanın KENDİ bekçisi ayrıca yazıldı:
    `test_section_names_by_ids_TEK_sorgu` + `test_YAPISAL_yasak_...`.
    """
    project = await project_factory("BOQALLOC-7")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    token = await _login(client, db_session, user_factory, "system_admin", "r7@boqalloc.co")

    tek = await _item(db_session, site, group, code="01.001")
    await _allocation(db_session, tek, await _section(db_session, site, "Kat 1-5"), "100.000")

    uclu = await _item(db_session, site, group, code="01.002")
    for ad in ("Kat 6-10", "Kat 11-15", "Kat 16-20"):
        await _allocation(db_session, uclu, await _section(db_session, site, ad), "100.000")

    tek_id, uclu_id = tek.id, uclu.id

    # 🔴 İKİ ÖLÇÜLMÜŞ TUZAK — ikisi de bu sayacı KÖR ediyordu:
    # (1) Kurulum nesneleri session'ın KİMLİK HARİTASINDA durduğu sürece
    #     `session.get(Section, ...)` HİÇ SQL ATMAZ; bölüm başına sorgu yazan
    #     bir mutasyon testi YEŞİL geçiyordu.
    # (2) İki çağrı arasında oturum ISINIR: ilk çağrı şantiye/proje zincirini
    #     (ve `selectin` koleksiyonlarını) yükler, ikincisi onları hazır bulur —
    #     ölçülen fark N+1 değil ISINMA olurdu.
    # İkisinin de tek çözümü: HER ölçümden hemen önce oturumu boşaltmak.
    db_session.expunge_all()
    with _sorgu_sayaci() as tek_ifadeler:
        tek_resp = await client.get(_url(tek_id), headers=_auth(token))

    db_session.expunge_all()
    with _sorgu_sayaci() as uclu_ifadeler:
        uclu_resp = await client.get(_url(uclu_id), headers=_auth(token))

    assert tek_resp.status_code == uclu_resp.status_code == 200
    assert len(tek_resp.json()["allocations"]) == 1
    assert len(uclu_resp.json()["allocations"]) == 3
    # Sayaç gerçekten sayıyor mu?
    assert len(tek_ifadeler) > 0
    # 🔴 Asıl iddia: bölüm sayısı 1'den 3'e çıkarken sorgu sayısı SABİT kalır.
    assert len(uclu_ifadeler) == len(tek_ifadeler)


# --- İddia 7: yuvarlak yolculuk (idempotans) --------------------------------


async def test_yuvarlak_yolculuk_GETin_ciktisi_PUTa_verilince_kume_DEGISMEZ(
    client, db_session, user_factory, project_factory
):
    """GET → (section_id, quantity) → PUT → GET. Küme aynen kalmalı.

    Bu, `oku → değiştir → yaz` döngüsünün gerçek testidir: GET eksik bir alan
    döndürseydi (ör. bir bölümü atlasaydı) PUT o payı SİLERDİ ve ikinci GET
    farklı çıkardı.
    """
    project = await project_factory("BOQALLOC-8")
    site = await _site(db_session, project)
    group = await _group(db_session, site)
    item = await _item(db_session, site, group)
    for ad, miktar in (("Kat 6-10", "400.000"), ("Kat 11-15", "300.000"), ("Kat 16-20", "125.500")):
        await _allocation(db_session, item, await _section(db_session, site, ad), miktar)
    token = await _login(client, db_session, user_factory, "system_admin", "r8@boqalloc.co")

    ilk = await client.get(_url(item.id), headers=_auth(token))
    assert ilk.status_code == 200

    put_resp = await client.put(
        _url(item.id),
        json={
            "allocations": [
                {"section_id": s["section_id"], "quantity": s["quantity"]}
                for s in ilk.json()["allocations"]
            ]
        },
        headers=_auth(token),
    )
    ikinci = await client.get(_url(item.id), headers=_auth(token))

    assert put_resp.status_code == 200
    assert ikinci.status_code == 200
    assert ikinci.json() == ilk.json()


# --- İddia 6b: ALT KATMANIN KENDİ BEKÇİSİ (iki katman kanonu) --------------


async def test_section_names_by_ids_TEK_sorgu_bolum_basina_DEGIL(db_session, project_factory):
    """🔴 Uçtan uca sayaç, `Site.sections` selectin ön-yüklemesi yüzünden bu

    sınıfı GÖREMEZ (bkz. yukarıdaki not). Alt katman bu yüzden DOĞRUDAN ölçülür:
    üç bölüm adı **tek** ifadeyle çözülmeli. Sayacın ayırt etme gücü, aynı testte
    naif alternatifin (bölüm başına `get_section`) bölüm sayısıyla BÜYÜYEN sayıda
    ifade ürettiği gösterilerek kanıtlanır — ölçemeyen bir sayaç "1" de derdi.
    """
    from app.modules.sites import repository as sites_repo

    project = await project_factory("BOQALLOC-9")
    site = await _site(db_session, project)
    bolumler = [await _section(db_session, site, ad) for ad in ("Kat 1-5", "Kat 6-10", "Kat 11-15")]
    kimlikler = [b.id for b in bolumler]

    db_session.expunge_all()
    with _sorgu_sayaci() as toplu:
        adlar = await sites_repo.section_names_by_ids(db_session, kimlikler)

    db_session.expunge_all()
    with _sorgu_sayaci() as naif:
        for kimlik in kimlikler:
            await sites_repo.get_section(db_session, kimlik)

    assert set(adlar.values()) == {"Kat 1-5", "Kat 6-10", "Kat 11-15"}
    assert len(toplu) == 1
    # Sayaç gerçekten ayırt ediyor mu: naif yol bölüm BAŞINA en az bir ifade
    # atar (ölçüm: 3 bölüm → 6 ifade; `Section` + `selectin` kilometre taşları).
    assert len(naif) >= len(kimlikler)
    assert len(naif) > len(toplu)


async def test_bos_girdide_HIC_sorgu_atilmaz(db_session):
    """Tahsissiz poz yolunda gereksiz gidiş-dönüş olmaz (K6'nın ölçülmüş kardeşi)."""
    from app.modules.sites import repository as sites_repo

    with _sorgu_sayaci() as ifadeler:
        assert await sites_repo.section_names_by_ids(db_session, []) == {}

    assert ifadeler == []


def test_YAPISAL_yasak_get_allocations_bolum_basina_cozum_YAPMAZ():
    """🔴 İkinci bağımsız katman (F-FAT2 kanonu: değer testi + YAPISAL yasak).

    Uçtan uca sayaç bu sınıfa kör olduğu için, servisin kaynağı DOĞRUDAN
    denetlenir: adlar toplu çözücüden gelmeli, tekil `get_section` gövdede
    GEÇMEMELİDİR. Bu bekçi bir gün toplu çözüm sessizce döngüye çevrilirse
    kırmızı olur — SQL sayacı olmayacaktır.
    """
    import inspect

    from app.modules.boq import service as boq_service

    kaynak = inspect.getsource(boq_service.get_allocations)
    govde = kaynak.split('"""')[-1]

    assert "section_names_by_ids" in govde
    assert "get_section(" not in govde
