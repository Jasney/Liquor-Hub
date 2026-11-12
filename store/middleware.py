from django.conf import settings
from django.shortcuts import render


class AgeGateMiddleware:
    exempt_prefixes = ("/admin/", "/static/", "/media/")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            settings.AGE_VERIFICATION_REQUIRED
            and not request.session.get("age_verified_at")
            and request.path != "/age-confirm/"
            and request.path != "/mpesa/callback/"
            and not request.path.startswith(self.exempt_prefixes)
        ):
            return render(request, "store/age_gate.html")
        return self.get_response(request)
