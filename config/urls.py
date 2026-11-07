from django.contrib import admin
from django.urls import include, path

admin.site.site_header = "Radlea Roster administration"
admin.site.site_title = "Radlea Roster"

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("roster.urls")),
]
