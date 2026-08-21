"""SQLAlchemy models.

Importing this package must register every model on ``Base.metadata`` so
Alembic autogeneration sees the complete schema. Domain models (tenant, user,
membership, and the rest) arrive in Phase 1; until then only the base metadata
is exported.
"""

from app.db.base import Base

__all__ = ["Base"]
