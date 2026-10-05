"""Onay motorunun korkuluk METINLERI — TEK KOPYA.

Testler durum kodunu bu sabitlerle birlikte iddia eder: yalniz "403 geldi"
demek, yanlis sebeple 403 donen bir uygulamada da YESIL kalirdi (bu turda
olculen sahte-yesil hâllerinden biri).

Mesajlar kullaniciya gorunur ve Turkcedir; hicbiri TUTAR, kimlik ya da baska
gizli deger TASIMAZ.
"""

__all__ = [
    "APPROVAL_ROLE_MISSING",
    "CHAIN_ALREADY_EXISTS",
    "CHAIN_COMPLETED",
    "NO_OPEN_CHAIN",
    "OWN_DOCUMENT",
    "REJECT_REASON_REQUIRED",
    "REJECT_REASON_TOO_LONG",
    "SEPARATION_OF_DUTIES",
    "STEP_NOT_CURRENT",
    "step_roles_unassigned",
]

# --- 409: kaydin DURUMU uygun degil ---
# IZN-B3b (KARAR, kullanici 2026-10-04): adim rolunun projede sahibi yoksa gonderim ENGELLENIR.
CHAIN_ALREADY_EXISTS = "Bu evrak icin zaten acik bir onay zinciri var"
NO_OPEN_CHAIN = "Bu evragin acik bir onay zinciri yok"
CHAIN_COMPLETED = "Onay zinciri tamamlanmis"
STEP_NOT_CURRENT = "Bu adim siradaki onay adimi degil"

# --- 403: AKTOR uygun degil ---
APPROVAL_ROLE_MISSING = "Bu projede bu onay adimi icin gereken role sahip degilsiniz"
OWN_DOCUMENT = "Kendi olusturdugunuz evrakin onay adimini onaylayamazsiniz"
SEPARATION_OF_DUTIES = "Ayni evrakin ikinci onay adimini onaylayamazsiniz"

# --- 422: GOVDE uygun degil ---
REJECT_REASON_REQUIRED = "Ret gerekcesi zorunludur"
REJECT_REASON_TOO_LONG = "Ret gerekcesi cok uzun"


def step_roles_unassigned(role_names: list[str]) -> str:
    """409 metni: zincirin bir ya da daha fazla adim rolu belgenin projesinde atanmamis."""
    return f"Bu projede {', '.join(role_names)} atanmamış; önce Ayarlar > Kullanıcılar'dan atayın"
