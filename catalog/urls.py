from django.urls import path

from . import views

urlpatterns = [
    path("", views.products, name="catalog"),
    path("models/", views.models, name="catalog_models"),
    path("models/new/", views.model_edit, name="catalog_model_new"),
    path("models/<int:pk>/", views.model_edit, name="catalog_model"),
    path("products/<uuid:pk>/", views.product_detail, name="catalog_product"),
    path("codes/", views.codes, name="catalog_codes"),
    path("combos/", views.combos, name="catalog_combos"),
    path("combos/new/", views.combo_edit, name="catalog_combo_new"),
    path("combos/<int:pk>/", views.combo_edit, name="catalog_combo"),
    path("codes/new/<str:kind>/", views.code_new, name="catalog_code_new"),
    path("ai/<str:kind>/", views.ai_suggest, name="catalog_ai"),
]
