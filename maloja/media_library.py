"""Read-only artwork index for Navidrome libraries, including STRM/NFO exports.

No Navidrome database schema, credentials or stream targets are required.
Snapshots are replaced atomically; lookups never walk the library or parse tags.
"""
import json
import logging
import os
from pathlib import Path
from threading import Lock, Thread
import time
import xml.etree.ElementTree as ET

from .artwork import atomic_write, identity, normalize

logger = logging.getLogger(__name__)
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")
AUDIO_EXTENSIONS = {".flac", ".mp3", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".aiff", ".strm"}


class MediaLibrary:
    def __init__(self, root, snapshot, interval=300):
        self.root = Path(root).expanduser().resolve()
        self.snapshot = Path(snapshot)
        self.interval = max(30, interval)
        self.entries = {}
        self.conflicts = {}
        self.records = {}
        self.last_scan = 0
        self.lock = Lock()
        self.scanning = False
        self.ready = False
        self.status = {}
        try:
            saved = json.loads(self.snapshot.read_text(encoding="utf-8"))
            if saved["root"] == str(self.root) and saved["version"] == 2:
                self.entries = saved["entries"]
                self.conflicts = saved["conflicts"]
                self.records = saved["records"]
                self.ready = True
        except (OSError, ValueError, KeyError):
            pass

    def contained(self, path):
        try:
            return path.resolve().is_relative_to(self.root)
        except OSError:
            return False

    def refresh(self):
        with self.lock:
            if self.scanning or time.monotonic() - self.last_scan < self.interval:
                return
            self.scanning = True
        Thread(target=self._refresh, name="media_artwork_scan", daemon=True).start()

    def _refresh(self):
        try:
            self.scan()
        except Exception:
            logger.exception("Could not index media artwork from %s", self.root)
        finally:
            with self.lock:
                self.last_scan = time.monotonic()
                self.scanning = False

    @staticmethod
    def picture(files, stems):
        for stem in stems:
            for ext in IMAGE_EXTENSIONS:
                found = files.get(normalize(stem + ext))
                if found:
                    return str(found)
        return None

    def metadata(self, path, records):
        stat = path.stat()
        signature = [stat.st_mtime_ns, stat.st_size]
        old = self.records.get(str(path))
        if old and old["signature"] == signature:
            records[str(path)] = old
            return old["metadata"]
        if stat.st_size > 4 * 1024 * 1024:
            raise ValueError("NFO exceeds 4 MiB")
        document = ET.fromstring(path.read_bytes())
        if document.tag != "musicfile":
            return None
        def artists(role):
            values = [p.findtext("name", "").strip() for p in document.findall("./participants/participant")
                      if p.findtext("role") == role]
            return [v for v in values if v] or [e.text.strip() for e in document.findall(role) if e.text and e.text.strip()]
        metadata = {"title": document.findtext("title", "").strip(),
                    "artists": artists("artist"),
                    "display_artists": [e.text.strip() for e in document.findall("artist") if e.text and e.text.strip()],
                    "album": {"albumtitle": document.findtext("album", "").strip(),
                              "artists": artists("albumartist") or artists("artist")}}
        records[str(path)] = {"signature": signature, "metadata": metadata}
        return metadata

    def scan(self):
        if not self.root.is_dir():
            raise FileNotFoundError(f"Media library is unavailable: {self.root}")
        candidates, records, artist_pictures = {}, {}, {}
        track_editions = {}
        errors = 0

        def add(kind, entity, picture, priority=0):
            if not picture:
                return
            key = identity(kind, entity)
            old = candidates.get(key)
            if old is None or priority < old[0]:
                candidates[key] = (priority, {picture})
            elif priority == old[0]:
                old[1].add(picture)

        def walk_error(error):
            # Keep the previous complete snapshot on inaccessible directories.
            raise error

        for directory, dirs, filenames in os.walk(self.root, onerror=walk_error, followlinks=False):
            folder = Path(directory)
            dirs[:] = sorted(d for d in dirs if self.contained(folder / d) and not (folder / d).is_symlink())
            files = {normalize(name): folder / name for name in sorted(filenames)
                     if self.contained(folder / name) and not (folder / name).is_symlink()}
            relative = folder.relative_to(self.root)
            artist_picture = self.picture(files, ["artist"])
            if len(relative.parts) == 1:
                add("artist", folder.name, artist_picture)
            album_picture = self.picture(files, ["cover", "folder", "front"])
            nfos = [p for p in files.values() if p.suffix.lower() == ".nfo"]
            for path in nfos:
                try:
                    metadata = self.metadata(path, records)
                    if not metadata or not metadata["title"] or not metadata["artists"]:
                        continue
                    album = metadata["album"]
                    track_picture = self.picture(files, [path.stem + "-cover", path.stem])
                    # Navidrome can scrobble its display artist as one string while
                    # exporting individual participants in the same NFO.
                    artist_variants = [metadata["artists"]]
                    if metadata["display_artists"] and metadata["display_artists"] not in artist_variants:
                        artist_variants.append(metadata["display_artists"])
                    for artists in artist_variants:
                        track = dict(metadata, artists=artists)
                        add("track", track, track_picture or album_picture)
                        bare_track = {"title": metadata["title"], "artists": artists}
                        track_editions.setdefault(identity("track", bare_track), set()).add(identity("album", album))
                        add("track", bare_track, track_picture or album_picture)
                        # Older scrobbles use track artists for compilation albums.
                        track_album = dict(album, artists=artists)
                        add("track", dict(track, album=track_album), track_picture or album_picture, 2)
                        if album["albumtitle"]:
                            add("album", track_album, album_picture or track_picture, 2 if album_picture else 3)
                    if album["albumtitle"]:
                        add("album", album, album_picture or track_picture, 0 if album_picture else 1)
                    # Artist sidecars belong to the artist directory, never an arbitrary album cover.
                    if len(relative.parts) >= 2:
                        artist_folder = self.root / relative.parts[0]
                        if artist_folder not in artist_pictures:
                            artist_files = {normalize(p.name): p for p in artist_folder.iterdir()
                                            if p.is_file() and self.contained(p) and not p.is_symlink()}
                            artist_pictures[artist_folder] = self.picture(artist_files, ["artist"])
                        # Use the actual media folder's portrait, not the first
                        # participant's folder. A named artist's own folder wins.
                        for artist in set(metadata["artists"] + metadata["display_artists"] + album["artists"]):
                            add("artist", artist, artist_pictures[artist_folder], 1)
                except (OSError, ValueError, ET.ParseError):
                    errors += 1
                    logger.warning("Skipping unreadable media NFO: %s", path)
            # Folder fallback for libraries without the custom NFO sidecars.
            if len(relative.parts) == 2 and not nfos:
                album = {"artists": [relative.parts[0]], "albumtitle": folder.name}
                add("album", album, album_picture)
                for path in files.values():
                    if path.suffix.lower() in AUDIO_EXTENSIONS:
                        title = path.stem.removesuffix(" - " + relative.parts[0])
                        track = {"artists": album["artists"], "title": title, "album": album}
                        bare_track = {"artists": track["artists"], "title": title}
                        track_editions.setdefault(identity("track", bare_track), set()).add(identity("album", album))
                        cover = self.picture(files, [path.stem + "-cover", path.stem]) or album_picture
                        add("track", track, cover)
                        add("track", bare_track, cover)

        # Ambiguous editions/collaborations must not resolve by directory order.
        entries = {key: next(iter(paths)) for key, (_, paths) in candidates.items() if len(paths) == 1}
        conflicts = {key: sorted(paths) for key, (_, paths) in candidates.items() if len(paths) > 1}
        # Even an edition without artwork prevents borrowing another edition's
        # image through the album-independent compatibility lookup.
        for key, editions in track_editions.items():
            if len(editions) > 1 and key in entries:
                conflicts[key] = [entries.pop(key)]
        status = {"matched": len(entries), "ambiguous": len(conflicts), "errors": errors}
        atomic_write(self.snapshot, json.dumps({"version": 2, "root": str(self.root),
                     "entries": entries, "records": records, "status": status,
                     "conflicts": conflicts}, ensure_ascii=False).encode())
        self.entries, self.conflicts, self.records, self.status = entries, conflicts, records, status
        self.ready = True
        logger.info("Media artwork index updated: %s", status)
        return status

    def lookup(self, kind, entity):
        key = identity(kind, entity)
        path = self.entries.get(key)
        if not path and key not in self.conflicts and kind == "track" and entity.get("album"):
            # Old scrobbles may have a different album label. Only accept a
            # unique full artist + song match; ambiguous editions stay blank.
            bare = {k: entity[k] for k in ("artists", "title")}
            path = self.entries.get(identity("track", bare))
        if path and self.contained(Path(path)) and Path(path).is_file():
            return Path(path)
        return None
