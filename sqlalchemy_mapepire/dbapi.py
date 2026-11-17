"""PEP 249 (DB-API 2.0) module used by the ``ibmi+mapepire`` dialect.

This is a thin layer on top of the PEP 249 classes that ship with
`mapepire-python <https://github.com/Mapepire-IBMi/mapepire-python>`_
(``mapepire_python.Connection`` / ``mapepire_python.Cursor``). It subclasses
them rather than replacing them, and only adjusts the behavior SQLAlchemy
depends on:

* **Exact numerics** - Mapepire sends DECIMAL/NUMERIC values as JSON numbers.
  The stock client parses them with ``json.loads`` and loses precision, so we
  parse with ``parse_float=decimal.Decimal`` instead.
* **Terse (positional) rows** - the stock cursor requests rows as JSON objects
  keyed by column name, which collapses duplicate column names. We request
  terse (array) rows.
* **Python types** - DATE/TIME/TIMESTAMP arrive as strings and binary data
  arrives as a ``{"blob_url": ...}`` reference (or inline base64). These are
  converted to ``datetime`` objects and ``bytes``.
* **Parameters** - ``Decimal``, ``bytes``, ``date``/``time``/``datetime`` and
  ``bool`` values are converted to the JSON forms the server can bind.
* **Exceptions** - SQLSTATEs are mapped to the right PEP 249 exception
  subclass (``IntegrityError``, ``ProgrammingError``, ...). The exception
  classes themselves are the same ones mapepire-python exports.
* **Transactions** - ``autocommit`` and isolation level control, implemented
  with ``SET TRANSACTION`` since Mapepire has no protocol messages for them.
"""

import base64
import dataclasses
import datetime
import decimal
import functools
import json
import logging
import re
import urllib.request
from collections import deque
from typing import Any, Dict, Optional

import mapepire_python
from mapepire_python import (  # noqa: F401 - re-exported as part of PEP 249
    DatabaseError,
    DataError,
    Error,
    IntegrityError,
    InterfaceError,
    InternalError,
    NotSupportedError,
    OperationalError,
    ProgrammingError,
)
from mapepire_python.client.query import Query as _MapepireQuery
from mapepire_python.client.query import QueryState
from mapepire_python.core.connection import Connection as _MapepireConnection
from mapepire_python.core.cursor import Cursor as _MapepireCursor
from mapepire_python.core.cursor import _ResultState
from mapepire_python.core.utils import QueryResultSet, row_to_tuple
from mapepire_python.data_types import (
    DaemonServer,
    GetVersionRequest,
    PrepareSqlExecuteRequest,
    QueryOptions,
    QueryResult,
)
from mapepire_python.websocket import BaseConnection as _MapepireBaseConnection

try:
    import pep249 as _pep249

    Warning = _pep249.Warning  # noqa: A001 - PEP 249 requires this name
except (ImportError, AttributeError):  # pragma: no cover

    class Warning(Exception):  # noqa: A001
        pass


__all__ = [
    "apilevel",
    "threadsafety",
    "paramstyle",
    "connect",
    "Connection",
    "Cursor",
    "Warning",
    "Error",
    "InterfaceError",
    "DatabaseError",
    "DataError",
    "OperationalError",
    "IntegrityError",
    "InternalError",
    "ProgrammingError",
    "NotSupportedError",
]

logger = logging.getLogger(__name__)

apilevel = "2.0"
# Connections may not be shared between threads: each one is a single websocket.
threadsafety = 1
paramstyle = "qmark"
version = mapepire_python.__version__

# ---------------------------------------------------------------------------
# PEP 249 type objects and constructors
# ---------------------------------------------------------------------------


class _DBAPITypeObject(frozenset):
    def __eq__(self, other):
        if isinstance(other, str):
            return other.upper() in self
        return frozenset.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = frozenset.__hash__


STRING = _DBAPITypeObject(
    {
        "CHAR",
        "VARCHAR",
        "CLOB",
        "NCHAR",
        "NVARCHAR",
        "NCLOB",
        "GRAPHIC",
        "VARGRAPHIC",
        "DBCLOB",
        "XML",
        "DATALINK",
    }
)
BINARY = _DBAPITypeObject({"BINARY", "VARBINARY", "BLOB"})
NUMBER = _DBAPITypeObject(
    {
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "DECIMAL",
        "NUMERIC",
        "REAL",
        "DOUBLE",
        "FLOAT",
        "DECFLOAT",
        "BOOLEAN",
    }
)
DATETIME = _DBAPITypeObject({"DATE", "TIME", "TIMESTAMP"})
ROWID = _DBAPITypeObject({"ROWID"})

Date = datetime.date
Time = datetime.time
Timestamp = datetime.datetime
Binary = bytes


def DateFromTicks(ticks):
    return datetime.date.fromtimestamp(ticks)


def TimeFromTicks(ticks):
    return datetime.datetime.fromtimestamp(ticks).time()


def TimestampFromTicks(ticks):
    return datetime.datetime.fromtimestamp(ticks)


# ---------------------------------------------------------------------------
# Exception translation
# ---------------------------------------------------------------------------

# SQLSTATE class (first two characters) -> PEP 249 exception
_SQLSTATE_CLASS_MAP = {
    "0A": NotSupportedError,
    "08": OperationalError,
    "21": DataError,  # cardinality violation
    "22": DataError,
    "23": IntegrityError,
    "24": ProgrammingError,  # invalid cursor state
    "25": OperationalError,  # invalid transaction state
    "26": ProgrammingError,
    "2D": OperationalError,
    "2E": ProgrammingError,
    "34": ProgrammingError,
    "38": InternalError,  # external routine exception
    "39": InternalError,
    "3B": OperationalError,  # savepoint exception
    "3F": ProgrammingError,
    "40": OperationalError,  # transaction rollback
    "42": ProgrammingError,
    "51": OperationalError,
    "53": OperationalError,
    "54": OperationalError,
    "55": OperationalError,
    "56": ProgrammingError,
    "57": OperationalError,  # resource not available (e.g. lock timeouts)
    "58": InternalError,
}

#: Messages that indicate the underlying websocket/job is gone.
DISCONNECT_MESSAGES = (
    "Cannot operate on a closed connection",
    "SQL Job not connected",
    "The Conection was closed",
    "The Connection was closed",
    "TCP connection failed",
    "received 1000",
    "received 1001",
    "received 1006",
    "no close frame received or sent",
    "keepalive ping timeout",
)


def _error_details(exc: BaseException) -> Dict[str, Any]:
    """Dig the server error dict ({"error", "sql_state", "sql_rc"}) out of a
    mapepire exception or its cause chain."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if exc.args and isinstance(exc.args[0], dict):
            return exc.args[0]
        exc = exc.__cause__ or exc.__context__
    return {}


def _translate(exc: BaseException) -> BaseException:
    details = _error_details(exc)
    message = details.get("error") or str(exc)
    sql_state = details.get("sql_state") or None
    sql_rc = details.get("sql_rc") or None

    if sql_state:
        cls = _SQLSTATE_CLASS_MAP.get(sql_state[:2], DatabaseError)
    elif isinstance(exc, Error):
        cls = type(exc)
    elif isinstance(exc, (OSError, EOFError, TimeoutError)) or any(
        m in message for m in DISCONNECT_MESSAGES
    ):
        cls = OperationalError
    elif type(exc).__module__.startswith("websockets"):
        cls = OperationalError
    else:
        cls = DatabaseError

    new = cls(message)
    new.sqlstate = sql_state
    new.sql_rc = sql_rc
    return new


def _translate_errors(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Error as err:
            new = _translate(err)
            if type(new) is type(err) and str(new) == str(err):
                if not hasattr(err, "sqlstate"):
                    err.sqlstate = new.sqlstate
                    err.sql_rc = new.sql_rc
                raise
            raise new from err
        except Exception as err:  # websockets, OSError, RuntimeError, ...
            raise _translate(err) from err

    return wrapper


# ---------------------------------------------------------------------------
# Value conversion
# ---------------------------------------------------------------------------


def _adapt_parameter(value: Any, binary_encoding: str = "base64") -> Any:
    """Convert a Python value into something the Mapepire server can bind.

    The server binds every non-binary parameter with ``setString()``. Binary
    parameters are decoded from base64 (Mapepire server 2.4.0+) or from a hex
    string (older servers).
    """
    if (
        value is None
        or isinstance(value, (str, int, float))
        and not isinstance(value, bool)
    ):
        return value
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise DataError(f"Cannot bind non-finite Decimal value {value!r}")
        return format(value, "f")
    if isinstance(value, (bytes, bytearray, memoryview)):
        if binary_encoding == "hex":
            return bytes(value).hex().upper()
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            value = value.replace(tzinfo=None)
        return value.isoformat(sep=" ", timespec="microseconds")
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, datetime.time):
        # Db2 for i TIME has no fractional seconds
        return value.replace(microsecond=0, tzinfo=None).isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    return str(value)


def _adapt_parameters(parameters, binary_encoding="base64"):
    if parameters is None:
        return None
    if isinstance(parameters, dict):
        raise ProgrammingError("Only qmark (positional) parameters are supported")
    return [_adapt_parameter(p, binary_encoding) for p in parameters]


_TIME_RE = re.compile(r"^(\d{1,2})[.:](\d{2})[.:](\d{2})")
_TS_RE = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})[ T-](\d{1,2})[.:](\d{2})[.:](\d{2})(?:[.,](\d+))?"
)


def _to_date(value):
    if isinstance(value, str):
        return datetime.date.fromisoformat(value[:10])
    return value


def _to_time(value):
    if isinstance(value, str):
        m = _TIME_RE.match(value)
        if m:
            return datetime.time(*(int(g) for g in m.groups()))
    return value


def _to_timestamp(value):
    if isinstance(value, str):
        m = _TS_RE.match(value)
        if m:
            parts = [int(g) for g in m.groups()[:6]]
            frac = m.group(7) or "0"
            micro = int((frac + "000000")[:6])
            return datetime.datetime(*parts, micro)
    return value


def _to_decimal(value):
    if isinstance(value, (int, str)) and not isinstance(value, bool):
        return decimal.Decimal(value)
    if isinstance(value, float):
        return decimal.Decimal(repr(value))
    return value


def _to_float(value):
    if isinstance(value, (decimal.Decimal, int, str)) and not isinstance(value, bool):
        return float(value)
    return value


def _to_int(value):
    if isinstance(value, (decimal.Decimal, str)):
        return int(value)
    return value


def _to_bool(value):
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "t", "y", "yes")
    if value is None:
        return None
    return bool(value)


_CONVERTERS = {
    "DATE": _to_date,
    "TIME": _to_time,
    "TIMESTAMP": _to_timestamp,
    "DECIMAL": _to_decimal,
    "NUMERIC": _to_decimal,
    "DECFLOAT": _to_decimal,
    "DOUBLE": _to_float,
    "REAL": _to_float,
    "FLOAT": _to_float,
    "SMALLINT": _to_int,
    "INTEGER": _to_int,
    "BIGINT": _to_int,
    "BOOLEAN": _to_bool,
}

_BINARY_TYPE_RE = re.compile(r"BINARY|BLOB|FOR BIT DATA", re.I)


# ---------------------------------------------------------------------------
# Query / Cursor / Connection
# ---------------------------------------------------------------------------


def _json_default(value):
    if isinstance(value, decimal.Decimal):
        return format(value, "f")
    raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


class _Query(_MapepireQuery):
    """mapepire Query that keeps DECIMAL precision and asks for terse rows."""

    def _execute_query(self, request):
        if self.job._socket is None:
            raise RuntimeError("SQL Job not connected")
        self.job.send(json.dumps(dataclasses.asdict(request), default=_json_default))
        return json.loads(self.job._socket.recv(), parse_float=decimal.Decimal)

    def prepare_sql_execute(self, rows: int):
        if self.state == QueryState.RUN_DONE:
            raise Exception("Statement has already been fully run")
        self._rows_to_fetch = rows
        query_result = QueryResult.from_dict(  # type: ignore[attr-defined]
            self._execute_query(
                PrepareSqlExecuteRequest(
                    id=self.job._get_unique_id("prepare_sql_execute"),
                    sql=self.sql,
                    rows=rows,
                    terse=True,
                    parameters=self.parameters,
                )
            )
        )
        self.state = (
            QueryState.RUN_DONE
            if query_result.is_done
            else QueryState.RUN_MORE_DATA_AVAIL
        )
        if not query_result.success:
            self.state = QueryState.ERROR
            error = {
                k: v
                for k, v in {
                    "error": query_result.error,
                    "sql_state": query_result.sql_state,
                    "sql_rc": query_result.sql_rc,
                }.items()
                if v
            }
            error.setdefault("error", "failed to run query for unknown reason")
            raise RuntimeError(error)
        self._correlation_id = query_result.id
        return query_result


class Cursor(_MapepireCursor):
    """A mapepire_python.Cursor with type conversion and SQLSTATE mapping."""

    #: Rows returned with the initial execute response, and per fetch round trip.
    fetch_size = 500

    def __init__(self, connection: "Connection", job) -> None:
        super().__init__(connection, job)
        self._converters = ()
        self.lastrowid = None

    # -- execution ---------------------------------------------------------

    def _set_has_results(self, value: bool) -> None:
        # mapepire's Cursor keeps this in a name-mangled private attribute
        self._Cursor__set_has_results(value)

    def _reset(self):
        self.query = None
        self._metadata = None
        self._converters = ()
        self.rowcount = -1
        self._set_has_results(False)

    @_translate_errors
    def execute(self, operation, parameters=None, **kwargs):
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed cursor.")
        enc = self.connection.binary_encoding
        params = _adapt_parameters(parameters, enc) if parameters else None
        return self._execute(operation, params)

    @_translate_errors
    def executemany(self, operation, seq_of_parameters, **kwargs):
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed cursor.")
        enc = self.connection.binary_encoding
        batch = [_adapt_parameters(p, enc) for p in seq_of_parameters]
        if not batch:
            self._reset()
            return self
        if not batch[0]:
            # A batch of parameterless statements: just run them one by one.
            total = 0
            for _ in batch:
                self._execute(operation, None)
                total += max(self.rowcount, 0)
            self.rowcount = total
            return self
        self._execute(operation, batch)
        return self

    def _execute(self, operation, params):
        self._reset()
        if self.connection._needs_txn_reset:
            self.connection._restore_transaction_mode()
        query = _Query(
            self.job, operation, QueryOptions(parameters=params, autoClose=False)
        )
        result = query.prepare_sql_execute(rows=self.fetch_size)
        qs = QueryResultSet(result)
        self._metadata = qs.metadata if qs.metadata.columns else None

        if result.has_results:
            self.query = query
            self._set_has_results(True)
            self.query_q.append(query)
            self._results[query] = _ResultState(self._metadata, deque(qs.data or []))
            self._converters = tuple(
                self._converter_for(col.type) for col in self._metadata.columns
            )
        else:
            count = result.update_count
            self.rowcount = count if count is not None and count >= 0 else -1
        return self

    # -- fetching ----------------------------------------------------------

    def _converter_for(self, type_name):
        type_name = (type_name or "").upper()
        if _BINARY_TYPE_RE.search(type_name):
            return self._to_bytes
        return _CONVERTERS.get(type_name)

    def _to_bytes(self, value):
        if isinstance(value, dict) and "blob_url" in value:
            return self.connection._fetch_blob(value["blob_url"])
        if isinstance(value, str):
            if self.connection.binary_encoding == "hex":
                return bytes.fromhex(value)
            return base64.b64decode(value)
        if isinstance(value, list):
            return bytes(value)
        return value

    def _convert_row(self, row):
        if not self._converters:
            return row
        return tuple(
            value if value is None or conv is None else conv(value)
            for value, conv in zip(row, self._converters)
        )

    def _fetch_rows(self, size, batch_size=0):
        """Return up to ``size`` converted rows, fetching blocks as needed.

        Unlike the base implementation, a single round trip never asks the
        server for more than ``max_fetch_block`` rows (``fetchall()`` would
        otherwise request two billion rows in one response)."""
        if self.query is None:
            return []
        state = self._results[self.query]
        rows = []
        while len(rows) < size:
            if not state.rows:
                if self.query.state == QueryState.RUN_DONE:
                    break
                want = max(min(size - len(rows), self.max_fetch_block), self.fetch_size)
                res = self.query.fetch_more(rows_to_fetch=want)
                if not res.data:
                    break
                state.rows.extend(res.data)
            rows.append(
                self._convert_row(row_to_tuple(state.rows.popleft(), state.metadata))
            )
        return rows

    #: Upper bound on rows requested per round trip.
    max_fetch_block = 10000

    fetchone = _translate_errors(_MapepireCursor.fetchone)
    fetchmany = _translate_errors(_MapepireCursor.fetchmany)
    fetchall = _translate_errors(_MapepireCursor.fetchall)

    @property
    def description(self):
        if not self._metadata or not self._metadata.columns:
            return None
        return [
            (
                col.name,
                (col.type or "").upper(),
                col.display_size,
                None,
                col.precision,
                col.scale,
                None if col.nullable is None else bool(col.nullable),
            )
            for col in self._metadata.columns
        ]

    @_translate_errors
    def close(self):
        try:
            super().close()
        except ReferenceError:
            self._closed = True

    def callproc(self, procname, parameters=None):
        params = list(parameters or [])
        markers = ", ".join("?" for _ in params)
        self.execute(f"CALL {procname}({markers})", params or None)
        return parameters


class Connection(_MapepireConnection):
    """A mapepire_python.Connection with transaction control.

    Mapepire runs every connection as a JDBC connection on the server.
    We open it with ``auto commit=false`` so that SQLAlchemy can control
    transaction boundaries, and implement ``autocommit=True`` by switching
    the job to isolation level ``NO COMMIT`` (``*NC``), where every statement
    is committed as it runs.
    """

    #: SQLAlchemy-style level name -> ``SET TRANSACTION ISOLATION LEVEL`` clause
    ISOLATION_LEVELS = {
        "READ UNCOMMITTED": "READ UNCOMMITTED, READ WRITE",
        "READ COMMITTED": "READ COMMITTED",
        "REPEATABLE READ": "REPEATABLE READ",
        "SERIALIZABLE": "SERIALIZABLE",
        "NO COMMIT": "NO COMMIT",
    }

    def __init__(
        self,
        server: DaemonServer,
        jdbc_options: Optional[Dict[str, Any]] = None,
        isolation_level: str = "READ COMMITTED",
    ) -> None:
        try:
            super().__init__(server, jdbc_options or {})
        except Exception as err:
            new = _translate(err)
            if type(new) in (DatabaseError, Error):
                # e.g. "User ID is disabled", bad password, unreachable host
                new = OperationalError(str(new))
            raise new from err
        self._server = server
        self._autocommit = False
        self._isolation_level = isolation_level
        self._needs_txn_reset = False
        # mapepire-python opens the websocket with the `websockets` default 1 MiB
        # message limit; a large/wide fetch block would close the connection
        # (Mapepire-IBMi/mapepire-python#173). Block size is bounded by row
        # count instead, so lift the byte limit.
        protocol = getattr(self.job._socket, "protocol", None)
        if protocol is not None and hasattr(protocol, "max_message_size"):
            protocol.max_message_size = None
        self.server_version = self._get_server_version()
        #: How binary values travel over the wire: base64 since server 2.4.0
        #: (which also introduced /blob/ URLs for result values), hex before.
        self.binary_encoding = "base64" if self.server_version >= (2, 4) else "hex"

    # -- helpers -----------------------------------------------------------

    def _run(self, sql: str) -> None:
        cur = self.cursor()
        try:
            cur._execute(sql, None)
        finally:
            cur.close()

    def _restore_transaction_mode(self):
        self._needs_txn_reset = False
        level = "NO COMMIT" if self._autocommit else self._isolation_level
        self._run("SET TRANSACTION ISOLATION LEVEL " + self.ISOLATION_LEVELS[level])

    def _get_server_version(self):
        """Return the Mapepire server version as a tuple of ints."""
        try:
            self.job.send(
                json.dumps(
                    dataclasses.asdict(
                        GetVersionRequest(id=self.job._get_unique_id("getversion"))
                    )
                )
            )
            reply = json.loads(self.job._socket.recv())
            parts = re.findall(r"\d+", reply.get("version") or "")
            return tuple(int(p) for p in parts[:3]) or (0,)
        except Exception:  # pragma: no cover - very old servers
            logger.warning("Unable to determine Mapepire server version", exc_info=True)
            return (0,)

    def _fetch_blob(self, blob_url: str) -> bytes:
        """Download a BLOB/BINARY value the server parked at /blob/{token}.

        Tokens are single use and expire after a short TTL on the server, so
        values are downloaded as rows are fetched."""
        server = self._server
        base = _MapepireBaseConnection(server)
        url = f"https://{server.host}:{server.port}{blob_url}"
        request = urllib.request.Request(url, headers=base.headers)
        context = base._create_ssl_context(server)
        with urllib.request.urlopen(request, context=context, timeout=60) as resp:
            return resp.read()

    # -- PEP 249 -----------------------------------------------------------

    def cursor(self) -> Cursor:
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed connection.")
        return Cursor(self, self.job)

    @_translate_errors
    def commit(self) -> None:
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed connection.")
        self._run("COMMIT")

    @_translate_errors
    def rollback(self) -> None:
        if self._closed:
            raise ProgrammingError("Cannot operate on a closed connection.")
        self._run("ROLLBACK")

    @_translate_errors
    def close(self) -> None:
        if self._closed:
            return
        try:
            if not self._autocommit:
                self._run("ROLLBACK")
        except Exception:  # pragma: no cover - best effort
            pass
        super().close()

    def execute(self, operation, parameters=None):
        return self.cursor().execute(operation, parameters)

    def executemany(self, operation, seq_of_parameters):
        return self.cursor().executemany(operation, seq_of_parameters)

    @property
    def closed(self) -> bool:
        return self._closed

    # -- extensions --------------------------------------------------------

    @property
    def autocommit(self) -> bool:
        return self._autocommit

    @autocommit.setter
    @_translate_errors
    def autocommit(self, value: bool) -> None:
        value = bool(value)
        if value == self._autocommit:
            return
        if value:
            self._run("COMMIT")
        self._autocommit = value
        self._restore_transaction_mode()

    @property
    def isolation_level(self) -> str:
        return self._isolation_level

    @_translate_errors
    def set_isolation_level(self, level: str) -> None:
        level = level.upper().replace("_", " ")
        if level not in self.ISOLATION_LEVELS:
            raise ProgrammingError(f"Invalid isolation level {level!r}")
        self._isolation_level = level
        if not self._autocommit:
            self._restore_transaction_mode()

    @_translate_errors
    def ping(self) -> bool:
        self._run("VALUES 1")
        return True


#: JDBC (JTOpen) properties that the SQLAlchemy dialect depends on.
REQUIRED_JDBC_OPTIONS = {
    "auto commit": "false",
    "date format": "iso",
    "time format": "iso",
}

_JDBC_ISOLATION = {
    "READ UNCOMMITTED": "read uncommitted",
    "READ COMMITTED": "read committed",
    "REPEATABLE READ": "repeatable read",
    "SERIALIZABLE": "serializable",
}


def connect(
    host: str,
    user: str,
    password: str,
    port: int = 8076,
    ignore_unauthorized: bool = False,
    ca: Optional[str] = None,
    jdbc_options: Optional[Dict[str, Any]] = None,
    isolation_level: str = "READ COMMITTED",
    autocommit: bool = False,
    **unused: Any,
) -> Connection:
    """Open a connection to a Mapepire server.

    :param host: IBM i host name running the Mapepire server
    :param user: IBM i user profile
    :param password: password
    :param port: Mapepire server port (default 8076)
    :param ignore_unauthorized: do not verify the server's TLS certificate
    :param ca: PEM text of the CA certificate to trust, or a path to a PEM file
    :param jdbc_options: extra JTOpen JDBC properties, e.g. ``{"naming": "system"}``
    :param isolation_level: initial isolation level
    :param autocommit: start in autocommit mode
    """
    if unused:
        raise InterfaceError(f"Unknown connect() arguments: {', '.join(unused)}")
    if ca and not ca.lstrip().startswith("-----BEGIN"):
        with open(ca, "r", encoding="ascii") as f:
            ca = f.read()

    isolation_level = isolation_level.upper().replace("_", " ")
    if isolation_level not in Connection.ISOLATION_LEVELS:
        raise ProgrammingError(f"Invalid isolation level {isolation_level!r}")

    options = dict(jdbc_options or {})
    options.update(REQUIRED_JDBC_OPTIONS)
    if isolation_level in _JDBC_ISOLATION:
        options["transaction isolation"] = _JDBC_ISOLATION[isolation_level]

    server = DaemonServer(
        host=host,
        port=int(port),
        user=user,
        password=password,
        ignoreUnauthorized=bool(ignore_unauthorized),
        ca=ca,
    )
    conn = Connection(server, options, isolation_level)
    if isolation_level == "NO COMMIT":
        conn._needs_txn_reset = True
    if autocommit:
        conn.autocommit = True
    return conn
