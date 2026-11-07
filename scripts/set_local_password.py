"""Set the local-only password for the designated NVSENTRY account."""

import argparse
import getpass
import hashlib
import json
import os
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import app


def save_password(password: str) -> None:
    if not 12 <= len(password) <= 1024:
        raise SystemExit("Password must be between 12 and 1024 characters.")

    salt = secrets.token_bytes(16)
    password_hash = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt,
        app.LOCAL_PASSWORD_ITERATIONS,
        dklen=32,
    )
    config = {
        "phone": app.LOCAL_PASSWORD_PHONE,
        "salt": salt.hex(),
        "password_hash": password_hash.hex(),
    }
    app.LOCAL_AUTH_PATH.write_text(
        json.dumps(config, indent=2) + "\n",
        encoding="utf-8",
    )
    if os.name == "posix":
        os.chmod(app.LOCAL_AUTH_PATH, 0o600)
    print(f"Password hash saved to {app.LOCAL_AUTH_PATH}. The password itself was not stored.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--generate",
        action="store_true",
        help="generate a random password instead of prompting for one",
    )
    args = parser.parse_args()
    if args.generate:
        password = secrets.token_urlsafe(24)
        save_password(password)
        print(f"Generated password for {app.LOCAL_PASSWORD_PHONE}: {password}")
        return

    password = getpass.getpass("Choose a password (minimum 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        raise SystemExit("Passwords do not match.")
    save_password(password)


if __name__ == "__main__":
    main()
