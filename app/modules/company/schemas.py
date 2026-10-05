import uuid
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.core.field_mask import Hassas
from app.modules.company.models import Company

_HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"


class CompanyUpdate(BaseModel):
    """Kismi guncelleme — tum alanlar opsiyonel. Gonderilmeyen alan degismez."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, max_length=200)
    # IZN-B4: şirket KÜNYESİ kişisel veri değil, belge başlıklarında HERKES kullanır → `yok`.
    tax_number: Annotated[str | None, Hassas.yok] = Field(default=None, max_length=50)
    tax_office: str | None = Field(default=None, max_length=100)
    trade_registry_no: str | None = Field(default=None, max_length=100)
    kep_address: Annotated[str | None, Hassas.yok] = Field(default=None, max_length=255)
    phone: Annotated[str | None, Hassas.yok] = Field(default=None, max_length=50)
    email: EmailStr | None = None
    website: str | None = Field(default=None, max_length=255)
    address: Annotated[str | None, Hassas.yok] = None
    brand_color: str | None = Field(default=None, pattern=_HEX_COLOR)
    gib_integration_code: str | None = Field(default=None, max_length=100)
    earsiv_portal: str | None = Field(default=None, max_length=255)
    default_vat_rate: Annotated[Decimal | None, Hassas.yok] = Field(default=None, ge=0, le=100)
    auto_einvoice: bool | None = None


class CompanyRead(BaseModel):
    """Sirket okuma modeli. Logo bytea'si ASLA burada donmez; yalnizca has_logo + logo_url."""

    id: uuid.UUID
    name: str | None
    tax_number: Annotated[str | None, Hassas.yok]
    tax_office: str | None
    trade_registry_no: str | None
    kep_address: Annotated[str | None, Hassas.yok]
    phone: Annotated[str | None, Hassas.yok]
    email: str | None
    website: str | None
    address: Annotated[str | None, Hassas.yok]
    brand_color: str
    gib_integration_code: str | None
    earsiv_portal: str | None
    default_vat_rate: Annotated[Decimal, Hassas.yok]
    auto_einvoice: bool
    has_logo: bool
    logo_url: str

    @classmethod
    def from_model(cls, company: Company) -> "CompanyRead":
        return cls(
            id=company.id,
            name=company.name,
            tax_number=company.tax_number,
            tax_office=company.tax_office,
            trade_registry_no=company.trade_registry_no,
            kep_address=company.kep_address,
            phone=company.phone,
            email=company.email,
            website=company.website,
            address=company.address,
            brand_color=company.brand_color,
            gib_integration_code=company.gib_integration_code,
            earsiv_portal=company.earsiv_portal,
            default_vat_rate=company.default_vat_rate,
            auto_einvoice=company.auto_einvoice,
            has_logo=company.logo_data is not None,
            logo_url="/company/logo",
        )
