import factory

from apps.catalog.models import Brand, Category, Product, ProductStatus


class CategoryFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Category

    name = factory.Sequence(lambda n: f"Category {n}")
    slug = factory.Sequence(lambda n: f"category-{n}")


class BrandFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Brand

    name = factory.Sequence(lambda n: f"Brand {n}")
    slug = factory.Sequence(lambda n: f"brand-{n}")


class ProductFactory(factory.django.DjangoModelFactory):
    """Direct model creation for test setup; tests of behaviour go through services."""

    class Meta:
        model = Product

    sku = factory.Sequence(lambda n: f"TST-{n:05d}")
    name = factory.Sequence(lambda n: f"Test product {n}")
    slug = factory.Sequence(lambda n: f"test-product-{n}")
    category = factory.SubFactory(CategoryFactory)
    status = ProductStatus.ACTIVE
