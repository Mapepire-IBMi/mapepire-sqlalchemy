# sqlalchemy-mapepire

An [SQLAlchemy](https://www.sqlalchemy.org/) dialect for Db2 for
[IBM i](https://en.wikipedia.org/wiki/IBM_i) that connects through
[Mapepire](https://mapepire-ibmi.github.io/) instead of ODBC.

> [!WARNING]
> **This project is experimental and was largely vibe-coded.** Most of it was
> written by an AI coding assistant, with a human steering. It does pass much of
> SQLAlchemy's dialect compliance suite against a real IBM i (see
> [Testing](#testing)), but it has not had serious human review, real-world use,
> or performance tuning. Don't put it anywhere you'd be upset to lose data. Bug
> reports and fixes are welcome.

## What it is

This dialect is a fork of [sqlalchemy-ibmi](https://github.com/IBM/sqlalchemy-ibmi).
The SQL compiler, DDL, type mapping, and reflection code are nearly unchanged. The
pyodbc / IBM i Access ODBC Driver connectivity is replaced with
[mapepire-python](https://github.com/Mapepire-IBMi/mapepire-python):

```text
SQLAlchemy  ──►  sqlalchemy_mapepire.base      (dialect, from sqlalchemy-ibmi)
            ──►  sqlalchemy_mapepire.dbapi     (PEP 249 module)
            ──►  mapepire_python.Connection / Cursor  (mapepire-python's PEP 249 classes)
            ──►  wss://  ──►  Mapepire server on IBM i  ──►  JDBC (JTOpen)  ──►  Db2 for i
```

The client needs only Python. There's no ODBC driver to install. The IBM i needs a
running [Mapepire server](https://mapepire-ibmi.github.io/guides/sysadmin/).

## Requirements

* Python 3.10+
* SQLAlchemy 2.0+
* mapepire-python 2.x
* A Mapepire server on the IBM i. Tested with Mapepire server 2.3.6 on IBM i 7.5.
  The dialect detects the server version and handles the binary-data changes made
  in Mapepire server 2.4.0.

## Installation

The package isn't on PyPI. Install it from a checkout:

```sh
pip install -e .
```

## Getting started

```python
import sqlalchemy as sa

engine = sa.create_engine(
    "ibmi+mapepire://user:password@my-ibmi.example.com/?ignore_unauthorized=true"
)

with engine.connect() as conn:
    print(conn.execute(sa.text("VALUES CURRENT_SERVER")).scalar())

metadata = sa.MetaData()
table = sa.Table("table_name", metadata, autoload_with=engine)

with engine.connect() as conn:
    for row in conn.execute(sa.select(table).limit(10)):
        print(row)
```

Both `ibmi+mapepire://` and `mapepire://` URLs work. The port defaults to 8076.

### Connection options

URL query options:

| Option                | Description                                                             |
|-----------------------|-------------------------------------------------------------------------|
| `current_schema`      | Default schema for unqualified names                                    |
| `library_list`        | Comma-separated libraries to add to the job's library list              |
| `use_system_naming`   | `true` for system (`LIB/FILE`) naming. Default is SQL naming            |
| `readonly`            | `true` for a read-only connection                                       |
| `autocommit`          | `true` to start in autocommit mode                                      |
| `timeout`             | Login timeout, in seconds                                               |
| `ignore_unauthorized` | `true` to skip TLS certificate verification (e.g. self-signed certs)    |
| `ca`                  | Path to a PEM file with the CA / server certificate to trust            |

Other [JTOpen JDBC properties](https://www.ibm.com/docs/en/i/7.5?topic=jdbc-toolbox-java-properties)
can be passed with `connect_args`. Note that `jdbc_options` in `connect_args`
replaces the properties generated from the URL options above:

```python
engine = sa.create_engine(url, connect_args={"jdbc_options": {"naming": "sql", "sort": "language"}})
```

`create_engine()` also accepts `isolation_level` (`READ UNCOMMITTED`, the default;
`READ COMMITTED`; `REPEATABLE READ`; `SERIALIZABLE`; `NO COMMIT`; or `AUTOCOMMIT`).
It also accepts `use_text_search=False`, which makes `match()` always use `LIKE`
even when OmniFind is installed.

See the module docstring in [base.py](sqlalchemy_mapepire/base.py) for more,
including how untyped literals are handled.

## How it differs from sqlalchemy-ibmi

These are mostly consequences of how Mapepire works:

* **Transactions are controlled with SQL.** The Mapepire protocol has no commit,
  rollback, or autocommit messages. Connections are opened with the JDBC property
  `auto commit=false`. `commit()` and `rollback()` run `COMMIT` / `ROLLBACK`, and
  isolation levels are changed with `SET TRANSACTION ISOLATION LEVEL`.
  `AUTOCOMMIT` runs the job under `*NC` (no commit).
* **The isolation level is tracked on the client.** Db2 for i has no
  `CURRENT ISOLATION` register, so the connection remembers what it last set.
* **CHAR values come back right-trimmed.** The Mapepire server always trims
  trailing blanks from CHAR/NCHAR values.
* **DECIMAL precision.** The server sends numbers as JSON numbers, and the stock
  mapepire-python client parses them into `float`. The dbapi layer parses them as
  `Decimal` instead, so DECIMAL/NUMERIC values keep full precision.
* **Binary data.** On Mapepire server 2.4.0 and later, BLOB/BINARY result values
  are downloaded from the server's single-use `/blob/` HTTP endpoint, and
  parameters are sent as base64. Older servers use hex strings for both.
* **Everything is bound as a string.** The server binds every non-binary
  parameter with JDBC `setString()`. The dbapi layer formats `Decimal`, `date`,
  `time`, `datetime`, and `bool` values so the server accepts them.
* **No `fast_executemany`.** `executemany()` always sends one batch request.
* `ssl` and `trim_char_fields` (ODBC options) don't exist. Mapepire always
  uses TLS and always trims CHAR values.

## Known limitations

These are inherited from sqlalchemy-ibmi / Db2 for i:

1) Non-standard SQL queries are not supported. e.g. "SELECT ? FROM TAB1"
2) For updates involving primary/foreign key references, the entries should be made
   in the correct order. Integrity checks are always on, so the primary keys
   referenced by foreign keys must already exist in the parent table.
3) Unique keys containing nullable columns are not supported
4) UPDATE CASCADE for foreign keys is not supported
5) DEFERRABLE INITIALLY DEFERRED is not supported
6) Subqueries in the ON clause of a LEFT OUTER JOIN are not supported

These are specific to this project:

7) Tables must be journaled to be modified inside a transaction. This means
   QTEMP tables, and tables in libraries that aren't journaled, fail with
   SQL7008 unless you use `AUTOCOMMIT` / `NO COMMIT`.
8) `TIME` values have no fractional seconds (a Db2 for i limitation).
9) A connection is a single websocket and must not be shared between threads.
   SQLAlchemy's connection pool already handles this.
10) `sqlalchemy_mapepire.dbapi` subclasses mapepire-python's PEP 249 classes and
    touches some of their internals, so it is pinned to mapepire-python 2.x.

## Testing

Offline unit tests (parameter/result conversion, error mapping, URL parsing) don't
need a server:

```sh
pip install -e . pytest
pytest unit_tests
```

The SQLAlchemy dialect compliance suite plus this project's live tests run
against a real IBM i. Use a dedicated schema: the reflection tests expect it to
contain only the tables the suite creates. A schema made with `CREATE SCHEMA` is
journaled automatically, which the transaction tests need.

```sh
export IBMI_HOSTNAME=my-ibmi.example.com
export IBMI_USERNAME=myuser
export IBMI_PASSWORD=...
export IBMI_SCHEMA=SQLAMPTEST       # created beforehand with CREATE SCHEMA SQLAMPTEST
export IBMI_IGNORE_UNAUTHORIZED=1   # if the server uses a self-signed certificate
python scripts/runtests.py
```

Extra arguments go to pytest, e.g. `python scripts/runtests.py test/test_mapepire.py`.

Last full run (SQLAlchemy 2.1.9, mapepire-python 2.0.0, Mapepire server 2.3.6,
IBM i 7.5): **784 passed, 0 failed, 646 skipped**. Skips are tests for features
that Db2 for i or this dialect doesn't support, as declared in
[requirements.py](sqlalchemy_mapepire/requirements.py) and
[test_suite.py](test/test_suite.py).

Fixes made here relative to the sqlalchemy-ibmi code this is based on, found by
that suite:

* Window frame bounds (`ROWS BETWEEN n PRECEDING ...`) were rendered with
  `literal_binds`, which put the first values into the statement cache. They
  are now rendered post-cache.
* True division by an untyped or unsized numeric divisor cast it to `DECIMAL`
  (which is `DECIMAL(5,0)` on Db2) and truncated it. It now uses `DECFLOAT(34)`.
* `CREATE TABLE ... AS SELECT` (SQLAlchemy 2.1) is rendered in the Db2 for i
  form `CREATE TABLE t AS (SELECT ...) WITH DATA`.
* `Enum` columns are sized in UTF-8 bytes, since `VARCHAR ... CCSID 1208`
  lengths are in bytes. Non-ASCII values were being truncated.
* Reflection recognizes the abbreviated `SYSCOLUMNS` type names (`TIMESTMP`,
  `VARBIN`, `VARG`), plus `BINARY`, `BOOLEAN` and `DECFLOAT`.

## License

Apache License 2.0, the same as sqlalchemy-ibmi, which this is derived from. See
[LICENSE](LICENSE). The original copyright notices are kept in the source files.
