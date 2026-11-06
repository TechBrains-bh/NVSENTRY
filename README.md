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

   from app import application
   ```

4. Click **Reload** on the Web tab. Visit the web app URL shown there.

The database is stored outside the cloned repository so code updates do not overwrite
shop records. Keep a separate backup of that SQLite file; the app does not currently
provide multi-user authentication or automatic backups.

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
