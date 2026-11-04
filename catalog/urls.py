from django.urls import path

from . import views

urlpatterns = [
    path("", views.products, name="catalog"),
    path("models/", views.models, name="catalog_models"),
    path("models/new/", views.model_edit, name="catalog_model_new"),
    path("models/<int:pk>/", views.model_edit, name="catalog_model"),
    path("products/<uuid:pk>/", views.product_detail, name="catalog_product"),
    path("codes/", views.codes, name="catalog_codes"),
]
