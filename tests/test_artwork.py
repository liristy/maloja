"""Regression tests: python -m unittest discover -s tests -v."""
import base64
from concurrent.futures import ThreadPoolExecutor
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
from xml.sax.saxutils import escape

# Never open the user's production database during tests.
_state = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ['MALOJA_DATA_DIRECTORY'] = _state.name
os.environ['MALOJA_SKIP_SETUP'] = 'true'
os.environ.pop('MALOJA_MEDIA_LIBRARY_PATH', None)

from PIL import Image
from maloja.artwork import cache_image, identity
from maloja.media_library import MediaLibrary
from maloja import images
from maloja.pkg_global.conf import DataDirs


def picture(path, color='red', size=(1000, 800)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new('RGB', size, color).save(path)
    return path


def nfo(folder, artist='蔡健雅', album='Goodbye & Hello', title='空白格', stem='song'):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (stem + '.nfo')
    path.write_text('<musicfile><title>' + escape(title) + '</title><artist>' + escape(artist) +
                    '</artist><album>' + escape(album) + '</album><albumartist>' + escape(artist) +
                    '</albumartist></musicfile>', encoding='utf-8')
    return path


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'library'
        self.root.mkdir()
        self.index = Path(self.tmp.name) / 'index.json'
        self.library = MediaLibrary(self.root, self.index)
        self.album = {'artists': ['蔡健雅'], 'albumtitle': 'Goodbye & Hello'}
        self.track = {'artists': ['蔡健雅'], 'title': '空白格', 'album': self.album}
        self.folder = self.root / '蔡健雅' / 'Goodbye & Hello'

    def seed(self):
        nfo(self.folder)
        self.artist_art = picture(self.folder.parent / 'artist.jpg')
        self.album_art = picture(self.folder / 'cover.jpg', 'blue')
        self.track_art = picture(self.folder / 'song-cover.jpg', 'green')
        self.library.scan()

    def test_navidrome_nfo_artist_album_and_track_are_distinct(self):
        self.seed()
        self.assertEqual(self.library.lookup('artist', '蔡健雅'), self.artist_art)
        self.assertEqual(self.library.lookup('album', self.album), self.album_art)
        self.assertEqual(self.library.lookup('track', self.track), self.track_art)

    def test_identity_preserves_chinese_punctuation_and_artist_order(self):
        self.assertNotEqual(identity('album', self.album), identity('album', {'artists': ['周杰伦'], 'albumtitle': '七里香'}))
        self.assertNotEqual(identity('artist', 'A-B'), identity('artist', 'AB'))
        self.assertEqual(identity('album', {'artists': ['B', 'A'], 'albumtitle': 'X'}),
                         identity('album', {'artists': ['A', 'B'], 'albumtitle': 'X'}))

    def test_same_album_title_different_artist_does_not_collide(self):
        self.seed()
        other = self.root / '周杰伦' / 'Goodbye & Hello'
        nfo(other, artist='周杰伦')
        art = picture(other / 'cover.jpg', 'yellow')
        self.library.scan()
        self.assertEqual(self.library.lookup('album', {'artists': ['周杰伦'], 'albumtitle': 'Goodbye & Hello'}), art)
        self.assertEqual(self.library.lookup('album', self.album), self.album_art)

    def test_ambiguous_editions_are_not_randomly_selected(self):
        self.seed()
        other = self.root / '蔡健雅' / 'another edition'
        nfo(other)
        picture(other / 'cover.jpg', 'yellow')
        status = self.library.scan()
        self.assertIsNone(self.library.lookup('album', self.album))
        self.assertGreater(status['ambiguous'], 0)

    def test_track_with_wrong_album_never_uses_bare_title_match(self):
        self.seed()
        wrong = dict(self.track, album={'artists': ['蔡健雅'], 'albumtitle': 'Other'})
        self.assertIsNone(self.library.lookup('track', wrong))

    def test_restarts_reuse_index_and_unchanged_nfo_is_not_reparsed(self):
        self.seed()
        restarted = MediaLibrary(self.root, self.index)
        self.assertTrue(restarted.ready)
        with patch('maloja.media_library.ET.fromstring', side_effect=AssertionError('reparsed')):
            restarted.scan()
        self.assertEqual(restarted.lookup('track', self.track), self.track_art)

    def test_deleted_files_and_changed_metadata_refresh(self):
        self.seed()
        self.track_art.unlink()
        nfo(self.folder, title='新歌名')
        self.library.scan()
        self.assertIsNone(self.library.lookup('track', self.track))
        self.assertEqual(self.library.lookup('track', dict(self.track, title='新歌名')), self.album_art)

    def test_unavailable_root_preserves_previous_snapshot(self):
        self.seed()
        before = self.index.read_bytes()
        self.library.root = self.root / 'missing'
        with self.assertRaises(FileNotFoundError):
            self.library.scan()
        self.assertEqual(self.index.read_bytes(), before)

    def test_malformed_nfo_is_isolated(self):
        self.seed()
        (self.folder / 'bad.nfo').write_text('<broken>', encoding='utf-8')
        self.assertEqual(self.library.scan()['errors'], 1)
        self.assertEqual(self.library.lookup('track', self.track), self.track_art)

    def test_folder_fallback_without_nfo(self):
        picture(self.folder / 'cover.jpg')
        (self.folder / '空白格 - 蔡健雅.strm').write_text('https://example.invalid/music')
        art = picture(self.folder / '空白格 - 蔡健雅-cover.jpg')
        self.library.scan()
        self.assertEqual(self.library.lookup('track', self.track), art)

    def test_source_update_and_encoder_settings_invalidate_thumbnail(self):
        source = picture(self.root / 'cover.png')
        cache = Path(self.tmp.name) / 'cache'
        first = cache_image(source, cache)
        self.assertEqual(first, cache_image(source, cache))
        self.assertNotEqual(first, cache_image(source, cache, size=128))
        original = source.read_bytes()
        with Image.open(cache / first.split('/')[-1]) as thumb:
            self.assertEqual(thumb.format, 'WEBP')
            self.assertEqual(thumb.size, (320, 256))
        self.assertEqual(source.read_bytes(), original)
        picture(source, color='blue', size=(1200, 1000))
        self.assertNotEqual(first, cache_image(source, cache))

    def test_external_index_paths_are_rejected(self):
        outside = picture(Path(self.tmp.name) / 'private.jpg')
        self.library.entries[identity('album', self.album)] = str(outside)
        self.assertIsNone(self.library.lookup('album', self.album))


class ResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.options = {'USE_LOCAL_IMAGES': True, 'MEDIA_LIBRARY_PATH': None,
                        'MEDIA_LIBRARY_SCAN_INTERVAL': 300, 'MEDIA_LIBRARY_EXTERNAL_FALLBACK': False,
                        'IMAGE_THUMBNAIL_SIZE': 320, 'IMAGE_QUALITY': 78,
                        'CACHE_EXPIRE_POSITIVE': 60, 'CACHE_EXPIRE_NEGATIVE': 5,
                        'PROXY_IMAGES': False, 'USE_ALBUM_ARTWORK_FOR_TRACKS': True,
                        'FANCY_PLACEHOLDER_ART': False}
        self.addCleanup(patch.stopall)
        patch.object(images, 'malojaconfig', self.options).start()
        patch.object(images, 'data_dir', DataDirs({'images': str(self.root / 'images'), 'cache': str(self.root / 'cache')})).start()
        self.album = {'artists': ['Regression Artist'], 'albumtitle': 'Regression Album'}
        self.album_id = images.database.sqldb.get_album_id(self.album)
        self.track = {'artists': self.album['artists'], 'title': 'Regression Track', 'album': self.album}
        self.track_id = images.database.sqldb.get_track_id(self.track)

    def upload(self, color='red'):
        stream = io.BytesIO()
        Image.new('RGB', (1000, 1000), color).save(stream, 'PNG')
        return 'data:image/png;base64,' + base64.b64encode(stream.getvalue()).decode()

    def test_upload_survives_expiry_and_late_provider_write(self):
        first = images.set_image(self.upload(), **self.album)
        second = images.set_image(self.upload('blue'), **self.album)
        self.assertNotEqual(first, second)
        images.remove_image_from_cache(album_id=self.album_id)
        self.assertEqual(images.image_request(album_id=self.album_id)['value'], second)
        images.set_image_in_cache('https://example.invalid/stale.jpg', album_id=self.album_id)
        self.assertEqual(images.image_request(album_id=self.album_id)['value'], second)
        self.assertEqual(len(list((self.root / 'images' / 'selected').glob('*.webp'))), 1)

    def test_track_upload_wins_even_with_album_artwork_enabled(self):
        url = images.set_image(self.upload(), **self.track)
        self.assertEqual(images.image_request(track_id=self.track_id)['value'], url)

    def test_templates_do_not_submit_image_work(self):
        with patch.object(images.resolver, 'submit', side_effect=AssertionError('eager work')):
            self.assertIn('album_id=', images.get_album_image(album_id=self.album_id))
            self.assertIn('track_id=', images.get_track_image(track_id=self.track_id))
            self.assertIn('artist_id=', images.get_artist_image(artist_id=1))

    def test_simultaneous_cache_writes_are_atomic(self):
        def write(n):
            images.set_image_in_cache('/images/' + str(n), album_id=self.album_id, local=True)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(write, range(24)))
        self.assertIsNotNone(images.get_image_from_cache(album_id=self.album_id))

    def test_resolver_queue_deduplicates_before_submission(self):
        started, release = threading.Event(), threading.Event()
        def slow(**keys):
            started.set()
            release.wait(3)
        with patch.object(images, 'resolve_image', side_effect=slow) as resolve:
            try:
                images.queue_resolve(album_id=self.album_id)
                self.assertTrue(started.wait(2))
                for _ in range(20):
                    images.queue_resolve(album_id=self.album_id)
                self.assertEqual(resolve.call_count, 1)
            finally:
                release.set()

    def test_chinese_legacy_names_do_not_include_empty_ascii_alias(self):
        names = images.get_all_possible_filenames(album={'artists': ['周杰伦'], 'albumtitle': '七里香'})
        self.assertNotIn('albums/_', names)
        self.assertEqual(names, sorted(names))

    def test_proxy_compresses_image_and_checks_redirect_destination(self):
        raw = base64.b64decode(self.upload().split(',')[1])
        result = MagicMock()
        result.__enter__.return_value = result
        result.is_redirect = False
        result.iter_content.return_value = [raw]
        with patch.object(images, 'validate_safe_url', return_value=True), patch.object(images.requests, 'get', return_value=result):
            url = images.dl_image('https://example.invalid/cover')
            self.assertTrue(url.endswith('.webp'))
            with Image.open(self.root / 'cache' / 'images' / url.split('/')[-1]) as image:
                self.assertEqual(image.size, (320, 320))
        result.is_redirect = True
        result.headers = {'Location': 'http://127.0.0.1/private'}
        with patch.object(images, 'validate_safe_url', side_effect=lambda url: url.startswith('https://')), patch.object(images.requests, 'get', return_value=result) as get:
            self.assertIsNone(images.dl_image('https://example.invalid/cover'))
            self.assertEqual(get.call_count, 1)

    def test_metadata_change_invalidates_numeric_id_cache(self):
        images.set_image_in_cache('/images/old', album_id=self.album_id, local=True)
        renamed = dict(self.album, albumtitle='Renamed Album')
        with patch.object(images.database.sqldb, 'get_album', return_value=renamed):
            self.assertIsNone(images.get_image_from_cache(album_id=self.album_id))
            images.set_image_in_cache('/images/late', album_id=self.album_id, local=True,
                                      entity_key=identity('album', self.album))
            self.assertIsNone(images.get_image_from_cache(album_id=self.album_id))

    def test_invalid_upload_does_not_replace_selection(self):
        url = images.set_image(self.upload(), **self.album)
        with self.assertRaises(images.MalformedB64):
            images.set_image('data:image/png;base64,bm90YW5pbWFnZQ==', **self.album)
        self.assertEqual(images.image_request(album_id=self.album_id)['value'], url)

    def test_http_serves_webp_with_correct_cache_headers(self):
        from maloja import server
        from wsgiref.util import setup_testing_defaults
        patch.object(server, 'data_dir', images.data_dir).start()
        def request(url):
            environ = {}
            setup_testing_defaults(environ)
            path, _, query = url.partition('?')
            environ.update(PATH_INFO=path, QUERY_STRING=query, **{'wsgi.errors': io.StringIO()})
            response = {}
            def start(status, headers, exc_info=None):
                response.update(status=status, headers=dict(headers))
            body = server.webserver(environ, start)
            try:
                response['body'] = b''.join(body)
            finally:
                if hasattr(body, 'close'):
                    body.close()
            return response
        url = images.set_image(self.upload(), **self.album)
        redirect = request('/image?album_id=' + str(self.album_id))
        self.assertTrue(redirect['status'].startswith('307'))
        self.assertEqual(redirect['headers']['Cache-Control'], 'no-store')
        result = request(url)
        self.assertTrue(result['status'].startswith('200'))
        self.assertEqual(result['headers']['Content-Type'], 'image/webp')
        self.assertIn('immutable', result['headers']['Cache-Control'])
        self.assertEqual(Image.open(io.BytesIO(result['body'])).format, 'WEBP')
        for query in ('', '?album_id=bad', '?album_id=-1', '?album_id=1&track_id=2'):
            self.assertTrue(request('/image' + query)['status'].startswith('400'))
        self.assertTrue(request('/cacheimages/absent.webp')['status'].startswith('404'))

    def test_library_replaces_old_negative_cache_and_track_uses_own_art(self):
        root = self.root / 'library'
        folder = root / self.album['artists'][0] / self.album['albumtitle']
        nfo(folder, artist=self.album['artists'][0], album=self.album['albumtitle'], title=self.track['title'])
        album_art = picture(folder / 'cover.jpg', 'blue')
        track_art = picture(folder / 'song-cover.jpg', 'green')
        library = MediaLibrary(root, self.root / 'index.json')
        library.scan()
        patch.object(images, 'media_library', return_value=library).start()
        images.set_image_in_cache(None, album_id=self.album_id)
        with patch.object(images, 'queue_resolve', side_effect=AssertionError('external request')):
            self.assertEqual(images.image_request(album_id=self.album_id)['value'], images.thumbnail(album_art))
            self.assertEqual(images.image_request(track_id=self.track_id)['value'], images.thumbnail(track_art))


if __name__ == '__main__':
    unittest.main()
