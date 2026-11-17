# Follow standard SQLAlchemy testing setup, see
# https://github.com/sqlalchemy/sqlalchemy/blob/main/README.dialects.rst

from sqlalchemy.dialects import registry
import pytest

registry.register("ibmi.mapepire", "sqlalchemy_mapepire.base", "IBMiMapepireDialect")
# The test suite refers to the dialect by its name ("ibmi") in skip markers.
# This is not registered as a package entry point to avoid clashing with
# sqlalchemy-ibmi, which owns "ibmi://".
registry.register("ibmi", "sqlalchemy_mapepire.base", "IBMiMapepireDialect")
registry.register("mapepire", "sqlalchemy_mapepire.base", "IBMiMapepireDialect")

pytest.register_assert_rewrite("sqlalchemy.testing.assertions")

from sqlalchemy.testing.plugin.pytestplugin import *  # noqa F401,F403
