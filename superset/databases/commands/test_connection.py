# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.
import logging
from contextlib import closing
from typing import Any, Dict, Optional

from flask_appbuilder.security.sqla.models import User
from flask_babel import gettext as _
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import DBAPIError, NoSuchModuleError

from superset.commands.base import BaseCommand
from superset.databases.commands.exceptions import (
    DatabaseSecurityUnsafeError,
    DatabaseTestConnectionDriverError,
    DatabaseTestConnectionFailedError,
    DatabaseTestConnectionUnexpectedError,
)
from superset.databases.dao import DatabaseDAO
from superset.exceptions import SupersetSecurityException
from superset.extensions import event_logger
from superset.models.core import Database

logger = logging.getLogger(__name__)


class TestConnectionDatabaseCommand(BaseCommand):
    def __init__(self, user: User, data: Dict[str, Any]):
        self._actor = user
        self._properties = data.copy()
        self._model: Optional[Database] = None

    def run(self) -> None:
        self.validate()
        uri = self._properties.get("sqlalchemy_uri", "")
        if self._model and uri == self._model.safe_sqlalchemy_uri():
            uri = self._model.sqlalchemy_uri_decrypted
        try:
            database = DatabaseDAO.build_db_for_connection_test(
                server_cert=self._properties.get("server_cert", ""),
                extra=self._properties.get("extra", "{}"),
                impersonate_user=self._properties.get("impersonate_user", False),
                encrypted_extra=self._properties.get("encrypted_extra", "{}"),
            )

            database.set_sqlalchemy_uri(uri)
            database.db_engine_spec.mutate_db_for_connection_test(database)
            username = self._actor.username if self._actor is not None else None
            engine = database.get_sqla_engine(user_name=username)
            with closing(engine.raw_connection()) as conn:
                if not engine.dialect.do_ping(conn):
                    raise DBAPIError(None, None, None)

            # Log succesful connection test with engine
            event_logger.log_with_context(
                action="test_connection_success",
                engine=database.db_engine_spec.__name__,
            )

        except (NoSuchModuleError, ModuleNotFoundError) as ex:
            event_logger.log_with_context(
                action=f"test_connection_error.{ex.__class__.__name__}",
                engine=database.db_engine_spec.__name__,
            )
            raise DatabaseTestConnectionDriverError(
                message=_("Could not load database driver: {}").format(
                    database.db_engine_spec.__name__
                ),
            )
        except DBAPIError as ex:
            event_logger.log_with_context(
                action=f"test_connection_error.{ex.__class__.__name__}",
                engine=database.db_engine_spec.__name__,
            )
            # check for custom errors (wrong username, wrong password, etc)
            url = make_url(uri)
            context = {
                "hostname": url.host,
                "password": url.password,
                "port": url.port,
                "username": url.username,
            }
            errors = database.db_engine_spec.extract_errors(ex, context)
            raise DatabaseTestConnectionFailedError(errors)
        except SupersetSecurityException as ex:
            event_logger.log_with_context(
                action=f"test_connection_error.{ex.__class__.__name__}",
                engine=database.db_engine_spec.__name__,
            )
            raise DatabaseSecurityUnsafeError(message=str(ex))
        except Exception as ex:  # pylint: disable=broad-except
            event_logger.log_with_context(
                action=f"test_connection_error.{ex.__class__.__name__}",
                engine=database.db_engine_spec.__name__,
            )
            raise DatabaseTestConnectionUnexpectedError(str(ex))

    def validate(self) -> None:
        database_name = self._properties.get("database_name")
        if database_name is not None:
            self._model = DatabaseDAO.get_database_by_name(database_name)
