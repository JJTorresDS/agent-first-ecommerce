from django.contrib import admin
from django.urls import include, path

from accounts.views import home, profile
from tasks.api import api
from tasks.views import health

admin.site.site_header = "Vimple"
admin.site.site_title = "Vimple"
admin.site.index_title = "Tasks"

urlpatterns = [
    path("", home, name="home"),
    path("profile/", profile, name="profile"),
    path("accounts/", include("allauth.urls")),
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
