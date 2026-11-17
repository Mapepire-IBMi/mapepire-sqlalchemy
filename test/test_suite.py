from sqlalchemy.testing.suite import *  # noqa - need * to import test suite
from sqlalchemy.testing.suite import testing

from sqlalchemy.testing.suite import ComponentReflectionTest as _ComponentReflectionTest
from sqlalchemy.testing.suite import ExpandingBoundInTest as _ExpandingBoundInTest
from sqlalchemy.testing.suite import InsertBehaviorTest as _InsertBehaviorTest
from sqlalchemy.testing.suite import (
    ComponentReflectionTestExtra as _ComponentReflectionTestExtra,
)


class ComponentReflectionTest(_ComponentReflectionTest):
    @testing.combinations(
        (True, testing.requires.schemas), (False,), argnames="use_schema"
    )
    @testing.requires.unique_constraint_reflection
    @testing.skip("ibmi", "Db2 doesn't support duplicate constraints")
    def test_get_unique_constraints(self, metadata, connection, use_schema):
        pass


# empty set tests not possible on DB2 for i
class ExpandingBoundInTest(_ExpandingBoundInTest):
    @testing.skip("ibmi")
    def test_multiple_empty_sets(self):
        pass

    @testing.skip("ibmi")
    def test_empty_set_against_integer(self):
        pass

    @testing.skip("ibmi")
    def test_empty_set_against_integer_negation(self):
        pass

    @testing.skip("ibmi")
    def test_empty_set_against_string(self):
        pass

    @testing.skip("ibmi")
    def test_empty_set_against_string_negation(self):
        pass

    @testing.skip("ibmi")
    def test_null_in_empty_set_is_false(self):
        pass


class InsertBehaviorTest(_InsertBehaviorTest):
    # Skipping test due to incompatible sql query with Db2. Using parameter
    # markers in a arithmetic expression is not supported. To force this to
    # work, one can cast the parameter marker to int or float before
    # performing the operation. However, this will not work here due to
    # SQLAlchemy code
    @testing.skip("ibmi")
    def test_insert_from_select_with_defaults(self):
        pass


class ComponentReflectionTestExtra(_ComponentReflectionTestExtra):
    # The upstream fixture always runs a "use_schema=True" variant against
    # config.test_schema, without checking requirements.schemas (which this
    # dialect, like sqlalchemy-ibmi, reports as unsupported).
    @testing.fixture(params=[False])
    def use_schema_fixture(self, request):
        return None


# NOTE: sqlalchemy-ibmi skips the test_literal / test_literal_non_ascii tests of
# StringTest, TextTest, UnicodeTextTest and UnicodeVarcharTest because of a bug
# in the IBM i Access ODBC Driver for Linux. That driver isn't involved here, so
# those tests run.
