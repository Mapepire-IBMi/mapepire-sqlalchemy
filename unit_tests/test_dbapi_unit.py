"""Offline unit tests for the dbapi layer and URL handling (no server needed)."""

import datetime
import decimal

import pytest
from sqlalchemy import create_engine

from sqlalchemy_mapepire import dbapi


class TestAdaptParameter:

    @pytest.mark.parametrize(
        "value, expected",
        [
            (None, None),
            (1, 1),
            (1.5, 1.5),
            ("abc", "abc"),
            (True, 1),
            (False, 0),
            (decimal.Decimal("12345678901234567.89"), "12345678901234567.89"),
            (decimal.Decimal("1E+3"), "1000"),
            (decimal.Decimal("1E-10"), "0.0000000001"),
            (datetime.date(2024, 2, 29), "2024-02-29"),
            (datetime.time(13, 45, 1, 500), "13:45:01"),
            (
                datetime.datetime(2024, 2, 29, 13, 45, 1, 123),
                "2024-02-29 13:45:01.000123",
            ),
        ],
    )
    def test_adapt(self, value, expected):
        assert dbapi._adapt_parameter(value) == expected

    def test_bytes_base64(self):
        assert dbapi._adapt_parameter(b"\x00\x01\xff", "base64") == "AAH/"

    def test_bytes_hex(self):
        assert dbapi._adapt_parameter(b"\x00\x01\xff", "hex") == "0001FF"

    def test_nonfinite_decimal(self):
        with pytest.raises(dbapi.DataError):
            dbapi._adapt_parameter(decimal.Decimal("NaN"))

    def test_named_params_rejected(self):
        with pytest.raises(dbapi.ProgrammingError):
            dbapi._adapt_parameters({"a": 1})


class TestConvertResult:

    def test_date(self):
        assert dbapi._to_date("2024-02-29") == datetime.date(2024, 2, 29)

    @pytest.mark.parametrize("value", ["13.45.01", "13:45:01"])
    def test_time(self, value):
        assert dbapi._to_time(value) == datetime.time(13, 45, 1)

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("2024-02-29 13:45:01", datetime.datetime(2024, 2, 29, 13, 45, 1)),
            (
                "2024-02-29 13:45:01.123456",
                datetime.datetime(2024, 2, 29, 13, 45, 1, 123456),
            ),
            (
                "2024-02-29-13.45.01.123456789012",
                datetime.datetime(2024, 2, 29, 13, 45, 1, 123456),
            ),
            (
                "2024-02-29 13:45:01.5",
                datetime.datetime(2024, 2, 29, 13, 45, 1, 500000),
            ),
        ],
    )
    def test_timestamp(self, value, expected):
        assert dbapi._to_timestamp(value) == expected

    def test_decimal(self):
        assert dbapi._to_decimal(5) == decimal.Decimal(5)
        d = decimal.Decimal("10.000")
        assert dbapi._to_decimal(d) is d

    def test_float(self):
        assert dbapi._to_float(decimal.Decimal("2.5")) == 2.5


class TestErrorTranslation:

    def _err(self, sqlstate):
        cause = RuntimeError({"error": "boom", "sql_state": sqlstate, "sql_rc": -1})
        try:
            raise dbapi.DatabaseError("boom") from cause
        except dbapi.DatabaseError as e:
            return dbapi._translate(e)

    @pytest.mark.parametrize(
        "sqlstate, cls",
        [
            ("23505", dbapi.IntegrityError),
            ("42704", dbapi.ProgrammingError),
            ("22001", dbapi.DataError),
            ("57033", dbapi.OperationalError),
            ("0A001", dbapi.NotSupportedError),
            ("99999", dbapi.DatabaseError),
        ],
    )
    def test_sqlstate(self, sqlstate, cls):
        err = self._err(sqlstate)
        assert type(err) is cls
        assert err.sqlstate == sqlstate

    def test_websocket_closed(self):
        err = dbapi._translate(Exception("SQL Job not connected"))
        assert isinstance(err, dbapi.OperationalError)


class TestURL:

    def _args(self, url):
        e = create_engine(url)
        return e.dialect.create_connect_args(e.url)[1]

    def test_basic(self):
        args = self._args("ibmi+mapepire://usr:pw@host.example.com/")
        assert args["host"] == "host.example.com"
        assert args["user"] == "usr"
        assert args["password"] == "pw"
        assert "port" not in args
        assert args["jdbc_options"] == {"naming": "sql"}
        assert args["isolation_level"] == "READ UNCOMMITTED"

    def test_short_scheme_and_port(self):
        args = self._args("mapepire://usr:pw@host:9000")
        assert args["port"] == 9000

    def test_options(self):
        args = self._args(
            "ibmi+mapepire://u:p@h/?current_schema=MYLIB&library_list=A,B"
            "&use_system_naming=true&readonly=1&timeout=5"
            "&autocommit=true&ignore_unauthorized=yes"
        )
        assert args["jdbc_options"] == {
            "libraries": "MYLIB,A,B",
            "naming": "system",
            "access": "read only",
            "login timeout": "5",
        }
        assert args["autocommit"] is True
        assert args["ignore_unauthorized"] is True

    def test_library_list_only(self):
        args = self._args("ibmi+mapepire://u:p@h/?library_list=A,B")
        assert args["jdbc_options"]["libraries"] == ",A,B"

    def test_isolation_level(self):
        e = create_engine("ibmi+mapepire://u:p@h/", isolation_level="SERIALIZABLE")
        assert e.dialect.create_connect_args(e.url)[1]["isolation_level"] == (
            "SERIALIZABLE"
        )
        e = create_engine("ibmi+mapepire://u:p@h/", isolation_level="AUTOCOMMIT")
        assert e.dialect.create_connect_args(e.url)[1]["isolation_level"] == (
            "NO COMMIT"
        )

    def test_bad_option(self):
        with pytest.raises(ValueError, match="bogus"):
            self._args("ibmi+mapepire://u:p@h/?bogus=1")
