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
    path("copy/", views.copy_blocks, name="catalog_copy"),
    path("copy/new/", views.copy_block_edit, name="catalog_copy_new"),
    path("copy/<int:pk>/", views.copy_block_edit, name="catalog_copy_block"),
]
