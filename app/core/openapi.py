COMMON_ERROR_RESPONSES = {
    403: {"description": "Yetkisiz işlem"},
    404: {"description": "Kayıt bulunamadı"},
}

#: Silme kapısının 403 gövdesi (SIL-B1): TEK metin; `require_system_admin` ve OpenAPI açıklaması
#: aynı sabiti kullanır, sapmazlar.
SYSTEM_ADMIN_ONLY_DETAIL = "Bu işlemi yalnızca Sistem Yöneticisi yapabilir"

#: HER `DELETE` ucunun `responses`ına girer: silme yalnız Sistem Yöneticisi'nindir (istisna YOK;
#: "kendi taslağı", "sahibi", `admin` seviyesi ya da `full` bir kapı DEĞİLDİR). Yönlendirici
#: düzeyindeki genel "Yetkisiz işlem" 403 açıklamasını bu uçta ezer.
DELETE_403_YANITI = {403: {"description": SYSTEM_ADMIN_ONLY_DETAIL}}
