from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import models, transaction
from django.utils import timezone

BUENOS_AIRES = ZoneInfo("America/Argentina/Buenos_Aires")

from .tokens import decrypt_secret, encrypt_secret


class MercadoLibreAccount(models.Model):
    """One Mercado Libre seller connected to an organization.

    A seller may be connected to several organizations. Each keeps its own row,
    and new tokens are written to every row for that seller, because a refresh
    token works only once.
    """

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="mercadolibre_account",
    )
    meli_user_id = models.CharField(max_length=64, db_index=True)
    access_token = models.TextField()
    refresh_token = models.TextField(blank=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "mercadolibre_account"

    def __str__(self):
        return self.meli_user_id

    def store_token_response(self, payload):
        from .client import expires_at_from

        self.access_token = encrypt_secret(payload.get("access_token") or "")
        refresh = payload.get("refresh_token")
        if refresh:
            self.refresh_token = encrypt_secret(refresh)
        user_id = payload.get("user_id")
        if user_id is not None:
            self.meli_user_id = str(user_id)
        self.expires_at = expires_at_from(payload.get("expires_in"))

    def _is_fresh(self):
        return self.expires_at > timezone.now() + timedelta(minutes=5)

    def _siblings(self):
        return MercadoLibreAccount.objects.filter(meli_user_id=self.meli_user_id)

    def share_tokens(self):
        """Give every organization connected to this seller this row's tokens."""
        self._siblings().exclude(pk=self.pk).update(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at,
            updated_at=timezone.now(),
        )

    def fresh_access_token(self):
        if self._is_fresh():
            return decrypt_secret(self.access_token)
        from .client import refresh_access_token

        with transaction.atomic():
            list(self._siblings().select_for_update().order_by("pk"))
            self.refresh_from_db(
                fields=["access_token", "refresh_token", "expires_at"]
            )
            if not self._is_fresh():
                payload = refresh_access_token(decrypt_secret(self.refresh_token))
                self.store_token_response(payload)
                self.save(
                    update_fields=[
                        "access_token",
                        "refresh_token",
                        "expires_at",
                        "updated_at",
                    ]
                )
                self.share_tokens()
        return decrypt_secret(self.access_token)


class MercadoLibreNotification(models.Model):
    """A Mercado Libre webhook delivery. Organization is set when the seller is connected."""

    organization = models.ForeignKey(
        "accounts.Organization",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        related_name="mercadolibre_notifications",
    )
    notification_id = models.CharField(max_length=128, blank=True)
    topic = models.CharField(max_length=64, blank=True)
    resource = models.CharField(max_length=512, blank=True)
    user_id = models.CharField(max_length=64, blank=True)
    application_id = models.CharField(max_length=64, blank=True)
    attempts = models.PositiveIntegerField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    received_at = models.DateTimeField(null=True, blank=True)
    buenos_aires_at = models.DateTimeField(null=True, blank=True)
    payload = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "mercadolibre_notification"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "notification_id"],
                condition=~models.Q(notification_id=""),
                name="uniq_meli_notification_org_id",
            ),
        ]

    def __str__(self):
        return f"{self.topic} {self.resource}".strip()

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if self.buenos_aires_at is None:
            self.buenos_aires_at = timezone.now().astimezone(BUENOS_AIRES)
            if update_fields is not None:
                kwargs["update_fields"] = {"buenos_aires_at", *update_fields}
        super().save(*args, **kwargs)


class SellerProfile(models.Model):
    """The Mercado Libre seller connected to an organization."""

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="seller_profile",
    )
    meli_user_id = models.CharField(max_length=64, blank=True)
    nickname = models.CharField(max_length=128, blank=True)
    first_name = models.CharField(max_length=200, blank=True)
    last_name = models.CharField(max_length=200, blank=True)
    email = models.CharField(max_length=254, blank=True)
    site_id = models.CharField(max_length=16, blank=True)
    country_id = models.CharField(max_length=8, blank=True)
    permalink = models.CharField(max_length=512, blank=True)
    payload = models.JSONField(default=dict)
    deleted_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "seller_profile"

    def __str__(self):
        return self.nickname or self.meli_user_id

    def display_name(self):
        return " ".join(part for part in (self.first_name, self.last_name) if part)

    def profile_url(self):
        if self.permalink.startswith(("http://", "https://")):
            return self.permalink
        return ""

    def store_user_profile(self, payload):
        self.meli_user_id = _text(payload.get("id"), 64)
        self.nickname = _text(payload.get("nickname"), 128)
        self.first_name = _text(payload.get("first_name"), 200)
        self.last_name = _text(payload.get("last_name"), 200)
        self.email = _text(payload.get("email"), 254)
        self.site_id = _text(payload.get("site_id"), 16)
        self.country_id = _text(payload.get("country_id"), 8)
        self.permalink = _text(payload.get("permalink"), 512)
        self.payload = payload
        self.deleted_at = None


class ItemQuestion(models.Model):
    """A presales question asked on one of the organization's items."""

    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="item_questions",
    )
    question_id = models.CharField(max_length=32)
    item_id = models.CharField(max_length=32, blank=True)
    item_title = models.CharField(max_length=200, blank=True)
    item_permalink = models.CharField(max_length=512, blank=True)
    text = models.TextField(blank=True)
    status = models.CharField(max_length=32, blank=True)
    from_user_id = models.CharField(max_length=64, blank=True)
    answer_text = models.TextField(blank=True)
    asked_at = models.DateTimeField(null=True, blank=True)
    answered_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "item_question"
        ordering = ["-asked_at", "-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "question_id"],
                name="uniq_item_question_org_id",
            ),
        ]

    def __str__(self):
        return self.question_id

    def can_reply(self):
        return self.status.upper() == "UNANSWERED" and not self.answer_text

    def item_url(self):
        if self.item_permalink.startswith(("http://", "https://")):
            return self.item_permalink
        return ""

    def asked_label(self):
        return _buenos_aires_label(self.asked_at)

    def answered_label(self):
        return _buenos_aires_label(self.answered_at)


def _buenos_aires_label(value):
    if value is None:
        return ""
    return value.astimezone(BUENOS_AIRES).strftime("%Y-%m-%d %H:%M")


def _text(value, limit):
    if value is None:
        return ""
    return str(value)[:limit]


class MeliListing(models.Model):
    """One sellable unit on Mercado Libre: an item, or one variation of it.

    product is the catalog product with the same SKU in this organization, when there is one.
    One catalog product can be on many listings.
    """

    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="meli_listings",
    )
    item_id = models.CharField(max_length=32)
    variation_id = models.CharField(max_length=32, blank=True)
    product = models.ForeignKey(
        "catalog.MasterProduct",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="meli_listings",
    )
    seller_sku = models.CharField(max_length=120, blank=True)
    title = models.CharField(max_length=255, blank=True)
    variation_label = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=32, blank=True)
    sub_status = models.JSONField(default=list, blank=True)
    price = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=8, blank=True)
    available_quantity = models.IntegerField(null=True, blank=True)
    sold_quantity = models.IntegerField(null=True, blank=True)
    listing_type_id = models.CharField(max_length=32, blank=True)
    category_id = models.CharField(max_length=32, blank=True)
    condition = models.CharField(max_length=32, blank=True)
    catalog_listing = models.BooleanField(default=False)
    permalink = models.CharField(max_length=512, blank=True)
    thumbnail = models.CharField(max_length=512, blank=True)
    units_per_sale = models.PositiveSmallIntegerField(
        default=1,
        help_text="Catalog units one sale takes: 2 for a Kit x2. Set in Vimple; the sync never changes it.",
    )
    logistic_type = models.CharField(max_length=32, blank=True, help_text="fulfillment means Mercado Libre Full.")
    inventory_id = models.CharField(max_length=32, blank=True)
    full_available = models.IntegerField(null=True, blank=True, help_text="Mercado Libre's Full stock.")
    meli_created_at = models.DateTimeField(null=True, blank=True)
    meli_updated_at = models.DateTimeField(null=True, blank=True)
    synced_at = models.DateTimeField()
    payload = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "meli_listing"
        ordering = ["title", "item_id", "variation_id"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "item_id", "variation_id"],
                name="uniq_meli_listing_org_item_variation",
            ),
        ]
        indexes = [
            models.Index(fields=["organization", "seller_sku"], name="meli_listing_org_sku"),
            models.Index(fields=["organization", "product"], name="meli_listing_org_product"),
        ]

    def __str__(self):
        return f"{self.item_id} {self.variation_id}".strip()

    def listing_url(self):
        if self.permalink.startswith(("http://", "https://")):
            return self.permalink
        return ""

    def thumbnail_url(self):
        if self.thumbnail.startswith("https://"):
            return self.thumbnail
        return ""

    def is_full(self):
        return self.logistic_type == "fulfillment"

    def link_state(self):
        if self.product_id:
            return "linked"
        return "unmatched" if self.seller_sku else "no_sku"


class MeliListingSync(models.Model):
    """The last full listing sync of one organization: its state and what it found."""

    class State(models.TextChoices):
        QUEUED = "queued", "Queued"
        RUNNING = "running", "Running"
        OK = "ok", "Done"
        FAILED = "failed", "Failed"

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="meli_listing_sync",
    )
    state = models.CharField(max_length=16, choices=State.choices, blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    total = models.PositiveIntegerField(default=0)
    linked = models.PositiveIntegerField(default=0)
    no_sku = models.PositiveIntegerField(default=0)
    unmatched = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "meli_listing_sync"

    def busy(self):
        """Queued or running, unless it has been stuck for longer than a sync can take."""
        if self.state not in {self.State.QUEUED, self.State.RUNNING}:
            return False
        return self.updated_at > timezone.now() - timedelta(minutes=30)


class MeliSale(models.Model):
    """One line of a Mercado Libre order. The inventory app turns it into stock movements."""

    COUNTED = {"paid", "partially_refunded"}
    CANCELLED = {"cancelled", "pending_cancel"}

    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="meli_sales",
    )
    order_id = models.CharField(max_length=32)
    item_id = models.CharField(max_length=32)
    variation_id = models.CharField(max_length=32, blank=True)
    listing = models.ForeignKey(
        MeliListing,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sales",
    )
    title = models.CharField(max_length=255, blank=True)
    seller_sku = models.CharField(max_length=120, blank=True)
    quantity = models.PositiveIntegerField()
    status = models.CharField(max_length=32, blank=True)
    from_full = models.BooleanField(default=False)
    sold_at = models.DateTimeField()
    applied = models.BooleanField(default=False)
    payload = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "meli_sale"
        ordering = ["-sold_at", "-pk"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "order_id", "item_id", "variation_id"],
                name="uniq_meli_sale_org_order_item",
            ),
        ]
        indexes = [models.Index(fields=["organization", "applied"], name="meli_sale_org_applied")]

    def __str__(self):
        return f"{self.order_id} {self.item_id}"

    def counts(self):
        return self.status in self.COUNTED

    def cancelled(self):
        return self.status in self.CANCELLED


class MeliSalesSync(models.Model):
    """The last sales catch-up of one organization."""

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="meli_sales_sync",
    )
    state = models.CharField(max_length=16, choices=MeliListingSync.State.choices, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    orders = models.PositiveIntegerField(default=0)
    error = models.CharField(max_length=300, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "meli_sales_sync"

    def busy(self):
        if self.state not in {MeliListingSync.State.QUEUED, MeliListingSync.State.RUNNING}:
            return False
        return self.updated_at > timezone.now() - timedelta(minutes=30)


class ListingSettings(models.Model):
    """The choices of Mercado Libre's sell flow that Vimple fills in, as defaults or for one draft.

    In-person pickup is not a choice: every listing goes out without it.
    """

    class Condition(models.TextChoices):
        NEW = "new", "New"
        USED = "used", "Used"

    class Warranty(models.TextChoices):
        SELLER = "seller", "Seller warranty"
        FACTORY = "factory", "Factory warranty"
        NONE = "none", "No warranty"

    installments = models.BooleanField(
        default=False,
        help_text="On: 3 installments at the published price (Premium). Off: no installments (Clásica).",
    )
    invoice_a = models.BooleanField(default=False, verbose_name="Factura A", help_text="Offer Factura A to companies.")
    condition = models.CharField(max_length=8, choices=Condition.choices, default=Condition.NEW)
    flex = models.BooleanField(default=False, verbose_name="Mercado Envíos Flex")
    warranty = models.CharField(max_length=8, choices=Warranty.choices, default=Warranty.SELLER)
    warranty_days = models.PositiveSmallIntegerField(default=30)
    regulatory_na = models.BooleanField(
        default=True,
        verbose_name="Resolución 155/98: no aplica",
        help_text="Sent only for categories that ask for it, such as perfumes.",
    )

    SETTING_FIELDS = ("installments", "invoice_a", "condition", "flex", "warranty", "warranty_days", "regulatory_na")

    class Meta:
        abstract = True


class ListingDefaults(ListingSettings):
    """An organization's defaults for new Mercado Libre listings."""

    organization = models.OneToOneField(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="listing_defaults",
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "meli_listing_defaults"


class ListingDraft(ListingSettings):
    """A Mercado Libre listing prepared in Vimple: checked by Mercado Libre, then posted.

    draft → checking → ready or needs_fix; ready → queued → publishing → published or failed.
    The settings fields apply only when use_defaults is off.
    """

    class Kind(models.TextChoices):
        CATALOG = "catalog", "Catalog"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        CHECKING = "checking", "Checking"
        READY = "ready", "Ready"
        NEEDS_FIX = "needs_fix", "Needs a fix"
        QUEUED = "queued", "Queued"
        PUBLISHING = "publishing", "Posting"
        PUBLISHED = "published", "Published"
        FAILED = "failed", "Failed"

    # Locked while Mercado Libre has it or is about to.
    LOCKED = (Status.QUEUED, Status.PUBLISHING, Status.PUBLISHED)
    BUSY = (Status.CHECKING, Status.QUEUED, Status.PUBLISHING)

    organization = models.ForeignKey(
        "accounts.Organization",
        on_delete=models.CASCADE,
        related_name="listing_drafts",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices, default=Kind.CATALOG)
    product = models.ForeignKey(
        "catalog.MasterProduct",
        on_delete=models.PROTECT,
        related_name="listing_drafts",
    )
    catalog_url = models.URLField(max_length=1024, blank=True)
    catalog_product_id = models.CharField(max_length=32, blank=True)
    catalog_name = models.CharField(max_length=255, blank=True)
    catalog_picture = models.CharField(max_length=1024, blank=True)
    category_id = models.CharField(max_length=32, blank=True)
    has_regulatory = models.BooleanField(default=False, help_text="The category asks for the 155/98 file number.")
    price = models.DecimalField(max_digits=14, decimal_places=2)
    quantity = models.PositiveIntegerField(default=1)
    use_defaults = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status.choices, default=Status.DRAFT)
    error = models.CharField(max_length=500, blank=True)
    error_details = models.JSONField(
        default=dict,
        blank=True,
        help_text="Mercado Libre's whole answer to a refused check or post, and the body Vimple sent. Never a token.",
    )
    item_id = models.CharField(max_length=32, blank=True)
    published_at = models.DateTimeField(null=True, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "meli_listing_draft"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "status"])]

    def locked(self):
        return self.status in self.LOCKED

    def error_details_text(self):
        """error_details as indented JSON, to read on the page and paste elsewhere."""
        if not self.error_details:
            return ""
        import json

        return json.dumps(self.error_details, indent=2, ensure_ascii=False, default=str)

    def item_url(self):
        return f"https://articulo.mercadolibre.com.ar/{self.item_id[:3]}-{self.item_id[3:]}" if self.item_id else ""
