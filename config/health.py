"""
/healthz for the hosting platform's health check.

Answered before any other middleware, because the platform calls it by the
container's internal IP address with no proper host name or HTTPS header,
which Django would otherwise reject or redirect.
"""
from django.http import HttpResponse


class HealthCheckMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz":
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)
