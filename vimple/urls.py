from django.contrib import admin
from django.urls import include, path

from accounts.views import access, home, profile, set_app_access
from tasks.api import api
from tasks.views import cashflow_list, health

admin.site.site_header = "Vimple"
admin.site.site_title = "Vimple"
admin.site.index_title = "Tasks"

urlpatterns = [
    path("", home, name="home"),
    path("profile/", profile, name="profile"),
    path("cashflow/", cashflow_list, name="cashflow"),
    path("access/", access, name="access"),
    path("access/<int:user_id>/", set_app_access, name="set_app_access"),
    path("accounts/", include("allauth.urls")),
    path("health/", health, name="health"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
