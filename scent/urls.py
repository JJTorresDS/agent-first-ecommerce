from django.contrib import admin
from django.urls import path
from django.views.generic import RedirectView

from tasks.api import api

admin.site.site_header = "Scent tasks"
admin.site.site_title = "Scent tasks"
admin.site.index_title = "Tasks"

urlpatterns = [
    path("", RedirectView.as_view(url="/admin/", permanent=False)),
    path("admin/", admin.site.urls),
    path("api/", api.urls),
]
