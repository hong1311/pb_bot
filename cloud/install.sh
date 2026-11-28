#!/bin/bash
set -euo pipefail
umask 077
cd "$(dirname "$0")"
APP="$PWD"
if [ "$(uname -s)" != Darwin ]; then echo 'This installer is for macOS. For other systems use the foreground command in README.'; exit 1; fi
python3 -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
mkdir -p "$HOME/.pb_telegram"
chmod 700 "$HOME/.pb_telegram"
if [ ! -s "$HOME/.pb_telegram/token" ]; then echo 'Existing token not found. Run python3 pb_telegram.py --setup first, then rerun this installer.'; exit 1; fi
# Do not create a competing getUpdates consumer or migrate an active journal.
python3 - <<'PY'
import fcntl, os, sqlite3, time
from pathlib import Path
home=Path.home()/'.pb_telegram'
with (home/'service.lock').open('a') as f:
    try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Stop the existing bot (Ctrl+C), then rerun bash install.sh.')
    p=home/'journal.sqlite3'
    if p.exists():
        dest=home/f'journal-backup-{int(time.time())}.sqlite3'
        with sqlite3.connect(p) as src, sqlite3.connect(dest) as dst: src.backup(dst)
        dest.chmod(0o600)
        print('Private journal backup saved.')
PY
python3 -m venv .venv
.venv/bin/python -m pip install --disable-pip-version-check -r requirements.txt
# Reuse pairing, token and database. Start on login; local time gates are NY-aware in Python.
export PB_INSTALL_APP="$APP"
python3 - <<'PY'
import os, plistlib
from pathlib import Path
home=Path.home(); app=Path(os.environ['PB_INSTALL_APP']); state=home/'.pb_telegram'
agent=home/'Library/LaunchAgents/com.pb.telegram.assistant.plist'; agent.parent.mkdir(parents=True,exist_ok=True)
conf={'Label':'com.pb.telegram.assistant','ProgramArguments':['/usr/bin/caffeinate','-i',str(app/'.venv/bin/python'),'-u',str(app/'pb_telegram.py')], 'WorkingDirectory':str(app),'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,'StandardOutPath':str(state/'service.log'),'StandardErrorPath':str(state/'service.err.log'),'EnvironmentVariables':{}}
# Ask the actual interpreter instead of inferring its certifi path (also handles spaces).
import subprocess
conf['EnvironmentVariables']['SSL_CERT_FILE']=subprocess.check_output([str(app/'.venv/bin/python'),'-m','certifi'],text=True).strip()
with agent.open('wb') as f: plistlib.dump(conf,f)
agent.chmod(0o600)
print('Installed launch agent:',agent)
PY
DOMAIN="gui/$(id -u)"
launchctl bootout "$DOMAIN/com.pb.telegram.assistant" >/dev/null 2>&1 || true
launchctl bootstrap "$DOMAIN" "$HOME/Library/LaunchAgents/com.pb.telegram.assistant.plist"
echo 'Automatic local service registered. Send /status privately to verify. Keep your Mac awake and online.'
echo 'Logs: ~/.pb_telegram/service.log and service.err.log'
