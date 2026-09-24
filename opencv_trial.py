"""Analyze an existing local recording without changing the application's history."""
import argparse
import json
from pathlib import Path
import shutil
import sqlite3
import time

from server import ROOT  # Load the same .env configuration as the application.
from analysis_pipeline import analyze_recording


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, required=True, help='New directory for trial evidence and JSON')
    parser.add_argument('--db', type=Path, default=ROOT/'data/kuweta.sqlite3')
    args = parser.parse_args()
    if not args.video.is_file():
        parser.error('Brak pliku filmu.')
    if args.output.exists():
        parser.error('Wybierz nowy katalog wyników — istniejące dane pozostają bez zmian.')
    with sqlite3.connect(args.db.resolve().as_uri() + '?mode=ro', uri=True) as db:
        row = db.execute("SELECT value FROM metadata WHERE key='camera_regions'").fetchone()
    if row is None:
        parser.error('Najpierw zaznacz obie kuwety w aplikacji.')
    args.output.mkdir(parents=True)
    video = args.output/'recording.mp4'
    # Evidence writers only create new JPEGs next to the video. A symlink saves
    # disk space; Windows installations without symlink support use a local copy.
    try:
        video.symlink_to(args.video.resolve())
    except OSError:
        shutil.copyfile(args.video, video)
    started = time.monotonic()
    result = analyze_recording(video, json.loads(row[0]), presence_engine='opencv',
                               identity_profiles=ROOT/'data/cat_profiles/manifest.json',
                               progress=lambda s: print(s, flush=True))
    result['elapsed_seconds'] = round(time.monotonic() - started, 2)
    target = args.output/'analysis.json'
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(result['summary'])
    print(f'Wyniki i zdjęcia: {target.resolve()}')


if __name__ == '__main__':
    main()
