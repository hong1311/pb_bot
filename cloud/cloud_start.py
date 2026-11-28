"""Single-instance Linux cloud startup. Never use ephemeral journal storage."""
import os
from contextlib import closing
import re
import sqlite3
from pathlib import Path


def mount_present(path, mountinfo):
    # /data is a fixed ASCII path; require an explicit mount in Linux mountinfo.
    return any(len(line.split()) > 5 and line.split()[4] == str(path)
               for line in mountinfo.splitlines())


def validate(state, env, mountinfo):
    token = env.get('PB_TELEGRAM_TOKEN', '').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]{20,}', token):
        raise ValueError('Set PB_TELEGRAM_TOKEN as a private runtime secret.')
    if state.is_symlink() or not state.is_dir() or not mount_present(state, mountinfo):
        raise ValueError('Mount a persistent volume at /data. Ephemeral journal storage is refused.')
    if not os.access(state, os.W_OK | os.X_OK):
        raise ValueError('The persistent volume must be writable by the service user.')
    journal = state / 'journal.sqlite3'
    if journal.is_symlink():
        raise ValueError('Journal must be a regular private file.')
    if not journal.is_file() or journal.stat().st_size == 0:
        if env.get('PB_ALLOW_EMPTY_JOURNAL') != '1':
            raise ValueError('Import the existing journal first. Fresh journal requires explicit opt-in.')
    else:
        try:
            with closing(sqlite3.connect(journal.resolve().as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Existing journal failed integrity check.')
                db.execute('SELECT k,v FROM settings LIMIT 1').fetchall()
        except sqlite3.Error:
            raise ValueError('Existing journal cannot be read; restore a valid backup.') from None
    return token


def main():
    os.umask(0o077)
    state = Path('/data')
    try:
        token = validate(state, os.environ, Path('/proc/self/mountinfo').read_text())
    except (ValueError, OSError) as exc:
        raise SystemExit(str(exc)) from None
    import pb_telegram
    pb_telegram.HOME = state
    # Existing bot ID validation rejects a token belonging to a different bot.
    # Credentials remain runtime secrets, not source code, database or logs.
    try:
        pb_telegram.run(token=token)
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == '__main__':
    main()
