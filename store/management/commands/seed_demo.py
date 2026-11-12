from decimal import Decimal

from django.core.management.base import BaseCommand

from store.models import Category, Product, ProductVariant


DEMO_CATALOG = {
    "Whisky": [
        ("Johnnie Walker Black Label", "750ml", "3200", 12),
        ("Jameson Irish Whiskey", "750ml", "2800", 10),
    ],
    "Gin": [
        ("Gordon's London Dry Gin", "750ml", "1900", 14),
        ("Tanqueray London Dry Gin", "750ml", "2600", 8),
    ],
    "Wine": [
        ("Four Cousins Natural Sweet Red", "750ml", "1100", 16),
        ("Robertson Winery Natural Sweet White", "750ml", "1250", 11),
    ],
    "Beer": [
        ("Tusker Lager", "500ml", "250", 24),
        ("Guinness Foreign Extra Stout", "500ml", "300", 20),
    ],
}


class Command(BaseCommand):
    help = "Create or update the eight clearly labeled Liquor Hub demonstration products."

    def handle(self, *args, **options):
        count = 0
        for category_name, products in DEMO_CATALOG.items():
            category, _ = Category.objects.update_or_create(
                name=category_name,
                defaults={"description": f"Demonstration {category_name.lower()} selection.", "is_active": True},
            )
            for name, size, price, stock in products:
                product, _ = Product.objects.update_or_create(
                    name=name,
                    defaults={
                        "category": category,
                        "description": "Sample product listing for demonstration purposes. Confirm actual availability and price with the shop.",
                        "is_featured": name in {"Johnnie Walker Black Label", "Gordon's London Dry Gin"},
                        "is_active": True,
                    },
                )
                ProductVariant.objects.update_or_create(
                    product=product,
                    bottle_size=size,
                    defaults={
                        "price": Decimal(price),
                        "sale_price": None,
                        "stock_quantity": stock,
                        "is_available": True,
                    },
                )
                count += 1
        self.stdout.write(self.style.SUCCESS(f"Seeded {count} demonstration products in four categories."))
        self.stdout.write("All displayed prices and inventory quantities are sample values, not actual store data.")
