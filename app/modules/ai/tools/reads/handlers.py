"""AI-0b'nin altı araç handler'ı.

🔴 **B14 — IMPORT SINIRI.** Bu dosya (ve `tools/**` altındaki her dosya)
`app.modules.<X>.{service,repository,models}` import EDEMEZ ve içinde `select(`
ya da `session.` token'ı **BULUNAMAZ**. Gerekçe ölçüldü ve #1 mimarisini
öldüren vakadır: `timesheet/week.py::build(session, site, project, section, *,
iso_year, iso_week)` **aktör ALMAZ**; kapsam kapısı router'ın çağırdığı
`service.visible_site(session, user, site_id)`tir. Servisi saran bir araç
`timesheet:view` olan herkese erişimi olmayan projelerin puantajını okuturdu.

Bu yüzden araçlar **UCU** sarar: her çağrı okuma düzlemine gerçek bir HTTP GET
olarak gider ve ucun `require_permission` + `visible_*` zinciri **aynen** koşar.

🔴 **Handler'lar doğrudan çağrılamaz** (Kapı E). Tek giriş `ToolRegistry.invoke`
ve bekçileri `test_ai0b_yapisal.py::test_B14_*` + `::test_B15_*`tir.
(🔴 Bu satır eskiden VAR OLMAYAN bir `test_ai0b_import_siniri.py`yi gösteriyordu.)
"""

from __future__ import annotations

from typing import Any

from app.modules.ai import guards
from app.modules.ai.navigation import EKRAN_ADLARI
from app.modules.ai.registry import AracBaglami
from app.modules.ai.result import AracSonucu, Ok, liste_sonucu
from app.modules.ai.tools import schemas
from app.modules.ai.tools.zarf import kod_hali


async def projeleri_listele(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /projects` — `visible_projects` kapsam süzgecinin araçtan geçtiğinin kanıtı."""
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "projects")) is not None:
        return hal
    govde = yanit.json()
    items = [
        schemas.AiProje(
            id=p["id"],
            code=p["code"],
            name=p["name"],
            status=p["status"],
            # 🔴 Uçtaki alan adı `project_type`tır, `type` DEĞİL (ölçüldü:
            # `ProjectListItem`). Araç yanıtında `type` diye yayınlanır ama
            # okuma doğru anahtardan yapılır.
            type=p["project_type"],
            progress_pct=p.get("progress_pct"),
        ).model_dump(mode="json")
        for p in govde["items"]
    ]
    # 🔴 `kapsam_modulu` verilir: bu ucun boş kümesi "hiç proje yok" DEĞİL
    # "senin kapsamında yok" olabilir (`visible_projects`). `Empty` yazmak
    # modele yalan söyletirdi.
    return liste_sonucu(data=items, total=govde["total"], kapsam_modulu="projects")


async def onay_kutum(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /approvals` — kapının **mekanik türetilemeyeceğinin** canlı vakası.

    Router'ın birebir cümlesi: *"Ayri bir yetki kapisi YOKTUR ve olmamalidir:
    donen kume zaten 'bu adim SANA dustu' olgusuyla sinirlidir; `approvals`
    izni dusuk olan bir rol de kendine dusen imzayi gormek zorundadir (matriste
    sef/saha/IK = `_OWN`)."*

    Doğru servis `approvals/service.py::pending_for_user`tır, `inbox.py::
    load_facts` DEĞİL — o fonksiyon **aktör almaz** (ölçüldü). Araç zaten servisi
    değil ucu sarar, ama yanlış eşleme kayda geçsin diye burada duruyor.
    """
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "approvals")) is not None:
        return hal
    govde = yanit.json()
    items = [
        schemas.AiOnayKalemi(
            document_type=k["document_type"],
            document_id=k["document_id"],
            title=k.get("title"),
            subtitle=k.get("subtitle"),
            created_by_name=k.get("created_by_name"),
            current_step_no=k["current_step_no"],
            gross_amount=k.get("gross_amount"),
            net_amount=k.get("net_amount"),
        ).model_dump(mode="json")
        for k in govde["items"]
    ]
    zarf = schemas.AiOnayKutusu(
        items=[schemas.AiOnayKalemi.model_validate(i) for i in items],
        total=govde["total"],
    )
    if not items:
        # 🔴 `ScopedEmpty` DEĞİL: burada kapsam süzgeci yok, "sana düşen imza
        # yok" olgusu var. `ScopedEmpty` yazmak "yetkin dar" ima ederdi.
        return Ok(data=zarf.model_dump(mode="json"), row_count=0)
    if govde["total"] > len(items):
        from app.modules.ai.result import Truncated

        return Truncated(
            data=zarf.model_dump(mode="json"), total=govde["total"], returned=len(items)
        )
    return Ok(data=zarf.model_dump(mode="json"), row_count=len(items))


async def puantaj_haftasi(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites/{site_id}/timesheet/week` — #1'i öldüren vakanın ta kendisi.

    Kapsam **ROUTER'dadır** (`service.visible_site`); erişilemeyen bir şantiye
    için uç 404 verir ve zarf `NotFound` olur — S14'ün "görünmeyen-var-olan ile
    var-olmayan BAYT BAYT AYNI" kuralı böyle sağlanır.
    """
    yanit = await ctx.get(params={"iso_year": girdi.iso_year, "iso_week": girdi.iso_week})
    if (hal := kod_hali(yanit.status_code, "timesheet")) is not None:
        return hal
    g = yanit.json()
    veri = schemas.AiPuantajHaftasi(
        site_id=g["site_id"],
        site_name=g["site_name"],
        project_name=g["project_name"],
        iso_year=g["iso_year"],
        iso_week=g["iso_week"],
        start_date=g["start_date"],
        end_date=g["end_date"],
        worker_count=g["worker_count"],
        totals=g["totals"],
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def gosterge_ozeti(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /dashboard/summary` — `MetricPlaceholder` üç hâlinin zarfa çevrilişi.

    🔴 **Aynı yanıtta ÜÇ AYRI proje sayısı var** ve araç bunları birbirinden
    TÜRETMEZ: `projects` dizisinin uzunluğu (`list_projects_for_user`, admin
    atlaması YOK — `GET /projects`ten FARKLI bir kapsam kuralı),
    `active_project_count` (taslakları DIŞLAR) ve portföyün saydığı küme.
    """
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, "dashboard")) is not None:
        return hal
    g = yanit.json()
    riskler = g.get("risks") or {}
    kaynaklar = riskler.get("sources") or []
    veri = schemas.AiGostergeOzeti(
        role_name=g["role_name"],
        active_project_count=g["active_project_count"],
        gorunur_proje_sayisi=len(g.get("projects") or []),
        portfoy=schemas.metrik_metni(g.get("portfolio")),
        alacaklar=schemas.metrik_metni(g.get("receivables")),
        ortalama_marj=schemas.metrik_metni(g.get("average_margin")),
        risk_notu=(
            f"{len(riskler.get('items') or [])} uyarı, {len(kaynaklar)} kaynaktan. "
            "⚠️ Bu kart KAYNAK BAŞINA EN FAZLA 3 satır döndürür ve toplam sayıyı "
            "HİÇ bildirmez; buradan 'toplam risk sayısı' çıkarılamaz."
        ),
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def yetkilerim(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /auth/me` — S14'ün korkuluğu: AI sınırını BİLEREK çağırsın.

    Uç **kapısızdır** (`UNGATED_ALLOWLIST` üyesi) ve bu bilinçlidir: aktör kendi
    yetkisini görmek için ek bir yetkiye ihtiyaç duymaz.
    """
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, guards.PERMISSION_MODULE)) is not None:
        return hal
    g = yanit.json()
    veri = schemas.AiYetkilerim(
        role_key=g["role_key"],
        permissions={k: str(v) for k, v in (g.get("permissions") or {}).items()},
        yaniti_besleyen_not=(
            "Bu harita INNER JOIN ile üretilir: izin SATIRI olmayan bir modülün "
            "anahtarı burada HİÇ görünmez. Bir modülün adı listede yoksa bu "
            "'öyle bir modül yok' DEMEK DEĞİLDİR."
        ),
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def navigate_to(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """Veri OKUMAZ. Kullanıcıyı bir ekrana yönlendirmeyi önerir (S22).

    🔴 URL üretmez: backend **ekran anahtarı** döner, URL'i frontend kurar
    (`routes.ts` AYRI BİR GİT DEPOSUNDADIR — türetme imkânsız).
    """
    veri = schemas.AiYonlendirme(ekran=girdi.ekran, ekran_adi=EKRAN_ADLARI[girdi.ekran])
    return Ok(data=veri.model_dump(mode="json"), row_count=1)
