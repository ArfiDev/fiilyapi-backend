"""P11 T3 — `GET /projects/timeline` (portföy Gantt verisi).

Bu dosyanın sabitlediği kararlar (P11 spec §3/§4/§5, kullanıcı onayı 2026-08-11):

1. **İlerleme yüzdesi YOKTUR (S1).** `progress_pct` ne proje ne bölüm satırında
   bulunur — `None`/pending zarfıyla bile dönülmez. Bar rengi `status`tan türer.
   `Project.progress_pct` kolonu F6 mirası olarak DB'de durduğu için bu ancak
   yanıt gövdesinde ARANARAK test edilebilir; kolonun varlığı testi geçirmez.
2. **HAM veri (S4).** Uç hiçbir sorgu parametresi almaz: ay ızgarası, zoom kipi
   ve bar genişliği istemci işidir. `today` ise SUNUCU damgasıdır
   (`core.timezone`) — istemcinin saatine bırakılırsa TR gecesi 00:00-03:00
   arasında bugün çizgisi yanlış güne düşer.
3. **IDOR.** Görünmeyen proje yanıtta yoktur; onun bölüm/milestone'ları da yoktur.
   Portföy uçları klasik sızıntı yüzeyidir: proje süzülüp alt satırlar toplu
   sorguyla çekilirse süzgeç SQL'de uygulanmadığında bölümler sızar.
4. **Deterministik sıra.** Aynı veri iki istekte aynı sırayla döner; sıra
   (şantiye kodu, `sort_order`, `id`) üçlüsüdür — `sort_order` eşitliğinde de
   kırıcı vardır.
5. **N+1 YOK.** Sorgu sayısı proje/şantiye/bölüm/milestone sayısından BAĞIMSIZ:
   iki farklı veri hacminde AYNI sayı ölçülür (TB3 sayaç emsali).
"""

import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date

from sqlalchemy import delete, event

from app.core import timezone
from app.core.access import AccessLevel
from app.modules.roles.models import RoleHiddenField
from app.modules.sites.models import Section, SectionMilestone, SectionStatus, Site
from app.modules.users.models import ProjectMember
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz
from tests.conftest import test_engine

VIEW_ROLE = "site_chief"  # projects=view (seed); testte açıkça kurulur


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def _login(client, user_factory, role_key: str, *, email: str | None = None) -> str:
    address = email or f"{role_key}-{uuid.uuid4().hex[:6]}@t.co"
    user = await user_factory(email=address, password="parola1234", role_key=role_key)
    resp = await client.post("/auth/login", json={"email": address, "password": "parola1234"})
    return user, resp.json()["access_token"]


async def _grant(
    session, user, *, project_id: uuid.UUID | None, all_projects: bool = False
) -> None:
    if all_projects:
        user.all_projects = True
    elif project_id is not None:
        session.add(ProjectMember(user_id=user.id, project_id=project_id, role_id=user.role_id))
    await session.flush()


async def _site(session, project_id: uuid.UUID, code: str, name: str = "Şantiye") -> Site:
    site = Site(project_id=project_id, code=code, name=name)
    session.add(site)
    await session.flush()
    return site


async def _section(
    session,
    site_id: uuid.UUID,
    name: str,
    *,
    sort_order: int = 0,
    status: SectionStatus = SectionStatus.planned,
    start: date | None = None,
    end: date | None = None,
    depends_on: uuid.UUID | None = None,
) -> Section:
    section = Section(
        site_id=site_id,
        name=name,
        sort_order=sort_order,
        status=status,
        start_date=start,
        end_date=end,
        depends_on_section_id=depends_on,
    )
    session.add(section)
    await session.flush()
    return section


async def _milestone(
    session, section_id: uuid.UUID, title: str, day: date, *, sort_order: int = 0
) -> SectionMilestone:
    row = SectionMilestone(
        section_id=section_id, title=title, milestone_date=day, sort_order=sort_order
    )
    session.add(row)
    await session.flush()
    return row


@contextmanager
def _sorgu_sayaci() -> Iterator[list[str]]:
    """İstek boyunca sürücüye giden HER ifadeyi toplar (TB3/H4 emsali):
    sorgu sayısı iddiaları tahmine değil ÖLÇÜME dayanır."""
    ifadeler: list[str] = []

    def kaydet(conn, cursor, statement, parameters, context, executemany) -> None:  # noqa: ANN001
        ifadeler.append(" ".join(statement.split()))

    event.listen(test_engine.sync_engine, "before_cursor_execute", kaydet)
    try:
        yield ifadeler
    finally:
        event.remove(test_engine.sync_engine, "before_cursor_execute", kaydet)


_TIMELINE_TABLOLARI = re.compile(r"\b(projects|sites|sections|section_milestones)\b")


def _timeline_sorgulari(ifadeler: list[str]) -> list[str]:
    """Yalnız timeline'ın okuduğu tablolara giden ifadeler. Kimlik doğrulama
    (users/roles) sorguları sayıma girmez: onlar bu ölçümden bağımsız ve
    veri hacminden etkilenmez."""
    return [i for i in ifadeler if _TIMELINE_TABLOLARI.search(i)]


# --- Yetki + rota ---


async def test_timeline_unauthenticated(client):
    resp = await client.get("/projects/timeline")
    assert resp.status_code == 401


async def test_timeline_forbidden_without_projects_view(client, user_factory, seeded_db):
    """`projects` izni `none` olan rol 403 alır (seed: procurement = none)."""
    await modul_duzeyi_yaz(seeded_db, "procurement", "projects", AccessLevel.none)
    _, token = await _login(client, user_factory, "procurement")
    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 403


async def test_timeline_rotasi_project_id_ile_golgelenmez(
    client, user_factory, seeded_db, project_factory
):
    """ROTA TUZAĞI: `/projects/{project_id}` daha ÖNCE tanımlanırsa `timeline`
    bir UUID sanılır ve uç 422 ile hiç çalışmaz. Bu test o sıralamayı sabitler."""
    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=None, all_projects=True)

    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert "today" in resp.json()


# --- Boş portföy ---


async def test_timeline_bos_portfoy(client, user_factory, seeded_db):
    """Görünür projesi olmayan kullanıcı boş liste + `today` alır (patlama yok)."""
    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    _, token = await _login(client, user_factory, VIEW_ROLE)

    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["today"] == timezone.today().isoformat()


# --- Gövde ---


async def test_timeline_govdesi(client, user_factory, seeded_db, project_factory):
    project = await project_factory(
        "GK-1",
        name="Güneşkent Konut",
        status="active",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 12, 31),
        contract_amount="22400000.00",
    )
    site = await _site(seeded_db, project.id, "SNT-1")
    temel = await _section(
        seeded_db,
        site.id,
        "Temel & Bodrum",
        sort_order=1,
        status=SectionStatus.completed,
        start=date(2025, 1, 1),
        end=date(2025, 7, 31),
    )
    await _section(
        seeded_db,
        site.id,
        "Kat 1-5 Kaba",
        sort_order=2,
        status=SectionStatus.active,
        depends_on=temel.id,
    )
    await _milestone(seeded_db, temel.id, "Temel tamamlandı", date(2025, 7, 31))

    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=project.id)
    # Sözleşme bedeli `sozlesme_fiyat` kategorisidir; `limited` seed rolü tum_tutarlar gizler.
    await seeded_db.execute(delete(RoleHiddenField).where(RoleHiddenField.role_id == user.role_id))
    await seeded_db.flush()

    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["today"] == timezone.today().isoformat()
    assert len(body["items"]) == 1

    item = body["items"][0]
    assert item["name"] == "Güneşkent Konut"
    assert item["start_date"] == "2025-01-01"
    assert item["end_date"] == "2026-12-31"
    assert item["contract_amount"] == "22400000.00"
    assert item["status"] == "active"

    sections = item["sections"]
    assert [s["name"] for s in sections] == ["Temel & Bodrum", "Kat 1-5 Kaba"]
    assert sections[0]["status"] == "completed"
    assert sections[0]["sort_order"] == 1
    assert sections[0]["start_date"] == "2025-01-01"
    assert sections[0]["end_date"] == "2025-07-31"
    assert sections[0]["depends_on_section_id"] is None
    assert sections[1]["depends_on_section_id"] == str(temel.id)
    assert sections[0]["milestones"] == [
        {
            "id": sections[0]["milestones"][0]["id"],
            "title": "Temel tamamlandı",
            "milestone_date": "2025-07-31",
        }
    ]
    assert sections[1]["milestones"] == []


async def test_timeline_ilerleme_yuzdesi_HIC_YOK(client, user_factory, seeded_db, project_factory):
    """S1 kalıcı kararı: `progress_pct` alanı hiç var olmaz — `null` zarfıyla da
    dönülmez. `Project.progress_pct` DB'de durduğu için bu ancak gövdede
    ARANARAK doğrulanabilir."""
    project = await project_factory("GK-2", name="Yüzde Yok", progress_pct="75.00")
    site = await _site(seeded_db, project.id, "SNT-2")
    await _section(seeded_db, site.id, "Faz", sort_order=1)

    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=project.id)

    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    assert "progress" not in resp.text
    body = resp.json()
    assert "progress_pct" not in body["items"][0]
    assert "progress_pct" not in body["items"][0]["sections"][0]


async def test_timeline_bolumsuz_proje_bos_liste_doner(
    client, user_factory, seeded_db, project_factory
):
    """Bölümü/şantiyesi olmayan proje satırı DÜŞMEZ — boş alt listeyle döner."""
    bos = await project_factory("BOS-1", name="Bölümsüz Proje")
    santiyeli = await project_factory("SNT-ONLY", name="Bölümsüz Şantiye")
    await _site(seeded_db, santiyeli.id, "SNT-3")

    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=bos.id)
    await _grant(seeded_db, user, project_id=santiyeli.id)

    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert {i["name"] for i in items} == {"Bölümsüz Proje", "Bölümsüz Şantiye"}
    assert all(i["sections"] == [] for i in items)


# --- IDOR ---


async def test_timeline_gorunmeyen_proje_yanitta_yok(
    client, user_factory, seeded_db, project_factory
):
    """Görünmeyen projenin ne satırı ne de BÖLÜM/MILESTONE'ları yanıta girer."""
    gorunur = await project_factory("VIS-1", name="Görünür Proje")
    gizli = await project_factory("HID-1", name="Gizli Proje")
    gorunur_site = await _site(seeded_db, gorunur.id, "SNT-V")
    gizli_site = await _site(seeded_db, gizli.id, "SNT-H")
    await _section(seeded_db, gorunur_site.id, "Görünür Bölüm", sort_order=1)
    gizli_bolum = await _section(seeded_db, gizli_site.id, "Gizli Bölüm", sort_order=1)
    await _milestone(seeded_db, gizli_bolum.id, "Gizli Milestone", date(2026, 5, 5))

    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=gorunur.id)

    resp = await client.get("/projects/timeline", headers=_auth(token))
    assert resp.status_code == 200, resp.text
    metin = resp.text
    assert "Gizli Proje" not in metin
    assert "Gizli Bölüm" not in metin
    assert "Gizli Milestone" not in metin
    assert str(gizli.id) not in metin
    assert str(gizli_bolum.id) not in metin
    assert [i["name"] for i in resp.json()["items"]] == ["Görünür Proje"]


# --- Deterministik sıra ---


async def test_timeline_deterministik_sira(client, user_factory, seeded_db, project_factory):
    """Proje sırası `code`; bölüm sırası (şantiye kodu, sort_order, id); milestone
    sırası (sort_order, id). `sort_order` eşitliğinde de kırıcı VARDIR — yoksa
    aynı veri iki istekte farklı sırayla dönebilirdi."""
    await project_factory("B-KOD", name="B Projesi")
    a_proje = await project_factory("A-KOD", name="A Projesi")

    ikinci_site = await _site(seeded_db, a_proje.id, "SNT-B")
    ilk_site = await _site(seeded_db, a_proje.id, "SNT-A")
    await _section(seeded_db, ikinci_site.id, "B-Şantiye Bölümü", sort_order=1)
    esit_1 = await _section(seeded_db, ilk_site.id, "Eşit-1", sort_order=5)
    esit_2 = await _section(seeded_db, ilk_site.id, "Eşit-2", sort_order=5)
    await _section(seeded_db, ilk_site.id, "Önce", sort_order=1)

    m_b = await _milestone(seeded_db, esit_1.id, "M-B", date(2026, 1, 2), sort_order=2)
    m_a = await _milestone(seeded_db, esit_1.id, "M-A", date(2026, 1, 1), sort_order=1)

    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=None, all_projects=True)

    ilk = (await client.get("/projects/timeline", headers=_auth(token))).json()
    ikinci = (await client.get("/projects/timeline", headers=_auth(token))).json()
    assert ilk == ikinci, "aynı veri iki istekte aynı sırayla dönmeli"

    assert [i["name"] for i in ilk["items"]] == ["A Projesi", "B Projesi"]
    a_bolumleri = [s["name"] for s in ilk["items"][0]["sections"]]
    beklenen_esit = sorted([(esit_1.id, "Eşit-1"), (esit_2.id, "Eşit-2")])
    assert a_bolumleri == ["Önce", *[ad for _, ad in beklenen_esit], "B-Şantiye Bölümü"]
    milestoneli = next(s for s in ilk["items"][0]["sections"] if s["name"] == "Eşit-1")
    assert [m["title"] for m in milestoneli["milestones"]] == ["M-A", "M-B"]
    assert m_a.sort_order < m_b.sort_order


# --- N+1 ---


async def test_timeline_n_plus_1_yok(client, user_factory, seeded_db, project_factory):
    """Veri hacmi büyüyünce sorgu sayısı SABİT kalmalı. N+1 geri gelirse ikinci
    ölçüm birinciden büyük çıkar ve bu test kırmızıya döner."""
    await modul_duzeyi_yaz(seeded_db, VIEW_ROLE, "projects", AccessLevel.view)
    user, token = await _login(client, user_factory, VIEW_ROLE)
    await _grant(seeded_db, user, project_id=None, all_projects=True)

    async def _hacim(etiket: str, proje_sayisi: int, santiye: int, bolum: int, milestone: int):
        for p in range(proje_sayisi):
            project = await project_factory(f"{etiket}-{p}", name=f"{etiket} Proje {p}")
            for s in range(santiye):
                site = await _site(seeded_db, project.id, f"{etiket}{p}-S{s}")
                for b in range(bolum):
                    section = await _section(seeded_db, site.id, f"Bölüm {b}", sort_order=b)
                    for m in range(milestone):
                        await _milestone(
                            seeded_db, section.id, f"M{m}", date(2026, 1, 1), sort_order=m
                        )

    async def _olc() -> tuple[int, dict]:
        with _sorgu_sayaci() as ifadeler:
            resp = await client.get("/projects/timeline", headers=_auth(token))
        assert resp.status_code == 200, resp.text
        return len(_timeline_sorgulari(ifadeler)), resp.json()

    await _hacim("KUCUK", 1, 1, 1, 1)
    kucuk_sayi, kucuk_govde = await _olc()

    await _hacim("BUYUK", 3, 2, 3, 2)
    buyuk_sayi, buyuk_govde = await _olc()

    # Ölçüm anlamlı olsun: veri gerçekten büyüdü.
    assert len(buyuk_govde["items"]) > len(kucuk_govde["items"])
    assert sum(len(i["sections"]) for i in buyuk_govde["items"]) >= 18
    assert buyuk_sayi == kucuk_sayi, (
        f"N+1: küçük hacimde {kucuk_sayi}, büyük hacimde {buyuk_sayi} sorgu"
    )
