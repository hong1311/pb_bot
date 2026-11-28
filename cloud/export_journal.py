"""Run on the Mac only after stopping the existing assistant. No token export."""
import argparse
from contextlib import closing
import fcntl
import os
import sqlite3
from pathlib import Path


def export(state, destination):
    os.umask(0o077)
    source = state / 'journal.sqlite3'
    if not source.is_file():
        raise ValueError('No existing journal found.')
    with (state / 'service.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Stop the local assistant before exporting the journal.') from None
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        try:
            with closing(sqlite3.connect(source.resolve().as_uri()+'?mode=ro', uri=True)) as src:
                with closing(sqlite3.connect(destination)) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise ValueError('Journal backup failed integrity check.')
        except BaseException:
            destination.unlink(missing_ok=True)
            raise


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('destination', type=Path)
    args=p.parse_args()
    export(Path.home()/'.pb_telegram', args.destination)
    print('Private journal backup saved. No Telegram token exported. Transfer only over a secure private channel.')


if __name__ == '__main__':
    main()
