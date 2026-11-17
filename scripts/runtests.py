#!/usr/bin/env python
"""Run the SQLAlchemy dialect compliance suite against a live IBM i.

Connection details come from the environment:

    IBMI_HOSTNAME   host running the Mapepire server
    IBMI_USERNAME   user profile
    IBMI_PASSWORD   password
    IBMI_PORT       Mapepire port (default 8076)
    IBMI_SCHEMA     schema (library) to create test tables in; defaults to the
                    user's default schema. Use a dedicated, journaled schema:
                    the reflection tests expect it to contain only the
                    tables the suite creates.
    IBMI_IGNORE_UNAUTHORIZED   set to 1 to skip TLS certificate verification

Extra arguments are passed to pytest, e.g.
``python scripts/runtests.py test/test_suite.py -k Boolean``.
"""

import os
import sys

import pytest
from sqlalchemy.engine import URL

url = URL.create(
    "ibmi+mapepire",
    username=os.environ["IBMI_USERNAME"],
    password=os.environ["IBMI_PASSWORD"],
    host=os.environ["IBMI_HOSTNAME"],
    port=int(os.environ.get("IBMI_PORT", "8076")),
    query={"ignore_unauthorized": os.environ.get("IBMI_IGNORE_UNAUTHORIZED", "0")},
)
if os.environ.get("IBMI_SCHEMA"):
    url = url.update_query_dict({"current_schema": os.environ["IBMI_SCHEMA"]})

sys.exit(
    pytest.main(["--dburi", url.render_as_string(hide_password=False)] + sys.argv[1:])
)
