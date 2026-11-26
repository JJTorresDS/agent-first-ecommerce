"""What still needs a catalog row, so it can't be deleted. Read by the page's Delete card and again on the POST."""

from vimple.deletion import counted


def product_blocked(product):
    return counted(
        (product.purchase_items.count(), "purchase line", "purchase lines"),
        (product.stock_movements.count(), "stock movement", "stock movements"),
        (product.combo_items.count(), "combo line", "combo lines"),
        (product.listing_drafts.count(), "Mercado Libre draft", "Mercado Libre drafts"),
    )


def combo_blocked(combo):
    return counted(
        (combo.listing_drafts.count(), "Mercado Libre draft", "Mercado Libre drafts"),
        (combo.meli_listings.filter(sales__isnull=False).values("item_id").distinct().count(),
         "listing with sales", "listings with sales"),
    )


def model_blocked(model):
    return counted((model.products.count(), "variant", "variants"))


def code_blocked(row):
    """A root category, category or brand; a category can also hold copy."""
    if not hasattr(row, "product_models"):
        return counted((row.categories.count(), "category uses it", "categories use it"))
    copy = row.copy_blocks.count() if hasattr(row, "copy_blocks") else 0
    return counted(
        (row.product_models.count(), "model uses it", "models use it"),
        (copy, "copy block is for it", "copy blocks are for it"),
    )


def unlinked(row):
    """Synced Mercado Libre listings stay on Mercado Libre; in Vimple they lose the link."""
    items = row.meli_listings.values("item_id").distinct().count()
    if not items:
        return ""
    return f"{items} Mercado Libre listing{'s' if items != 1 else ''} stay on Mercado Libre, unlinked from Vimple."
