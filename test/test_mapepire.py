"""Live tests for behavior specific to the Mapepire connectivity layer."""

import datetime
import decimal

from sqlalchemy import Column
from sqlalchemy import exc
from sqlalchemy import insert
from sqlalchemy import Integer
from sqlalchemy import LargeBinary
from sqlalchemy import Numeric
from sqlalchemy import select
from sqlalchemy import String
from sqlalchemy import Table
from sqlalchemy import testing
from sqlalchemy import text
from sqlalchemy.testing import config
from sqlalchemy.testing import eq_
from sqlalchemy.testing import fixtures
from sqlalchemy.testing import is_true


class MapepireTypesTest(fixtures.TablesTest):
    __backend__ = True

    @classmethod
    def define_tables(cls, metadata):
        Table(
            "mp_types",
            metadata,
            Column("id", Integer, primary_key=True, autoincrement=False),
            Column("num", Numeric(31, 6)),
            Column("data", LargeBinary(100000)),
            Column("name", String(40)),
        )

    def test_decimal_precision(self, connection):
        # Mapepire sends DECIMAL values as JSON numbers; make sure the full
        # precision survives the JSON decoding.
        value = decimal.Decimal("1234567890123456789012345.123456")
        t = self.tables.mp_types
        connection.execute(insert(t).values(id=1, num=value))
        eq_(connection.scalar(select(t.c.num).where(t.c.id == 1)), value)

    def test_binary_roundtrip(self, connection):
        t = self.tables.mp_types
        small = bytes(range(256))
        large = bytes(range(256)) * 300  # > the server's in-memory blob threshold
        connection.execute(
            insert(t), [{"id": 2, "data": small}, {"id": 3, "data": large}]
        )
        rows = connection.execute(select(t.c.data).order_by(t.c.id)).scalars().all()
        eq_(rows, [small, large])

    def test_duplicate_column_names(self, connection):
        # Rows are fetched in terse (positional) form, so duplicate column
        # names don't collapse into one value.
        row = connection.execute(
            text("SELECT 1 AS a, 2 AS a FROM SYSIBM.SYSDUMMY1")
        ).fetchone()
        eq_(tuple(row), (1, 2))

    def test_datetime_types(self, connection):
        row = connection.execute(
            text(
                "SELECT DATE('2024-02-29'), TIME('13.45.01'), "
                "TIMESTAMP('2024-02-29-13.45.01.123456') FROM SYSIBM.SYSDUMMY1"
            )
        ).fetchone()
        eq_(
            tuple(row),
            (
                datetime.date(2024, 2, 29),
                datetime.time(13, 45, 1),
                datetime.datetime(2024, 2, 29, 13, 45, 1, 123456),
            ),
        )

    def test_many_rows(self, connection):
        # More rows than a single fetch block
        t = self.tables.mp_types
        connection.execute(
            insert(t), [{"id": i, "name": str(i)} for i in range(10, 1300)]
        )
        result = connection.execute(select(t.c.id).where(t.c.id >= 10).order_by(t.c.id))
        eq_(result.scalars().all(), list(range(10, 1300)))

    def test_integrity_error(self, connection):
        t = self.tables.mp_types
        connection.execute(insert(t).values(id=5))
        with testing.expect_raises(exc.IntegrityError):
            connection.execute(insert(t).values(id=5))


class MapepireTransactionTest(fixtures.TablesTest):
    __backend__ = True

    @classmethod
    def define_tables(cls, metadata):
        Table("mp_tx", metadata, Column("id", Integer, primary_key=True))

    def _count(self):
        with config.db.connect() as conn:
            return conn.scalar(text("SELECT COUNT(*) FROM mp_tx"))

    def test_rollback(self):
        with config.db.connect() as conn:
            conn.execute(insert(self.tables.mp_tx).values(id=1))
            conn.rollback()
        eq_(self._count(), 0)

    def test_commit(self):
        with config.db.connect() as conn:
            conn.execute(insert(self.tables.mp_tx).values(id=2))
            conn.commit()
        eq_(self._count(), 1)

    def test_autocommit(self):
        with config.db.connect().execution_options(
            isolation_level="AUTOCOMMIT"
        ) as conn:
            eq_(conn.get_isolation_level(), "AUTOCOMMIT")
            conn.execute(insert(self.tables.mp_tx).values(id=3))
            # visible to another connection without a commit
            eq_(self._count(), 1)

    def test_isolation_level_reset(self):
        with config.db.connect().execution_options(
            isolation_level="SERIALIZABLE"
        ) as conn:
            eq_(conn.get_isolation_level(), "SERIALIZABLE")
        with config.db.connect() as conn:
            eq_(conn.get_isolation_level(), config.db.dialect.isolation_level)
            # READ UNCOMMITTED must still allow writes
            conn.execute(insert(self.tables.mp_tx).values(id=4))
            conn.rollback()

    def test_ping(self):
        with config.db.connect() as conn:
            is_true(conn.dialect.do_ping(conn.connection.dbapi_connection))
