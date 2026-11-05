from django.urls import path

from . import views

urlpatterns = [
    path("", views.stock, name="inventory_stock"),
    path("count/", views.count, name="inventory_count"),
    path("movements/", views.movements, name="inventory_movements"),
    path("sales/", views.sales, name="inventory_sales"),
    path("products/<uuid:pk>/", views.product_stock, name="inventory_product"),
]
