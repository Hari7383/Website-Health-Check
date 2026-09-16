#!/usr/bin/env python
"""Account management and local server launcher.

    python manage.py adduser          create or replace an account
    python manage.py listusers        show accounts
    python manage.py deluser NAME     remove an account
    python manage.py runserver        start the app on http://127.0.0.1:5000

Passwords are typed at a hidden prompt, hashed with scrypt, and never stored
or echoed in plaintext.
"""
import getpass
import sys

from app import auth


def adduser():
    print("Create an account for the hygiene check tool.\n")
    username = input("Username (used to sign in): ").strip()
    display_name = input("Display name (goes in the Checked By column): ").strip()

    password = getpass.getpass("Password (min 10 characters, not shown): ")
    confirm = getpass.getpass("Confirm password: ")

    if password != confirm:
        print("\nPasswords did not match. Nothing was saved.")
        return 1

    try:
        auth.add_user(username, password, display_name)
    except ValueError as exc:
        print(f"\n{exc}")
        return 1

    print(f"\nAccount '{username}' created. Checks will be recorded as '{display_name}'.")
    return 0


def listusers():
    users = auth.list_users()
    if not users:
        print("No accounts yet. Run: python manage.py adduser")
        return 0
    print(f"{len(users)} account(s):\n")
    for u in users:
        print(f"  {u['username']:<20} -> {u['display_name']}")
    return 0


def deluser():
    if len(sys.argv) < 3:
        print("Usage: python manage.py deluser USERNAME")
        return 1
    name = sys.argv[2]
    if auth.delete_user(name):
        print(f"Removed '{name}'.")
        return 0
    print(f"No account named '{name}'.")
    return 1


def runserver():
    from app.server import app
    if auth.user_count() == 0:
        print("No accounts exist yet. In another terminal run:\n")
        print("    python manage.py adduser\n")
    print("Website Hygiene Check running at http://127.0.0.1:5000")
    print("Press Ctrl+C to stop.\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
    return 0


COMMANDS = {"adduser": adduser, "listusers": listusers,
            "deluser": deluser, "runserver": runserver}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "runserver"
    if cmd not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    sys.exit(COMMANDS[cmd]())
