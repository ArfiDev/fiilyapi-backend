"""Silme motorunun bağ grafiği (SIL-B1): FK'ler `Base.metadata`dan, FK OLMAYAN bağlar kancadan.

Her `Kenar` "üst tablonun satırı silinirse şu alt satırlar etkilenir" der:

* `cascade` — DB bağı `ON DELETE CASCADE`: alt satır birlikte gider.
* `restrict` — `RESTRICT` / `NO ACTION` (varsayılan): normalde silmeyi ENGELLER. Sistem
  Yöneticisi için alt satır da ağaca girer ve ÖNCE silinir.
* `detach` — `SET NULL` / `SET DEFAULT`: alt satır KALIR, yalnız bağı kopar.
* `linked` — FK OLMAYAN bağ (kanca): çok biçimli `document_id` / `source_id`. Alt satır
  birlikte silinir. Modül kendi kancasını `kanca_kaydet` ile kaydeder; motor modül bilmez.

Harita ELLE listelenmez: yeni bir FK eklendiği an bir sonraki önizlemede ağaçta görünür.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import ColumnElement, ForeignKeyConstraint, MetaData, Table

Iliski = Literal["cascade", "restrict", "detach", "linked"]

#: Aynı tabloya iki yoldan ulaşılırsa EN GÜÇLÜ ilişki yazılır.
ILISKI_GUCU: dict[str, int] = {"cascade": 0, "linked": 1, "restrict": 2}

#: `(alt, ust) -> koşul`: `alt` satırının `ust` satırına bağlı olduğunu söyleyen SQL ifadesi.
Kosul = Callable[[Table, Table], ColumnElement[bool]]


@dataclass(frozen=True)
class Kenar:
    ust: str
    alt: str
    iliski: Iliski
    kosul: Kosul
    #: Tek kolonlu FK ve üstün TEK kolonlu PK'sına bakıyorsa alt kolonun adı: join'siz
    #: `alt.kolon IN (üst id'leri)` kısayolu. Diğer her kenarda `None` (join'li sorgu).
    kisayol_kolonu: str | None = None
    ad: str = ""
    #: `True`: bu kenar ağaca satır katar ama silme SIRASINI belirlemez (`yurutucu.silme_sirasi`).
    #: Karşılıklı kancalar (çek ↔ ödeme, hakediş satırı → başlık) FK yönüyle zaten sıralıdır;
    #: ikinci yön sıralamaya girseydi döngü doğardı.
    sirayi_etkilemez: bool = False
    #: Silme yolunda alt satırın DURUMDAN BAĞIMSIZ kilitlenmesi (bkz. `FkDisiBag.kilit_kosulu`).
    kilit_kosulu: Kosul | None = None


@dataclass(frozen=True)
class FkDisiBag:
    """Modülün kaydettiği FK olmayan bağ: `ust` silinirse `kosul`a uyan `alt` satırları da gider."""

    ad: str
    ust_tablo: str
    alt_tablo: str
    kosul: Kosul
    sirayi_etkilemez: bool = False
    #: `kosul` alt satırı YALNIZ bir duruma bağlı seçiyorsa (ör. "yalnız onaylı başlık"), o durum
    #: kilitsiz okunduğunda yarışta DEĞİŞEBİLİR (onay bekleyen başlık silme sürerken onaylanır).
    #: Bu koşul durumsuz bağdır (ör. satır → başlık): silme yolu ilgili alt satırları durumuna
    #: BAKMADAN `FOR UPDATE` ile kilitler, sonra `kosul`u kilit ALTINDA yeniden değerlendirir.
    kilit_kosulu: Kosul | None = None


_KANCALAR: list[FkDisiBag] = []


def kanca_kaydet(kanca: FkDisiBag) -> None:
    """Idempotent: aynı adlı kanca ikinci kez kaydedilmez (modül birden çok kez ithal edilir)."""
    if all(k.ad != kanca.ad for k in _KANCALAR):
        _KANCALAR.append(kanca)


def kayitli_kancalar() -> tuple[FkDisiBag, ...]:
    return tuple(_KANCALAR)


def _ondelete_iliskisi(ondelete: str | None) -> Iliski:
    kural = (ondelete or "NO ACTION").upper()
    if kural == "CASCADE":
        return "cascade"
    if kural in ("SET NULL", "SET DEFAULT"):
        return "detach"
    return "restrict"


def _fk_kosulu(kisit: ForeignKeyConstraint) -> Kosul:
    ciftler = [(e.parent.name, e.column.name) for e in kisit.elements]

    def kosul(alt: Table, ust: Table) -> ColumnElement[bool]:
        parcalar = [alt.c[a] == ust.c[u] for a, u in ciftler]
        sonuc = parcalar[0]
        for parca in parcalar[1:]:
            sonuc = sonuc & parca
        return sonuc

    return kosul


def fk_kenarlari(metadata: MetaData) -> list[Kenar]:
    kenarlar: list[Kenar] = []
    for alt in metadata.tables.values():
        for kisit in alt.foreign_key_constraints:
            ust = kisit.referred_table
            kisayol = None
            ust_pk = [c.name for c in ust.primary_key.columns]
            if len(kisit.elements) == 1 and ust_pk == [kisit.elements[0].column.name]:
                kisayol = kisit.elements[0].parent.name
            kenarlar.append(
                Kenar(
                    ust=ust.name,
                    alt=alt.name,
                    iliski=_ondelete_iliskisi(kisit.ondelete),
                    kosul=_fk_kosulu(kisit),
                    kisayol_kolonu=kisayol,
                    ad=kisit.name or "",
                )
            )
    return kenarlar


def tum_kenarlar(metadata: MetaData) -> list[Kenar]:
    """FK kenarları + kayıtlı kancalar."""
    kenarlar = fk_kenarlari(metadata)
    for kanca in _KANCALAR:
        kenarlar.append(
            Kenar(
                ust=kanca.ust_tablo,
                alt=kanca.alt_tablo,
                iliski="linked",
                kosul=kanca.kosul,
                ad=kanca.ad,
                sirayi_etkilemez=kanca.sirayi_etkilemez,
                kilit_kosulu=kanca.kilit_kosulu,
            )
        )
    return kenarlar
