"""PARADAN TÜREYEN ORANLAR para KATEGORİSİNDEDİR (kullanıcı kararı 2026-09-19).

IZN-B4'te eski `Gorunurluk` kovaları `Hassas` etiketlerine taşındı.

## Neden ayrı bir bekçi

`test_hassas_alan_bekcisi` bir alanın ETİKETLİ OLDUĞUNU şart koşar, ama
HANGİ KATEGORİDE olduğunu ölçmez. Bu ayrım ölçülmezse bir etiketi sessizce ters
çevirmek hiçbir testi kırmaz — nitekim mutasyonla ölçüldü: `progress_pct`i
`Hassas.yok`a geri almak TÜM kümeyi yeşil bıraktı. Kararın kendisi bekçisizdi.

## Karar

Bir oran, GİRDİLERİ paradan geliyorsa PARADIR. Aksi hâlde iki kusur birden doğar:

* bedeli gizli rol bedeli göremezken ORANI görür — ve oran, gizlenen bedeli dolaylı
  olarak ele verir (kümülatif brüt biliniyorsa bedel `brüt / oran`dır).
* (IZN-B4: eski `finance` kapsamı karşılıksızdır; muhasebe artık hiçbir şeyi gizlemez, yani
  "muhasebe kendi metriğini görür" kuralı gizli kategorisi olmayan rol için geçerlidir.)

Emsal: `projects/land_share_schemas.py`deki `our_actual_pct` · `owner_actual_pct`
· `deviation_pct` — üçü de değerden türer ve ÜÇÜ DE para kategorili etiketlidir.

## Karşıt emsal (bilerek DIŞARIDA)

FİZİKSEL ilerleme (`projects` kartlarındaki `physical_progress`, BOQ'un
`progress_pct`i) şantiye günlüğünden türer, paradan DEĞİL — o `Hassas.yok`
kalır ve bu ayrım KASITLIDIR (`boq/schemas.py` docstring'i gerekçelendirir).
"""

from decimal import Decimal

from app.core.field_mask import MaskeKumeleri, etiketler, maskele
from app.core.sayfalar import HiddenCategory

#: Paradan türediği ÖLÇÜLMÜŞ oranlar: (şema, alan, üreticisinin formülü).
#: Yeni bir para-oranı eklendiğinde buraya da eklenir; liste kararın kendisidir.
PARA_ORANLARI = [
    ("contracts", "ContractListItem", "progress_pct", "kümülatif brüt ÷ sözleşme bedeli"),
    ("sales", "CollectionKpi", "collection_pct", "tahsil edilen ÷ sözleşmeye bağlanan"),
]


def _alan_etiketleri(modul_key: str, sema_adi: str, alan_adi: str) -> frozenset:
    import importlib

    mod = importlib.import_module(f"app.modules.{modul_key}.schemas")
    return etiketler(getattr(mod, sema_adi).model_fields[alan_adi])


def test_PARADAN_tureyen_oranlar_PARA_kategorisindedir() -> None:
    from app.core.field_mask import Hassas

    yanlis = {
        f"{sema}.{alan} ({formul})": sorted(k.value for k in _alan_etiketleri(modul, sema, alan))
        for modul, sema, alan, formul in PARA_ORANLARI
        if not (_alan_etiketleri(modul, sema, alan) - {Hassas.yok})
    }
    assert not yanlis, (
        "Paradan türeyen bir oran para kategorisinde DEĞİL (`Hassas.yok`/etiketsiz): bedeli "
        "gizli rol oranı görüp gizlenen bedeli dolaylı öğrenir (brüt ÷ oran = bedel). "
        f"{yanlis}"
    )


def test_oran_SOZLESME_FIYATI_gizliyken_gizlenir_gizli_olmayan_rolde_GORUNUR() -> None:
    """🔴 Etiket bir NİYETTİR; bu test onun DAVRANIŞA dönüştüğünü ölçer.

    Yalnız etiketi çakan bir test, maskenin kategori eşlemesi bozulursa yine yeşil kalırdı.
    """
    from app.modules.contracts.schemas import ContractListItem

    kayit = ContractListItem(
        id="00000000-0000-0000-0000-000000000001",
        project_id="00000000-0000-0000-0000-000000000002",
        title="A Blok Kaba İnşaat",
        contract_no="SZ-1",
        counterparty_name="Akın İnşaat",
        amount=Decimal("1000000.00"),
        start_date=None,
        end_date=None,
        progress_pct=Decimal("42.50"),
        status="active",
        is_draft=False,
    )
    sozlesme_gizli = MaskeKumeleri(varsayilan=frozenset({HiddenCategory.sozlesme_fiyat}))
    maas_gizli = MaskeKumeleri(varsayilan=frozenset({HiddenCategory.maas_kisisel}))

    assert maskele(kayit, sozlesme_gizli).progress_pct is None, "oran gizli kümede SIZDI"
    assert maskele(kayit, maas_gizli).progress_pct == Decimal("42.50"), (
        "oran ALAKASIZ kategoride YANLIŞLIKLA gizlendi"
    )
    # 🔴 POZİTİF KONTROL: hiçbir şey gizli değilken değer BOZULMAZ.
    assert maskele(kayit, MaskeKumeleri()).progress_pct == Decimal("42.50")
    # Kimlik alanı hiçbir kümede gizlenmez.
    assert maskele(kayit, sozlesme_gizli).title == "A Blok Kaba İnşaat"
