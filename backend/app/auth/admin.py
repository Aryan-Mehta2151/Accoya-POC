"""Database-local administrator CLI for closed user enrollment."""

from __future__ import annotations

import argparse
import getpass
import sys
from collections.abc import Callable, Sequence

from pydantic import EmailStr, TypeAdapter, ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.database import SessionLocal
from app.db.models import PasswordResetToken, User
from app.services.auth_security import normalize_email
from app.services.password_service import hash_password, validate_password


_email_adapter = TypeAdapter(EmailStr)


class AdminCommandError(ValueError):
    """An expected administrator input or account-state failure."""


def _validated_email(raw_email: str) -> str:
    try:
        parsed = str(_email_adapter.validate_python(raw_email))
    except ValidationError as exc:
        raise AdminCommandError("Invalid email address.") from exc
    normalized = normalize_email(parsed)
    if len(normalized) > 255:
        raise AdminCommandError("Email address is too long.")
    return normalized


def _password_from_prompt() -> str:
    first = getpass.getpass("Password: ")
    second = getpass.getpass("Confirm password: ")
    if first != second:
        raise AdminCommandError("Passwords do not match.")
    try:
        return validate_password(first)
    except ValueError as exc:
        raise AdminCommandError(str(exc)) from exc


def _find_user(db: Session, raw_email: str, *, lock: bool = False) -> User:
    email = _validated_email(raw_email)
    statement = select(User).where(User.email == email)
    if lock:
        statement = statement.with_for_update()
    user = db.execute(statement).scalars().first()
    if user is None:
        raise AdminCommandError(f"No user exists for {email}.")
    return user


def _create(db: Session, args: argparse.Namespace) -> str:
    email = _validated_email(args.email)
    if db.execute(select(User.id).where(User.email == email)).first() is not None:
        raise AdminCommandError(f"A user already exists for {email}.")
    name = args.name.strip() if args.name else None
    if name and len(name) > 255:
        raise AdminCommandError("Name is too long.")
    password_hash = None
    if not args.google_only:
        password_hash = hash_password(_password_from_prompt())
    user = User(
        email=email,
        name=name or None,
        password_hash=password_hash,
        is_active=True,
        auth_version=0,
    )
    db.add(user)
    db.flush()
    method = "Google-only" if args.google_only else "password"
    return f"Created active {method} user {email}."


def _enable(db: Session, args: argparse.Namespace) -> str:
    user = _find_user(db, args.email, lock=True)
    user.is_active = True
    return f"Enabled {user.email}."


def _disable(db: Session, args: argparse.Namespace) -> str:
    user = _find_user(db, args.email, lock=True)
    user.is_active = False
    user.auth_version += 1
    db.execute(
        delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    return f"Disabled {user.email} and revoked its sessions and reset links."


def _set_password(db: Session, args: argparse.Namespace) -> str:
    user = _find_user(db, args.email, lock=True)
    password = _password_from_prompt()
    user.password_hash = hash_password(password)
    user.auth_version += 1
    db.execute(
        delete(PasswordResetToken).where(PasswordResetToken.user_id == user.id)
    )
    return f"Updated the password for {user.email} and revoked its sessions."


def _revoke_sessions(db: Session, args: argparse.Namespace) -> str:
    user = _find_user(db, args.email, lock=True)
    user.auth_version += 1
    return f"Revoked sessions for {user.email}."


def _list_users(db: Session, _: argparse.Namespace) -> str:
    users = db.execute(select(User).order_by(User.email)).scalars().all()
    lines = ["EMAIL\tNAME\tACTIVE\tAUTHENTICATION"]
    for user in users:
        methods: list[str] = []
        if user.password_hash:
            methods.append("password")
        if user.oauth_provider:
            methods.append(user.oauth_provider)
        lines.append(
            "\t".join(
                (
                    user.email,
                    user.name or "",
                    "yes" if user.is_active else "no",
                    ",".join(methods) or "none",
                )
            )
        )
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Manage approved Accoya web application users."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create an active approved user")
    create.add_argument("--email", required=True)
    create.add_argument("--name")
    create.add_argument(
        "--google-only",
        action="store_true",
        help="Create an approved account without a password for first Google login",
    )
    create.set_defaults(handler=_create)

    for command, help_text, handler in (
        ("enable", "Enable an existing account", _enable),
        ("disable", "Disable an account and revoke sessions", _disable),
        ("set-password", "Set a password and revoke sessions", _set_password),
        ("revoke-sessions", "Revoke all current sessions", _revoke_sessions),
    ):
        subparser = subparsers.add_parser(command, help=help_text)
        subparser.add_argument("--email", required=True)
        subparser.set_defaults(handler=handler)

    list_parser = subparsers.add_parser("list", help="List approved accounts")
    list_parser.set_defaults(handler=_list_users)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run one administrator command and return a process exit code."""

    args = _build_parser().parse_args(argv)
    handler: Callable[[Session, argparse.Namespace], str] = args.handler
    try:
        with SessionLocal() as db:
            with db.begin():
                message = handler(db, args)
        print(message)
        return 0
    except AdminCommandError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except IntegrityError:
        print(
            "Error: the account change conflicted with current database state.",
            file=sys.stderr,
        )
        return 2
    except SQLAlchemyError:
        print("Database operation failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
