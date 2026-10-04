"""Silme motoruna KAYIT noktası: her aile türünü ve FK olmayan bağ kancasını burada ithal ettirir.

Yeni bir dilim (SIL-B2…) kendi `silme_kaydi.py`sini yazar ve buraya bir satır ekler. İthal
yan etkidir (`tur_kaydet` / `kanca_kaydet`); bu yüzden hem önizleme/silme servisi hem
bekçi testleri bu modülü ithal eder.
"""

from app.modules.accounting import silme_kaydi as accounting_silme_kaydi
from app.modules.approvals import silme_kaydi as approvals_silme_kaydi
from app.modules.sites import silme_kaydi as sites_silme_kaydi
from app.modules.units import silme_kaydi as units_silme_kaydi

KAYITLI_MODULLER = (
    accounting_silme_kaydi,
    approvals_silme_kaydi,
    sites_silme_kaydi,
    units_silme_kaydi,
)
