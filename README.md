# NVSENTRY

A daily shop ledger with a browser interface, a Python API, and SQLite storage.

## Run locally

Requires Python 3.10 or later; there are no third-party Python dependencies.

```powershell
python app.py
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). The local database is created as
`daily_entries.sqlite3` in the project directory and is intentionally not tracked by Git.

## Free PythonAnywhere deployment

GitHub stores the source code, but does not run this Python API or its SQLite database.
PythonAnywhere's free Beginner account can host the full app at
`your-username.pythonanywhere.com`, subject to that plan's limits.

1. Create a free account at [PythonAnywhere](https://www.pythonanywhere.com/).
2. In the **Consoles** tab, open a **Bash** console and clone this repository:

   ```bash
   git clone https://github.com/TechBrains-bh/NVSENTRY.git
   ```

3. In the **Web** tab, create a web app using **Manual configuration** and Python 3.10
   or later. Open the WSGI configuration file linked on that tab, replace its contents
   with the following, and change `your-username` to your PythonAnywhere username:

   ```python
   import os
   import sys

   project_home = "/home/your-username/NVSENTRY"
   if project_home not in sys.path:
       sys.path.insert(0, project_home)

   os.environ["NVSENTRY_DB_PATH"] = "/home/your-username/.local/share/nvsentry.sqlite3"
   os.environ["NVSENTRY_BOOTSTRAP_SECRET"] = "paste-a-private-random-secret-here"

   from app import application
   ```

   Generate a secret in the Bash console with
   `python -c 'import secrets; print(secrets.token_urlsafe(32))'`. Put it only in the
   PythonAnywhere WSGI configuration, never in the GitHub repository. Click **Reload**
   on the Web tab, visit the web app URL, and use the secret once to create the platform
   owner account. After setup, remove the `NVSENTRY_BOOTSTRAP_SECRET` line from the WSGI
   configuration and reload the app.

The database is stored outside the cloned repository so code updates do not overwrite
shop records. Back up the SQLite file before updating an existing deployment: old
date-only ledger entries are moved into the first shop registered after this update.
The first platform owner can register multiple shops, issue shop-owner invitations,
and grant platform roles. Shop owners can register staff for their shops. Login codes
are generated in the app and must currently be delivered privately by the issuer;
WhatsApp delivery is not configured. Codes expire in 10 minutes and are single-use.
Signed-in sessions expire after 24 hours. Keep a separate backup of the database; the
app does not currently provide automatic backups.

### Optional password sign-in for the designated account

The account `9350025160` can use an account-specific password. After successful
password sign-in, that account is granted the `platform_owner` role and can access
every shop and its ledger, manage shop staff, and create shops. From the project root,
run `python scripts/set_local_password.py` and enter a password of at least 12
characters twice. The app writes only a salted password hash to `local_auth.json`;
that root-level file is ignored by Git. Never remove it from `.gitignore`, commit it,
or copy it into a public repository. Run the same command to change the password.

Password sign-in is limited to that mobile number and locks after five failed attempts
for 15 minutes. When the local password config is present, the sign-in page opens on
the password form; other users can select **Use a one-time code instead**. On
PythonAnywhere, run the command in a Bash console from `~/NVSENTRY`. This is an
account-specific password, not a shared master code.

### Updating the deployed app

After pushing changes to GitHub, open a Bash console on PythonAnywhere and run:

```bash
cd ~/NVSENTRY
git pull
```

Then click **Reload** on the Web tab.

## Checks

Run the focused Python WSGI tests and browser voice tests locally:

```powershell
python -m unittest discover -s tests -p test_wsgi.py
node --test tests/test_voice.cjs
```
