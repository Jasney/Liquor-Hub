from .models import Category, Product


def store_info(request):
    cart = request.session.get("cart", {})
    return {
        "store_name": "Liquor Hub Diani",
        "store_location": "Diani Bazaar, Diani Beach Road, Ukunda, Kenya",
        "store_phone": "+254 768 668 668",
        "store_email": "drinks@liquorhub.co.ke",
        "cart_item_count": sum(max(0, int(quantity)) for quantity in cart.values()),
        "nav_categories": Category.objects.filter(is_active=True),
        "catalogue_prices_available": Product.objects.filter(catalogue_key__isnull=False, is_active=True, category__is_active=True).exists(),
    }
