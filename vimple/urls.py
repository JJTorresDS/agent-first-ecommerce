from django.contrib import admin
from django.urls import include, path

from accounts.views import (
    access,
    home,
    organization_delete,
    organization_settings,
    organization_setup,
    profile,
    profile_menu,
    set_app_access,
)
from meli.views import (
    mercadolibre_callback,
    mercadolibre_connect,
    mercadolibre_disconnect,
    mercadolibre_webhook,
)
from tasks.api import api
from tasks.views import cashflow_csv, cashflow_list, health

admin.site.site_header = "Vimple"
admin.site.site_title = "Vimple"
admin.site.index_title = "Finances"

urlpatterns = [
    path("", home, name="home"),
    path("profile/", profile, name="profile"),
    path("profile/menu/", profile_menu, name="profile_menu"),
    path("organization/setup/", organization_setup, name="organization_setup"),
    path("organization/", organization_settings, name="organization"),
    path("organization/delete/", organization_delete, name="organization_delete"),
    path("cashflow/", cashflow_list, name="cashflow"),
    path("cashflow/export.csv", cashflow_csv, name="cashflow_csv"),
    path("access/", access, name="access"),
    path("access/<int:user_id>/", set_app_access, name="set_app_access"),
    path("accounts/", include("allauth.urls")),
    path("health/", health, name="health"),
    path("meli/connect/", mercadolibre_connect, name="mercadolibre_connect"),
    path("meli/callback/", mercadolibre_callback, name="mercadolibre_callback"),
    path("meli/disconnect/", mercadolibre_disconnect, name="mercadolibre_disconnect"),
    path("meli/webhooks/", mercadolibre_webhook, name="mercadolibre_webhook"),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
