"""AI-2b + AI-2d — on altı okuma aracının handler'ı.

`handlers.py`nin ikinci parçasıdır (**800 satır tavanı bölmesi**, `_journal.py`
emsali): AI-0b'nin altı aracı orada, bu dilimin on altısı burada. Ortak zarf
çevirici `tools/zarf.py`dedir — ikinci bir kopya YAZILMADI.

🔴 **B14 — IMPORT SINIRI burada da geçerlidir**: `app.modules.<X>.{service,
repository,models}` import EDİLEMEZ ve `select(` / `session.` token'ı
BULUNAMAZ. Araçlar SERVİSİ değil UCU sarar (T2).

🔴 **Handler'lar doğrudan çağrılamaz** (Kapı E). Tek giriş `ToolRegistry.invoke`.
"""

from __future__ import annotations

from typing import Any

from app.modules.ai.registry import AracBaglami
from app.modules.ai.result import (
    AracSonucu,
    Ok,
    ScopedEmpty,
    Truncated,
    liste_sonucu,
    sayfalamasiz_liste_sonucu,
)
from app.modules.ai.tools import schemas
from app.modules.ai.tools.zarf import kod_hali

# =========================================================================== #
# AI-2b + AI-2d — 16 okuma aracı
#
# 🔴 ÜÇ ORTAK KURAL (üçü de ölçüme dayanır, hiçbiri üsluptan değildir):
#
# 1. **Zorunlu sorgu parametresi olan uç `BosGirdi` ile sarılamaz.** Beş uç
#    zorunlu parametre bildirir (`rota.dependant.query_params[*].field_info
#    .is_required()` ile ölçüldü): `/contracts` (`type`), `…/timesheet`
#    (`year`+`month`), `…/plan/day-summary` (`start`), `/equipment/work-summary`
#    ve `/equipment/fuel-summary` (`year`+`month`). Eksik gönderilirse uç 422
#    verir ve araç HER çağrıda `ust_kaynak_hatasi` döner.
#
# 2. **Sorgu parametresi TEL ÜZERİNDEKİ ADIYLA gönderilir (ALIAS).** `/contracts`
#    parametresinin python adı `contract_type`, tel adı `type`tir. FastAPI
#    alias varsa YALNIZ alias'ı tanır; python adını göndermek parametreyi
#    SESSİZCE düşürür.
#
# 3. **Sayfalamasız uç `limit` ALMAZ.** `/progress-payments` · `/contracts` ·
#    `/subcontractors` `limit`/`offset` BİLDİRMEZ ve FastAPI bilinmeyen
#    parametreyi 422 ile reddetmez, **sessizce yutar** — tavan uygulandığı
#    SANILIR. Bu üç uçta `sayfalamasiz_liste_sonucu` (ya da elle ölçülen toplam)
#    kullanılır.
# =========================================================================== #


def _kart_sonucu(govde: dict[str, Any], *, toplam: int, donen: int) -> AracSonucu:
    """Kart zarfı + DÜRÜST kırpma. `toplam` ÖLÇÜLÜR, uydurulmaz."""
    if toplam > donen:
        return Truncated(data=govde, total=toplam, returned=donen)
    return Ok(data=govde, row_count=donen)


async def proje_detayi(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /projects/{project_id}` — görünmeyen proje **404** (S14).

    🔴 `employer` NESNESİ OKUNMAZ: `tax_number` taşır ve `AiProjeDetayi` onu
    yapısal olarak taşımaz; okunsaydı bile `dogrula_spec` aracı kaydettirmezdi.
    """
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, "projects")) is not None:
        return hal
    g = yanit.json()
    veri = schemas.AiProjeDetayi(
        id=g["id"],
        code=g["code"],
        name=g["name"],
        type=g["project_type"],
        status=g["status"],
        city=g.get("city"),
        employer_name=g.get("employer_name"),
        start_date=g.get("start_date"),
        end_date=g.get("end_date"),
        contract_no=g.get("contract_no"),
        contract_amount=g.get("contract_amount"),
        budget=g["budget"],
        progress_pct=g["progress_pct"],
        site_count=g["site_count"],
        is_draft=g["is_draft"],
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def santiyeleri_listele(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites` — DÜZ şantiye listesi (`visible_projects` süzgeciyle).

    🔴 Bu uç `SiteCard` DÖNDÜRMEZ: `response_model` `SiteOptionListResponse`tir
    (`sites/flat_list_router.py`) ve `address` alanı **YOKTUR** — işlevsel
    ölçüm (`exposure.sema_anahtarlari`) 9 anahtar, yasak kesişimi BOŞ dedi.
    `SiteCard` `/projects/{id}/sites` ve `/sites/{id}` uçlarındadır.
    """
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "sites")) is not None:
        return hal
    g = yanit.json()
    items = [
        schemas.AiSantiyeSecenegi(
            id=s["id"],
            code=s["code"],
            name=s["name"],
            project_id=s["project_id"],
            project_name=s["project_name"],
        ).model_dump(mode="json")
        for s in g["items"]
    ]
    return liste_sonucu(data=items, total=g["total"], kapsam_modulu="sites")


async def santiye_detayi(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites/{site_id}` — 🔴 `address` OKUNMAZ (S5-c)."""
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, "sites")) is not None:
        return hal
    g = yanit.json()
    veri = schemas.AiSantiyeDetayi(
        id=g["id"],
        code=g["code"],
        name=g["name"],
        status=g["status"],
        city=g.get("city"),
        project_name=g["project"]["name"],
        site_manager_name=g.get("site_manager_name"),
        safety_officer_name=g.get("safety_officer_name"),
        start_date=g.get("start_date"),
        end_date=g.get("end_date"),
        delivery_date=g.get("delivery_date"),
        remaining_days=g.get("remaining_days"),
        section_count=g["section_count"],
        planned_worker_count=g.get("planned_worker_count"),
        is_draft=g["is_draft"],
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def is_kalemleri(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites/{site_id}/boq` — poz cetveli KARTI.

    🔴 Boş poz listesi `ScopedEmpty` DEĞİLDİR: görünen bir şantiyenin cetveli
    gerçekten boş olabilir ve bu bir kapsam olgusu değildir. Kapsam farkı ucun
    404'ünde konuşur (`service.visible_site`).

    Üç toplam `MetricPlaceholder`dır ve **üç hâli düzleştirilmez** (S25/B18):
    "yetkin yok" ile "değer 0" aynı metne inmez.
    """
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, "boq")) is not None:
        return hal
    g = yanit.json()
    t = g["totals"]
    tum_kalemler = [(grup, kalem) for grup in g["groups"] for kalem in grup["items"]]
    dilim = tum_kalemler[: ctx.spec.satir_tavani]
    gruplar: list[schemas.AiPozGrubu] = []
    for grup in g["groups"]:
        kalemler = [
            schemas.AiPozKalemi(
                code=k["code"],
                description=k["description"],
                unit=k["unit"],
                quantity=k["quantity"],
                unit_price=k["unit_price"],
            )
            for gr, k in dilim
            if gr["id"] == grup["id"]
        ]
        if kalemler:
            gruplar.append(schemas.AiPozGrubu(name=grup["name"], items=kalemler))
    veri = schemas.AiIsKalemleri(
        site_id=girdi.site_id,
        gruplar=gruplar,
        kalem_sayisi=len(tum_kalemler),
        grand_total=t["grand_total"],
        sozlesme_toplami=schemas.metrik_metni(t.get("contract_total")),
        gerceklesen_toplam=schemas.metrik_metni(t.get("realized_total")),
        kalan_toplam=schemas.metrik_metni(t.get("remaining_total")),
    )
    return _kart_sonucu(veri.model_dump(mode="json"), toplam=len(tum_kalemler), donen=len(dilim))


def _deger_dengesi_notu(sapma: Any, vb: dict[str, Any], *, para_gizli: bool) -> str:
    """Değer dengesi cümlesi — 🔴 MASKE SAYIYI GİZLERSE CÜMLE DE DEĞİŞİR.

    Eski hâl sapma hesaplanamadığında İKİ sebep sayıyor ve kullanıcıyı
    `toplam_deger` ile `atanmamis_unite`ye baktırıyordu. `projects=limited`
    rolünde o cümlenin İKİ YALANI birden vardı: (1) üçüncü bir sebep — değerin
    GİZLENMİŞ olması — hiç anılmıyordu, (2) yönlendirdiği `toplam_deger` alanı
    da maskeliydi, yani talimat İZLENEMEZDİ.

    🔴 Cümle `para_gizli` DOLAYIMIYLA seçilir, aktörün kapsamıyla DEĞİL:
    handler kapsamı okuyamaz (`ActorContext.scope` YOKTUR, S1) ve okuyabilseydi
    bile kapsam bilgisini metne taşımak S1'in reddettiği şeyin ta kendisi
    olurdu. Burada taşınan şey KAPSAM değil, GÖZLEMDİR: "değer toplamı boş".

    🔴 Ve "yetkin yok" DİYE KESİLMEZ, "olabilir" denir: değer toplamı gerçekten
    boş da olabilir. Kesin konuşmak, rayiç girmeyi unutmuş bir yöneticiyi var
    olmayan bir yetki sorununa baktırırdı.
    """
    if sapma is not None:
        return (
            f"Sapma %{sapma}, tolerans %{vb['tolerance_pct']}. "
            f"Tolerans {'içinde' if vb.get('is_within_tolerance') else 'DIŞINDA'}. "
            "Adet dengesi ile değer dengesi AYRI kararlardır."
        )
    if para_gizli:
        return (
            "Değer sapması HESAPLANAMADI ve bunun ÜÇ sebebi olabilir: (1) bu "
            "değerleri görme YETKİNİZ olmayabilir — rolünüzün veri kapsamı para "
            "alanlarını gizliyorsa `toplam_deger` de boş gelir, (2) rayiç değer "
            "hiç girilmemiş olabilir, (3) hiçbir ünite bir tarafa ATANMAMIŞ "
            "olabilir (payda ATANMIŞ değerdir). 🔴 `toplam_deger` BOŞ olduğu "
            "için bu üç hâl BURADAN AYIRT EDİLEMEZ; adet tarafı (`atanmamis_"
            "unite`) yalnız üçüncüyü eler. Bu 'denge uygun' DEMEK DEĞİLDİR."
        )
    return (
        "Değer sapması HESAPLANAMADI ve bunun İKİ sebebi olabilir: rayiç "
        "değer hiç girilmemiş olabilir, ya da hiçbir ünite bir tarafa "
        "ATANMAMIŞ olabilir (payda ATANMIŞ değerdir). Hangisi olduğunu "
        "`toplam_deger` ile `atanmamis_unite` birlikte söyler. Bu 'denge "
        "uygun' DEMEK DEĞİLDİR."
    )


async def arsa_payi(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /projects/{project_id}/land-share/summary` — ÖZET ucu (kararlı seçim).

    🔴 `…/land-share/units` SEÇİLMEDİ: gövdesi `buyer_name` taşır ve o ad
    `Customer.name`tir (`units/repository.py::_open_sales_stmt` → `customers`,
    kapısı `require_permission("sales", …)`, `sales` KAPALI). Özet ucunda o alan
    **hiç yoktur**; düşürülmüş bir alan ile var olmayan bir alan aynı güvence
    değildir.

    `landowner_name` ve hissedar adları KALIR: ikisi de `projects` modülünün
    tablolarındandır (ölçüldü).

    Kat karşılığı OLMAYAN proje ucun kararıyla **404** alır (boş özet DEĞİL) —
    `NotFound` bu yüzden meşru bir cevaptır ve "yetkin yok" DEMEK DEĞİLDİR.
    """
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, "projects")) is not None:
        return hal
    g = yanit.json()
    c, t, o, a = g["contract"], g["totals"], g["our_side"], g["owner_side"]
    cb = g["balance"]["count_balance"]
    vb = g["balance"]["value_balance"]
    sapma = vb.get("deviation_pct")
    # 🔴 PARA MASKESİNİN ÇIKARIMI — tahmin DEĞİL, gözlem.
    #
    # `ActorContext` `scope` ALANI TAŞIMAZ (S1, bekçisi `test_ai0b_yapisal.py`),
    # yani handler aktörün kapsamını SORAMAZ. Ama sormasına gerek de yok: kapsam
    # maskesi `para` kovasının TAMAMINI birden `null`a çeker, dolayısıyla
    # `value_total` DOLUYSA para kesinlikle maskeli DEĞİLDİR. Boşsa iki hâl
    # ayrışmaz (gerçekten rayiç yok ya da maskeli) ve aşağıdaki cümle bu
    # belirsizliği İTİRAF EDER — yanlış bir kesinlik uydurmaz.
    para_gizli = t["value_total"] is None
    veri = schemas.AiArsaPayi(
        project_id=g["project_id"],
        project_name=g["project_name"],
        landowner_name=c["landowner_name"],
        our_share_pct=c["our_share_pct"],
        owner_share_pct=c["owner_share_pct"],
        contract_no=c.get("contract_no"),
        delivery_date=c.get("delivery_date"),
        toplam_unite=t["unit_count"],
        toplam_deger=t["value_total"],
        bizim_unite=o["unit_count"],
        bizim_deger=o["value_total"],
        satilan_adet=o["sold_count"],
        arsa_sahibi_unite=a["unit_count"],
        arsa_sahibi_deger=a["value_total"],
        atanmamis_unite=g["unassigned"]["unit_count"],
        hissedarlar=[
            schemas.AiHissedar(
                shareholder_name=h["name"],
                share_pct=h["share_pct"],
                unit_count=h["unit_count"],
            )
            for h in g["shareholders"]
        ],
        adet_dengesi_notu=(
            f"Bizim pay: beklenen {cb['our_expected_count']}, atanan "
            f"{cb['our_assigned_count']}. Arsa sahibi: beklenen "
            f"{cb['owner_expected_count']}, atanan {cb['owner_assigned_count']}. "
            f"Atanmamış {cb['unassigned_count']}. İşaret ANLAMLIDIR: eksi = fazla atama."
        ),
        # 🔴 `deviation_pct` `None` iken sebep TEK DEĞİLDİR: payda ATANMIŞ
        # rayiç toplamıdır, yani (a) rayiç hiç girilmemiş ya da (b) hiçbir ünite
        # bir tarafa atanmamış olabilir. Tek sebep yazmak ikinci hâlde
        # kullanıcıyı yanlış yere baktırırdı.
        deger_dengesi_notu=_deger_dengesi_notu(sapma, vb, para_gizli=para_gizli),
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def isveren_hakedisleri(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /progress-payments` — 🔴 SAYFALAMASIZ UÇ.

    `limit`/`offset` BİLDİRİLMEZ (ölçüldü) ve FastAPI bilinmeyeni sessizce
    yutar; bu yüzden `params` HİÇ gönderilmez ve tavan `sayfalamasiz_liste_
    sonucu` ile **gövde okunduktan sonra** uygulanır. Toplam ÖLÇÜLÜR.
    """
    yanit = await ctx.get()
    if (hal := kod_hali(yanit.status_code, "progress_payments")) is not None:
        return hal
    g = yanit.json()
    satirlar = [
        schemas.AiIsverenHakedisi(
            id=k["id"],
            project_name=k["project_name"],
            sequence_no=k["sequence_no"],
            period_year=k.get("period_year"),
            period_month=k.get("period_month"),
            status=k["status"],
            gross_total=k["gross_total"],
            net_total=k["net_total"],
        ).model_dump(mode="json")
        for k in g["items"]
    ]
    return sayfalamasiz_liste_sonucu(
        satirlar, tavan=ctx.spec.satir_tavani, kapsam_modulu="progress_payments"
    )


async def taseron_hakedisleri(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /subcontractor-progress-payments` — bu uç SAYFALIDIR (kardeşi değil).

    ⚠️ Ölçüldü: işveren tarafı `limit` bildirmez, taşeron tarafı bildirir
    (`limit`/`offset`, `le=200`). İki kardeş uç aynı sanılıp aynı kalıpla
    yazılsaydı biri sessizce tavansız kalırdı.
    """
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "progress_payments")) is not None:
        return hal
    g = yanit.json()
    satirlar = [
        schemas.AiTaseronHakedisi(
            id=k["id"],
            project_name=k["project_name"],
            subcontractor_name=k.get("subcontractor_name"),
            contract_no=k.get("contract_no"),
            work_category=k.get("work_category"),
            sequence_no=k["sequence_no"],
            period_year=k.get("period_year"),
            period_month=k.get("period_month"),
            status=k["status"],
            gross_total=k["gross_total"],
            net_total=k["net_total"],
        ).model_dump(mode="json")
        for k in g["items"]
    ]
    return liste_sonucu(data=satirlar, total=g["total"], kapsam_modulu="progress_payments")


async def sozlesmeler(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /contracts` — 🔴 ZORUNLU + ALIAS'LI parametre, SAYFALAMASIZ uç.

    Üç ölçülmüş tuzak tek uçta buluşur:

    1. `contract_type` **varsayılansız** bildirilir → girdi modeli onu ZORUNLU
       taşır, yoksa her çağrı 422.
    2. Tel üzerindeki ad **`type`**tir (`Query(alias="type")`). Python adını
       (`contract_type`) göndermek parametreyi sessizce düşürürdü.
    3. `limit`/`offset` YOKTUR → tavan gövdeden uygulanır, toplam ÖLÇÜLÜR.
    """
    yanit = await ctx.get(params={"type": girdi.contract_type})
    if (hal := kod_hali(yanit.status_code, "contracts")) is not None:
        return hal
    g = yanit.json()
    ozet = g["summary"]
    tum = [
        schemas.AiSozlesme(
            id=k["id"],
            title=k["title"],
            contract_no=k.get("contract_no"),
            counterparty_name=k.get("counterparty_name"),
            amount=k["amount"],
            start_date=k.get("start_date"),
            end_date=k.get("end_date"),
            progress_pct=k.get("progress_pct"),
            status=k["status"],
        )
        for k in g["items"]
    ]
    if not tum:
        # 🔴 `Empty` DEĞİL: uç `visible_projects` süzgeci taşır, boşluk "hiç
        # sözleşme yok" değil "senin kapsamında yok" olabilir.
        return ScopedEmpty("contracts")
    dilim = tum[: ctx.spec.satir_tavani]
    zarf = schemas.AiSozlesmeListesi(
        contract_type=girdi.contract_type,
        items=dilim,
        total=len(tum),
        total_amount=ozet["total_amount"],
        active_count=ozet["active_count"],
        expiring_this_month_count=ozet["expiring_this_month_count"],
    )
    return _kart_sonucu(zarf.model_dump(mode="json"), toplam=len(tum), donen=len(dilim))


async def taseronlar(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /subcontractors` — 🔴 PROJE KAPSAMLI DEĞİLDİR ve SAYFALAMASIZDIR.

    Uç imzası `user` parametresi bile ALMAZ (ölçüldü, `contracts/router.py`
    yorumu birebir: *"`visible_projects` süzgeci BİLİNÇLİ OLARAK yok:
    kartoteks proje-bağımsızdır"*). Bu yüzden `ToolKumesi.SIRKET_GENELI`
    beyan edilir.

    🔴 Ve `kapsam_modulu` VERİLMEZ: boş küme burada gerçekten "kayıt yok"
    demektir. `ScopedEmpty` yazmak var olmayan bir kapsam süzgecini ima ederdi
    — kapsam notunun tersi bir yalan.

    🔴 **`active_only=False` AÇIKÇA GÖNDERİLİR — ve bu bir DÜZELTMEDİR.**
    Uç `active_only: bool = True` bildirir; parametresiz çağırmak repository'ye
    `WHERE is_active IS TRUE` koydurur. Handler bunu bilmeden `ctx.get()`
    çağırırsa ÜÇ YERDE BİRDEN yalan doğar: (a) katalog açıklaması "şirketin tüm
    taşeronları" der ama küme süzülmüştür, (b) `AiTaseron.is_active` yapısal
    olarak HEP `True` olur — alanın kendisi bilgi taşımaz, (c) boş küme "kayıt
    yok" der, doğrusu "aktif kayıt yok"tur. Varsayılanı devralmak yerine ONU
    EZMEK üçünü birden gerçeğe eşitler. `active_only` ucun BİLDİRDİĞİ bir addır,
    params bekçisi geçirir.

    🔴 `tax_number` · `phone` · `email` OKUNMAZ (S5-c, üçü de yasak anahtar).

    ⚠️ **`contact_person` KALIR — bilinçli bir sınır çizgisi.** Düşürülen üç
    komşusu **kimlik ya da iletişim KANALIDIR**: vergi no bir kişiyi eşleştirir,
    telefon ve e-posta doğrudan erişim açar. `contact_person` ise kurumsal
    muhatabın adıdır ve "bu taşeronla kiminle konuşuluyor" sorusu tam da bu
    aracın işidir. Ne `YASAK_ALAN_ANAHTARLARI`nda ne de
    `KISI_ADI_ANAHTARLARI`ndadır (`created_by_name` ile aynı sınıf: bir
    kaydın/kurumun muhatabı). Kullanıcı sınırı daraltmak isterse alan tek
    satırda düşer — ama bu bir KARAR olmalı, sessiz bir tercih değil.
    """
    yanit = await ctx.get(params={"active_only": False})
    if (hal := kod_hali(yanit.status_code, "contracts")) is not None:
        return hal
    satirlar = [
        schemas.AiTaseron(
            id=t["id"],
            name=t["name"],
            contact_person=t.get("contact_person"),
            category=t.get("category"),
            is_active=t["is_active"],
        ).model_dump(mode="json")
        for t in yanit.json()["items"]
    ]
    return sayfalamasiz_liste_sonucu(satirlar, tavan=ctx.spec.satir_tavani)


async def puantaj(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites/{site_id}/timesheet` — AYLIK matris, **KİŞİ SATIRI YOK**.

    🔴 `TimesheetMatrix.rows[].full_name` okunmaz ve araç `personnel` BEYAN
    ETMEZ. İkisi bir arada zorunludur: beyan etseydi (AGREGA) kayıt anında
    `IfsaIhlali` alırdı; satırı basıp beyan etmeseydi kişi verisi sessizce
    sağlayıcıya giderdi. Emsal `puantaj_haftasi`.
    """
    yanit = await ctx.get(params={"year": girdi.year, "month": girdi.month})
    if (hal := kod_hali(yanit.status_code, "timesheet")) is not None:
        return hal
    g = yanit.json()
    veri = schemas.AiPuantajAyi(
        site_id=g["site_id"],
        site_name=g["site_name"],
        project_name=g["project_name"],
        year=g["year"],
        month=g["month"],
        section_name=g.get("section_name"),
        worker_count=g["worker_count"],
        total_hours=g["total_hours"],
        total_man_days=g["total_man_days"],
        gun_toplamlari=[
            schemas.AiPuantajGunu(
                work_date=t["work_date"],
                total_hours=t["total_hours"],
                worked_day_count=t["worked_day_count"],
                leave_count=t["leave_count"],
                temporary_duty_count=t["temporary_duty_count"],
            )
            for t in g["day_totals"]
        ],
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


async def gunluk_kayit(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites/{site_id}/diary` — şantiye günlüğü kayıtları.

    🔴 Kayıt METNİ (`notes`, satırlar, olay açıklaması) BASILMAZ: künye
    yeterlidir ve serbest metin, sağlayıcıya giden yüzeyin en zehirlenebilir
    parçasıdır (blocks.py'nin uzak-görsel sızıntısı gerekçesi).

    🔴 **`kapsam_modulu` VERİLMEZ — ve bu bir DÜZELTMEDİR.** İlk hâlim
    `kapsam_modulu="site_diary"` yazıyordu, yani boş liste `ScopedEmpty` olup
    *"bu modülde kayıt olabilir ama hiçbiri sizin kapsamınızda değil"* diyordu.
    **YALAN.** Ölçüldü: uç kapsam kararını ŞANTİYE üzerinden verir
    (`site_diary/read.py::list_entries` → `service.visible_site`) ve görünmeyen
    şantiye **404** alır. Yani 200 + boş listenin **tek** anlamı vardır: *bu
    şantiyede günlük kaydı girilmemiş.* Kapsam dışılık bu dala hiç ulaşamaz.
    `taseronlar`la aynı sonuç, farklı sebep: orada süzgeç YOK, burada süzgeç
    ucun 404'ünde TÜKENİYOR.
    """
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "site_diary")) is not None:
        return hal
    g = yanit.json()
    satirlar = [
        schemas.AiGunlukKayit(
            id=k["id"],
            entry_date=k["entry_date"],
            status=k["status"],
            weather=k.get("weather"),
            has_incident=k["has_incident"],
            worker_total=k["worker_total"],
            lines_total=k["lines_total"],
        ).model_dump(mode="json")
        for k in g["items"]
    ]
    return liste_sonucu(data=satirlar, total=g["total"])


async def gun_plani(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /sites/{site_id}/plan/day-summary` — kayan gün penceresi.

    🔴 `…/plan` (haftalık ızgara) SEÇİLMEDİ: `week_start` PAZARTESİ olmak
    zorundadır, bu uç ise HERHANGİ bir günden başlar (ucun kendi gerekçesi).
    Model bir tarih uydurduğunda haftalık uç 422 verirdi.

    🔴 İzin kapısı `site_diary`dir, `site_planning` DEĞİL — `site_planning`
    bir izin modülü değildir (rota tablosundan ölçüldü).

    🔴 **`text` BASILIR ve bu `gunluk_kayit`la ÇELİŞMEZ — sınır aynı yerdedir.**
    Kardeş araç günlük kaydın serbest metnini düşürür; burada plan metni
    kalıyor. Fark üslup değil ÖLÇÜLMÜŞ bir kaynak farkıdır: `gunluk_kayit`
    GERÇEKLEŞEN kaydın notudur, aracın cevabı için gerekmez (künye yeter).
    `…/plan/day-summary` ise ızgaradan TÜRETİLİR (uç sözleşmesi: "gün başına
    hücre metinlerinin birleşimi") ve aracın TEK işi "önümüzdeki günlerde ne
    planlanmış" sorusudur — metin düşerse araç boş bir iskelet döner.

    ⚠️ **DÜRÜST SINIR: bu metnin GÜVENLİ olduğu anlamına GELMEZ.** Plan hücresi
    de kullanıcı girdisidir ve enjeksiyon taşıyabilir. Taşıyan korkuluklar başka
    katmanlardadır ve bu dilimde DEĞİŞMEDİ: model çıktısı düz metin basılır
    (`blocks.py` — markdown/HTML çözücü YOK), CSP `img-src 'self'`, ve tur
    başına niyet allowlist'i araç çıktısından gelen bir talimatın yeni araç
    çağırmasını engeller (`niyet_disi`). Bu araç o korkulukların ARKASINDADIR,
    yerine geçmez.
    """
    yanit = await ctx.get(params={"start": girdi.start.isoformat()})
    if (hal := kod_hali(yanit.status_code, "site_diary")) is not None:
        return hal
    g = yanit.json()
    veri = schemas.AiGunPlani(
        site_id=g["site_id"],
        site_name=g["site_name"],
        project_name=g["project_name"],
        start=g["start"],
        end=g["end"],
        days=[
            schemas.AiPlanGunu(
                plan_date=d["plan_date"],
                is_weekend=d["is_weekend"],
                has_plan=d["has_plan"],
                text=d["text"],
                planned_worker_total=d["planned_worker_total"],
                section_names=list(d["section_names"]),
            )
            for d in g["days"]
        ],
    )
    return Ok(data=veri.model_dump(mode="json"), row_count=1)


# --- AI-2d — makine ------------------------------------------------------- #


async def makine_listesi(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /equipment` — 🔴 KAPSAM İKİ DALLI ve **OR**'ludur.

    `equipment/repository.py::scope` `Equipment.site_id IS NULL` (DEPODAKİ
    makine) dalını kapsam süzgecinin DIŞINDA tutar. Yani dönen küme aktörün
    hiçbir projesine bağlı olmayan satır içerebilir → `SIRKET_GENELI`.
    `gosterge_ozeti`nin beyanıyla aynı gerekçe; desen ikizdir.
    """
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "equipment")) is not None:
        return hal
    g = yanit.json()
    satirlar = [
        schemas.AiMakine(
            id=m["id"],
            name=m["name"],
            category=m["category"],
            brand=m.get("brand"),
            model=m.get("model"),
            plate_no=m.get("plate_no"),
            ownership=m["ownership"],
            status=m["status"],
            site_id=m.get("site_id"),
            is_active=m["is_active"],
        ).model_dump(mode="json")
        for m in g["items"]
    ]
    return liste_sonucu(data=satirlar, total=g["total"], kapsam_modulu="equipment")


async def makine_calisma(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /equipment/work-summary` — AYLIK AGREGAT (kapalı yönetim kararı).

    🔴 `…/work-logs` SEÇİLMEDİ: satır kaydı ham hareket tablosudur, model için
    karar bilgisi değil hacimdir.

    🔴 Kapsam `work_log_scope`tur ve kaydın KENDİ `site_id`sine bakar, iki
    dallı OR'ludur (`site_id IS NULL` = depodaki makinenin işi) → `SIRKET_GENELI`.
    """
    yanit = await ctx.get(params={"year": girdi.year, "month": girdi.month})
    if (hal := kod_hali(yanit.status_code, "equipment")) is not None:
        return hal
    g = yanit.json()
    t = g["totals"]
    tum = g["rows"]
    dilim = tum[: ctx.spec.satir_tavani]
    bilinmeyen = sum(1 for r in tum if r.get("cost") is None)
    veri = schemas.AiMakineCalismasi(
        year=g["year"],
        month=g["month"],
        rows=[
            schemas.AiCalismaSatiri(
                equipment_name=r["equipment_name"],
                site_id=r.get("site_id"),
                hours=r["hours"],
                usage_pct=r.get("usage_pct"),
                breakdown_hours=r["breakdown_hours"],
                cost=r.get("cost"),
            )
            for r in dilim
        ],
        total_hours=t["hours"],
        total_breakdown_hours=t["breakdown_hours"],
        total_cost=t["cost"],
        usage_pct_avg=t.get("usage_pct_avg"),
        bilinmeyen_bedel_notu=(
            # IZN-B4d: maliyet kategorisi gizliyken toplam `null`dur ve satır bedelleri de
            # `null`dır — bu "bilinmiyor" DEĞİL "bu rolde gösterilmiyor"dur; ikisi karıştırılmaz.
            "Bedeller bu rolde GİZLİ; saat alanları geçerlidir."
            if t["cost"] is None
            else f"{bilinmeyen} makinenin bedeli BİLİNMİYOR ve toplama UYDURMA bir 0 "
            "ile GİRMEDİ; `total_cost` yalnız bilinenlerin toplamıdır."
            if bilinmeyen
            else "Her satırın bedeli biliniyor; toplam eksiksizdir."
        ),
    )
    return _kart_sonucu(veri.model_dump(mode="json"), toplam=len(tum), donen=len(dilim))


async def makine_yakit(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /equipment/fuel-summary` — AYLIK AGREGAT.

    🔴 `…/fuel-logs` SEÇİLMEDİ: `work-summary` ile simetri korunur ve sapma
    rozeti (`consumption_status`) yalnız özet ucunda SUNUCUDAN gelir; ham
    kayıttan istemci/model kendi eşiğiyle türetseydi iki yüzey ayrışırdı.

    🔴 Kapsam `fuel_log_scope` — `work_log_scope`un kardeşi, aynı OR →
    `SIRKET_GENELI`.
    """
    yanit = await ctx.get(params={"year": girdi.year, "month": girdi.month})
    if (hal := kod_hali(yanit.status_code, "equipment")) is not None:
        return hal
    g = yanit.json()
    tum = g["rows"]
    dilim = tum[: ctx.spec.satir_tavani]
    veri = schemas.AiMakineYakiti(
        year=g["year"],
        month=g["month"],
        total_liters=g["total_liters"],
        total_amount=g["total_amount"],
        lt_per_hour_avg=g.get("lt_per_hour_avg"),
        avg_unit_price=g.get("avg_unit_price"),
        abnormal_count=g["abnormal_count"],
        rows=[
            schemas.AiYakitSatiri(
                equipment_name=r["equipment_name"],
                site_id=r.get("site_id"),
                liters=r["liters"],
                amount=r["amount"],
                actual=r.get("actual"),
                norm=r.get("norm"),
                deviation_pct=r.get("deviation_pct"),
                consumption_status=r.get("consumption_status"),
            )
            for r in dilim
        ],
    )
    return _kart_sonucu(veri.model_dump(mode="json"), toplam=len(tum), donen=len(dilim))


async def makine_kira(ctx: AracBaglami, girdi: Any) -> AracSonucu:
    """`GET /equipment/rental-invoices` — kira hakediş faturaları.

    🔴 Kapsam `rental_repository.invoice_scope` — `equipment.scope()`un birebir
    kardeşi, `site_id IS NULL` ("Tüm Projeler" faturası) dalı OR'ludur →
    `SIRKET_GENELI`.

    `supplier_name` `procurement` modülünün `Supplier` tablosundan gelir
    (ölçüldü) ve bu yüzden `veri_modulleri` onu da BEYAN EDER.
    """
    yanit = await ctx.get(params={"limit": ctx.spec.satir_tavani, "offset": 0})
    if (hal := kod_hali(yanit.status_code, "equipment")) is not None:
        return hal
    g = yanit.json()
    satirlar = [
        schemas.AiKiraFaturasi(
            id=f["id"],
            supplier_name=f.get("supplier_name"),
            invoice_no=f.get("invoice_no"),
            period_year=f["period_year"],
            period_month=f["period_month"],
            site_name=f.get("site_name"),
            invoice_amount=f.get("invoice_amount"),
            vat_amount=f.get("vat_amount"),
            payable_total=f.get("payable_total"),
            status=f["status"],
        ).model_dump(mode="json")
        for f in g["items"]
    ]
    return liste_sonucu(data=satirlar, total=g["total"], kapsam_modulu="equipment")
