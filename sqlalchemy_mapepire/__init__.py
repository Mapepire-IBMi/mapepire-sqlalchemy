"""SQLAlchemy dialect for Db2 for IBM i, using Mapepire for connectivity.

Derived from sqlalchemy-ibmi (https://github.com/IBM/sqlalchemy-ibmi).
"""

__version__ = "0.1.0.dev0"

from . import base  # noqa: F401
from .base import IBMiMapepireDialect  # noqa: F401

dialect = IBMiMapepireDialect
