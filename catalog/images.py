from django.core.exceptions import ValidationError
from django.core.validators import URLValidator
from django.db import transaction

from .models import MAX_IMAGES, ProductImage

_URL = URLValidator(schemes=["http", "https"])


def clean_urls(urls):
    """The picture URLs in order, without blanks or repeats. ValidationError for a bad one or more than 10."""
    kept = []
    for url in urls:
        url = (url or "").strip()
        if not url or url in kept:
            continue
        try:
            _URL(url)
        except ValidationError:
            raise ValidationError(f"Not a picture URL: {url[:80]}") from None
        if len(url) > 1024:
            raise ValidationError("A picture URL can be at most 1024 characters.")
        kept.append(url)
    if len(kept) > MAX_IMAGES:
        raise ValidationError(f"Use at most {MAX_IMAGES} pictures.")
    return kept


@transaction.atomic
def set_images(product, urls):
    """Replace a product's pictures. The first becomes image_url, so the two never disagree.

    An empty list leaves image_url as it is (a product always has a main picture).
    """
    urls = clean_urls(urls)
    ProductImage.objects.filter(organization_id=product.organization_id, product=product).delete()
    ProductImage.objects.bulk_create(
        [
            ProductImage(organization_id=product.organization_id, product=product, url=url, position=position)
            for position, url in enumerate(urls)
        ]
    )
    if urls and product.image_url != urls[0]:
        product.image_url = urls[0]
        product.save(update_fields=["image_url", "updated_at"])
    return urls
