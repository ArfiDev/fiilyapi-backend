import uuid


class DomainError(Exception):
    """Alan kuralı ihlali. Router katmanı bunu uygun HTTP koduna çevirir."""


class PermissionLockedError(DomainError):
    """system_admin rolünün izinleri değiştirilemez (kilitlenme koruması, spec §5.0)."""


class NotFoundError(DomainError):
    """İstenen kayıt bulunamadı — router katmanı 404'e çevirir."""


class ProjectTypeMismatchError(DomainError):
    """Tip uzantısı proje tipiyle uyuşmuyor (Alt-Proje 2 P1 spec §3.5) — 422."""


class ProjectValidationError(DomainError):
    """Proje oluşturma iş kuralı ihlali (spec §3.6: zorunluluk, tarih sırası, endeks) — 422.

    Pydantic alan doğrulamasının ötesinde, taslak-farkındalıklı ve alanlar-arası
    kurallar için. Mesaj Türkçe ve kullanıcıya gösterilir.
    """


class SiteValidationError(DomainError):
    """Şantiye formu iş kuralı ihlali (spec §5.1, §7.2) — 422.

    `ProjectValidationError` deseninin aynısı ve aynı ayrımı taşır: Pydantic'in
    alan doğrulamasının ötesinde, TASLAK-FARKINDALIKLI ve alanlar-arası kurallar
    için. Kural: tutarlılık kuralları her zaman, zorunluluk kuralları yalnız
    taslak-dışında koşar — böylece yarım kalmış taslak geçersiz değil yalnız
    eksik veri saklar. Mesaj Türkçe ve doğrudan kullanıcıya gösterilir.
    """


class InventoryValidationError(DomainError):
    """Stok hareketi alanlar-arasi is kurali ihlali (STOK-BOLUM) — 422.

    🔴 Bu sinifin VAROLUS SEBEBI: kural IKI TABLOYA birden bakar
    (`warehouses.site_id` ↔ `sections.site_id` / `boq_items.site_id`), yani ne
    DB `CHECK`inde ne Pydantic semasinda yasayabilir. `NotFoundError`dan AYRI
    durur cunku referans edilen varlik VARDIR ve GORUNURDUR — ihlal edilen sey
    iki alan ARASINDAKI iliskidir, yani govdenin duzeltilebilir bir kuralidir.
    Mesaj Turkce ve dogrudan kullaniciya gosterilir.
    """


class DuplicateError(DomainError):
    """Benzersiz olması gereken bir değer zaten kayıtlı (spec §3.2) — router 409'a çevirir.

    Servis, IntegrityError'a düşmeden ÖNCE açık bir SELECT ile bunu fırlatır ki
    kullanıcıya alanına özel Türkçe mesaj verilebilsin. IntegrityError → 409 handler'ı
    yarış durumu emniyet ağı olarak KALIR.
    """


class SectionTypeTakenError(DuplicateError):
    """Bolum tipi adi zaten var (BLF-B1) — 409 + `existing {id, name}`.

    `DaysLockedError` deseni: duz `DuplicateError` govdesi yalniz `detail` tasir, istemci
    adi metinden ayiklamak zorunda kalirdi. Mevcut satirin kimligi yapisal alanla gider.
    """

    def __init__(self, message: str, *, existing_id: uuid.UUID, existing_name: str) -> None:
        super().__init__(message)
        self.existing_id = existing_id
        self.existing_name = existing_name


class UnitValidationError(DomainError):
    """Blok/ünite iş kuralı ihlali (Alt-Proje 2 P3 spec §4.5, §7.11) — 422.

    `BoqGroupSiteMismatchError` deseninin aynısı: DB `CHECK` ile zorlanamayan ya da
    zorlansa bile kullanıcıya Türkçe mesaj veremeyecek kurallar (şantiye sayısına
    bağlı blok ataması, `net > brüt`) tek yazma yolunda servis korkuluğuyla tutulur.
    """


class CustomerValidationError(DomainError):
    """Alıcı (müşteri) tip/kimlik kuralı ihlali (P8 spec §2) — 422.

    `UnitValidationError` deseninin aynısı: DB `CHECK` ile zorlanamayan
    (`person` -> TCKN, `company` -> VKN) ve zorlansa bile kullanıcıya Türkçe
    mesaj veremeyecek kural, tek yazma yolunda servis korkuluğuyla tutulur.
    Pydantic'te DEĞİL: PATCH kısmi gövde gönderir, kural ancak DB'deki kayıtla
    birleştirilmiş değerler üzerinde anlamlıdır.
    """


class PersonnelValidationError(DomainError):
    """Personel kaynak/taşeron uyuşmazlığı (puantaj spec §2) — 422.

    `CustomerValidationError` deseninin aynısı: DB `CHECK`
    (`ck_personnel_subcontractor_only_for_subcontractor_source`) VARDIR ama ihlali
    409 "Veri bütünlüğü hatası" verirdi; kullanıcı hangi alanı düzelteceğini
    öğrenemezdi. Servis korkuluğu DB'ye düşmeden Türkçe 422 atar, CHECK ikinci
    katman olarak kalır. Pydantic'te DEĞİL: PATCH kısmi gövde gönderir, kural
    ancak DB'deki kayıtla birleştirilmiş değerler üzerinde anlamlıdır.
    """


class RelatedRecordsExistError(DomainError):
    """Silinmek istenen kayda bağlı alt kayıtlar var (Alt-Proje 2 P3 spec §7.9) — 409.

    Bu bir ÇAKIŞMA'dır: kullanıcının yetkisi vardır ama kaydın durumu silmeye elverişli
    değildir ("yetkin yok" 403'ünden ayrı kod).

    Cascade'e KAYILMAZ: ünitesi olan blok silinirse 24 daire tek istekte gider ve
    geri alınamaz. DB tarafındaki `ON DELETE RESTRICT` (spec §4.2) ikinci katmandır.
    """


class BoqGroupSiteMismatchError(DomainError):
    """Poz kaleminin bağlanmak istediği grup, hedef şantiyeye ait değil

    (Alt-Proje 2 P4 spec §3.3 invariant 1, §5.4) — 422. DB'de bileşik FK
    açılmadığı için (P1 §3.5 gerekçesi) tek yazma yolunda servis korkuluğu.
    """


class DocumentValidationError(DomainError):
    """Belge/klasör kapsam kuralı ihlali (belge çekirdeği spec §2, §3) — 422.

    `UnitValidationError`/`CustomerValidationError` deseninin aynısı: DB ile
    zorlanamayan (klasörün `site_id`/`parent_id`sinin AYNI kapsamda olması)
    kurallar tek yazma yolunda servis korkuluğuyla tutulur. 404 DEĞİL: istenen
    kaynak projedir, `site_id`/`parent_id` gövdedeki düzeltilebilir ALAN
    DEĞERLERİDİR; 404 verilseydi ekran "proje yok" ile "şantiye başka projede"yi
    ayırt edemezdi.
    """


class ProcurementValidationError(DomainError):
    """Satınalma iş kuralı ihlali (SA spec §3) — 422.

    `DocumentValidationError`/`PersonnelValidationError` deseninin aynısı: DB
    `CHECK` ile zorlanamayan ya da zorlansa bile kullanıcıya Türkçe mesaj
    veremeyecek kurallar tek yazma yolunda servis korkuluğuyla tutulur. İki
    kullanıcısı vardır:

    * `submit` engelleri — TASLAK-FARKINDALIKLI zorunluluklar (`validation.
      submit_blockers`); taslakta gevşek, onaya gönderirken sıkı. Tüm engeller
      TEK gövdede birleşir, çünkü uzun bir formda eksikleri birer birer
      keşfettirmek kabul edilemez.
    * teklifin nakliye kuralı — PATCH kısmi gövde gönderir, kural ancak
      DB'deki kayıtla BİRLEŞTİRİLMİŞ değerler üzerinde anlamlıdır; bu yüzden
      Pydantic'te değil serviste koşar.

    404 DEĞİL: istenen kaynak vardır ve görünür, ihlal eden şey düzeltilebilir
    ALAN DEĞERLERİDİR. 409 da DEĞİL: engel kaydın DURUMU değil İÇERİĞİDİR.
    """


class ApprovalNotAllowedError(DomainError):
    """Onay YETKİSİ tutar eşiğini karşılamıyor (SA spec §3, §7 S2) — 403.

    Yetki engelidir ama onaya bakar (silme kapısı `require_system_admin`dir).

    409 DEĞİL: kayıt DOĞRU durumdadır (`pending_approval`), engelleyen şey
    AKTÖRÜN SEVİYESİDİR — başka bir kullanıcı aynı anda onaylayabilir. 422 da
    değildir: gövdede düzeltilecek bir alan yoktur.
    """


class PayrollValidationError(DomainError):
    """Bordro para kuralı ihlali (İK-3 spec §6/1, §6/3) — 422.

    `ProcurementValidationError`/`PersonnelValidationError` deseninin aynısı ve
    aynı sebeple onlardan AYRI: DB `CHECK` ile zorlanamayan kurallar tek yazma
    yolunda servis korkuluğuyla tutulur. İki kullanıcısı vardır:

    * **S3 invariantı** (`banka + elden = net`) — DB CHECK'i OLAMAZ çünkü üç
      kolonun da NULL olabildiği `uncomputed` durumda CHECK ya S4'ü kırar ya da
      hiçbir şey zorlamaz (`payroll/models.py` gerekçesi). Ayrıca ihlali
      409/500 olarak dönmek kullanıcıya hangi kuruşun kaydığını söylemezdi.
    * **fail-closed hesap engelleri** — oran seti yokken brüt override'ı,
      neti hesaplanmamış satırda bölüşüm.

    409 DEĞİL: kayıt DOĞRU durumdadır (`pending`), engelleyen şey GÖVDEDEKİ
    düzeltilebilir alan değerleridir. 404 da değildir: satır vardır ve görünür.
    """


class EquipmentValidationError(DomainError):
    """Makine & ekipman iş kuralı ihlali (MK-1 spec §3) — 422.

    `PayrollValidationError`/`ProcurementValidationError` deseninin aynısı ve
    aynı sebeple onlardan AYRI: DB `CHECK` ile zorlanamayan ya da zorlansa bile
    kullanıcıya Türkçe mesaj veremeyecek kurallar tek yazma yolunda servis
    korkuluğuyla tutulur. Kullanıcıları:

    * **K2 — koşullu zorunluluk** (`ownership == owned` iken `purchase_amount`).
      CHECK OLAMAZ: kural kısmi PATCH'te ancak DB'deki kayıtla BİRLEŞTİRİLMİŞ
      değerler üzerinde anlamlıdır.
    * (T4) K11 `hours` sunucu hesabının gövde kuralları ve K12 günlük tavan.

    404 DEĞİL: kayıt vardır ve görünür, ihlal eden şey düzeltilebilir ALAN
    DEĞERLERİDİR. 409 da DEĞİL: engel kaydın DURUMU değil İÇERİĞİDİR.
    """


class InvoicingValidationError(DomainError):
    """Fatura gövde kuralı ihlali (FAT-1 spec §2, §5) — 422.

    `EquipmentValidationError`/`ProcurementValidationError` deseninin aynısı ve
    aynı sebeple onlardan AYRI: DB `CHECK` ile zorlanamayan ya da zorlansa bile
    kullanıcıya Türkçe mesaj veremeyecek kurallar tek yazma yolunda servis
    korkuluğuyla tutulur. Kullanıcıları:

    * **`ck_invoices_single_party` / `ck_invoices_single_source`** — CHECK'ler
      VARDIR (T1) ama ihlalleri 409 "Veri bütünlüğü hatası" olarak dönerdi ve
      kullanıcı hangi iki alanı birden doldurduğunu öğrenemezdi. DB SON
      savunmadır; kullanıcıya 500/409 gitmez.
    * **numaranın sahibi** (§4/S5): giden faturada istemci numara gönderemez,
      gelen faturada göndermek ZORUNDADIR.
    * **kesinti oranları toplamı** (`validation.body_blockers`): %100'ü aşan
      toplam `tax_base`i negatife düşürür ve DB CHECK'i ancak KDV'den sonra,
      okunamaz bir hatayla yakalardı.
    * **gelen faturada PATCH kapsamı**: satıcının belgesinin tutarı bizim
      düzeltebileceğimiz bir alan değildir.

    404 DEĞİL: kayıt vardır ve görünür, ihlal eden şey düzeltilebilir ALAN
    DEĞERLERİDİR. 409 da DEĞİL: engel kaydın DURUMU değil İÇERİĞİDİR.
    """


class TreasuryValidationError(DomainError):
    """Hazine gövde kuralı ihlali (HZ-1 spec §2.1) — 422.

    `InvoicingValidationError`/`EquipmentValidationError` deseninin aynısı ve
    aynı sebeple onlardan AYRI: DB `CHECK` ile zorlanabilse bile kullanıcıya
    Türkçe mesaj veremeyecek kurallar tek yazma yolunda servis korkuluğuyla
    tutulur. Bugünkü tek kullanıcısı:

    * **`ck_bank_accounts_cash_has_name`** — CHECK VARDIR (T1) ve SON savunma
      olarak kalır; ihlali 409 "Veri bütünlüğü hatası" olarak dönerdi ve
      kullanıcı Kasa satırında hangi alanı doldurması gerektiğini öğrenemezdi.
      Pydantic'te DEĞİL: PATCH kısmi gövde gönderir, kural ancak DB'deki kayıtla
      BİRLEŞTİRİLMİŞ değerler üzerinde anlamlıdır (tipi `cash`e çevirip adı
      göndermemek de ihlaldir).

    404 DEĞİL: kayıt vardır ve görünür (hesap şirket genelidir, K3), ihlal eden
    şey düzeltilebilir ALAN DEĞERLERİDİR. 409 da DEĞİL: engel kaydın DURUMU
    değil İÇERİĞİDİR.
    """


class AccountingValidationError(DomainError):
    """Yevmiye fişi gövde kuralı ihlali (MU-1 spec §4, K1) — 422.

    `TreasuryValidationError`/`InvoicingValidationError` deseninin aynısı ve aynı
    sebeple onlardan AYRI: DB `CHECK` ile zorlanamayan ya da zorlansa bile
    kullanıcıya Türkçe mesaj veremeyecek kurallar tek yazma yolunda servis
    korkuluğuyla tutulur. Tek kullanıcısı **K1'in servis katmanıdır**
    (`accounting/validation.py`):

    * **`Σ debit ≠ Σ credit`** — `ck_journal_entries_posting_balanced` VARDIR ama
      yalnız BAŞLIK toplamlarına bakar ve yalnız DEFTERE GİRENLERDE ısırır
      (`posted` + `reversed`, TB6 T2); ihlali ayrımsız bir 409 "Veri bütünlüğü
      hatası" olarak dönerdi ve kullanıcı hangi kuruşun kaydığını öğrenemezdi.
    * **`len(lines) < 2`** — bir CHECK BAŞKA SATIRLARIN sayısını GÖREMEZ.
    * **yaprak olmayan hesaba satır** (§4c) — hiyerarşi kodun içinde taşındığı
      için (K4) DB'de zorlanacak bir FK yoktur.

    Üç engel TEK gövdede birleşir: çok satırlı bir fişte eksikleri birer birer
    keşfettirmek kabul edilemez (FAT-1 `_raise_blockers` dersi).

    404 DEĞİL: kayıt vardır ve görünür, ihlal eden şey düzeltilebilir ALAN
    DEĞERLERİDİR. 409 da DEĞİL: engel kaydın DURUMU değil İÇERİĞİDİR — durum
    engelleri (`ConflictError`) ayrı bir sınıftır ve `transitions.py`den gelir.
    """


class ApprovalValidationError(DomainError):
    """Onay zinciri govde kurali ihlali (OK-1A K2) — 422.

    `AccountingValidationError`/`ProcurementValidationError` deseninin aynisi ve
    ayni sebeple onlardan AYRI: DB `CHECK` ile zorlanamayan kurallar tek yazma
    yolunda servis korkuluguyla tutulur. Bugunku tek kullanicisi RET
    GEREKCESIDIR (zorunlu metin + `FREE_TEXT_MAX_LENGTH` tavani).

    Gerekce neden Pydantic'te DEGIL: ret uclari UC AYRI evrak ailesinde yasar ve
    ikisinin govdesi bugun `reason`u OPSIYONEL tasir (`RejectBody`). Kurali
    semaya yazsaydik uc semayi birden sikilastirmak gerekirdi ve biri
    unutuldugunda kapi O UCTAN atlatilirdi (BC dersi). Motor tek hunidir.

    403 DEGIL: aktor uygundur, duzeltilecek sey GOVDEDEDIR.
    409 da DEGIL: zincir DOGRU durumdadir, engel ICERIKTIR.
    """


class RoleValidationError(DomainError):
    """Rol sayfa izni gövde kuralı ihlali (IZN-B2) — 422.

    DB `CHECK` yalnız "onay görünmeyen sayfada olmaz" der; "bu sayfada onay eylemi var mı"
    kuralı katalogdadır (`onay_var`) ve DB'ye sığmaz — tek yazma yolunda (`PUT /roles/{id}/pages`)
    servis korkuluğuyla tutulur. 403 DEĞİL: aktör yetkilidir, düzeltilecek şey GÖVDEDEDİR.
    Mesaj Türkçe ve doğrudan kullanıcıya gösterilir.
    """


class UserAccessValidationError(DomainError):
    """Kullanıcı erişimi (ana rol + proje ekibi) gövde kuralı ihlali (IZN-B3) — 422.

    Kural DB'ye sığmaz ya da DB hatası anlamlı Türkçe mesaj veremez: "Tüm projeler" işaretliyken
    ekip satırı olmaz, aynı proje iki kez gelmez, bilinmeyen proje/rol/disiplin, proje rolü olarak
    Sistem Yöneticisi. 403 DEĞİL: aktör yetkilidir, düzeltilecek şey GÖVDEDEDİR.
    """


class OfferValidationError(DomainError):
    """Teklif gövde/iş kuralı ihlali (TKL-B4.2) — 422.

    `EquipmentValidationError` deseninin aynısı: DB `CHECK` ile zorlanabilse bile (elle B.F.
    ⇒ maliyet dolu, `tuik` ⇒ endeks türü dolu) kullanıcıya Türkçe mesaj veremeyen ve KISMİ
    PATCH'te yalnız DB'deki kayıtla BİRLEŞTİRİLMİŞ değerler üzerinde anlamlı kurallar tek
    yazma yolunda servis korkuluğuyla tutulur. 409 DEĞİL: engel kaydın DURUMU değil
    İÇERİĞİDİR (durum engeli `ConflictError`dır).

    İsteğe bağlı `errors` (TKL-B6.8b BD-4): alan konumlu YAPISAL hata listesi
    (`{"loc": [...], "message": str}`). Doluysa 422 gövdesine `errors` olarak EKLENİR,
    `detail` metni AYNEN kalır; boşsa gövde bugünkü gibidir (yalnız `detail`).
    """

    def __init__(self, message: str = "", errors: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, object]] = list(errors) if errors else []


class ConflictError(DomainError):
    """Durum makinesi / iş kuralı çakışması — 409 (P7 hakediş spec §7, §9.2, §9.7).

    `DuplicateError`'dan (benzersizlik ihlali) kasıtlı olarak AYRI: burada
    çakışan şey bir alan değeri değil, kaydın MEVCUT DURUMU — açık hakediş
    varken ikincisinin açılamaması (D8/`OPEN_PAYMENT_EXISTS`), taslak
    dışındayken yazılamaması (`INVALID_STATUS_TRANSITION`), onaylanmış/ödenmiş
    kaydın silinememesi (`PAYMENT_NOT_DELETABLE`) gibi. `RelatedRecordsExistError`
    (bağlı alt kayıt) semantiğine de UYMADIĞI için P7 ile birlikte açılır.

    İsteğe bağlı `errors` (TKL-B6.9 R5): `OfferValidationError.errors` ile AYNI biçim
    (`{"loc": [...], "message": str}`); doluysa 409 gövdesine EKLENİR, `detail` AYNEN kalır;
    boşsa gövde bugünkü gibidir (yalnız `detail`).
    """

    def __init__(self, message: str = "", errors: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, object]] = list(errors) if errors else []


class EarnedValueValidationError(DomainError):
    """Planlama (earned value) govde kurali ihlali (PLN-B1) — 422.

    Kayit vardir ve gorunur; ihlal edilen sey duzeltilebilir ALAN DEGERLERIDIR
    (tarih araligi, bant sirasi, taslak olmayan revizyona yazma DEGIL — o durum
    engelidir ve `ConflictError`dir). Mesaj Turkce ve dogrudan kullaniciya gosterilir.

    Istege bagli `errors` (KAT-B1): `OfferValidationError.errors` ile AYNI bicim
    (`{"loc": [...], "message": str}`); doluysa 422 govdesine EKLENIR, `detail` AYNEN kalir;
    bossa govde bugunku gibidir (yalniz `detail`).
    """

    def __init__(self, message: str = "", errors: list[dict[str, object]] | None = None) -> None:
        super().__init__(message)
        self.errors: list[dict[str, object]] = list(errors) if errors else []
