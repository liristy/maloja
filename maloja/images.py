from .pkg_global.conf import data_dir, malojaconfig
from . import thirdparty
from . import database
from .artwork import atomic_write, cache_image, encode_image, identity
from .media_library import MediaLibrary
from pathlib import Path
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from doreah.logging import log

import itertools
import os
import urllib
import urllib.parse
import ipaddress
import socket
import random
import base64
import requests
import io
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
import re
import datetime
import time

import sqlalchemy as sql



MAX_RESOLVE_THREADS = 5


# remove old db file (columns missing)
try:
	os.remove(data_dir['cache']('images.sqlite'))
except:
	pass

DB = {}
engine = sql.create_engine(f"sqlite:///{data_dir['cache']('imagecache.sqlite')}", echo = False)
meta = sql.MetaData()

dblock = Lock()

DB['artists'] = sql.Table(
	'artists', meta,
	sql.Column('entitykey',sql.String),
	sql.Column('id',sql.Integer,primary_key=True),
	sql.Column('url',sql.String),
	sql.Column('expire',sql.Integer),
#	sql.Column('raw',sql.String)
	sql.Column('local',sql.Boolean),
	sql.Column('localproxyurl',sql.String)
)
DB['tracks'] = sql.Table(
	'tracks', meta,
	sql.Column('entitykey',sql.String),
	sql.Column('id',sql.Integer,primary_key=True),
	sql.Column('url',sql.String),
	sql.Column('expire',sql.Integer),
#	sql.Column('raw',sql.String)
	sql.Column('local',sql.Boolean),
	sql.Column('localproxyurl',sql.String)
)
DB['albums'] = sql.Table(
	'albums', meta,
	sql.Column('entitykey',sql.String),
	sql.Column('id',sql.Integer,primary_key=True),
	sql.Column('url',sql.String),
	sql.Column('expire',sql.Integer),
#	sql.Column('raw',sql.String)
	sql.Column('local',sql.Boolean),
	sql.Column('localproxyurl',sql.String)
)

meta.create_all(engine)
# NULL keys invalidate pre-upgrade cache entries without touching uploaded files.
for table in DB:
	if 'entitykey' not in {c['name'] for c in sql.inspect(engine).get_columns(table)}:
		with engine.begin() as conn:
			conn.execute(sql.text(f'ALTER TABLE {table} ADD COLUMN entitykey VARCHAR'))

library = None
library_lock = Lock()

def media_library():
	global library
	root = malojaconfig["MEDIA_LIBRARY_PATH"]
	if not root:
		return None
	with library_lock:
		if library is None or library.root != Path(root).expanduser().resolve():
			library = MediaLibrary(root, data_dir['cache']('media-artwork.json'), malojaconfig["MEDIA_LIBRARY_SCAN_INTERVAL"])
		library.refresh()
		return library

def thumbnail(path):
	return cache_image(path, data_dir['cache']('images'), malojaconfig["IMAGE_THUMBNAIL_SIZE"], malojaconfig["IMAGE_QUALITY"])

def entity_info(artist_id=None, track_id=None, album_id=None):
	if track_id:
		return "track", database.sqldb.get_track(track_id)
	if album_id:
		return "album", database.sqldb.get_album(album_id)
	return "artist", database.sqldb.get_artist(artist_id)

def preferred_image(kind, entity):
	# User selections are durable state, independent of expiring provider caches.
	selected = Path(data_dir['images']('selected', identity(kind, entity) + '.webp'))
	if selected.is_file():
		return {'type': 'localurl', 'value': thumbnail(selected)}
	if malojaconfig["USE_LOCAL_IMAGES"]:
		local = local_files(**{kind: entity})
		# Recover the most recent legacy upload instead of randomly rotating it.
		uploads = [p for p in local if '/webupload' in p]
		if uploads:
			path = max(uploads, key=lambda p: (Path(data_dir['images'](p.removeprefix('/images/'))).stat().st_mtime_ns, p))
			return {'type': 'localurl', 'value': thumbnail(data_dir['images'](path.removeprefix('/images/')))}
		media = media_library()
		if media:
			path = media.lookup(kind, entity)
			if path:
				try:
					return {'type': 'localurl', 'value': thumbnail(path)}
				except (OSError, ValueError):
					log(f"Cannot read media artwork: {path}")
		if local:
			return {'type': 'localurl', 'value': thumbnail(data_dir['images'](local[0].removeprefix('/images/')))}
	return None

def get_id_and_table(track_id=None,artist_id=None,album_id=None):
	if track_id:
		return track_id,'tracks'
	elif album_id:
		return album_id,'albums'
	elif artist_id:
		return artist_id,'artists'

def get_image_from_cache(track_id=None,artist_id=None,album_id=None):
	kind, entity = entity_info(track_id=track_id, artist_id=artist_id, album_id=album_id)
	now = int(datetime.datetime.now().timestamp())
	entity_id, table = get_id_and_table(track_id=track_id,artist_id=artist_id,album_id=album_id)

	with engine.begin() as conn:
		op = DB[table].select().where(
			DB[table].c.id==entity_id,
			DB[table].c.expire>now,
			DB[table].c.entitykey == identity(kind, entity)
		)
		result = conn.execute(op).all()
	for row in result:
		if row.local:
			return {'type':'localurl','value':row.url}
		elif row.localproxyurl:
			return {'type':'localurl','value':row.localproxyurl}
		else:
			return {'type':'url','value':row.url or None}
			# value none means nonexistence is cached
			# for some reason this can also be an empty string, so use or None here to unify
	return None # no cache entry

def set_image_in_cache(url,track_id=None,artist_id=None,album_id=None,local=False,entity_key=None):
	if entity_key is None:
		kind, entity = entity_info(track_id=track_id, artist_id=artist_id, album_id=album_id)
		entity_key = identity(kind, entity)
	entity_id, table = get_id_and_table(track_id=track_id,artist_id=artist_id,album_id=album_id)
	# Network IO must not hold the global cache lock or block unrelated covers.
	localproxyurl = dl_image(url) if not local and malojaconfig["PROXY_IMAGES"] and url else None
	with dblock:
		now = int(datetime.datetime.now().timestamp())
		if url is None:
			expire = now + (malojaconfig["CACHE_EXPIRE_NEGATIVE"] * 24 * 3600)
		else:
			expire = now + (malojaconfig["CACHE_EXPIRE_POSITIVE"] * 24 * 3600)

		with engine.begin() as conn:
			op = sqlite_insert(DB[table]).values(
				id=entity_id,
				url=url,
				expire=expire,
				entitykey=entity_key,
				local=local,
				localproxyurl=localproxyurl
			)
			op = op.on_conflict_do_update(index_elements=['id'], set_={c: getattr(op.excluded, c) for c in ['url', 'expire', 'entitykey', 'local', 'localproxyurl']})
			conn.execute(op)

def remove_image_from_cache(track_id=None,artist_id=None,album_id=None):
	entity_id, table = get_id_and_table(track_id=track_id,artist_id=artist_id,album_id=album_id)

	with dblock:
		with engine.begin() as conn:
			op = DB[table].delete().where(
				DB[table].c.id==entity_id,
			).returning(
				DB[table].c.id,
				DB[table].c.localproxyurl
			)
			result = conn.execute(op).all()

		for row in result:
			try:
				targetpath = data_dir['cache']('images',row.localproxyurl.split('/')[-1])
				os.remove(targetpath)
			except:
				pass


def dl_image(url):
	if not validate_safe_url(url):
		log(f"Blocked SSRF attempt: {url}")
		return None
	try:
		for _ in range(5):
			if not validate_safe_url(url):
				return None
			with requests.get(url, timeout=(3, 10), allow_redirects=False, stream=True) as r:
				if r.is_redirect:
					url = urllib.parse.urljoin(url, r.headers['Location'])
					continue
				r.raise_for_status()
				buffer = io.BytesIO()
				for chunk in r.iter_content(65536):
					buffer.write(chunk)
					if buffer.tell() > 20 * 1024 * 1024:
						raise ValueError('Remote image exceeds 20 MiB')
				buffer.seek(0)
				data = encode_image(buffer, malojaconfig["IMAGE_THUMBNAIL_SIZE"], malojaconfig["IMAGE_QUALITY"])
				break
		else:
			return None
		targetname = '%030x.webp' % random.getrandbits(128)
		targetpath = data_dir['cache']('images',targetname)
		atomic_write(targetpath, data)
		return "/cacheimages/" + targetname
	except Exception:
		log(f"Image {url} could not be downloaded for local caching")
		return None

def validate_safe_url(url):
	#extra check in addition to the 3rd party fetch checks
	parsed = urllib.parse.urlparse(url)
	if parsed.scheme not in ("http", "https"):
		return False
	try:
		ip_str = socket.gethostbyname(parsed.hostname)
		ip = ipaddress.ip_address(ip_str)
		if not ip.is_global:
			return False
	except (socket.gaierror, ValueError, TypeError):
		return False
	return True


resolver = ThreadPoolExecutor(max_workers=MAX_RESOLVE_THREADS,thread_name_prefix='image_resolve')

### getting images for any website embedding now ALWAYS returns just the generic link
### even if we have already cached it, we will handle that on request
def get_track_image(track=None,track_id=None):
	if track_id is None:
		track_id = database.sqldb.get_track_id(track,create_new=False)

	return f"/image?track_id={track_id}"

def get_artist_image(artist=None,artist_id=None):
	if artist_id is None:
		artist_id = database.sqldb.get_artist_id(artist,create_new=False)

	return f"/image?artist_id={artist_id}"

def get_album_image(album=None,album_id=None):
	if album_id is None:
		album_id = database.sqldb.get_album_id(album,create_new=False)

	return f"/image?album_id={album_id}"


# this is to keep track of what is currently being resolved
# so new requests know that they don't need to queue another resolve
image_resolve_controller_lock = Lock()
image_resolve_controller = {
	'artists':set(),
	'albums':set(),
	'tracks':set()
}

# this function doesn't need to return any info
# it runs async to do all the work that takes time and only needs to write the result
# to the cache so the synchronous functions (http requests) can access it
def resolve_image(artist_id=None,track_id=None,album_id=None):
	result = get_image_from_cache(artist_id=artist_id,track_id=track_id,album_id=album_id)
	if result is not None:
		# No need to do anything
		return

	if artist_id:
		entitytype = 'artist'
		table = 'artists'
		getfunc, entity_id = database.sqldb.get_artist, artist_id
	elif track_id:
		entitytype = 'track'
		table = 'tracks'
		getfunc, entity_id = database.sqldb.get_track, track_id
	elif album_id:
		entitytype = 'album'
		table = 'albums'
		getfunc, entity_id = database.sqldb.get_album, album_id



	# is another thread already working on this?
	with image_resolve_controller_lock:
		if entity_id in image_resolve_controller[table]:
			return
		else:
			image_resolve_controller[table].add(entity_id)




	try:
		entity = getfunc(entity_id)

		# local image
		if malojaconfig["USE_LOCAL_IMAGES"]:
			images = local_files(**{entitytype: entity})
			if len(images) != 0:
				result = images[0]
				result = urllib.parse.quote(result)
				result = {'type':'localurl','value':result}
				set_image_in_cache(artist_id=artist_id,track_id=track_id,album_id=album_id,url=result['value'],local=True,entity_key=identity(entitytype, entity))
				return result

		# third party
		if artist_id:
			result = thirdparty.get_image_artist_all(entity)
		elif track_id:
			result = thirdparty.get_image_track_all((entity['artists'],entity['title']))
		elif album_id:
			result = thirdparty.get_image_album_all((entity['artists'],entity['albumtitle']))

		result = {'type':'url','value':result or None}
		set_image_in_cache(artist_id=artist_id,track_id=track_id,album_id=album_id,url=result['value'],entity_key=identity(entitytype, entity))
	finally:
		with image_resolve_controller_lock:
			image_resolve_controller[table].remove(entity_id)



# the actual http request for the full image
def image_request(artist_id=None,track_id=None,album_id=None):
	kind, entity = entity_info(artist_id=artist_id, track_id=track_id, album_id=album_id)
	preferred = preferred_image(kind, entity)
	if preferred:
		return preferred
	if track_id and malojaconfig["USE_ALBUM_ARTWORK_FOR_TRACKS"] and entity.get('album'):
		return image_request(album_id=database.sqldb.get_album_id(entity['album'], create_new=False))
	media = media_library() if malojaconfig['USE_LOCAL_IMAGES'] else None
	if media:
		if not media.ready:
			return {'type': 'noimage', 'value': 'wait'}
		if not malojaconfig['MEDIA_LIBRARY_EXTERNAL_FALLBACK']:
			if track_id and entity.get('album'):
				return image_request(album_id=database.sqldb.get_album_id(entity['album'], create_new=False))
			return {'type': 'localurl', 'value': f'/static/svg/placeholder_{kind}.svg'}
	# Resolve only when the browser actually requests this image.
	queue_resolve(artist_id=artist_id, track_id=track_id, album_id=album_id)

	# Bound each HTTP wait so slow providers cannot occupy all server workers.
	# The lazy loader retries pending images with exponential backoff.
	deadline = time.monotonic() + 0.3
	while time.monotonic() < deadline:
		# check cache
		result = get_image_from_cache(artist_id=artist_id,track_id=track_id,album_id=album_id)
		if result is not None:
			# we got an entry, even if it's that there is no image (value None)
			if result['value'] is None:
				# fallback to album regardless of setting (because we have no image)
				if track_id:
					track = database.sqldb.get_track(track_id)
					if track.get("album"):
						album_id = database.sqldb.get_album_id(track["album"])
						return image_request(album_id=album_id)
				# use placeholder
				if malojaconfig["FANCY_PLACEHOLDER_ART"]:
					placeholder_url = "https://generative-placeholders.glitch.me/image?width=300&height=300&style="
					if artist_id:
						result['value'] = placeholder_url + f"tiles&colors={artist_id % 100}"
					if track_id:
						result['value'] = placeholder_url + f"triangles&colors={track_id % 100}"
					if album_id:
						result['value'] = placeholder_url + f"joy-division&colors={album_id % 100}"
				else:
					if artist_id:
						result['value'] = "/static/svg/placeholder_artist.svg"
					if track_id:
						result['value'] = "/static/svg/placeholder_track.svg"
					if album_id:
						result['value'] = "/static/svg/placeholder_album.svg"
			return result
		time.sleep(0.05)

	# no entry, which means we're still working on it
	return {'type':'noimage','value':'wait'}


pending_resolves = set()
pending_resolves_lock = Lock()

def queue_resolve(**keys):
	key = get_id_and_table(**keys)
	with pending_resolves_lock:
		if key in pending_resolves or len(pending_resolves) >= 64:
			return
		pending_resolves.add(key)
	def run():
		try:
			resolve_image(**keys)
		except Exception as error:
			log(f"Image resolution failed for {key}: {error}")
		finally:
			with pending_resolves_lock:
				pending_resolves.discard(key)
	resolver.submit(run)



# removes emojis and weird shit from names
def clean(name):
	return "".join(c for c in name if c.isalnum() or c in []).strip()

# new and improved
def get_all_possible_filenames(artist=None,track=None,album=None):
	if track:
		title, artists = clean(track['title']), [clean(a) for a in track['artists']]
		superfolder = "tracks/"
	elif album:
		title, artists = clean(album['albumtitle']), [clean(a) for a in album.get('artists') or []]
		superfolder = "albums/"
	elif artist:
		artist = clean(artist)
		superfolder = "artists/"
	else:
		return []

	filenames = []

	if track or album:
		if len(artists) < 4:
			unsafeperms = itertools.permutations(artists)
		else:
			unsafeperms = [sorted(artists)]

		for unsafeartistlist in unsafeperms:
			filename = "-".join(unsafeartistlist) + "_" + title
			if filename != "":
				filenames.append(filename)
				filenames.append(filename.lower())
		# ASCII-only aliases collapse unrelated Chinese artist/album names to "_".
		filenames = sorted(set(filenames))
		if len(filenames) == 0: filenames.append(str(hash((frozenset(artists),title))))
	else:
		filename = artist
		if filename != "":
			filenames.append(filename)
			filenames.append(filename.lower())
		filenames = sorted(set(filenames))
		if len(filenames) == 0: filenames.append(str(hash(artist)))

	return [superfolder + name for name in filenames]


def local_files(artist=None,album=None,track=None):


	filenames = get_all_possible_filenames(artist=artist,album=album,track=track)

	images = []

	for purename in filenames:
		# direct files
		for ext in ["webp","png","jpg","jpeg","gif"]:
			#for num in [""] + [str(n) for n in range(0,10)]:
			if os.path.exists(data_dir['images'](purename + "." + ext)):
				images.append("/images/" + purename + "." + ext)

		# folder
		try:
			for f in os.listdir(data_dir['images'](purename)):
				if f.split(".")[-1].lower() in ["webp","png","jpg","jpeg","gif"]:
					images.append("/images/" + purename + "/" + f)
		except Exception:
			pass

	return sorted(set(images))



class MalformedB64(Exception):
	pass

def set_image(b64,**keys):
	if "title" in keys:
		entity = {"track":keys}
		id = database.sqldb.get_track_id(entity['track'])
		idkeys = {'track_id':id}
		dbtable = "tracks"
	elif "albumtitle" in keys:
		entity = {"album":keys}
		id = database.sqldb.get_album_id(entity['album'])
		idkeys = {'album_id':id}
		dbtable = "albums"
	elif "artist" in keys:
		entity = keys
		id = database.sqldb.get_artist_id(entity['artist'])
		idkeys = {'artist_id':id}
		dbtable = "artists"

	log("Trying to set image, b64 string: " + str(b64[:30] + "..."),module="debug")

	regex = r"data:image/(\w+);base64,(.+)"
	match = re.fullmatch(regex,b64)
	if not match: raise MalformedB64()

	try:
		data = base64.b64decode(match.group(2), validate=True)
		data = encode_image(io.BytesIO(data), malojaconfig["IMAGE_THUMBNAIL_SIZE"], malojaconfig["IMAGE_QUALITY"])
	except Exception as error:
		raise MalformedB64() from error
	# Use canonical DB metadata, not potentially incomplete upload query fields.
	kind, canonical = entity_info(**idkeys)
	path = data_dir['images']('selected', identity(kind, canonical) + '.webp')
	atomic_write(path, data)
	url = thumbnail(path)
	set_image_in_cache(**idkeys, url=url, local=True)
	return url
