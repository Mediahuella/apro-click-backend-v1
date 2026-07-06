"""All SQLAlchemy models — import here so Alembic sees every table."""

from database.models.company import Company
from database.models.company_address import CompanyAddress
from database.models.user import User
from database.models.user_company import UserCompany
from database.models.client import Client
from database.models.audit_log import AuditLog
from database.models.registration_request import CompanyRegistrationRequest
from database.models.conversation import Conversation
from database.models.message import Message
from database.models.shopify import ShopifyAppInstallation
from database.models.shopify_order import ShopifyOrder
from database.models.price_list import (
    B2cPriceUpload,
    PriceListUpload,
    ShopifyPriceSegment,
)
from database.models.quote_request import QuoteRequest, QuoteRequestLine

__all__ = [
    "Company",
    "CompanyAddress",
    "User",
    "UserCompany",
    "Client",
    "AuditLog",
    "CompanyRegistrationRequest",
    "Conversation",
    "Message",
    "ShopifyAppInstallation",
    "ShopifyOrder",
    "PriceListUpload",
    "ShopifyPriceSegment",
    "B2cPriceUpload",
    "QuoteRequest",
    "QuoteRequestLine",
]
