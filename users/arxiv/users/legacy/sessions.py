"""Provides API for legacy user sessions."""

import ipaddress
import json
from datetime import datetime, timedelta
from pytz import timezone
import hashlib
from base64 import b64encode, b64decode

from typing import Optional, Generator, Tuple, List

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm.exc import NoResultFound
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.session import Session

from arxiv.base import logging

from .. import domain
from . import cookies, util

from .models import Base, DBSession, DBSessionsAudit, DBUser, DBEndorsement, \
    DBUserNickname
from .exceptions import UnknownSession, SessionCreationFailed, \
    SessionDeletionFailed, SessionExpired, InvalidCookie
from .endorsements import get_endorsements

logger = logging.getLogger(__name__)
EASTERN = timezone('US/Eastern')


def _load(session_id: str) -> DBSession:
    """Get DBSession from session id."""
    with util.transaction() as session:
        db_session: DBSession = session.query(DBSession) \
            .filter(DBSession.session_id == session_id) \
            .first()
    if not db_session:
        logger.error(f'No session found with id {session_id}')
        raise UnknownSession('No such session')
    return db_session


def load(cookie: str) -> domain.Session:
    """
    Given a session cookie (from request), load the logged-in user.

    Parameters
    ----------
    cookie : str
        Legacy cookie value passed with the request.

    Returns
    -------
    :class:`.domain.Session`

    Raises
    ------
    :class:`.legacy.exceptions.SessionExpired`
    :class:`.legacy.exceptions.UnknownSession`

    """
    session_id, user_id, ip, issued_at, expires_at, _ = cookies.unpack(cookie)
    logger.debug('Load session %s for user %s at %s',
                 session_id, user_id, ip)

    if expires_at <= datetime.now(tz=EASTERN):
        raise SessionExpired(f'Session {session_id} has expired')

    with util.transaction() as session:
        data: Tuple[DBUser, DBSession, DBUserNickname] = (
            session.query(DBUser, DBSession, DBUserNickname)
            .filter(DBUser.user_id == user_id)
            .filter(DBUserNickname.user_id == user_id)
            .filter(DBSession.user_id == user_id)
            .filter(DBSession.session_id == session_id)
            .first()
        )

        if not data:
            raise UnknownSession('No such user or session')

        db_user, db_session, db_nick = data

        # Verify that the session is not expired.
        if db_session.end_time != 0 and db_session.end_time < util.now():
            logger.info('Session has expired: %s', session_id)
            raise SessionExpired(f'Session {session_id} has expired')

        user = domain.User(
            user_id=str(user_id),
            username=db_nick.nickname,
            email=db_user.email,
            name=domain.UserFullName(
                forename=db_user.first_name,
                surname=db_user.last_name,
                suffix=db_user.suffix_name
            ),
            verified=bool(db_user.flag_email_verified)
        )

        # We should get one row per endorsement.
        authorizations = domain.Authorizations(
            classic=util.compute_capabilities(db_user),
            endorsements=get_endorsements(user),
            scopes=util.get_scopes(db_user)
        )

    user_session = domain.Session(str(db_session.session_id),
                                  start_time=issued_at, end_time=expires_at,
                                  user=user, authorizations=authorizations)
    logger.debug('loaded session %s', user_session.session_id)
    return user_session


def create(authorizations: domain.Authorizations,
           ip: str, remote_host: str, tracking_cookie: str = '',
           user: Optional[domain.User] = None) -> domain.Session:
    """
    Create a new legacy session for an authenticated user.

    Parameters
    ----------
    user : :class:`.User`
    ip : str
        Client IP address.
    remote_host : str
        Client hostname.
    tracking_cookie : str
        Tracking cookie payload from client request.

    Returns
    -------
    :class:`.Session`

    """
    if user is None:
        raise SessionCreationFailed('Legacy sessions require a user')

    logger.debug('create session for user %s', user.user_id)
    start = datetime.now(tz=EASTERN)
    end = start + timedelta(seconds=util.get_session_duration())
    try:
        with util.transaction() as session:
            tapir_session = DBSession(
                user_id=user.user_id,
                last_reissue=util.epoch(start),
                start_time=util.epoch(start),
                end_time=0
            )
            tapir_sessions_audit = DBSessionsAudit(
                session=tapir_session,
                ip_addr=ip,
                remote_host=remote_host,
                tracking_cookie=tracking_cookie
            )
            session.add(tapir_sessions_audit)
            session.commit()
    except Exception as e:  # TODO: be more specific.
        raise SessionCreationFailed(f'Failed to create: {e}') from e

    user_session = domain.Session(str(tapir_session.session_id), user=user,
                                  start_time=start, end_time=end,
                                  authorizations=authorizations)
    logger.debug('created session %s', user_session.session_id)
    return user_session


def generate_cookie(session: domain.Session) -> str:
    """
    Generate a cookie from a :class:`domain.Session`.

    Parameters
    ----------
    session : :class:`domain.Session`

    Returns
    -------
    str
    """
    return cookies.pack(str(session.session_id), session.user.user_id,
                        session.ip_address, session.start_time,
                        str(session.authorizations.classic))


def invalidate(cookie: str) -> None:
    """
    Invalidate a legacy user session.

    Parameters
    ----------
    cookie : str
        Session cookie generated when the session was created.

    Raises
    ------
    :class:`UnknownSession`
        The session could not be found, or the cookie was not valid.

    """
    try:
        session_id, user_id, ip, _, _, _ = cookies.unpack(cookie)
    except InvalidCookie as e:
        raise UnknownSession('No such session') from e

    invalidate_by_id(session_id)


def invalidate_by_id(session_id: str) -> None:
    """
    Invalidate a legacy user session by ID.

    Parameters
    ----------
    session_id : str
        Unique identifier for the session.

    Raises
    ------
    :class:`UnknownSession`
        The session could not be found, or the cookie was not valid.

    """
    delta = datetime.now(tz=EASTERN) - datetime.fromtimestamp(0, tz=EASTERN)
    end = (delta).total_seconds()
    try:
        with util.transaction() as session:
            tapir_session = _load(session_id)
            tapir_session.end_time = end - 1
            session.merge(tapir_session)
    except NoResultFound as e:
        raise UnknownSession(f'No such session {session_id}') from e
    except SQLAlchemyError as e:
        raise IOError(f'Database error') from e
