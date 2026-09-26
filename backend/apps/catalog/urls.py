from django.urls import path

from . import api

urlpatterns = [
    path("categories/", api.CategoryListView.as_view(), name="catalog-categories"),
    path("brands/", api.BrandListView.as_view(), name="catalog-brands"),
    path("products/", api.ProductListView.as_view(), name="catalog-products"),
    path("products/legacy/", api.LegacyProductsView.as_view(), name="catalog-products-legacy"),
    path("products/<str:code>/", api.ProductDetailView.as_view(), name="catalog-product-detail"),
]
