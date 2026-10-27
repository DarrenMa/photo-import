#!/usr/bin/env python3
"""Resumable photo import for Linux. Never modifies source files.

Publication copy: private folder exclusions removed; paths must be supplied.
Review EXCLUDED and the image filters before using on your own collection.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import time
import uuid

from PIL import Image

EXCLUDED = {
    # Add your own unwanted directory names here (lowercase).
    '$recycle.bin', 'recycler',
    'system volume information', 'node_modules', '.git', '__pycache__',
    '.cache', 'cache', 'caches', 'thumbnails', '.thumbnails',
}
EXTENSIONS = {'.jpg', '.jpeg', '.png'}
JUNK = re.compile(r'(^|[\s_.-])(thumb(nail)?s?|icons?|logos?|sprites?|screenshots?|screen[ _-]?shots?)([\s_.-]|\d|$)', re.I)
CHUNK = 1024 * 1024
PREFIX = 64 * 1024


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--source', type=Path, required=True)
    ap.add_argument('--destination', type=Path, required=True)
    ap.add_argument('--state', type=Path, required=True)
    ap.add_argument('--min-long', type=int, default=1200)
    ap.add_argument('--min-short', type=int, default=600)
    ap.add_argument('--max-aspect', type=float, default=3)
    ap.add_argument('--reserve-gb', type=float, default=5)
    ap.add_argument('--inventory-only', action='store_true')
    ap.add_argument('--max-io-errors-per-folder', type=int, default=3)
    ap.add_argument('--retry-io-errors', action='store_true')
    args = ap.parse_args()
    source = args.source.resolve(strict=True)
    dest = args.destination.resolve()
    state = args.state.resolve()
    if dest == source or source in dest.parents or source == state or source in state.parents:
        ap.error('Destination and cache must be outside the source')
    dest.mkdir(parents=True, exist_ok=True)
    state.mkdir(parents=True, exist_ok=True)
    import fcntl
    lock = (state / 'lock').open('w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        ap.error('Another importer is already running')
    db = sqlite3.connect(state / 'cache.sqlite')
    db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS sources (
            path TEXT PRIMARY KEY, size INTEGER, mtime INTEGER,
            status TEXT, reason TEXT, width INTEGER, height INTEGER,
            digest TEXT, destination TEXT, seen TEXT);
        CREATE TABLE IF NOT EXISTS files (
            digest TEXT PRIMARY KEY, size INTEGER, prefix TEXT,
            path TEXT, mtime INTEGER);
        CREATE INDEX IF NOT EXISTS files_sample ON files(size,prefix);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS bad_folders (path TEXT PRIMARY KEY,reason TEXT);
    ''')
    config = json.dumps([str(source), str(dest), args.min_long, args.min_short,
                         args.max_aspect, sorted(EXCLUDED), JUNK.pattern])
    old = db.execute("SELECT value FROM settings WHERE key='config'").fetchone()
    if old and old[0] != config:
        db.execute("UPDATE sources SET status='pending',reason=NULL")
    db.execute("INSERT OR REPLACE INTO settings VALUES ('config',?)", (config,))
    db.commit()
    if args.retry_io_errors:
        db.execute('DELETE FROM bad_folders')
        db.commit()
    bad_folders = {Path(row[0]) for row in db.execute('SELECT path FROM bad_folders')}
    io_errors = {}
    started = time.monotonic()
    stats = dict(phase='inventory', directories=0, excluded_directories=0,
                 image_candidates=0, candidate_bytes=0, scan_errors=0,
                 copied=0, copied_bytes=0, duplicates=0, rejected=0,
                 cached=0, errors=0, source_bytes_read=0)
    stats['folders_skipped_io'] = len(bad_folders)
    last_report = 0

    def report(force=False):
        nonlocal last_report
        now = time.monotonic()
        if not force and now - last_report < 10:
            return
        last_report = now
        stats['elapsed_seconds'] = round(now - started, 1)
        stats['free_bytes'] = shutil.disk_usage(dest).free
        tmp = state / 'progress.json.tmp'
        tmp.write_text(json.dumps(stats, indent=2) + '\n')
        tmp.replace(state / 'progress.json')
        db.commit()
        print(json.dumps(stats), flush=True)

    def error(path, exc):
        with (state / 'errors.jsonl').open('a') as f:
            f.write(json.dumps({'path': str(path), 'error': str(exc)}) + '\n')

    def io_failure(folder, exc):
        if not isinstance(exc, OSError) or exc.errno != errno.EIO:
            return False
        io_errors[folder] = io_errors.get(folder, 0) + 1
        if io_errors[folder] < args.max_io_errors_per_folder:
            return False
        if folder not in bad_folders:
            bad_folders.add(folder)
            stats['folders_skipped_io'] += 1
            reason = 'Repeated I/O errors; remaining files in folder skipped for speed'
            db.execute('INSERT OR REPLACE INTO bad_folders VALUES (?,?)', (str(folder), reason))
            error(folder, reason)
            db.commit()
        return True

    def blocked(path):
        return path in bad_folders or any(parent in bad_folders for parent in path.parents)

    run = uuid.uuid4().hex
    stack = [source]
    report(True)
    while stack:
        folder = stack.pop()
        if blocked(folder):
            continue
        try:
            with os.scandir(folder) as entries:
                stats['directories'] += 1
                for entry in entries:
                    report()
                    try:
                        if entry.is_symlink():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if entry.name.casefold() in EXCLUDED:
                                stats['excluded_directories'] += 1
                            else:
                                stack.append(Path(entry.path))
                        elif Path(entry.name).suffix.casefold() in EXTENSIONS and entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            stats['image_candidates'] += 1
                            stats['candidate_bytes'] += st.st_size
                            db.execute('''INSERT INTO sources(path,size,mtime,status,seen)
                                VALUES (?,?,?,'pending',?) ON CONFLICT(path) DO UPDATE SET
                                status=CASE WHEN sources.size=excluded.size AND sources.mtime=excluded.mtime
                                    THEN sources.status ELSE 'pending' END,
                                size=excluded.size,mtime=excluded.mtime,seen=excluded.seen''',
                                (entry.path, st.st_size, st.st_mtime_ns, run))
                    except OSError as exc:
                        stats['scan_errors'] += 1
                        error(entry.path, exc)
                        if io_failure(folder, exc):
                            break
        except OSError as exc:
            stats['scan_errors'] += 1
            error(folder, exc)
            io_failure(folder, exc)
    report(True)
    if args.inventory_only:
        stats['phase'] = 'inventory_complete'
        report(True)
        return

    stats['phase'] = 'index_destination'
    # Index any preexisting photos too. Cached destination hashes avoid rereads.
    for path in dest.rglob('*'):
        if path.is_symlink() or not path.is_file() or path.suffix.casefold() not in EXTENSIONS:
            continue
        st = path.stat()
        if db.execute('SELECT 1 FROM files WHERE path=? AND size=? AND mtime=?',
                      (str(path), st.st_size, st.st_mtime_ns)).fetchone():
            continue
        h = hashlib.sha256()
        with path.open('rb') as f:
            prefix = f.read(PREFIX)
            sample = hashlib.sha256(prefix).hexdigest()
            h.update(prefix)
            while chunk := f.read(CHUNK):
                h.update(chunk)
        db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?,?)',
                   (h.hexdigest(), st.st_size, sample, str(path), st.st_mtime_ns))
        report()
    # Remove stale entries before using them to decide whether to skip a source.
    for digest, size, path, mtime in db.execute('SELECT digest,size,path,mtime FROM files').fetchall():
        try:
            st = Path(path).stat()
            valid = st.st_size == size and st.st_mtime_ns == mtime
        except OSError:
            valid = False
        if not valid:
            db.execute('DELETE FROM files WHERE digest=?', (digest,))
    db.commit()
    stats['phase'] = 'import'
    report(True)

    def finish(path, status, reason=None, width=None, height=None, digest=None, destination=None):
        db.execute('''UPDATE sources SET status=?,reason=?,width=?,height=?,digest=?,destination=?
                      WHERE path=?''', (status, reason, width, height, digest, destination, path))

    rows = db.execute('SELECT path,size,mtime,status,digest FROM sources WHERE seen=? ORDER BY rowid', (run,))
    for name, size, mtime, status, old_digest in rows:
        report()
        if status == 'rejected' or (status in ('copied', 'duplicate') and
                db.execute('SELECT 1 FROM files WHERE digest=?', (old_digest,)).fetchone()):
            stats['cached'] += 1
            continue
        path = Path(name)
        temporary = None
        try:
            if blocked(path):
                finish(name, 'error', 'folder_skipped_repeated_io_errors')
                stats['errors'] += 1
                continue
            st = path.stat()
            if (st.st_size, st.st_mtime_ns) != (size, mtime):
                raise RuntimeError('Source changed since inventory; retry on next run')
            reason = None
            width = height = None
            if JUNK.search(path.stem):
                reason = 'junk_filename'
            else:
                with Image.open(path) as im:
                    width, height = im.size
                    if im.format not in {'JPEG', 'PNG'}:
                        reason = 'wrong_format'
                    elif min(width, height) < args.min_short or max(width, height) < args.min_long:
                        reason = 'too_small'
                    elif max(width, height) / min(width, height) > args.max_aspect:
                        reason = 'extreme_aspect_ratio'
            if reason:
                finish(name, 'rejected', reason, width, height)
                stats['rejected'] += 1
                continue
            if shutil.disk_usage(dest).free < size + args.reserve_gb * 1024**3:
                stats['phase'] = 'stopped_low_space'
                report(True)
                return
            h = hashlib.sha256()
            with path.open('rb') as src:
                prefix = src.read(PREFIX)
                stats['source_bytes_read'] += len(prefix)
                sample = hashlib.sha256(prefix).hexdigest()
                potential = db.execute('SELECT 1 FROM files WHERE size=? AND prefix=? LIMIT 1',
                                       (size, sample)).fetchone()
                # Most unique files are hashed and copied in the same pass.
                if not potential:
                    temporary = dest / ('.partial-' + uuid.uuid4().hex)
                    output = temporary.open('xb')
                else:
                    output = None
                try:
                    h.update(prefix)
                    if output:
                        output.write(prefix)
                    while chunk := src.read(CHUNK):
                        stats['source_bytes_read'] += len(chunk)
                        h.update(chunk)
                        if output:
                            output.write(chunk)
                        report()
                finally:
                    if output:
                        output.close()
            st = path.stat()
            if (st.st_size, st.st_mtime_ns) != (size, mtime):
                raise RuntimeError('Source changed during read; retry on next run')
            digest = h.hexdigest()
            match = db.execute('SELECT path FROM files WHERE digest=?', (digest,)).fetchone()
            if match:
                finish(name, 'duplicate', width=width, height=height, digest=digest, destination=match[0])
                stats['duplicates'] += 1
                continue
            if temporary is None:
                temporary = dest / ('.partial-' + uuid.uuid4().hex)
                # Rare prefix collision: copy again and verify against the first pass.
                check = hashlib.sha256()
                with path.open('rb') as src, temporary.open('xb') as output:
                    while chunk := src.read(CHUNK):
                        check.update(chunk)
                        output.write(chunk)
                        stats['source_bytes_read'] += len(chunk)
                if check.hexdigest() != digest:
                    raise RuntimeError('Source changed during copy')
            if temporary.stat().st_size != size:
                raise RuntimeError('Incomplete source read')
            target = dest / (path.stem[:100] + '--' + digest + path.suffix.lower())
            os.utime(temporary, ns=(st.st_atime_ns, mtime))
            # Atomic publication without overwriting any existing destination.
            os.link(temporary, target)
            temporary.unlink()
            temporary = None
            dst = target.stat()
            db.execute('INSERT INTO files VALUES (?,?,?,?,?)',
                       (digest, size, sample, str(target), dst.st_mtime_ns))
            finish(name, 'copied', width=width, height=height, digest=digest, destination=str(target))
            stats['copied'] += 1
            stats['copied_bytes'] += size
        except Exception as exc:
            stats['errors'] += 1
            finish(name, 'error', str(exc))
            error(path, exc)
            io_failure(path.parent, exc)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    stats['phase'] = 'complete_with_errors' if stats['errors'] or stats['scan_errors'] or bad_folders else 'complete'
    report(True)


if __name__ == '__main__':
    main()
