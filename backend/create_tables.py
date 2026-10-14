from sqlalchemy import inspect

from alisync_app.database import Base, engine
from alisync_app import models  # noqa: F401

expected = sorted(Base.metadata.tables)
if not expected or any(not name.startswith("app_") for name in expected):
    raise RuntimeError(f"Unsafe table metadata: {expected}")
Base.metadata.create_all(engine, checkfirst=True)
actual = set(inspect(engine).get_table_names())
missing = [name for name in expected if name not in actual]
if missing:
    raise RuntimeError(f"Missing tables: {missing}")
print("AliSync tables ready:")
for name in expected:
    print(f"  - {name}")
