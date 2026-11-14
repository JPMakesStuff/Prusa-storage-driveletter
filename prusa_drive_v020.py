"""Experimental Windows WinFsp drive for five PrusaLink printers.

Requires Windows + WinFsp + winfspy. v0.20: fix subprocess handles for windowed executable; no original backups.
"""
import argparse
import subprocess
import ctypes
import sys
import shutil
import uuid
import json
import logging
import os
from pathlib import Path, PureWindowsPath
import tempfile
import threading
import time
from urllib.parse import quote, urlparse, urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from winfspy import FileSystem, FILE_ATTRIBUTE, CREATE_FILE_CREATE_OPTIONS
from winfspy import NTStatusAccessDenied, NTStatusObjectNameNotFound, NTStatusObjectNameCollision
from winfspy import NTStatusNotADirectory, NTStatusEndOfFile
from winfspy.memfs import InMemoryFileSystemOperations, FileObj, FolderObj, OpenedObj

LOG = logging.getLogger('prusa-drive')

class Printer:
    def __init__(self, conf):
        self.name = conf['name']
        host = conf['host'].strip()
        if not host or host == 'REPLACE_IP':
            raise ValueError(f'Configure host for {self.name}')
        self.base = host if host.startswith(('http://', 'https://')) else 'http://' + host
        self.base = self.base.rstrip('/')
        self.key = conf['api_key']
        if not self.key or self.key == 'REPLACE_ME':
            raise ValueError(f'Configure API key for {self.name}')
        self.session = requests.Session()
        self.session.headers.update({'X-Api-Key': self.key})

    def request(self, method, path, **kwargs):
        if not path.startswith('/') or path.startswith('//'):
            raise ValueError('Invalid API path')
        r = self.session.request(method, self.base + path, timeout=kwargs.pop("timeout", (5, 30)), **kwargs)
        r.raise_for_status()
        return r

    @staticmethod
    def remote_path(parts):
        return '/api/v1/files/usb/' + '/'.join(quote(x, safe='') for x in parts)

    def list_dir(self, parts):
        data = self.request('GET', self.remote_path(parts), headers={'Accept': 'application/json'}).json()
        if not isinstance(data, dict) or 'children' not in data:
            raise ValueError('Unexpected PrusaLink directory response')
        return data['children']

    def _download_path(self, download_ref):
        """Resolve only same-printer /usb/ references; reject external URLs."""
        if not isinstance(download_ref, str) or not download_ref.strip():
            raise ValueError('Missing download reference')
        base = urlparse(self.base)
        target = urlparse(urljoin(self.base + '/', download_ref.strip()))
        if (target.scheme != base.scheme or target.netloc.lower() != base.netloc.lower()
                or target.username or target.password or target.fragment
                or not target.path.startswith('/usb/')):
            raise ValueError('Download reference is not a same-printer /usb/ URL')
        return target.path + (('?' + target.query) if target.query else '')

    def file_size(self, download_ref):
        """PrusaLink omits sizes in listings and rejects HEAD and Range.

        A streaming GET exposes Content-Length without reading the body.
        """
        path = self._download_path(download_ref)
        with self.request('GET', path, stream=True, allow_redirects=False, timeout=(5, 10)) as r:
            if 300 <= r.status_code < 400:
                raise ValueError('Printer returned a redirect for size probe')
            length = r.headers.get('Content-Length')
            if length is None or not length.isdecimal():
                raise ValueError('Printer did not provide a numeric Content-Length')
            return int(length)

    def download(self, download_ref, output):
        path = self._download_path(download_ref)
        started = time.monotonic()
        LOG.info('DOWNLOAD START printer=%s path=%s', self.name, path)
        received = 0
        with self.request('GET', path, stream=True, allow_redirects=False, timeout=(5, 30)) as r:
            if 300 <= r.status_code < 400:
                raise ValueError('Printer returned a redirect for download')
            expected = r.headers.get('Content-Length')
            LOG.info('DOWNLOAD HEADERS status=%s length=%s', r.status_code, expected)
            with open(output, 'wb') as f:
                for chunk in r.iter_content(64 * 1024):
                    if chunk:
                        f.write(chunk)
                        received += len(chunk)
        if expected is not None and expected.isdecimal() and received != int(expected):
            raise IOError(f'Incomplete download: {received} of {expected} bytes')
        LOG.info('DOWNLOAD COMPLETE bytes=%d elapsed=%.2fs', received, time.monotonic() - started)

    def delete(self, parts):
        # Keep filesystem callbacks bounded; a hung printer must not block
        # Explorer for minutes. HTTP 409 means the printer refused deletion.
        path = self.remote_path(parts)
        LOG.info('DELETE HTTP method=DELETE url=%s%s headers=Accept:application/json;force:?1', self.base, path)
        try:
            response = self.request('DELETE', path, timeout=(5, 8), allow_redirects=False,
                                    headers={'Accept': 'application/json', 'force': '?1'})
        except requests.exceptions.HTTPError as exc:
            response = exc.response
            if response is not None:
                LOG.warning('DELETE HTTP FAILURE status=%s body=%r', response.status_code, response.text[:500])
            raise
        if 300 <= response.status_code < 400:
            raise IOError('Printer redirected delete unexpectedly')
        return response.status_code

    def upload(self, parts, path, overwrite=False):
        # PrusaLink's legacy file API supports a PUT to the requested USB path.
        # Overwrite is permitted only for an explicit replacement operation.
        with open(path, 'rb') as f:
            response = self.request('PUT', self.remote_path(parts), data=f,
                                    timeout=(10, 600), allow_redirects=False,
                                    headers={'Content-Type': 'application/octet-stream',
                                             'Content-Length': str(os.path.getsize(path)),
                                             'Print-After-Upload': '?0', 'Overwrite': '?1' if overwrite else '?0'})
        if 300 <= response.status_code < 400:
            raise IOError('Printer redirected upload unexpectedly')
        return response.status_code

class DiskFile(FileObj):
    """Temp-file-backed file to avoid holding huge G-code files in RAM."""
    def __init__(self, path, attributes, security_descriptor, allocation_size=0):
        super().__init__(path, attributes, security_descriptor, 0)
        fd, self.local_path = tempfile.mkstemp(prefix='prusa-', suffix='.tmp')
        os.close(fd)
        self.loaded = False
        self.pending = False
        self.replacing = False
        self.upload_complete = False
        self.upload_failed = False
        self.deleted = False
        self.handles = 0
        self.remote_ref = None
        self.remote_parts = None
        self.printer = None
        self.size_known = False
        self.download_event = threading.Event()
        self.download_lock = threading.Lock()
        self.download_started = False
        self.download_error = None

    @property
    def allocation_size(self):
        return self.file_size

    def set_allocation_size(self, allocation_size):
        # Windows may preallocate a file before writing actual data.
        pass

    def set_file_size(self, file_size):
        with open(self.local_path, 'r+b') as f:
            f.truncate(file_size)
        self.file_size = file_size

    def read(self, offset, length):
        if offset >= self.file_size:
            raise NTStatusEndOfFile()
        with open(self.local_path, 'rb') as f:
            f.seek(offset)
            return f.read(length)

    def write(self, buffer, offset, write_to_end_of_file):
        if write_to_end_of_file:
            offset = self.file_size
        with open(self.local_path, 'r+b') as f:
            f.seek(offset)
            f.write(buffer)
        self.file_size = max(self.file_size, offset + len(buffer))
        return len(buffer)

    def constrained_write(self, buffer, offset):
        if offset >= self.file_size:
            return 0
        return self.write(buffer[:self.file_size-offset], offset, False)

class PrusaOperations(InMemoryFileSystemOperations):
    def __init__(self, printers):
        super().__init__('Prusa Printers')
        self.printers = {p.name.lower(): p for p in printers}
        self.last_refresh = {}
        # (printer, parent directory, short API filename) -> friendly name.
        # This is presentation-only; remote_parts always uses the printer's
        # actual API name. Never infer a short name from a long filename.
        self.friendly_names = {}
        self.refresh_seconds = 5
        # Extra strong references to opened objects (WinFsp also tracks CFFI handles).
        self._open_handles = {}
        self._handle_lock = threading.RLock()
        self._retired_handles = []  # retain through entire mount to avoid premature CFFI collection
        for p in printers:
            self._create_directory(p.name)

    def _printer_parts(self, path):
        parts = PureWindowsPath(path).parts
        # PureWindowsPath('\\name\\foo').parts is ('\\', 'name', 'foo')
        parts = [x for x in parts if x not in ('\\', '/', '')]
        if not parts:
            return None, []
        return self.printers.get(parts[0].lower()), parts[1:]

    def _refresh(self, path, force=False):
        path = PureWindowsPath(path)
        p, parts = self._printer_parts(path)
        if not p:
            return
        now = time.monotonic()
        if not force and now - self.last_refresh.get(path, 0) < self.refresh_seconds:
            return
        try:
            children = p.list_dir(parts)
        except Exception as exc:
            LOG.warning('Cannot list %s: %s', path, exc)
            # Never claim an unavailable printer has an empty directory.
            raise NTStatusAccessDenied() from exc
        old = {k: v for k, v in self._entries.items() if k.parent == path}
        # Windows can ask for paths in upper case. Reconcile with the exact
        # display_name from PrusaLink rather than creating duplicate objects.
        old_by_fold = {str(k).casefold(): (k, v) for k, v in old.items()}
        old_by_remote = {}
        for old_key, old_obj in old.items():
            if isinstance(old_obj, DiskFile) and old_obj.remote_parts:
                old_by_remote[tuple(x.casefold() for x in old_obj.remote_parts)] = (old_key, old_obj)
        seen = set()
        for child in children:
            remote_name = child.get('name') or child.get('display_name')
            if not remote_name:
                continue
            remote_key = (p.name.casefold(), str(path).casefold(), remote_name.casefold())
            name = self.friendly_names.get(remote_key) or child.get('display_name') or remote_name
            if not name or name in ('.', '..') or '/' in name or '\\' in name:
                continue
            # Preserve an established friendly name when the firmware returns
            # only an 8.3 short name for an existing file after overwrite.
            prior_remote = old_by_remote.get(tuple(x.casefold() for x in (parts + [remote_name])))
            if prior_remote is not None and not self.friendly_names.get(remote_key) and not child.get('display_name'):
                name = prior_remote[0].name
            key = path / name
            seen.add(key)
            is_dir = child.get('type') == 'FOLDER'
            obj = self._entries.get(key)
            previous = old_by_fold.get(str(key).casefold())
            if obj is None and previous is None and prior_remote is not None:
                previous = prior_remote
            if obj is None and previous is not None:
                previous_key, obj = previous
                if str(previous_key) != str(key) and not (isinstance(obj, DiskFile) and obj.pending):
                    # Preserve the original object and open-handle references.
                    self._entries.pop(previous_key, None)
                    obj.path = key
                    self._entries[key] = obj
                    LOG.info('CASE REFRESH %s -> %s', previous_key, key)
            if obj is None:
                obj = FolderObj(key, FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY,
                                self._root_obj.security_descriptor) if is_dir else DiskFile(
                                key, FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE,
                                self._root_obj.security_descriptor)
                self._entries[key] = obj
            if isinstance(obj, DiskFile):
                if not obj.pending:
                    if not obj.loaded and not obj.size_known:
                        reported = child.get('size') or (child.get('metadata') or {}).get('size')
                        if reported is not None:
                            obj.file_size = int(reported)
                            obj.size_known = True
                    obj.remote_ref = (child.get('refs') or {}).get('download') or p.remote_path(parts + [child.get('name') or name])
                    obj.remote_parts = parts + [remote_name]
                    obj.printer = p
                    if name.casefold() != remote_name.casefold():
                        self.friendly_names[remote_key] = name
        # Size is not present in PrusaLink's JSON directory entries. Resolve
        # unknown sizes from streaming GET headers in parallel, then cache them.
        unknown = [obj for key in seen for obj in [self._entries.get(key)]
                   if isinstance(obj, DiskFile) and not obj.pending
                   and not obj.loaded and not obj.size_known and obj.remote_ref]
        if unknown:
            with ThreadPoolExecutor(max_workers=min(6, len(unknown))) as pool:
                futures = {pool.submit(p.file_size, obj.remote_ref): obj for obj in unknown}
                for future in as_completed(futures):
                    obj = futures[future]
                    try:
                        obj.file_size = future.result()
                        obj.size_known = True
                    except Exception as exc:
                        LOG.warning('Cannot determine size for %s: %s', obj.path, exc)
        seen_fold = {str(key).casefold() for key in seen}
        for key, obj in old.items():
            if str(key).casefold() not in seen_fold and not (isinstance(obj, DiskFile) and (obj.pending or obj.handles or obj.download_started)):
                self._entries.pop(key, None)
                if isinstance(obj, DiskFile):
                    try: os.unlink(obj.local_path)
                    except OSError: pass
        self.last_refresh[path] = now

    def get_security_by_name(self, file_name):
        path = PureWindowsPath(file_name)
        if path not in self._entries:
            self._refresh(path.parent)
        return super().get_security_by_name(file_name)

    def _retain_handle(self, obj):
        handle = OpenedObj(obj)
        with self._handle_lock:
            self._open_handles[id(handle)] = handle
        return handle

    def _download_worker(self, obj):
        """Run outside WinFsp OPEN; readers wait for a complete local cache."""
        try:
            # Write to a separate staging file, then atomically replace cache.
            stage = obj.local_path + '.part'
            try:
                obj.printer.download(obj.remote_ref, stage)
                actual_size = os.path.getsize(stage)
                if obj.size_known and actual_size != obj.file_size:
                    raise IOError(f'File size changed: expected {obj.file_size}, received {actual_size}')
                os.replace(stage, obj.local_path)
                obj.loaded = True
                obj.file_size = actual_size
                obj.size_known = True
                LOG.info('CACHE READY %s bytes=%d', obj.path, actual_size)
            finally:
                try:
                    if os.path.exists(stage):
                        os.unlink(stage)
                except OSError:
                    pass
        except Exception as exc:
            obj.download_error = exc
            LOG.exception('BACKGROUND DOWNLOAD FAILED %s', obj.path)
        finally:
            obj.download_event.set()

    def _start_download(self, obj):
        with obj.download_lock:
            if obj.loaded or obj.download_started:
                return
            if not obj.remote_ref or obj.printer is None:
                raise NTStatusAccessDenied()
            obj.download_started = True
            threading.Thread(target=self._download_worker, args=(obj,),
                             daemon=True, name='prusa-download').start()

    def open(self, file_name, create_options, granted_access):
        started = time.monotonic()
        LOG.info('OPEN START %s options=%s access=%s', file_name, create_options, granted_access)
        path = PureWindowsPath(file_name)
        if path not in self._entries:
            self._refresh(path.parent)
        obj = self._entries.get(path)
        if obj is None:
            raise NTStatusObjectNameNotFound()
        if isinstance(obj, DiskFile):
            # Do not fetch file contents for Explorer's metadata/security opens.
            # Opening a GET stream can make PrusaLink report 'File is busy'
            # when DELETE follows. Only an actual READ starts a download.
            LOG.info('OPEN FILE STATE %s loaded=%s pending=%s download_started=%s download_finished=%s access=%s',
                     obj.path, obj.loaded, obj.pending, obj.download_started, obj.download_event.is_set(), granted_access)
            with self._handle_lock:
                obj.handles += 1
        handle = self._retain_handle(obj)
        LOG.info('OPEN COMPLETE %s elapsed=%.2fs', file_name, time.monotonic() - started)
        return handle

    def create(self, file_name, create_options, granted_access, file_attributes,
               security_descriptor, allocation_size):
        path = PureWindowsPath(file_name)
        p, parts = self._printer_parts(path)
        if not p or not parts or path.suffix.lower() not in ('.gcode', '.bgcode'):
            raise NTStatusAccessDenied()
        if create_options & CREATE_FILE_CREATE_OPTIONS.FILE_DIRECTORY_FILE:
            raise NTStatusAccessDenied()
        self._refresh(path.parent, force=True)
        if path in self._entries:
            raise NTStatusObjectNameCollision()
        obj = DiskFile(path, FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE,
                       self._root_obj.security_descriptor)
        obj.pending = True
        obj.loaded = True
        obj.handles = 1
        obj.printer = p
        obj.remote_parts = parts
        self._entries[path] = obj
        LOG.info('UPLOAD STAGING %s', path)
        return self._retain_handle(obj)

    def _commit_upload(self, obj):
        """Commit while WinFsp can still report an error to the Windows caller."""
        if obj.upload_complete:
            return
        if obj.upload_failed:
            raise NTStatusAccessDenied()
        LOG.info('UPLOAD START %s bytes=%d overwrite=%s', obj.path,
                 os.path.getsize(obj.local_path), obj.replacing)
        try:
            status = obj.printer.upload(obj.remote_parts, obj.local_path,
                                        overwrite=obj.replacing)
        except requests.exceptions.HTTPError as exc:
            obj.upload_failed = True
            LOG.error('UPLOAD REJECTED %s HTTP %s; temporary staging file=%s',
                      obj.path, exc.response.status_code if exc.response is not None else '?', obj.local_path)
            raise NTStatusAccessDenied() from exc
        except Exception as exc:
            obj.upload_failed = True
            LOG.exception('UPLOAD FAILED %s; temporary staging file=%s', obj.path, obj.local_path)
            raise NTStatusAccessDenied() from exc
        obj.upload_complete = True
        LOG.info('UPLOAD SUCCESS %s status=%s overwrite=%s', obj.path,
                 status, obj.replacing)
        self.last_refresh.pop(obj.path.parent, None)
        # The printer may regenerate its FAT 8.3 alias after a successful
        # overwrite. Reconcile the remote identity before Explorer's next
        # operation, retaining the human-friendly name when possible.
        try:
            parent = obj.path.parent
            old_remote = tuple(x.casefold() for x in (obj.remote_parts or []))
            children = obj.printer.list_dir(self._printer_parts(parent)[1])
            candidates = [c for c in children if c.get('type') != 'FOLDER']
            match = next((c for c in candidates if (c.get('display_name') or '').casefold() == obj.path.name.casefold()), None)
            if match is None:
                match = next((c for c in candidates if (c.get('name') or '').casefold() == obj.path.name.casefold()), None)
            if match is None and old_remote:
                match = next((c for c in candidates if tuple(x.casefold() for x in
                              (self._printer_parts(parent)[1] + [c.get('name') or ''])) == old_remote), None)
            if match is None:
                # Only match an otherwise unidentifiable short-name entry if
                # there is exactly one plausible new candidate. Avoid guessing
                # when multiple files could share a size/extension.
                known_remote = {tuple(x.casefold() for x in (v.remote_parts or []))
                                for k, v in self._entries.items()
                                if k.parent == parent and isinstance(v, DiskFile) and v is not obj}
                unmatched = [c for c in candidates if c.get('name') and
                             tuple(x.casefold() for x in (self._printer_parts(parent)[1] + [c['name']])) not in known_remote
                             and (c.get('display_name') or c['name']).lower().endswith(obj.path.suffix.lower())]
                if len(unmatched) == 1:
                    match = unmatched[0]
            if match is not None:
                short = match.get('name') or match.get('display_name')
                key = (obj.printer.name.casefold(), str(parent).casefold(), short.casefold())
                self.friendly_names[key] = obj.path.name
                obj.remote_parts = self._printer_parts(parent)[1] + [short]
                obj.remote_ref = (match.get('refs') or {}).get('download') or obj.printer.remote_path(obj.remote_parts)
                LOG.info('UPLOAD NAME RECONCILED %s remote=%s', obj.path, short)
            else:
                LOG.warning('UPLOAD NAME NOT YET RECONCILED %s; will retry on directory refresh', obj.path)
            # The old local file cache must never represent the new remote
            # contents. Invalidate it after upload/overwrite.
            obj.loaded = False
            obj.size_known = False
            obj.download_started = False
            obj.download_event = threading.Event()
            obj.download_error = None
        except Exception as exc:
            LOG.warning('UPLOAD NAME REFRESH DEFERRED %s: %s', obj.path, exc)

    def close(self, file_context):
        obj = file_context.file_obj
        LOG.info('CLOSE START %s', getattr(obj, 'path', '?'))
        try:
            if isinstance(obj, DiskFile):
                with self._handle_lock:
                    obj.handles = max(0, obj.handles - 1)
                    last = obj.handles == 0
                if last and obj.pending:
                    # WinFsp cleanup normally precedes close. Never claim a
                    # failed upload succeeded by retrying invisibly in close.
                    if not obj.upload_complete:
                        LOG.error('UPLOAD NOT COMMITTED %s; removing temporary staging file %s',
                                  obj.path, obj.local_path)
                        try: os.unlink(obj.local_path)
                        except OSError: pass
                    else:
                        try: os.unlink(obj.local_path)
                        except OSError: pass
                    self._entries.pop(obj.path, None)
        finally:
            with self._handle_lock:
                held = self._open_handles.pop(id(file_context), None)
                if held is not None:
                    self._retired_handles.append(held)
            LOG.info('CLOSE COMPLETE %s', getattr(obj, 'path', '?'))

    def read(self, file_context, offset, length):
        started = time.monotonic()
        obj = file_context.file_obj
        LOG.info('READ START %s offset=%d length=%d', obj.path, offset, length)
        if isinstance(obj, DiskFile) and not obj.pending:
            self._start_download(obj)
            # A bounded wait avoids hanging a Windows filesystem worker forever.
            if not obj.download_event.wait(timeout=180):
                LOG.error('READ TIMEOUT %s after 180 seconds', obj.path)
                raise NTStatusAccessDenied()
            if obj.download_error is not None or not obj.loaded:
                LOG.error('READ FAILED %s: %s', obj.path, obj.download_error)
                raise NTStatusAccessDenied()
        result = super().read(file_context, offset, length)
        LOG.info('READ COMPLETE %s bytes=%d elapsed=%.2fs', obj.path, len(result), time.monotonic() - started)
        return result

    def read_directory(self, file_context, marker):
        self._refresh(file_context.file_obj.path)
        return super().read_directory(file_context, marker)

    def get_dir_info_by_name(self, file_context, file_name):
        self._refresh(file_context.file_obj.path)
        return super().get_dir_info_by_name(file_context, file_name)

    def write(self, file_context, buffer, offset, write_to_end_of_file, constrained_io):
        if not isinstance(file_context.file_obj, DiskFile) or not file_context.file_obj.pending:
            raise NTStatusAccessDenied()
        return super().write(file_context, buffer, offset, write_to_end_of_file, constrained_io)

    def set_file_size(self, file_context, new_size, set_allocation_size):
        if not isinstance(file_context.file_obj, DiskFile) or not file_context.file_obj.pending:
            raise NTStatusAccessDenied()
        return super().set_file_size(file_context, new_size, set_allocation_size)

    def overwrite(self, file_context, file_attributes, replace_file_attributes,
                  allocation_size):
        obj = file_context.file_obj
        if not isinstance(obj, DiskFile) or obj.pending or obj.deleted:
            raise NTStatusAccessDenied()
        # Only the printer destination is replaced; the Windows source is
        # never opened by PrusaDrive.
        with self._handle_lock:
            obj.pending = True
            obj.replacing = True
            obj.loaded = True
            obj.set_file_size(0)
        LOG.info('OVERWRITE STAGED %s (no original backup)', obj.path)
        return obj.get_file_info()

    def can_delete(self, file_context, file_name):
        obj = file_context.file_obj
        if not isinstance(obj, DiskFile) or obj.pending or obj.deleted:
            raise NTStatusAccessDenied()
        LOG.info('DELETE APPROVED %s (no backup)', obj.path)

    def rename(self, *args, **kwargs):
        raise NTStatusAccessDenied()

    def cleanup(self, file_context, file_name, flags):
        LOG.info('CLEANUP START %s flags=%s', file_name, flags)
        obj = file_context.file_obj
        if isinstance(obj, DiskFile):
            if obj.pending:
                if flags & 0x01:
                    # Cancel an unfinished staged upload, not a remote delete.
                    LOG.warning('STAGED UPLOAD CANCELLED %s', file_name)
                    obj.pending = False
                    self._entries.pop(obj.path, None)
                    try: os.unlink(obj.local_path)
                    except OSError: pass
                    return None
                self._commit_upload(obj)
                return None
            if flags & 0x01:  # WinFsp FspCleanupDelete
                if obj.deleted:
                    return None
                try:
                    LOG.info('DELETE REQUEST %s printer=%s remote_parts=%r download_started=%s download_finished=%s loaded=%s timeout=8s',
                             obj.path, obj.printer.name, obj.remote_parts, obj.download_started,
                             obj.download_event.is_set(), obj.loaded)
                    if obj.download_started and not obj.download_event.is_set():
                        LOG.warning('DELETE BLOCKED: an active background download is still running for %s', obj.path)
                        raise NTStatusAccessDenied()
                    status = obj.printer.delete(obj.remote_parts)
                except requests.exceptions.HTTPError as exc:
                    code = exc.response.status_code if exc.response is not None else None
                    if code == 409:
                        LOG.warning('DELETE REFUSED (HTTP 409, printer says file busy) %s', obj.path)
                    else:
                        LOG.error('DELETE REFUSED HTTP %s %s', code, obj.path)
                    self.last_refresh.pop(obj.path.parent, None)
                    raise NTStatusAccessDenied() from exc
                except requests.exceptions.Timeout as exc:
                    LOG.error('DELETE TIMEOUT %s after <= 8s response wait; result uncertain, refresh printer listing', obj.path)
                    self.last_refresh.pop(obj.path.parent, None)
                    raise NTStatusAccessDenied() from exc
                except Exception as exc:
                    LOG.exception('DELETE FAILED %s', obj.path)
                    self.last_refresh.pop(obj.path.parent, None)
                    raise NTStatusAccessDenied() from exc
                LOG.info('DELETE SUCCESS %s status=%s', file_name, status)
                obj.deleted = True
                self.last_refresh.pop(obj.path.parent, None)
                self._entries.pop(obj.path, None)
                return None
        result = super().cleanup(file_context, file_name, flags)
        LOG.info('CLEANUP COMPLETE %s', file_name)
        return result

    def set_basic_info(self, file_context, file_attributes, creation_time,
                       last_access_time, last_write_time, change_time, file_info):
        return file_context.file_obj.get_file_info()

    def set_security(self, *args, **kwargs):
        raise NTStatusAccessDenied()


def _tray_image():
    """Create an icon in memory; no external .ico file is needed."""
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle((5, 7, 59, 57), radius=12, fill=(31, 84, 132, 255))
    draw.rounded_rectangle((13, 17, 51, 47), radius=5, fill=(246, 249, 252, 255))
    draw.rectangle((19, 23, 45, 29), fill=(31, 84, 132, 255))
    draw.rectangle((19, 34, 45, 40), fill=(31, 84, 132, 255))
    return img


def _show_startup_error(message, log_path):
    try:
        ctypes.windll.user32.MessageBoxW(None,
            f'{message}\n\nDetails are saved in:\n{log_path}',
            'PrusaDrive - Startup Error', 0x10)
    except Exception:
        print(message, file=sys.stderr)


def main():
    parser = argparse.ArgumentParser()
    app_dir = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent
    parser.add_argument('--config', default=str(app_dir / 'printers.json'))
    parser.add_argument('--drive', default=None)
    args = parser.parse_args()

    # A new log each launch, never append to an older session.
    log_path = app_dir / 'PrusaDrive.log'
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
                        handlers=[logging.FileHandler(log_path, mode='w', encoding='utf-8')], force=True)
    logging.getLogger('winfspy').setLevel(logging.WARNING)
    fs = None
    tray = None
    mounted = False
    clean_exit = False
    try:
        import pystray
        config = json.loads(Path(args.config).read_text(encoding='utf-8'))
        printers = [Printer(x) for x in config['printers']]
        drive = args.drive or config.get('drive', 'P:')
        if not isinstance(drive, str) or len(drive) != 2 or drive[0].upper() not in 'DEFGHIJKLMNOPQRSTUVWXYZ' or drive[1] != ':':
            raise ValueError('Drive must be a letter from D: through Z: (example: P:)')
        drive = drive.upper()
        ops = PrusaOperations(printers)
        fs = FileSystem(drive, ops, sector_size=512, sectors_per_allocation_unit=1,
                        volume_creation_time=0, volume_serial_number=0x50525553,
                        file_info_timeout=1000, um_file_context_is_user_context2=1,
                        persistent_acls=1, post_cleanup_when_modified_only=1,
                        case_sensitive_search=0,
                        case_preserved_names=1, unicode_on_disk=1,
                        file_system_name='PrusaDrive')
        fs.start()
        mounted = True
        LOG.info('PrusaDrive v0.20 mounted at %s; config=%s', drive, args.config)

        def show_log(icon=None, item=None):
            # Notepad can continue displaying the snapshot after the log is deleted on Exit.
            try:
                for handler in logging.getLogger().handlers:
                    handler.flush()
                windows_dir = Path(os.environ.get('SystemRoot', r'C:\Windows'))
                notepad_path = windows_dir / 'System32' / 'notepad.exe'
                subprocess.Popen(
                    [str(notepad_path), str(log_path)], cwd=str(app_dir),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True)
                LOG.info('Show Log launched: %s', notepad_path)
            except Exception as exc:
                LOG.exception('Unable to open log in Notepad')
                _show_startup_error(f'Unable to open the log in Notepad: {exc}', log_path)

        def open_drive(icon=None, item=None):
            try:
                subprocess.Popen(
                    ['explorer.exe', drive + "\\"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, close_fds=True)
            except Exception:
                LOG.exception('Unable to open drive in Explorer')

        def exit_app(icon, item):
            LOG.info('Exit selected from tray; unmounting %s', drive)
            icon.stop()

        tray = pystray.Icon('PrusaDrive', _tray_image(), f'PrusaDrive ({drive})',
            menu=pystray.Menu(
                pystray.MenuItem(f'Open {drive} Drive', open_drive, default=True),
                pystray.MenuItem('Show Log', show_log),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem('Exit (Unmount Drive)', exit_app)))
        tray.run()  # Blocks until Exit is selected.
        clean_exit = True
    except KeyboardInterrupt:
        clean_exit = True
    except Exception as exc:
        LOG.exception('PrusaDrive failed')
        _show_startup_error(str(exc), log_path)
    finally:
        if tray is not None:
            try:
                tray.stop()
            except Exception:
                LOG.exception('Tray shutdown failed')
        if mounted:
            try:
                fs.stop()
                LOG.info('Drive unmounted successfully')
            except Exception:
                clean_exit = False
                LOG.exception('Drive unmount failed')
                _show_startup_error('Unable to unmount the drive cleanly', log_path)
        for handler in logging.getLogger().handlers[:]:
            handler.flush()
            handler.close()
            logging.getLogger().removeHandler(handler)
        if clean_exit:
            try:
                log_path.unlink(missing_ok=True)
            except OSError:
                pass  # Another program may have the log open; next launch overwrites it.


if __name__ == '__main__':
    main()
