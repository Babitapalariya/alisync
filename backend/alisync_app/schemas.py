from pydantic import BaseModel, EmailStr, Field


class RegisterRequest(BaseModel):
    email: EmailStr
    country_code: str = Field(default='+91', min_length=2, max_length=5)
    country_iso: str | None = Field(default=None, min_length=2, max_length=2)
    contact_number: str = Field(min_length=7, max_length=32)
    password: str = Field(min_length=8, max_length=128)
    confirm_password: str = Field(min_length=8, max_length=128)


class ContactNumberRequest(BaseModel):
    country_code: str = Field(min_length=2, max_length=5)
    country_iso: str | None = Field(default=None, min_length=2, max_length=2)
    contact_number: str = Field(min_length=4, max_length=32)


class VerifyEmailRequest(BaseModel):
    email: EmailStr
    code: str = Field(min_length=6, max_length=6)


class ResendVerificationRequest(BaseModel):
    email: EmailStr


class PlanSelectionRequest(BaseModel):
    plan: str = Field(min_length=1, max_length=32)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class ShopRequest(BaseModel):
    shop_domain: str


class ShopifyCredentialRequest(BaseModel):
    client_id: str = Field(min_length=1, max_length=255)
    client_secret: str = Field(min_length=1, max_length=1000)


class ProductCreate(BaseModel):
    shop_id: int
    aliexpress_id: str = Field(min_length=5, max_length=64)
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    currency: str = "USD"
    image_url: str | None = None
    raw_data: dict | None = None


class ProductUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=500)
    description: str | None = None
    price: float | None = Field(default=None, ge=0)
    status: str | None = None
    track_price: bool | None = None


class MappingCreate(BaseModel):
    shop_id: int
    aliexpress_id: str
    shopify_product_id: str
    title: str | None = None


class TokenRequest(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_in: int | None = None


class AliExpressCredentialRequest(BaseModel):
    app_key: str = Field(min_length=1, max_length=255)
    app_secret: str = Field(min_length=1, max_length=1000)
    redirect_uri: str = Field(min_length=8, max_length=1000)


class AliExpressCodeRequest(BaseModel):
    code: str = Field(min_length=1, max_length=2000)
