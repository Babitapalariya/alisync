"""Small additive migration for deployments created by early AliSync builds."""

from sqlalchemy import inspect, text

from .database import Base, engine


def add_missing_columns() -> None:
    """Add model columns without deleting or rewriting existing tenant data."""
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    preparer = engine.dialect.identifier_preparer
    with engine.begin() as connection:
        for table in Base.metadata.sorted_tables:
            if table.name not in tables:
                continue
            existing = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in existing or column.primary_key:
                    continue
                column_type = column.type.compile(dialect=engine.dialect)
                nullable = "" if column.nullable else " NULL"
                statement = (
                    f"ALTER TABLE {preparer.quote(table.name)} ADD COLUMN "
                    f"{preparer.quote(column.name)} {column_type}{nullable}"
                )
                connection.execute(text(statement))

        mapping_table = "app_product_mappings"
        if mapping_table in tables:
            unique_names = {item.get("name") for item in inspector.get_unique_constraints(mapping_table)}
            constraint_name = "uq_app_shop_shopify_mapping"
            if constraint_name not in unique_names:
                duplicate = connection.execute(text(
                    "SELECT shop_id, shopify_product_id, COUNT(*) AS mapping_count "
                    "FROM app_product_mappings GROUP BY shop_id, shopify_product_id HAVING COUNT(*) > 1 LIMIT 1"
                )).first()
                if duplicate:
                    print("[Migration] Cannot add Shopify mapping uniqueness constraint: "
                          f"shop_id={duplicate[0]}, shopify_product_id={duplicate[1]} has {duplicate[2]} mappings")
                else:
                    connection.execute(text(
                        "ALTER TABLE app_product_mappings ADD CONSTRAINT uq_app_shop_shopify_mapping "
                        "UNIQUE (shop_id, shopify_product_id)"
                    ))

        product_table = "app_products"
        if product_table in tables:
            unique_names = {item.get("name") for item in inspector.get_unique_constraints(product_table)}
            constraint_name = "uq_app_shop_shopify_product"
            if constraint_name not in unique_names:
                duplicate = connection.execute(text(
                    "SELECT shop_id, shopify_product_id, COUNT(*) AS product_count FROM app_products "
                    "WHERE shopify_product_id IS NOT NULL GROUP BY shop_id, shopify_product_id "
                    "HAVING COUNT(*) > 1 LIMIT 1"
                )).first()
                if duplicate:
                    print("[Migration] Cannot add imported-product Shopify uniqueness constraint: "
                          f"shop_id={duplicate[0]}, shopify_product_id={duplicate[1]} has {duplicate[2]} products")
                else:
                    connection.execute(text(
                        "ALTER TABLE app_products ADD CONSTRAINT uq_app_shop_shopify_product "
                        "UNIQUE (shop_id, shopify_product_id)"
                    ))
