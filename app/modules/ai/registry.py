"""Araç kaydı ve **TEK HUNİ** — `ToolRegistry.invoke()` (spec §2.2 KATMAN 3-4).

Handler'lar hiçbir yerden doğrudan çağrılamaz. İzin + sysadmin + şema doğrulama
+ yol parametresi kaçışı + denetim + satır tavanı **burada** koşar. "Her
handler'ın ilk satırı `can_read` çağırır" tarzı elle tekrarlanan kapı
**yasaktır**: üç yargıcın da işaret ettiği "geliştirici atlayabilir" deliği tam
oradaydı.

## Kapı beyanı TÜRETİLMEZ, BEYAN EDİLİR (GRAFT-3)

`ToolSpec.kapilar` bir `frozenset[tuple[str, AccessLevel]]`tir — **ÇOĞUL**,
çünkü ölçüldü: 342 operasyonun tam 2'si İKİ kapı taşır (`diary-suggestion`,
`progress_payments:view` + `site_diary:view`). Tekil bir alan bu iki ucu
yapısal olarak yanlış modellerdi.

Kapının **mekanik türetilemeyeceğinin** canlı vakası `GET /approvals`tır:
router'da *"Ayri bir yetki kapisi YOKTUR ve olmamalidir"* yazar (birebir
alıntı). Türeten bir sistem oraya bir kapı uydururdu ya da "kapısız = herkese
açık" diye okurdu; ikisi de yanlıştır. Bu yüzden `kapsam` alanı vardır.

## `min_level` alanı YOKTUR

Ölçüldü: `core/permissions.py::can_read` `AccessLevel.view`i **SABİT KODLAR** ve
seviye parametresi almaz — yani #1 ve #3'teki `min_level` alanı ölüydü. Burada
kapı `satisfies(permission.access_level, seviye)` ile **gerçekten** uygulanır ve
`kapilar` demetinin **her üyesi için ayrı ayrı** koşar.

## `ActorContext`ta `scope` ALANI YOKTUR — ama gerekçesi artık BAŞKA

IZN-B4/B6a: eski kapsam maskesi (`field_scope`, `scoped_route`, `kapsam_rotasi`,
`kapsam_kapisi`) SÖKÜLDÜ; yerine `core/mask_route` + `core/field_mask`.
Aşağıdaki metin TARİHSEL gerekçedir.

🔴 **ESKİ GEREKÇE BAYATTI, KARAR AYNI KALDI** (ölçüldü, `tests/modules/ai/
test_p8_kapsam_maskesi.py`). Burada *"`Scope` enum'unun 14 isabetinin hepsi
`roles/` altındadır ve hiçbir süzgeç `permission.scope` OKUMAZ"* yazıyordu. Bu
2026-09-19'dan beri YANLIŞTIR: `core/field_scope` + `core/scoped_route` altı
modülde (`boq · contracts · dashboard · projects · sales · sites`) ALAN
DÜZEYİNDE gerçek bir maske uygular ve AI hattı bu maskeden GEÇER — `ReadOnlyTransport`
gerçek `APIRoute` nesnelerini (dolayısıyla `kapsam_rotasi` sarmalayıcısını ve
router düzeyindeki `kapsam_kapisi` köprüsünü) taşır; araçlar SERVİSİ değil UCU
sarar. Kalan iş #4 (2026-09-23) bu altı modülü ATANABİLİR kümeyle eşitledi
(`app.modules.roles.scope_wiring.kablolu_moduller()`) ama AI'nin bu maskeyle
ilişkisini DEĞİŞTİRMEDİ: değişen yalnız HANGİ modüllerin `limited`/`finance`
kapsamını yönetici hücreye ATAYABİLDİĞİ, maskenin AI hattına NASIL bağlandığı
DEĞİL.

Sonuç yine de aynı kaldı ve gerekçesi ŞUDUR: maske `roles.RolePermission.scope`u
route sarmalayıcısı düzeyinde okur ve yanıt modelini DÖNÜŞTE değiştirir —
`ToolRegistry.invoke()`in KAPI kararına (izin + sysadmin + şema doğrulama)
hiçbir şey EKLEMEZ, yalnız dönen VERİYİ daraltır. Bu yüzden `ActorContext`
dataclass'ında (bir KAPI kararı taşıyan yapı) **`scope` ALANI hâlâ BULUNMAZ**
(S1): kapı kararı zaten `permissions`/`role_is_system` alanlarıyla verilir,
kapsam maskesi ayrı bir katmanda (yanıt zarfında) yaşar. Bekçisi tip testidir.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.access import AccessLevel, satisfies
from app.modules.ai import guards
from app.modules.ai.models import AiToolCallPhase, AiToolDecision
from app.modules.ai.result import AracSonucu, ToolError
from app.modules.ai.transport import ReadOnlyTransport, YolReddedildi

logger = logging.getLogger(__name__)


class ToolKapsami(str, enum.Enum):
    """Aracın kapısının **nasıl** kurulduğu — beyan, türetme değil."""

    MODUL_KAPISI = "modul_kapisi"
    #: Kapı YOK **ve OLMAMALI** — dönen küme zaten "bu SANA düştü" olgusuyla
    #: sınırlı (`GET /approvals`).
    KENDI_KUMESI = "kendi_kumesi"
    SISTEM_YONETICISI = "sistem_yoneticisi"


class ToolKumesi(str, enum.Enum):
    """Aracın hangi veri kümesine baktığı — **varsayılanı YOK** (S10)."""

    PROJE_KAPSAMLI = "proje_kapsamli"
    SIRKET_GENELI = "sirket_geneli"
    #: Veri okumaz (`navigate_to`) ya da yalnız aktörün kendi kimliğini okur
    #: (`yetkilerim`). B9 bu araçları ATLAMAZ; **farklı** bir iddia kurar.
    KAPSAMSIZ = "kapsamsiz"


@dataclasses.dataclass(frozen=True, slots=True)
class ActorContext:
    """Dispatch anında **taze** çözülen aktör bağlamı (S19).

    🔴 `scope` ALANI YOKTUR ve eklenmemelidir (S1). Bekçisi
    `test_ai0b_yapisal.py::test_S1_ActorContext_scope_ALANI_TASIMAZ`
    (🔴 eskiden VAR OLMAYAN bir `test_ai0b_registry.py`yi gösteriyordu).
    """

    user_id: uuid.UUID
    role_key: str
    role_is_system: bool
    permissions: Mapping[str, AccessLevel]
    #: DSC-B5 (Ü2): kullanıcı disiplin kapsamıyla KISITLI mı (`user_scope(...).is_restricted`).
    #: `scope` ALANI DEĞİL (S1 bekçisi, izin-matrisi `Scope`u ile karışmasın diye ayrı ad).
    disiplin_kisitli: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class ToolSpec:
    ad: str
    #: "NE ZAMAN çağrılır" + "NE SORMAZ". Prompt'a birebir bu metin gider.
    aciklama: str
    kapsam: ToolKapsami
    kume: ToolKumesi
    #: 🔴 ÇOĞUL: 2 uç İKİ kapı taşıyor (ölçüldü).
    kapilar: frozenset[tuple[str, AccessLevel]]
    #: `("/projects",)` — `ReadOnlyTransport` bunun DIŞINA çağrı yaptırmaz.
    ucler: tuple[str, ...]
    #: 🔴 **VARSAYILANI YOK** (A1/K1). Bu aracın YANITININ hangi izin
    #: modüllerinin verisini taşıdığı — `kapilar`dan AYRI bir eksendir ve
    #: türetilemez; ikisi de ölçümle çürütüldü:
    #:
    #: * `GET /dashboard/summary` **tek** kapı taşır (`dashboard:view`) ama
    #:   gövdesinde `progress_payments` (portföy) + `inventory` + `sites` (risk
    #:   kartı) verisi vardır. Kapıdan türeten bir sistem bunu göremezdi.
    #: * `onay_kutum`un `kapilar`ı **boştur** (`GET /approvals` bilinçli
    #:   kapısız). `kapilar`dan türeten bir sistem için `personnel`i saran ve
    #:   `kapilar=∅` yazan bir araç "hiçbir modülün verisi" sayılırdı.
    #:
    #: `exposure.dogrula_spec` bu alanı okur ve KAPALI bir modül görürse aracı
    #: **kaydettirmez**.
    veri_modulleri: frozenset[str]
    #: TİPLİ (`{"site_id": uuid.UUID}`) — S27'nin ikinci kilidi.
    yol_parametreleri: Mapping[str, type]
    girdi: type[BaseModel]
    yanit_modeli: type[BaseModel]
    calistir: Callable[[AracBaglami, BaseModel], Awaitable[AracSonucu]]
    satir_tavani: int = 200
    #: DSC-B5 (Ü2): `ucler`den biri `RequireUnrestricted` kapılı bir rotaya gidiyorsa True →
    #: disiplin kısıtlısına HİÇ sunulmaz. Tanım TEK yerdedir (burası); rota bağımlılık
    #: ağacıyla eşitliği `tests/modules/ai/test_dsc_ai_hakedis.py` kilitler.
    disiplin_kisitliya_kapali: bool = False


@dataclasses.dataclass(frozen=True, slots=True)
class AracBaglami:
    """Handler'a geçen **tek** bağlam. Ham `httpx` istemcisi TAŞIMAZ (B-D5)."""

    spec: ToolSpec
    actor: ActorContext
    transport: ReadOnlyTransport
    #: 🔴 Yol **HUNİDE** çözülür, handler'da DEĞİL. Handler kendi f-string'ini
    #: kurabilseydi `kacisla` çağrısını unutmak tek satırlık bir hata olurdu ve
    #: S27 geri gelirdi. Ayrıca denetime yazılan yol ile fiilen çağrılan yolun
    #: aynı olduğu ancak böyle GARANTİ edilir.
    cozulmus_yol: str

    async def get(self, yol: str | None = None, *, params: dict | None = None) -> httpx.Response:
        """Aracın kendi `ucler` desenlerine kilitli GET (varsayılan: çözülmüş yol)."""
        return await self.transport.get(
            yol if yol is not None else self.cozulmus_yol,
            izinli_desenler=self.spec.ucler,
            params=params,
        )


class BilinmeyenArac(KeyError):
    """Katı sözlük araması başarısız (B20). Bulanık eşleşme **YOKTUR**."""


class KapsamGerekli(ValueError):
    """Zorunlu bir yol parametresi `None` kaldı: ne model verdi ne bağlam.

    🔴 `YolReddedildi`nin bir alt hâli DEĞİL, AYRI bir hâl (AI-BAĞLAM). İkisi
    aynı cümleye düşerse kullanıcı yanlış şeyi düzeltmeye çalışır: biri
    "argümanın biçimi bozuk", öteki "hangi şantiye olduğu belli değil"dir.
    """


def kapilar_gecti(spec: ToolSpec, permissions: Mapping[str, AccessLevel]) -> bool:
    """`kapilar` demetinin **HER** üyesi ayrı ayrı sağlanmalı.

    Varsayılan KAPALI: izin satırı yoksa `AccessLevel.none` sayılır — `can_read`
    ve `require_permission` ile aynı fail-closed duruş.
    """
    return all(
        satisfies(permissions.get(modul, AccessLevel.none), seviye)
        for modul, seviye in spec.kapilar
    )


class ToolRegistry:
    """Katalog + dispatch. Katalog bir LİSTELEME'dir, **yaptırım dispatch'tedir**."""

    def __init__(
        self,
        okuma_araclari: tuple[ToolSpec, ...],
        propose_araclari: tuple[ToolSpec, ...] = (),
    ) -> None:
        # 🔴 KVKK KAPISI **KAYIT ANINDADIR** (A1/K1, `exposure.py`). Katalog bir
        # LİSTELEME, dispatch bir KARARDIR; ama sağlayıcıya kapalı bir modülün
        # verisini taşıyan aracın **hiç var olmaması** gerekir. İhlalde
        # `IfsaIhlali` atılır → uygulama açılmaz (fail-closed). Bir liste ya da
        # bir test dosyasına bırakılsaydı, `Scope` enum'unun ve
        # `YONETISIM_DENYLIST`in düştüğü yere düşerdi: **dekoratif** olurdu.
        from app.modules.ai import exposure

        for spec in (*okuma_araclari, *propose_araclari):
            exposure.dogrula_spec(spec)

        self._okuma = okuma_araclari
        self._propose = propose_araclari
        # 🔴 KATI SÖZLÜK (B20). `difflib.get_close_matches` gibi bir yedek
        # eklenirse "hiç DB sorgusu koşmaz" iddiası çöker: model uydurduğu bir
        # adla gerçek bir aracı çağırır.
        self._sozluk: dict[str, ToolSpec] = {s.ad: s for s in (*okuma_araclari, *propose_araclari)}

    @property
    def tum_araclar(self) -> tuple[ToolSpec, ...]:
        return (*self._okuma, *self._propose)

    def katalog(self, actor: ActorContext) -> list[ToolSpec]:
        """Aktörün **görebildiği** araçlar (Kapı A).

        `propose_*` kapısı `AccessLevel.admin` üzerine KURULMAZ: ölçülmüş klon
        riski var (`create_custom_role` + her hücrede `admin` = "`is_system=False`
        süper rol"). Kapı `role.key` + `is_system` ikilisine bakar.
        """
        from app.modules.roles.models import SYSTEM_ADMIN_KEY

        araclar = [s for s in self._okuma if self._sunulur(s, actor)]
        if actor.role_key == SYSTEM_ADMIN_KEY and actor.role_is_system:
            araclar += [s for s in self._propose if self._sunulur(s, actor)]
        return araclar

    @staticmethod
    def _sunulur(spec: ToolSpec, actor: ActorContext) -> bool:
        """İzin kapıları + DSC-B5 (Ü2) disiplin süzgeci: kısıtlıya ticari araç sunulmaz."""
        if actor.disiplin_kisitli and spec.disiplin_kisitliya_kapali:
            return False
        return kapilar_gecti(spec, actor.permissions)

    def dusurulen_moduller(self, actor: ActorContext) -> list[str]:
        """Yetkisi olmadığı için kataloğa GİRMEYEN araçların modülleri (S9-c).

        Prompt bunları **adıyla** listeler; yoksa model "bordro yok" der ve
        yalan söyler.
        """
        gorunur = {s.ad for s in self.katalog(actor)}
        moduller = {
            modul for s in self.tum_araclar if s.ad not in gorunur for modul, _ in s.kapilar
        }
        return sorted(moduller)

    # ------------------------------------------------------------------ #
    # TEK HUNİ
    # ------------------------------------------------------------------ #

    async def invoke(
        self,
        *,
        arac_adi: str,
        argumanlar: Mapping[str, Any],
        actor: ActorContext,
        transport: ReadOnlyTransport,
        conversation_id: uuid.UUID | None = None,
        ai_session_id: uuid.UUID | None = None,
        provider: str | None = None,
        model: str | None = None,
        varsayilan_kapsam: Mapping[str, Any] | None = None,
    ) -> AracSonucu:
        """Araç çağrısının **tek** giriş noktası.

        🔴 `actor` ZORUNLU, `Optional` DEĞİL, varsayılanı YOK (S15/T1).

        Sıra bağlayıcıdır:
          0. **Bağlam kapsamı** eksik argümanlara doldurulur (AI-BAĞLAM).
          1. Katı sözlük araması (B20).
          2. Katalog **yeniden hesaplanır** — yaptırım dispatch'tedir (§6.1-2).
          3. Şema doğrulama.
          4. Yol parametresi çözümü + nokta-segment reddi (S27/B16).
          5. **Denetim** (`started`), fail-closed (B6b).
          6. Handler.
          7. **Denetim** (`finished`).

        🔴 `varsayilan_kapsam` sohbet bağlamının araçlara ulaşan **TEK** yoludur
        (`context.varsayilan_kapsam`). Doldurma burada, huninin ağzında yapılır;
        handler'lara kopyalanmaz. Kopyalansaydı bir handler'ı yazan kişi satırı
        unutabilir ve o araç sessizce bağlamsız kalırdı — kapının handler'da
        tekrarlanmasının bu dosyanın açılış paragrafında reddedilen hâlinin
        aynısı.
        """
        from app.modules.ai.audit import record_tool_call

        call_id = uuid.uuid4()
        spec = self._sozluk.get(arac_adi)
        # --- 0. BAĞLAM KAPSAMI, denetim satırından ÖNCE ------------------
        # 🔴 Sıra bağlayıcıdır: `ai_tool_calls`e **fiilen koşan** argümanlar
        # yazılmalıdır. Doldurma denetimden sonra yapılsaydı iz, aracın hangi
        # şantiyeyi okuduğunu YANLIŞ gösterirdi ve bu hattın tüm meselesi
        # atfedilebilirlikti.
        if spec is not None:
            argumanlar = self._kapsamla(spec, argumanlar, varsayilan_kapsam)

        async def _iz(
            phase: AiToolCallPhase,
            decision: AiToolDecision,
            **ek: Any,
        ) -> None:
            await record_tool_call(
                call_id=call_id,
                phase=phase,
                user_id=actor.user_id,
                tool_name=arac_adi,
                module_keys=sorted(m for m, _ in spec.kapilar) if spec else [],
                arguments=dict(argumanlar),
                decision=decision,
                conversation_id=conversation_id,
                ai_session_id=ai_session_id,
                provider=provider,
                model=model,
                **ek,
            )

        # --- 1. Katı sözlük -------------------------------------------
        if spec is None:
            await _iz(AiToolCallPhase.started, AiToolDecision.denied_unknown_tool)
            await _iz(AiToolCallPhase.finished, AiToolDecision.denied_unknown_tool)
            return ToolError("bilinmeyen_arac")

        # --- 2. Katalog YENİDEN hesaplanır ----------------------------
        gorunur = {s.ad for s in self.katalog(actor)}
        if arac_adi not in gorunur:
            karar = (
                AiToolDecision.denied_write_role
                if spec.kapsam is ToolKapsami.SISTEM_YONETICISI
                else AiToolDecision.denied_permission
            )
            await _iz(AiToolCallPhase.started, karar)
            await _iz(AiToolCallPhase.finished, karar)
            return ToolError(
                "yazma_rolu_yok" if karar is AiToolDecision.denied_write_role else "yetkisiz_arac"
            )

        # --- 3. Şema --------------------------------------------------
        try:
            girdi = spec.girdi.model_validate(dict(argumanlar))
        except ValidationError:
            await _iz(AiToolCallPhase.started, AiToolDecision.denied_permission)
            await _iz(AiToolCallPhase.finished, AiToolDecision.denied_permission)
            return ToolError("gecersiz_argüman")

        # --- 4. Yol parametreleri (S27) -------------------------------
        try:
            cozulmus = self._cozulmus_yol(spec, girdi)
        except KapsamGerekli:
            # 🔴 `gecersiz_yol`dan AYRI bir hâl. Model kapsamı boş bıraktı ve
            # dolduracak bir bağlam da yoktu; "yol parametresi reddedildi"
            # demek operatörü yanlış yerde arattırırdı ve model onu "kayıt yok"
            # diye özetlerdi. Cümle kullanıcıya EYLEM verir: kapsam seç.
            await _iz(AiToolCallPhase.started, AiToolDecision.denied_permission)
            await _iz(AiToolCallPhase.finished, AiToolDecision.denied_permission)
            return ToolError("kapsam_gerekli")
        except YolReddedildi:
            await _iz(AiToolCallPhase.started, AiToolDecision.denied_permission)
            await _iz(AiToolCallPhase.finished, AiToolDecision.denied_permission)
            return ToolError("gecersiz_yol")

        # --- 5. Denetim, FAIL-CLOSED ----------------------------------
        try:
            await _iz(
                AiToolCallPhase.started,
                AiToolDecision.allowed,
                resolved_path=cozulmus or None,
            )
        except Exception:  # noqa: BLE001 — iz yazılamadıysa araç KOŞMAZ
            return ToolError("denetim_yazilamadi")

        # --- 6. Handler -----------------------------------------------
        baslangic = time.monotonic()
        hata: str | None = None
        transport.cagrilan_yollar.clear()
        try:
            sonuc = await spec.calistir(
                AracBaglami(spec=spec, actor=actor, transport=transport, cozulmus_yol=cozulmus),
                girdi,
            )
        except YolReddedildi:
            sonuc = ToolError("yol_kapsam_disi")
            hata = "yol_kapsam_disi"
        except httpx.HTTPError as exc:
            sonuc = ToolError("ust_kaynak_hatasi")
            hata = type(exc).__name__
        # --- 6a. BEKLENMEYEN İSTİSNA — HUNİNİN SON DALI --------------
        # 🔴 İki dal YETMİYORDU ve eksiği ölçüldü: 22 okuma handler'ı üst
        # kaynak gövdesini `k["..."]` ile ayıklayıp STRICT bir pydantic
        # modeli kurar. Bir alan NULL/eksik gelirse `ValidationError` ya da
        # `KeyError` doğar; ikisi de ne `YolReddedildi` ne `httpx.HTTPError`
        # olduğu için istisna huniden KAÇIYORDU. Kaçtığında iki hasar birden
        # oluşuyordu: (1) `started` satırı eşsiz kalıyor, `finished` HİÇ
        # yazılmıyordu — denetim tablosunda atfedilemez bir çağrı asılı
        # kalırdı; (2) `raise_app_exceptions=True` olduğu için istisna SSE
        # gövdesine kaçıyor, yanıt BAŞLAMIŞ olduğundan akış yarıda kopuyor ve
        # `tur_bitti` karesi hiç gelmiyordu.
        #
        # 🔴 Kapı HUNİDEDİR, handler'larda DEĞİL: bu dosyanın açılış
        # paragrafındaki "TEK HUNİ" doktrini gereği. 22 handler'a tek tek
        # `try` koymak, birini yazan kişinin satırı unutmasına açıktır.
        #
        # 🔴 Kod `ust_kaynak_hatasi`dır: kullanıcıya giden cümle aynıdır ve
        # kod sözlüğü (`guards.HATA_METINLERI`) KAPALI kalır. Ayrımı
        # denetim taşır — `error` alanına istisnanın TÜRÜ yazılır.
        except Exception as exc:  # noqa: BLE001 — gerekçe yukarıda
            logger.exception(
                "AI aracı '%s' handler'ının İÇİNDE beklenmeyen istisna: "
                "zarf ToolError'a düşürüldü, tur devam ediyor.",
                arac_adi,
            )
            sonuc = ToolError("ust_kaynak_hatasi")
            hata = type(exc).__name__
        # --- 6b. ALAN MASKESİ, ÇALIŞMA ANINDA (S5-c / A1) --------------
        # 🔴 Kayıt anındaki şema taraması **YETMEZ** ve bu eşdeğer bir mutant
        # DEĞİLDİR: ölçüldü, `AiPuantajHaftasi.totals` `dict[str, Any]` ve
        # `AiYetkilerim.permissions` `dict[str, str]`tir — bu iki alanın
        # ANAHTARLARI şemada YOKTUR, yalnız gövdede vardır. Ucun gövdesine bir
        # gün `wage_amount` eklenirse şema kapısı sessiz kalır, bu kapı konuşur.
        #
        # 🔴 Gövde KISMEN temizlenmez, TAMAMEN düşürülür: anahtarları ayıklamak
        # sızıntıyı yok etmez, yalnız fark edilmesini zorlaştırır.
        if not isinstance(sonuc, ToolError):
            from app.modules.ai import exposure

            sizan = exposure.yasak_anahtarlar(getattr(sonuc, "data", None))
            if sizan:
                sonuc = ToolError("alan_maskesi_ihlali")
                hata = f"alan_maskesi_ihlali:{','.join(sizan)}"
            elif hasattr(sonuc, "data"):
                # --- 6c. DEĞER MASKESİ (KVKK) -------------------------
                # 🔴 Anahtar taraması SERBEST METNİ göremez: `text` yasak bir
                # ad değildir, içine yazılmış bir TCKN/IBAN da anahtar
                # kesişimine düşmez. Burada zarf DÜŞÜRÜLMEZ, yalnız eşleşen
                # alt dize yutulur — meşru bir kaydın içeriği yüzünden aracı
                # tümden kırmak yanlış araçtır (gerekçe `exposure.py`de).
                maskeli = exposure.deger_maskesi(sonuc.data)
                if maskeli != sonuc.data:
                    sonuc = dataclasses.replace(sonuc, data=maskeli)
        if isinstance(sonuc, ToolError):
            hata = hata or sonuc.kod

        # --- 7. Denetim (`finished`) ----------------------------------
        # 🔴 `finished` satırına **fiilen çağrılan** yol yazılır (öngörülen
        # değil). Araç hiç çağrı yapmadıysa (`navigate_to`) öngörülen kalır.
        await _iz(
            AiToolCallPhase.finished,
            AiToolDecision.allowed,
            resolved_path=" ".join(transport.cagrilan_yollar) or cozulmus or None,
            http_status=transport.son_yanit_kodu,
            latency_ms=int((time.monotonic() - baslangic) * 1000),
            error=hata,
        )
        return sonuc

    def mesaj_govdesi(self, arac_adi: str, sonuc: AracSonucu) -> dict[str, Any]:
        """Modele giden **tam** gövde: zarf + **KAPSAM NOTU** (S10).

        🔴 Bu, `SIRKET_GENELI` beyanının kullanıcıya ulaşan tek yoludur. Beyan
        bir enum alanında kalsaydı `Scope` enum'unun kaderini paylaşırdı: kod
        onu hiçbir yerde okumazdı ve `GET /ai/tools` çıktısındaki etiket
        **dekoratif** olurdu.

        🔴 Bilinmeyen araç adı SESSİZCE atlanmaz — üçüncü bir not basılır. "Not
        yok" ile "kapsam iddiası yok" farklı iki şeydir.
        """
        govde = sonuc.govde()
        spec = self._sozluk.get(arac_adi)
        govde["kapsam_notu"] = (
            guards.KAPSAM_NOTU_BILINMEYEN
            if spec is None
            else guards.KAPSAM_NOTLARI[spec.kume.value]
        )
        return govde

    @staticmethod
    def _kapsamla(
        spec: ToolSpec,
        argumanlar: Mapping[str, Any],
        varsayilan_kapsam: Mapping[str, Any] | None,
    ) -> dict[str, Any]:
        """Sohbet bağlamının kapsamını **eksik** argümanlara doldurur — TEK YER.

        🔴 **MODELİN AÇIKÇA VERDİĞİ DEĞER BAĞLAMI EZER.** Koşul `is None`dır,
        `not in`dir DEĞİL: sağlayıcı şeması `strict`tir ve `girdi_semasi` HER
        alanı `required` yapar, yani model bir alanı atlayamaz — ancak `null`
        gönderebilir. Yalnız `not in` bakan bir doldurma bu yüzden **hiçbir
        zaman ateşlenmezdi** ve bu dilim dekoratif kalırdı.

        🔴 Yalnız `spec.girdi`nin **beyan ettiği** alanlar doldurulur. Girdi
        modelleri `extra="forbid"`dir; bilmediği bir alanı eklemek her çağrıyı
        `gecersiz_argüman`a düşürürdü.
        """
        birlesik = dict(argumanlar)
        if not varsayilan_kapsam:
            return birlesik
        for ad, deger in varsayilan_kapsam.items():
            if deger is None or ad not in spec.girdi.model_fields:
                continue
            if birlesik.get(ad) is None:
                birlesik[ad] = deger
        return birlesik

    @staticmethod
    def _cozulmus_yol(spec: ToolSpec, girdi: BaseModel) -> str:
        """Şablonu **çözülmüş** yola çevirir.

        🔴 Denetime ŞABLON yazılırsa `ai_tool_calls` yalan söyler ve bu dilimin
        tüm meselesi atfedilebilirlikti. Bu yüzden çözüm burada, denetimden
        ÖNCE yapılır.

        🔴 `None` bir yol parametresi **`KapsamGerekli`dir**, `kacisla`ya
        bırakılmaz: `str(None)` == `"None"` ve `"None"` yasak segment değildir —
        yani sessizce `/sites/None` istenir, uç 422 verir ve araç
        `ust_kaynak_hatasi` döner. Kullanıcı "bir hata oldu" görürdü; doğrusu
        "kapsam seçilmedi"dir.
        """
        from app.modules.ai.transport import kacisla

        # `ucler[0]` = BİRİNCİL uç. AI-0b'nin altı aracının hepsi tek uçludur
        # ve bunu `test_ai0b_yapisal.py::test_katalog_her_arac_TEK_UCLUDUR` yapısal
        # olarak kilitler (🔴 eskiden VAR OLMAYAN `test_ai0b_catalog.py`); çok uçlu bir
        # araç eklendiğinde bu satır yeniden düşünülmek zorunda kalır.
        if not spec.ucler:
            # Veri okumayan araç (`navigate_to`). Denetime yazılacak bir yol
            # yoktur; boş dize `resolved_path`i NULL bırakır.
            return ""
        desen = spec.ucler[0]
        for ad in spec.yol_parametreleri:
            deger = getattr(girdi, ad)
            if deger is None:
                raise KapsamGerekli(ad)
            desen = desen.replace("{" + ad + "}", kacisla(deger))
        return desen


__all__ = [
    "ActorContext",
    "AracBaglami",
    "BilinmeyenArac",
    "KapsamGerekli",
    "ToolKapsami",
    "ToolKumesi",
    "ToolRegistry",
    "ToolSpec",
    "kapilar_gecti",
    "guards",
]
