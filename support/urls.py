from django.urls import path

from . import views

urlpatterns = [
    path("report/draft/<int:pk>/", views.report_draft_error, name="report_draft_error"),
]
