from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func

from .database import Base


class User(Base):
    __tablename__ = "app_users"
    id = Column(Integer, primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(500), nullable=False)
    full_name = Column(String(255))
    contact_number = Column(String(32), unique=True, index=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    verification_code_hash = Column(String(64))
    verification_expires_at = Column(DateTime(timezone=True))
    selected_plan = Column(String(32))
    subscription_catalog_initialized = Column(Boolean, nullable=False, default=False)
    subscription_status = Column(String(20), nullable=False, default="none")
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Shop(Base):
    __tablename__ = "app_shops"
    __table_args__ = (UniqueConstraint("user_id", "shop_domain", name="uq_app_user_shop"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_domain = Column(String(255), nullable=False, index=True)
    access_token = Column(String(500))
    access_token_expires_at = Column(DateTime(timezone=True))
    shopify_auth_method = Column(String(32))
    shopify_refresh_token_encrypted = Column(Text)
    shopify_refresh_token_expires_at = Column(DateTime(timezone=True))
    shopify_client_id = Column(String(255))
    shopify_client_secret_encrypted = Column(Text)
    scopes = Column(String(1000))
    is_active = Column(Boolean, nullable=False, default=True)
    installed_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    # Operational fields used by the full Shopify feature set.
    scope = Column(String(1000))
    uninstalled_at = Column(DateTime(timezone=True))
    subscription_status = Column(String(20), default="none", nullable=False)
    subscription_id = Column(String(64))
    plan_name = Column(String(100))
    trial_ends_at = Column(DateTime(timezone=True))


class AliExpressToken(Base):
    __tablename__ = "app_aliexpress_tokens"
    __table_args__ = (UniqueConstraint("user_id", name="uq_app_aliexpress_user"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    access_token = Column(String(1000), nullable=False)
    refresh_token = Column(String(1000))
    expires_in = Column(Integer)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    imported_at = Column(DateTime(timezone=True), server_default=func.now())


class AliExpressCredential(Base):
    __tablename__ = "app_aliexpress_credentials"
    __table_args__ = (UniqueConstraint("user_id", name="uq_app_aliexpress_credentials_user"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    app_key = Column(String(255), nullable=False)
    app_secret_encrypted = Column(Text, nullable=False)
    redirect_uri = Column(String(1000), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Product(Base):
    __tablename__ = "app_products"
    __table_args__ = (
        UniqueConstraint("shop_id", "aliexpress_id", name="uq_app_shop_product"),
        UniqueConstraint("shop_id", "shopify_product_id", name="uq_app_shop_shopify_product"),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("app_shops.id", ondelete="CASCADE"), nullable=False, index=True)
    aliexpress_id = Column(String(64), nullable=False, index=True)
    shopify_product_id = Column(String(64), index=True)
    title = Column(String(500), nullable=True)
    description = Column(Text)
    price = Column(Float)
    currency = Column(String(10), default="USD")
    image_url = Column(String(1000))
    status = Column(String(30), default="draft", nullable=False)
    track_price = Column(Boolean, default=True, nullable=False)
    raw_data = Column(JSON)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    imported_at = Column(DateTime(timezone=True), server_default=func.now())
    original_title = Column(String(500))
    custom_title = Column(String(500))
    original_price = Column(String(32))
    custom_price = Column(String(32))
    original_description = Column(Text)
    custom_description = Column(Text)
    main_image = Column(String(1000))
    all_images = Column(JSON)
    store_name = Column(String(200))
    avg_rating = Column(Float)
    custom_rating = Column(Float)
    review_count = Column(Integer)
    orders = Column(Integer)
    sku_count = Column(Integer)
    skus = Column(JSON)
    shopify_status = Column(String(20), default="draft")
    price_increase = Column(Float, default=0.0, nullable=False)
    price_percentage = Column(Float, default=0.0, nullable=False)
    shipping_cost = Column(String(32))
    shipping_method = Column(String(100))
    total_stock = Column(Integer)
    last_shipment_fetch = Column(DateTime(timezone=True))
    price_mode = Column(String(20), default="auto", nullable=False)
    replacement_aliexpress_id = Column(String(64), index=True)
    is_dead_listing = Column(Boolean, default=False, nullable=False)
    supplier_status = Column(String(40), default="active", nullable=False, index=True)
    verification_attempts = Column(Integer, default=0, nullable=False)
    last_verification_at = Column(DateTime(timezone=True))
    last_successful_sync_at = Column(DateTime(timezone=True))
    last_failure_reason = Column(Text)
    inventory_protected = Column(Boolean, default=False, nullable=False)
    auto_inventory_sync = Column(Boolean, default=True, nullable=False)
    auto_restore_inventory = Column(Boolean, default=False, nullable=False)


class Mapping(Base):
    __tablename__ = "app_product_mappings"
    __table_args__ = (
        UniqueConstraint("shop_id", "aliexpress_id", name="uq_app_shop_mapping"),
        UniqueConstraint("shop_id", "shopify_product_id", name="uq_app_shop_shopify_mapping"),
    )
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("app_shops.id", ondelete="CASCADE"), nullable=False, index=True)
    aliexpress_id = Column(String(64), nullable=False, index=True)
    shopify_product_id = Column(String(64), nullable=False, index=True)
    title = Column(String(500))
    track_price = Column(Boolean, default=True, nullable=False)
    price_adjustment = Column(Float, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    shopify_product_title = Column(String(500))
    price_mode = Column(String(20), default="auto", nullable=False)
    price_increase = Column(Float, default=0.0, nullable=False)
    price_percentage = Column(Float, default=0.0, nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    is_dead_listing = Column(Boolean, default=False, nullable=False)
    custom_title = Column(String(500))
    custom_description = Column(Text)
    custom_rating = Column(Float)
    replacement_aliexpress_id = Column(String(64), index=True)
    supplier_status = Column(String(40), default="active", nullable=False, index=True)
    verification_attempts = Column(Integer, default=0, nullable=False)
    last_verification_at = Column(DateTime(timezone=True))
    last_successful_sync_at = Column(DateTime(timezone=True))
    last_failure_reason = Column(Text)
    inventory_protected = Column(Boolean, default=False, nullable=False)
    auto_inventory_sync = Column(Boolean, default=True, nullable=False)
    auto_restore_inventory = Column(Boolean, default=False, nullable=False)


class PendingImport(Base):
    __tablename__ = "app_pending_imports"
    __table_args__ = (UniqueConstraint("shop_id", "aliexpress_id", name="uq_app_shop_pending"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("app_shops.id", ondelete="CASCADE"), nullable=False, index=True)
    aliexpress_id = Column(String(64), nullable=False, index=True)
    status = Column(String(30), default="pending", nullable=False)
    error_message = Column(Text)
    product_data = Column(JSON)
    retry_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    out_of_stock_skus = Column(JSON)
    in_stock_skus = Column(JSON)
    last_checked = Column(DateTime(timezone=True))


class SyncLog(Base):
    __tablename__ = "app_sync_logs"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("app_shops.id", ondelete="CASCADE"), nullable=False, index=True)
    entity_type = Column(String(30), nullable=False)
    entity_id = Column(String(64))
    action = Column(String(50), nullable=False)
    status = Column(String(30), nullable=False)
    message = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class NotificationPreference(Base):
    __tablename__ = "app_notification_preferences"
    __table_args__ = (UniqueConstraint("user_id", name="uq_app_notification_preferences_user"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    in_app_enabled = Column(Boolean, nullable=False, default=True)
    email_enabled = Column(Boolean, nullable=False, default=False)
    email_critical_only = Column(Boolean, nullable=False, default=True)
    inventory_notifications = Column(Boolean, nullable=False, default=True)
    pricing_notifications = Column(Boolean, nullable=False, default=True)
    dead_listing_notifications = Column(Boolean, nullable=False, default=True)
    sync_notifications = Column(Boolean, nullable=False, default=True)
    locked_price_notifications = Column(Boolean, nullable=False, default=False)
    new_variant_notifications = Column(Boolean, nullable=False, default=True)
    large_inventory_drop_notifications = Column(Boolean, nullable=False, default=False)
    low_stock_threshold = Column(Integer, nullable=False, default=5)
    large_price_change_percent = Column(Float, nullable=False, default=20.0)
    large_inventory_drop_percent = Column(Float, nullable=False, default=50.0)
    pause_large_price_updates = Column(Boolean, nullable=False, default=False)
    minimum_profit = Column(Float, nullable=False, default=0.0)
    auto_restore_inventory = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Notification(Base):
    __tablename__ = "app_notifications"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("app_shops.id", ondelete="CASCADE"), index=True)
    entity_type = Column(String(30))
    entity_id = Column(String(64))
    event_type = Column(String(60), nullable=False, index=True)
    category = Column(String(30), nullable=False, index=True)
    priority = Column(String(20), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    message = Column(Text, nullable=False)
    product_name = Column(String(500))
    product_image = Column(String(1000))
    variant_name = Column(String(500))
    old_value = Column(JSON)
    new_value = Column(JSON)
    automatic_action = Column(Text)
    merchant_action_required = Column(Boolean, nullable=False, default=False)
    action_label = Column(String(100))
    action_url = Column(String(1000))
    supplier_url = Column(String(1000))
    shopify_url = Column(String(1000))
    dedupe_key = Column(String(255), index=True)
    occurrence_count = Column(Integer, nullable=False, default=1)
    is_read = Column(Boolean, nullable=False, default=False, index=True)
    email_status = Column(String(20), nullable=False, default="not_requested")
    email_error = Column(Text)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False, index=True)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    read_at = Column(DateTime(timezone=True))


# Compatibility names used by the ported single-user operational service.
ImportedProduct = Product
ProductMapping = Mapping


class PlanProduct(Base):
    """Account quota reservation and explicitly selected Free automation slot."""
    __tablename__ = "app_plan_products"
    __table_args__ = (UniqueConstraint("shop_id", "aliexpress_id", name="uq_plan_shop_supplier"),)
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("app_users.id", ondelete="CASCADE"), nullable=False, index=True)
    shop_id = Column(Integer, ForeignKey("app_shops.id", ondelete="CASCADE"), nullable=False, index=True)
    aliexpress_id = Column(String(64), nullable=False)
    full_access = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
