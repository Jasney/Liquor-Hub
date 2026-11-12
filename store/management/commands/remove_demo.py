from django.core.management.base import BaseCommand
from django.db import transaction

from store.management.commands.seed_demo import DEMO_CATALOG
from store.models import Category, Product


class Command(BaseCommand):
    help = "Remove unchanged sample listings, preserving products referenced by orders."

    @transaction.atomic
    def handle(self, *args, **options):
        removed = archived = 0
        for category_name, products in DEMO_CATALOG.items():
            samples = Product.objects.filter(
                category__name=category_name,
                name__in=[row[0] for row in products],
                description="Sample product listing for demonstration purposes. Confirm actual availability and price with the shop.",
            )
            for product in samples:
                if product.variants.filter(order_items__isnull=False).exists():
                    product.is_active = False
                    product.is_featured = False
                    product.save(update_fields=["is_active", "is_featured"])
                    archived += 1
                else:
                    product.delete()
                    removed += 1
            Category.objects.filter(
                name=category_name,
                description=f"Demonstration {category_name.lower()} selection.",
            ).update(description="")
        self.stdout.write(self.style.SUCCESS(
            f"Removed {removed} sample products; hid {archived} with order history."
        ))
