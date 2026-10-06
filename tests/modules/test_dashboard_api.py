from decimal import Decimal

from sqlalchemy import select

from app.core.access import AccessLevel
from app.modules.dashboard.schemas import (
    DashboardSummaryResponse,
    MetricPlaceholder,
    PendingApprovalsPlaceholder,
    RiskAlertsPlaceholder,
)
from app.modules.roles.models import Role
from tests._hassas_alan import gizli_alanlar_ayarla
from tests._modul_duzeyi_yardimcisi import modul_duzeyi_yaz


def test_metric_placeholder_defaults_to_unavailable():
    metric = MetricPlaceholder(pending_module="progress_payments")

    assert metric.available is False
    assert metric.value is None
    assert metric.pending_module == "progress_payments"


def test_pending_approvals_placeholder_has_zero_count():
    placeholder = PendingApprovalsPlaceholder(pending_module="approvals")

    assert placeholder.available is False
    assert placeholder.count == 0
    assert placeholder.items == []


def test_summary_serializes_decimal_project_fields():
    summary = DashboardSummaryResponse(
        role_name="Patron",
        active_project_count=1,
        projects=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "code": "GK-A",
                "name": "Güneşkent A-Blok",
                "status": "active",
                "budget": Decimal("1500000.00"),
                "progress_pct": Decimal("42.50"),
            }
        ],
        portfolio=MetricPlaceholder(pending_module="progress_payments"),
        receivables=MetricPlaceholder(pending_module="invoicing"),
        average_margin=MetricPlaceholder(pending_module="progress_payments"),
        pending_approvals=PendingApprovalsPlaceholder(pending_module="approvals"),
        risks=RiskAlertsPlaceholder(),
    )

    dumped = summary.model_dump(mode="json")

    assert dumped["projects"][0]["budget"] == "1500000.00"
    assert dumped["risks"]["available"] is False
    assert dumped["risks"]["items"] == []
    # RISK-1: tek `pending_module` yerine KAYNAK LISTESI. Bos zarfta liste de
    # bostur — kartin durumunu artik kaynaklar tasir.
    assert dumped["risks"]["sources"] == []


async def _login(client, user_factory, role_key: str) -> str:
    await user_factory(email=f"{role_key}@t.co", password="parola1234", role_key=role_key)
    resp = await client.post(
        "/auth/login", json={"email": f"{role_key}@t.co", "password": "parola1234"}
    )
    return resp.json()["access_token"]


async def test_summary_requires_authentication(client):
    resp = await client.get("/dashboard/summary")
    assert resp.status_code == 401


async def test_summary_forbidden_without_dashboard_permission(client, user_factory):
    # seed_data.py:140 -> dashboard satirinda procurement = none
    token = await _login(client, user_factory, "procurement")
    resp = await client.get("/dashboard/summary", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 403


async def test_summary_returns_projects_for_permitted_role(
    client, db_session, user_factory, project_factory
):
    await project_factory("GK-A", name="Güneşkent A-Blok", status="active")
    await project_factory("OSB-1", name="Çelik OSB Fabrika", status="on_hold")
    user = await user_factory(email="patron@t.co", password="parola1234", role_key="patron")
    user.all_projects = True
    await db_session.flush()
    login = await client.post(
        "/auth/login", json={"email": "patron@t.co", "password": "parola1234"}
    )
    token = login.json()["access_token"]

    resp = await client.get("/dashboard/summary", headers={"Authorization": f"Bearer {token}"})

    assert resp.status_code == 200
    body = resp.json()
    assert [p["code"] for p in body["projects"]] == ["GK-A", "OSB-1"]
    assert body["active_project_count"] == 1
    assert body["role_name"] == "Patron"
    # DASH-1: portfoy BAGLANDI. Iki gorunur proje var, hakedis yok -> zarf
    # DOLU gelir ve degeri `0.00`dir: bu sifir OTORITERDIR ("tamamlanmis
    # hakedisiniz yok"), "bilinmiyor" degil. Bos zarf (`available=false`)
    # artik YALNIZ gorunur proje HIC yokken ya da izin yokken doner
    # (`test_dash1_portfolio.py`).
    assert body["portfolio"]["available"] is True
    assert body["portfolio"]["value"] == "0.00"
    assert body["portfolio"]["pending_module"] is None
    assert body["pending_approvals"]["count"] == 0
    assert "password_hash" not in resp.text


async def test_summary_empty_state_is_not_an_error(client, user_factory):
    token = await _login(client, user_factory, "patron")
    resp = await client.get("/dashboard/summary", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    assert resp.json()["projects"] == []
    assert resp.json()["active_project_count"] == 0


async def _ozet(client, email: str):
    """Panel yanitini HAM doner — govde METNI de iddiaya girer (butce sizintisi)."""
    login = await client.post("/auth/login", json={"email": email, "password": "parola1234"})
    assert login.status_code == 200, login.text
    return await client.get(
        "/dashboard/summary",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )


async def test_projects_izni_KAPALIYKEN_panel_proje_karti_BASMAZ(
    client, seeded_db, user_factory, project_factory
):
    """🔴 K4 (TUREV ALAN SIZINTISI) — proje kartlarinin ALAN KAPISI.

    `GET /dashboard/summary`in TEK kapisi `require_permission("dashboard", view)`
    idi. Ayni yanit `code` · `name` · `status` · **`budget`** · `progress_pct`
    tasiyor; ayni veri `GET /projects` ucundan `require_permission("projects",
    ...)` ile korunuyor. Yani AYNI VERI iki uctan cikiyordu ve yalniz birinde
    kapi vardi.

    Bugun kazara guvenliydi: tohum matrisinde `dashboard` (seed_data.py:183) ve
    `projects` (:187) satirlari BIREBIR AYNI. Ama izin hucreleri CALISMA ANINDA
    degistirilebilir (`PUT /roles/{role_id}/permissions/{module_key}`), ve
    `test_seed_matrix.py` yalnizca TOHUMUN esitligini kilitler.

    Bekci CIFT YONLUDUR (K-IKIZ1): once izin VARKEN kartlarin GELDIGI, sonra
    hucre `none` yapilinca DUSTUGU cakilir — "her seyi bosalt" mutasyonu da
    yakalanir.
    """
    await project_factory("GK-A", name="Güneşkent A-Blok", status="active", budget="7654321.00")
    # `hr_manager`: `dashboard = _LIM` (paneli acabilir) ve `projects = _LIM`.
    user = await user_factory(email="ik-panel@t.co", password="parola1234", role_key="hr_manager")
    user.all_projects = True
    await seeded_db.flush()

    # 🔴 Bu test K4 KAPISINI ölçer, KAPSAM MASKESİNİ değil: `hr_manager` seed'de
    #    her iki modülde de `limited` taşır ve bütçe o yüzden maskelenirdi —
    #    aşağıdaki "BÜTÇE sızdı" iddiası o zaman kapıdan değil maskeden geçerdi
    #    ve kapı kaldırılsa bile YEŞİL kalırdı (sahte-yeşil). Kapsam açıkça
    #    `all`a çekilir ki ölçülen tek şey izin hücresi olsun.
    await modul_duzeyi_yaz(seeded_db, "hr_manager", "dashboard", AccessLevel.view)
    await modul_duzeyi_yaz(seeded_db, "hr_manager", "projects", AccessLevel.view)
    # IZN-B4: maske `role_hidden_fields`tan gelir; seed `limited` rolü tutarları gizler → sıfırla.
    rol = (await seeded_db.execute(select(Role).where(Role.key == "hr_manager"))).scalar_one()
    await gizli_alanlar_ayarla(seeded_db, rol, [])

    # (a) OLUMLU KONTROL — `projects` izni VARKEN kart DOLU.
    izinli = await _ozet(client, "ik-panel@t.co")
    assert izinli.status_code == 200, izinli.text
    assert [p["code"] for p in izinli.json()["projects"]] == ["GK-A"]
    assert izinli.json()["active_project_count"] == 1
    assert "7654321.00" in izinli.text

    # (b) Hucre CALISMA ANINDA kapatilir — uc hâlâ acik (403 DEGIL) ama kart bos.
    await modul_duzeyi_yaz(seeded_db, "hr_manager", "projects", AccessLevel.none)

    kapali = await _ozet(client, "ik-panel@t.co")
    assert kapali.status_code == 200, kapali.text
    govde = kapali.json()
    assert govde["projects"] == [], govde["projects"]
    assert govde["active_project_count"] == 0
    assert "7654321.00" not in kapali.text, "BÜTÇE sızdı"
    assert "Güneşkent A-Blok" not in kapali.text, "proje ADI sızdı"
