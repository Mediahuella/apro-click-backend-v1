"""All SQLAlchemy models — import here so Alembic sees every table."""

from database.models.company import Company
from database.models.user import User
from database.models.user_company import UserCompany
from database.models.client import Client
from database.models.audit_log import AuditLog
from database.models.registration_request import CompanyRegistrationRequest
from database.models.conversation import Conversation
from database.models.message import Message
from database.models.shopify import ShopifyAppInstallation
from database.models.shopify_order import ShopifyOrder
from database.models.price_list import PriceListUpload, ShopifyPriceSegment

__all__ = [
    "Company",
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
]
