"""数据模型模块

导入顺序很重要：`tables` 必须在 `db.create_all()` / alembic autogenerate 之前被导入，
否则 Base.metadata 里没有这些表。
"""

from . import schemas, tables  # noqa: F401
from .tables import (  # noqa: F401
    CacheCounter,
    CacheEntry,
    Job,
    Task,
    TaskEvent,
    TaskStep,
)
