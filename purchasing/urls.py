from django.urls import path

from . import views

urlpatterns = [
    path("", views.purchases, name="purchases"),
    path("new/", views.purchase_edit, name="purchase_new"),
    path("<int:pk>/", views.purchase_edit, name="purchase"),
    path("suppliers/", views.suppliers, name="suppliers"),
    path("suppliers/<int:pk>/", views.supplier_edit, name="supplier"),
]
