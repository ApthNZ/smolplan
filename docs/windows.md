# Running smolplan on Windows 11

smolplan is a small Python web app with a SQLite file behind it and no build
step, so on a laptop the simplest thing is to run it directly. Docker is
covered at the end, but you don't need it.

Nothing in the app is platform-specific — no POSIX-only calls, and every
dependency ships a Windows wheel.

## What you need

- **Python 3.10 or newer.** The test suite is run on both 3.10 and 3.14, so
  anything in that range is fine.
- **Git**, optionally. You can download the code as a zip instead.

Install both from an ordinary PowerShell window:

```powershell
winget install -e --id Python.Python.3.13
winget install -e --id Git.Git
```

Close and reopen PowerShell afterwards so the new `PATH` takes effect.

> **`py` or `python`?** This guide uses `py`, the launcher that ships with the
> python.org and winget installers and picks the right version when you have
> several. If `py` is not recognised, `python` works just as well — substitute
> it throughout.
>
> **If `python` opens the Microsoft Store instead of running:** that's the
> Windows app-execution alias. Use `py`, or turn the alias off under
> Settings → Apps → Advanced app settings → App execution aliases.

## Install

```powershell
cd $HOME
git clone https://github.com/ApthNZ/smolplan.git
cd smolplan
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

No `git`? Download <https://github.com/ApthNZ/smolplan/archive/refs/heads/main.zip>,
right-click → Extract All, then `cd` into the extracted folder and run the last
two commands.

> **Why `.\.venv\Scripts\python.exe` and not `activate`?** Activating a virtual
> environment runs a PowerShell script, and a default Windows 11 install blocks
> that with *"running scripts is disabled on this system"*. Calling the venv's
> `python.exe` directly sidesteps the whole problem. If you would rather
> activate, either run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy
> Bypass` first (it lasts only for that window), or use `cmd` and
> `.\.venv\Scripts\activate.bat`.

## Run it

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000
```

Then open <http://127.0.0.1:8000>.

The first run creates `smolplan.db` in the project folder and seeds it with a
demo: teams SOC and GRC, three initiatives starting from the current month, and
one of them short of GRC capacity so you can see a red bar straight away.
Settings → *Reset to fixture* puts it back at any time.

Stop the server with `Ctrl+C`.

## A shortcut so you don't type that every time

Save this as `smolplan.bat` in the project folder:

```bat
@echo off
cd /d "%~dp0"
start "" http://127.0.0.1:8000
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000
```

Double-clicking it starts the server and opens your browser. A console window
stays open while it runs; closing that window stops the app.

To start it automatically when you log in, press `Win+R`, run `shell:startup`,
and put a shortcut to `smolplan.bat` in the folder that opens.

## Where your data lives

Everything is in `smolplan.db` in the project folder. Back it up by copying that
file — but stop the server first, or you may copy it mid-write. You will also
see `smolplan.db-wal` and `smolplan.db-shm` beside it while the app is running;
they disappear on a clean shutdown and don't need copying separately.

To keep the database somewhere else, set `SMOLPLAN_DB` before starting:

```powershell
$env:SMOLPLAN_DB = "$HOME\OneDrive\smolplan\smolplan.db"
.\.venv\Scripts\python.exe -m uvicorn app:app --port 8000
```

The folder must already exist. Putting it in OneDrive gives you backups for
free, though don't run two machines against the same synced file at once.

## Updating

```powershell
cd $HOME\smolplan
git pull
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Your `smolplan.db` is untouched by a pull — it's not tracked in git.

## Reaching it from another device

By default the server listens only on `127.0.0.1`, so nothing else on the
network can reach it. That is the right default: **smolplan has no login of any
kind**, so anyone who can open the page can edit the plan.

If you do want it on your home network:

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 0.0.0.0 --port 8000
```

Windows Defender Firewall will prompt the first time — allow it on **private**
networks only, never public. Other devices then use `http://<your-laptop-ip>:8000`;
find the address with `ipconfig`. Don't do this on café or hotel wifi.

## Troubleshooting

**`[Errno 10048] error while attempting to bind`** — something already has port
8000. Run it on another port with `--port 8010`, or find the culprit:

```powershell
Get-NetTCPConnection -LocalPort 8000 -State Listen |
  Select-Object -ExpandProperty OwningProcess |
  ForEach-Object { Get-Process -Id $_ }
```

**`.\.venv\Scripts\python.exe` is not recognised** — this reads like a missing
program, but PowerShell uses the same wording for a **path that does not
exist**. Python is fine; the virtual environment isn't there. Either the
`py -m venv .venv` step was skipped or it failed, or you are not in the project
folder. Run `dir` — you should see `app.py` and a `.venv` folder. If `.venv` is
missing:

```powershell
cd $HOME\smolplan
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

**`py` is not recognised** — Python isn't installed, or PowerShell was open
before you installed it. Reopen PowerShell and try `python` instead; if both
fail, reinstall with the winget command above.

**The page loads but is blank, or looks unstyled** — a stale cache. Hard-reload
with `Ctrl+F5`.

**`ModuleNotFoundError: No module named 'fastapi'`** — you're running the system
Python rather than the one in `.venv`. Use the full `.\.venv\Scripts\python.exe`
path.

**Dates look wrong** — the app uses your laptop's clock for the current month.
Settings has an override for experimenting; blank it to go back to the real
clock.

## Or with Docker Desktop

If you already run Docker Desktop, the repo's compose file works as-is:

```powershell
docker compose up -d --build
```

Then <http://127.0.0.1:8107> — the compose file publishes **8107** by default,
not 8000. Set `SMOLPLAN_PORT` to change it.

Two things differ from the Python route:

- It publishes on all interfaces, so the whole network can reach it. Given
  there is no login, change the ports line to `"127.0.0.1:8107:8000"` if you
  want it kept to the laptop.
- The container is pinned to uid 1000, which matters on Linux and means nothing
  on Windows. It keeps the database on a bind mount at `.\data`; if Docker
  Desktop reports a permissions error writing `/data`, delete the
  `user: "1000:1000"` line from `docker-compose.yml`.

For a laptop, though, the plain Python route above is lighter and starts faster.
