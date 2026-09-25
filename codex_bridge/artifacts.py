"""Bounded, resumable artifact transfers. No peer chooses a filesystem root."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import time
import uuid
import weakref

from .core import BridgeError, canonical, digest, identifier, now, safe_path

INLINE_BYTES = 8 * 1024 * 1024
CHUNK_BYTES = 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_STORAGE_BYTES = 1024 * 1024 * 1024
TRANSFER_TTL_SECONDS = 24 * 60 * 60
MAX_ACTIVE_TRANSFERS = 8
CAPABILITY = 'artifacts_chunked_v1'
PEER_METHODS = {'peer.artifact_begin', 'peer.artifact_chunk', 'peer.artifact_commit',
                'peer.artifact_abort', 'peer.artifact_manifest', 'peer.artifact_read'}


def sha_file(path):
    path = safe_path(path.parent, path.name)
    value = hashlib.sha256()
    size = 0
    with path.open('rb') as handle:
        while block := handle.read(CHUNK_BYTES):
            size += len(block)
            if size > MAX_FILE_BYTES:
                raise BridgeError('file_denied', 'Artifact exceeds the 256 MiB transfer limit')
            value.update(block)
    return size, value.hexdigest()


def valid_hash(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{64}', value):
        raise BridgeError('invalid_artifact', 'Expected a lowercase SHA256 digest')
    return value


async def disk_work(function, *args):
    # A cancelled asyncio.to_thread does not stop its underlying write. Wait for
    # that bounded write before releasing locks or closing the SQLite journal.
    operation = asyncio.create_task(asyncio.to_thread(function, *args))
    try:
        return await asyncio.shield(operation)
    except asyncio.CancelledError:
        await operation
        raise


class ArtifactTransfers:
    def __init__(self, bridge):
        self.bridge = bridge
        self.root = safe_path(bridge.state_dir, 'transfers')
        self.root.mkdir(exist_ok=True)
        self.staging = safe_path(bridge.state_dir, 'artifact-staging')
        self.staging.mkdir(exist_ok=True)
        self.running = {}
        self.cancel_locks = weakref.WeakValueDictionary()
        self.receive_lock = asyncio.Lock()
        self.archive_lock = asyncio.Lock()
        for item in bridge.store.all('artifact_requests'):
            if item['status'] in ('queued', 'preparing', 'transferring'):
                item.update(status='paused', error={'code': 'process_interrupted', 'retryable': True,
                    'message': 'Repeat the original artifact operation with the same request ID to resume'})
                self.record('artifact_requests', item['request_id'], item)

    @staticmethod
    def capabilities():
        return {'protocol': 1, 'chunk_bytes': CHUNK_BYTES, 'max_file_bytes': MAX_FILE_BYTES,
                'max_storage_bytes': MAX_STORAGE_BYTES, 'incomplete_ttl_seconds': TRANSFER_TTL_SECONDS,
                'max_active_transfers': MAX_ACTIVE_TRANSFERS}

    def record(self, kind, key, value):
        value['updated_at'] = now()
        value['updated_epoch'] = time.time()
        self.bridge.store.put(kind, key, value)

    def blob(self, artifact_id):
        return safe_path(self.bridge.state_dir / 'artifacts', identifier(artifact_id, 'artifact_id'))

    def part(self, transfer_id, index):
        return safe_path(self.root, identifier(transfer_id) + '/' + str(index))

    def reserve(self, size, request_id=None):
        active = [t for t in self.bridge.store.all('artifact_transfers') if t['status'] == 'receiving']
        if len(active) >= MAX_ACTIVE_TRANSFERS:
            raise BridgeError('transfer_limit', 'Eight incomplete transfers are already reserved', True)
        # Reserve the complete size at begin, not merely bytes uploaded so far.
        archived = self.archive_bytes()
        # Three reserved copies cover chunks, assembly, and archive publication.
        # Final import files are owner-selected output, outside the archive quota.
        preparing = [r for r in self.bridge.store.all('artifact_requests')
                     if r.get('direction') == 'send' and r.get('status') == 'preparing' and r['request_id'] != request_id]
        if archived + 3 * sum(t['size'] for t in active) + 2 * sum(r.get('size', 0) for r in preparing) + 3 * size > MAX_STORAGE_BYTES:
            raise BridgeError('storage_limit', 'Artifact archive and incomplete reservations would exceed 1 GiB')

    def archive_bytes(self):
        archive_root = self.bridge.state_dir / 'artifacts'
        total = max(sum(a['size'] for a in self.bridge.store.all('artifacts')),
                    sum(safe_path(archive_root, p.name).stat().st_size
                        for p in archive_root.iterdir() if p.is_file()))
        # Account for journaled import scratch after a process crash as well.
        for journal in self.staging.glob('*.json'):
            try:
                record = json.loads(safe_path(self.staging, journal.name).read_text(encoding='utf-8'))
                if Path(record['root']) == archive_root:
                    continue
                scratch = safe_path(record['root'], record['relative'])
                if scratch.exists():
                    total += scratch.stat().st_size
            except (OSError, ValueError, KeyError, TypeError, BridgeError):
                continue
        return total

    def archive_capacity(self, artifact_id, size):
        """The inline protocol shares the same quota as chunked transfers."""
        if self.bridge.store.get('artifacts', artifact_id):
            return
        reserved = 3 * sum(t['size'] for t in self.bridge.store.all('artifact_transfers') if t['status'] == 'receiving')
        reserved += 2 * sum(r.get('size', 0) for r in self.bridge.store.all('artifact_requests') if r['status'] == 'preparing')
        if self.archive_bytes() + reserved + size > MAX_STORAGE_BYTES:
            raise BridgeError('storage_limit', 'Artifact archive and incomplete reservations would exceed 1 GiB')

    def claim(self, session, artifact_id, size, sha256):
        """One synchronous durable claim shared by inline and chunked writers."""
        artifact_id = identifier(artifact_id, 'artifact_id')
        intent = {'session_id': session['session_id'], 'size': size, 'sha256': sha256}
        existing = self.bridge.store.get('artifact_claims', artifact_id)
        archived = self.bridge.store.get('artifacts', artifact_id)
        for prior in (existing, archived):
            if prior and any(prior[k] != value for k, value in intent.items()):
                raise BridgeError('duplicate_conflict', 'Artifact ID is already claimed for different bytes or session')
        if not existing:
            self.bridge.store.put('artifact_claims', artifact_id, intent)

    def metadata(self, session, artifact_id):
        artifact_id = identifier(artifact_id, 'artifact_id')
        item = self.bridge.store.get('artifacts', artifact_id)
        if not item or item['session_id'] != session['session_id']:
            raise BridgeError('artifact_not_found', 'Artifact does not belong to this session')
        return item

    def check_metadata(self, p):
        identifier(p['artifact_id'], 'artifact_id')
        valid_hash(p['sha256'])
        if type(p['size']) is not int or not 0 <= p['size'] <= MAX_FILE_BYTES:
            raise BridgeError('file_denied', 'Artifact size must be between zero and 256 MiB')
        if p.get('chunk_bytes', CHUNK_BYTES) != CHUNK_BYTES:
            raise BridgeError('unsupported_transfer', 'Chunk protocol 1 requires 1 MiB chunks')

    def validate_existing(self, path, size, sha256):
        if not path.is_file() or path.stat().st_size != size or sha_file(path) != (size, sha256):
            raise BridgeError('file_exists', 'Destination already exists with different content; choose another name')

    def atomic_copy(self, source, root, relative, size, sha256, tag, guard=None):
        """Publish complete bytes without replacing any existing destination."""
        if guard:
            guard()
        target = safe_path(root, relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target = safe_path(root, relative)
        if target.exists():
            self.validate_existing(target, size, sha256)
            return target
        scratch_id = uuid.uuid4().hex
        rel_temp = str(Path(relative).parent / ('.codex-bridge-' + scratch_id + '.partial'))
        temporary = safe_path(root, rel_temp)
        journal = safe_path(self.staging, scratch_id + '.json')
        created = False
        journal_created = False
        try:
            source = safe_path(source.parent, source.name)
            with source.open('rb') as handle, temporary.open('xb') as output:
                created = True
                identity = os.fstat(output.fileno())
                # Journal only files we actually created. A crash before this
                # journal leaves at most an empty file; no payload is written yet.
                with journal.open('x', encoding='utf-8') as record:
                    journal_created = True
                    json.dump({'root': str(Path(root).absolute()), 'relative': rel_temp,
                               'device': identity.st_dev, 'inode': identity.st_ino,
                               'created_epoch': time.time()}, record)
                    record.flush()
                    os.fsync(record.fileno())
                value = hashlib.sha256()
                written = 0
                while block := handle.read(CHUNK_BYTES):
                    written += len(block)
                    if written > size:
                        raise BridgeError('invalid_artifact', 'Artifact changed during transfer')
                    output.write(block)
                    value.update(block)
                output.flush()
                os.fsync(output.fileno())
            if written != size or value.hexdigest() != sha256:
                raise BridgeError('invalid_artifact', 'Complete artifact SHA256 verification failed')
            safe_path(root, rel_temp)
            safe_path(root, relative)
            if guard:
                guard()
            try:
                # The temp and destination share a directory/filesystem. Unlike replace,
                # link atomically refuses to overwrite a file created by another writer.
                os.link(temporary, target)
            except FileExistsError:
                self.validate_existing(target, size, sha256)
            except OSError as exc:
                raise BridgeError('atomic_publish_unavailable',
                    'The selected filesystem must support atomic hard-link publication') from exc
            return target
        finally:
            if created:
                safe_path(root, rel_temp).unlink(missing_ok=True)
            if journal_created:
                journal.unlink(missing_ok=True)

    async def archive(self, session, artifact_id, source, size, sha256, path):
        self.claim(session, artifact_id, size, sha256)
        async with self.archive_lock:
            def current():
                prior = self.bridge.store.get('artifacts', artifact_id)
                if prior and (prior['session_id'] != session['session_id'] or
                              prior['sha256'] != sha256 or prior['size'] != size):
                    raise BridgeError('duplicate_conflict', 'Artifact ID already refers to other content or scope')
                return prior
            current()
            await disk_work(self.atomic_copy, source, self.bridge.state_dir / 'artifacts', artifact_id,
                                    size, sha256, artifact_id)
            # Inline protocol writes run synchronously on the loop, so also
            # recheck after the background publication before committing metadata.
            prior = current()
            result = prior or {'artifact_id': artifact_id, 'session_id': session['session_id'],
                               'sha256': sha256, 'size': size, 'path': path, 'created_at': now()}
            self.bridge.store.put('artifacts', artifact_id, result)
            return result

    async def negotiate(self, peer_id):
        status = await self.bridge.remote(peer_id, 'peer.status', {})
        if status.get('peer_id') != peer_id:
            raise BridgeError('peer_mismatch', 'The endpoint belongs to another paired computer')
        info = status.get('artifact_transfer', {})
        if CAPABILITY not in status.get('capabilities', []) or info.get('protocol') != 1 or info.get('chunk_bytes') != CHUNK_BYTES:
            raise BridgeError('unsupported_transfer', 'Peer does not support verified chunked artifacts; update both bridges or select a file at most 8 MiB')
        return info

    def transfer(self, session, transfer_id, actor):
        value = self.bridge.store.get('artifact_transfers', identifier(transfer_id, 'transfer_id'))
        if not value or value['session_id'] != session['session_id'] or value['actor'] != actor:
            raise BridgeError('transfer_not_found', 'Transfer does not belong to this session and peer')
        return value

    def transfer_view(self, value):
        return {k: value[k] for k in ('transfer_id', 'request_id', 'session_id', 'artifact_id',
                'status', 'size', 'sha256', 'received', 'created_at', 'updated_at') if k in value} | {
                'chunk_bytes': CHUNK_BYTES, 'received_bytes': sum(c['size'] for c in value.get('chunks', {}).values()),
                'result': value.get('result')}

    async def begin(self, session, p, actor):
        self.cleanup()
        self.check_metadata(p)
        request_id = identifier(p['request_id'], 'request_id')
        project = self.bridge.project(session['project_id'], session['peer_id'])
        destination = safe_path(project['import_root'], p['destination'])
        intent = {k: p[k] for k in ('session_id', 'request_id', 'artifact_id', 'destination', 'size', 'sha256')}
        fingerprint = digest({'actor': actor, **intent})
        transfer_id = 'transfer-' + hashlib.sha256((actor + '|' + request_id).encode()).hexdigest()[:32]
        previous = self.bridge.store.get('artifact_transfers', transfer_id)
        if previous:
            if previous['digest'] != fingerprint:
                raise BridgeError('duplicate_conflict', 'Transfer request ID was used for different bytes or scope')
            if previous['status'] in ('expired', 'aborted'):
                raise BridgeError('transfer_closed', 'Transfer expired or was cancelled; select a new request ID')
            self.record('artifact_transfers', transfer_id, previous)
            return self.transfer_view(previous)
        self.bridge.mutation(request_id, 'artifact_chunk_in', {'actor': actor, **intent})
        old = self.bridge.store.get('artifacts', p['artifact_id'])
        if old and (old['session_id'] != session['session_id'] or old['sha256'] != p['sha256']):
            raise BridgeError('duplicate_conflict', 'Artifact ID already belongs to other bytes or scope')
        if destination.exists():
            await disk_work(self.validate_existing, destination, p['size'], p['sha256'])
        self.reserve(p['size'])
        self.claim(session, p['artifact_id'], p['size'], p['sha256'])
        value = {**intent, 'transfer_id': transfer_id, 'actor': actor, 'digest': fingerprint,
                 'status': 'receiving', 'chunks': {}, 'received': [], 'created_at': now(),
                 'project_id': session['project_id'], 'peer_id': session['peer_id']}
        self.record('artifact_transfers', transfer_id, value)
        return self.transfer_view(value)

    async def chunk(self, session, p, actor):
        value = self.transfer(session, p['transfer_id'], actor)
        if value['status'] != 'receiving':
            raise BridgeError('transfer_closed', 'Transfer is no longer receiving chunks')
        index = p['index']
        count = (value['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES
        if type(index) is not int or not 0 <= index < count:
            raise BridgeError('invalid_artifact', 'Chunk index is outside this artifact')
        valid_hash(p['sha256'])
        try:
            data = base64.b64decode(p['data'], validate=True)
        except (ValueError, TypeError):
            raise BridgeError('invalid_artifact', 'Invalid chunk encoding')
        expected = min(CHUNK_BYTES, value['size'] - index * CHUNK_BYTES)
        if len(data) != expected or hashlib.sha256(data).hexdigest() != p['sha256']:
            raise BridgeError('invalid_artifact', 'Chunk size or SHA256 verification failed')
        prior = value['chunks'].get(str(index))
        if prior and prior['sha256'] != p['sha256']:
            raise BridgeError('duplicate_conflict', 'Chunk index was already used for different bytes')
        path = self.part(value['transfer_id'], index)
        path.parent.mkdir(exist_ok=True)
        path = self.part(value['transfer_id'], index)
        # A crash during this write leaves a partial chunk. Retrying the same index
        # repairs it before its durable journal entry is added.
        def write_chunk():
            with path.open('wb') as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        await disk_work(write_chunk)
        # Cancellation/revocation may arrive during disk I/O.
        self.bridge.session(session['session_id'], operation='artifacts')
        value = self.transfer(session, p['transfer_id'], actor)
        if value['status'] != 'receiving':
            path.unlink(missing_ok=True)
            raise BridgeError('transfer_closed', 'Transfer was cancelled')
        value['chunks'][str(index)] = {'sha256': p['sha256'], 'size': len(data)}
        value['received'] = sorted(int(i) for i in value['chunks'])
        self.record('artifact_transfers', value['transfer_id'], value)
        return self.transfer_view(value)

    def remove_parts(self, value):
        folder = safe_path(self.root, value['transfer_id'])
        if folder.exists():
            for path in folder.iterdir():
                safe_path(self.root, value['transfer_id'] + '/' + path.name).unlink()
            folder.rmdir()

    def cleanup_staging(self):
        allowed = {str((self.bridge.state_dir / 'artifacts').absolute())}
        allowed.update(str(Path(p['import_root']).absolute()) for p in self.bridge.config().get('projects', {}).values())
        for journal in self.staging.glob('*.json'):
            try:
                journal = safe_path(self.staging, journal.name)
                record = json.loads(journal.read_text(encoding='utf-8'))
                if time.time() - record['created_epoch'] <= TRANSFER_TTL_SECONDS:
                    continue
                expected = '.codex-bridge-' + journal.stem + '.partial'
                if record['root'] not in allowed or Path(record['relative']).name != expected:
                    continue
                path = safe_path(record['root'], record['relative'])
                if path.exists():
                    identity = path.stat()
                    if (identity.st_dev, identity.st_ino) != (record['device'], record['inode']):
                        continue
                    path.unlink()
                journal.unlink()
            except (OSError, ValueError, KeyError, TypeError, BridgeError):
                # Unrecognized files are never cleanup authority.
                continue

    async def commit(self, session, p, actor):
        value = self.transfer(session, p['transfer_id'], actor)
        if value['status'] == 'completed':
            return value['result']
        if value['status'] != 'receiving':
            raise BridgeError('transfer_closed', 'Transfer is no longer receiving chunks')
        count = (value['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES
        if len(value['chunks']) != count:
            raise BridgeError('transfer_incomplete', 'Some chunks are missing', True)
        assembled = self.part(value['transfer_id'], 'assembled')
        assembled.parent.mkdir(exist_ok=True)
        def assemble():
            with assembled.open('wb') as output:
                for index in range(count):
                    path = self.part(value['transfer_id'], index)
                    with path.open('rb') as handle:
                        block = handle.read(CHUNK_BYTES + 1)
                    expected = value['chunks'][str(index)]
                    if len(block) != expected['size'] or hashlib.sha256(block).hexdigest() != expected['sha256']:
                        raise BridgeError('invalid_artifact', 'Stored chunk verification failed')
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            if sha_file(assembled) != (value['size'], value['sha256']):
                raise BridgeError('invalid_artifact', 'Complete artifact SHA256 verification failed')
        await disk_work(assemble)
        self.bridge.session(session['session_id'], operation='artifacts')
        if self.transfer(session, p['transfer_id'], actor)['status'] != 'receiving':
            raise BridgeError('transfer_closed', 'Transfer was cancelled')
        project = self.bridge.project(session['project_id'], session['peer_id'])
        value['project_id'] = session['project_id']
        self.record('artifact_transfers', value['transfer_id'], value)
        def guard():
            # This runs in the disk worker: read owner configuration only, never
            # the event-loop SQLite connection. Recheck immediately before link.
            self.bridge.peer(session['peer_id'])
            current = self.bridge.project(session['project_id'], session['peer_id'])
            if 'artifacts' not in current.get('allowed_ops', []):
                raise BridgeError('scope_denied', 'Artifact permission was revoked')
            if Path(current['workspace']).resolve() != Path(session['workspaces'][self.bridge.node_id]).resolve() or \
                    Path(current['import_root']).resolve() != Path(project['import_root']).resolve():
                raise BridgeError('scope_changed', 'Selected artifact locations changed during transfer')
        path = await disk_work(self.atomic_copy, assembled, project['import_root'], value['destination'],
                               value['size'], value['sha256'], value['transfer_id'], guard)
        result = await self.archive(session, value['artifact_id'], assembled, value['size'], value['sha256'], str(path))
        value.update(status='completed', result=result)
        self.record('artifact_transfers', value['transfer_id'], value)
        self.remove_parts(value)
        return result

    def abort(self, session, p, actor):
        value = self.transfer(session, p['transfer_id'], actor)
        if value['status'] != 'completed':
            value['status'] = 'aborted'
            self.record('artifact_transfers', value['transfer_id'], value)
            self.remove_parts(value)
        return self.transfer_view(value)

    def cleanup(self, revoked_peer=None):
        if self.receive_lock.locked():
            return
        self.cleanup_staging()
        for value in self.bridge.store.all('artifact_transfers'):
            revoked = revoked_peer is not None and value.get('peer_id', value['actor']) == revoked_peer
            if value['status'] == 'receiving' and (revoked or
                    time.time() - value['updated_epoch'] > TRANSFER_TTL_SECONDS):
                value['status'] = 'aborted' if revoked else 'expired'
                self.record('artifact_transfers', value['transfer_id'], value)
            if value['status'] in ('completed', 'expired', 'aborted'):
                self.remove_parts(value)

    async def revoke(self, peer_id):
        for item in self.bridge.store.all('artifact_requests'):
            if item['peer_id'] == peer_id and item['status'] != 'completed':
                item['status'] = 'aborted'
                self.record('artifact_requests', item['request_id'], item)
        async with self.receive_lock:
            for item in self.bridge.store.all('artifact_transfers'):
                if item.get('peer_id', item['actor']) == peer_id and item['status'] != 'completed':
                    item['status'] = 'aborted'
                    self.record('artifact_transfers', item['transfer_id'], item)
                    self.remove_parts(item)

    async def incoming(self, method, p, actor):
        session = self.bridge.session(p['session_id'], actor, 'artifacts')
        if method == 'peer.artifact_begin':
            self.cleanup()
            async with self.receive_lock:
                return await self.begin(session, p, actor)
        if method == 'peer.artifact_chunk':
            async with self.receive_lock:
                return await self.chunk(session, p, actor)
        if method == 'peer.artifact_commit':
            async with self.receive_lock:
                return await self.commit(session, p, actor)
        if method == 'peer.artifact_abort':
            async with self.receive_lock:
                return self.abort(session, p, actor)
        item = self.metadata(session, p['artifact_id'])
        if method == 'peer.artifact_manifest':
            return {**item, 'chunk_bytes': CHUNK_BYTES, 'protocol': 1}
        index = p['index']
        count = (item['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES
        if type(index) is not int or not 0 <= index < count:
            raise BridgeError('invalid_artifact', 'Chunk index is outside this artifact')
        def read_chunk():
            with self.blob(item['artifact_id']).open('rb') as handle:
                handle.seek(index * CHUNK_BYTES)
                return handle.read(CHUNK_BYTES)
        block = await disk_work(read_chunk)
        return {'index': index, 'data': base64.b64encode(block).decode(), 'sha256': hashlib.sha256(block).hexdigest()}

    def request(self, p, direction):
        identifier(p['request_id'], 'request_id')
        prior, fingerprint = self.bridge.mutation(p['request_id'], 'artifact_' + direction, p)
        existing = self.bridge.store.get('artifact_requests', p['request_id'])
        if existing and existing['digest'] != fingerprint:
            raise BridgeError('duplicate_conflict', 'Artifact request ID was used for different content or scope')
        return prior, fingerprint, existing

    def check_active(self, request_id):
        item = self.bridge.store.get('artifact_requests', request_id)
        if item and item['status'] == 'aborted':
            raise BridgeError('transfer_closed', 'Transfer was cancelled')

    async def start(self, p, direction):
        session = self.bridge.session(p['session_id'], operation='artifacts')
        prior, fingerprint, value = self.request(p, 'out' if direction == 'send' else 'fetch')
        if prior:
            return prior['result']
        self.check_active(p['request_id'])
        if p['request_id'] not in self.running:
            value = value or {**p, 'digest': fingerprint, 'direction': direction,
                              'peer_id': session['peer_id'], 'created_at': now()}
            value.update(status='queued')
            value.pop('error', None)
            self.record('artifact_requests', p['request_id'], value)
            async def run():
                try:
                    if direction == 'send':
                        await self.send(p)
                    else:
                        await self.negotiate(session['peer_id'])
                        manifest = await self.bridge.remote(session['peer_id'], 'peer.artifact_manifest', {
                            'session_id': p['session_id'], 'artifact_id': p['artifact_id']})
                        await self.fetch(p, manifest)
                except asyncio.CancelledError:
                    current = self.bridge.store.get('artifact_requests', p['request_id'])
                    if current['status'] != 'aborted':
                        current.update(status='paused', error={'code': 'process_interrupted', 'retryable': True,
                            'message': 'Repeat the original operation with the same request ID to resume'})
                        self.record('artifact_requests', p['request_id'], current)
                except Exception as exc:
                    current = self.bridge.store.get('artifact_requests', p['request_id'])
                    error = exc.as_dict() if isinstance(exc, BridgeError) else {
                        'code': 'artifact_io_error', 'message': 'Artifact I/O failed; inspect selected paths and retry the same request ID', 'retryable': True}
                    if current['status'] != 'aborted':
                        current.update(status='paused' if error.get('retryable') else 'failed', error=error)
                        self.record('artifact_requests', p['request_id'], current)
                finally:
                    self.running.pop(p['request_id'], None)
            self.running[p['request_id']] = asyncio.create_task(run())
        return await self.status(p)

    async def send(self, p):
        session = self.bridge.session(p['session_id'], operation='artifacts')
        prior, fingerprint, request = self.request(p, 'out')
        if prior:
            return prior['result']
        if request and request['status'] == 'aborted':
            raise BridgeError('transfer_closed', 'Transfer was cancelled; choose a new request ID')
        capabilities = await self.negotiate(session['peer_id'])
        if not request or not request.get('snapshot_ready'):
            project = self.bridge.project(session['project_id'], session['peer_id'])
            source = safe_path(project['export_root'], p['path'])
            if not source.is_file():
                raise BridgeError('file_denied', 'Selected export file does not exist')
            if source.stat().st_size > MAX_FILE_BYTES:
                raise BridgeError('file_denied', 'Artifact exceeds 256 MiB')
            size, sha256 = await disk_work(sha_file, source)
            if request and request.get('sha256') and request['sha256'] != sha256:
                raise BridgeError('duplicate_conflict', 'Export file changed after this request started; choose a new request ID')
            if size > capabilities['max_file_bytes']:
                raise BridgeError('file_denied', 'Artifact exceeds the peer transfer limit')
            artifact_id = 'artifact-' + hashlib.sha256((self.bridge.node_id + '|' + p['request_id']).encode()).hexdigest()[:32]
            self.reserve(size, p['request_id'])
            self.check_active(p['request_id'])
            request = {**p, 'digest': fingerprint, 'artifact_id': artifact_id, 'size': size, 'sha256': sha256,
                       'direction': 'send', 'status': 'preparing', 'peer_id': session['peer_id'], 'created_at': now()}
            self.record('artifact_requests', p['request_id'], request)
            await self.archive(session, artifact_id, source, size, sha256, p['path'])
            self.check_active(p['request_id'])
            request['snapshot_ready'] = True
            request['status'] = 'transferring'
            self.record('artifact_requests', p['request_id'], request)
        offer = {k: request[k] for k in ('session_id', 'request_id', 'artifact_id', 'destination', 'size', 'sha256')}
        reply = await self.bridge.remote(session['peer_id'], 'peer.artifact_begin', offer)
        current = self.bridge.store.get('artifact_requests', p['request_id'])
        if current['status'] == 'aborted':
            # A cancellation while begin was in flight had no remote ID yet.
            # Retain it before the existing abort attempt so an explicit retry
            # can reconcile a lost reply without creating another transfer.
            current['transfer_id'] = reply['transfer_id']
            self.record('artifact_requests', p['request_id'], current)
            return (await self.cancel(p)).get('result')
        self.check_active(p['request_id'])
        request['transfer_id'] = reply['transfer_id']
        self.record('artifact_requests', p['request_id'], request)
        if reply['status'] == 'completed':
            result = reply['result']
        else:
            with self.blob(request['artifact_id']).open('rb') as handle:
                for index in range((request['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES):
                    self.bridge.session(session['session_id'], operation='artifacts')
                    if self.bridge.store.get('artifact_requests', p['request_id'])['status'] == 'aborted':
                        raise BridgeError('transfer_closed', 'Transfer was cancelled')
                    block = await disk_work(handle.read, CHUNK_BYTES)
                    if index in reply['received']:
                        continue
                    await self.bridge.remote(session['peer_id'], 'peer.artifact_chunk', {
                        'session_id': session['session_id'], 'transfer_id': reply['transfer_id'], 'index': index,
                        'sha256': hashlib.sha256(block).hexdigest(), 'data': base64.b64encode(block).decode()})
                    self.check_active(p['request_id'])
                    request['transferred_bytes'] = (index + 1) * CHUNK_BYTES if len(block) == CHUNK_BYTES else request['size']
                    self.record('artifact_requests', p['request_id'], request)
            result = await self.bridge.remote(session['peer_id'], 'peer.artifact_commit', {
                'session_id': session['session_id'], 'transfer_id': reply['transfer_id']})
        request.update(status='completed', result=result)
        self.record('artifact_requests', p['request_id'], request)
        self.bridge.store.put('mutations', p['request_id'], {'digest': fingerprint, 'result': result})
        return result

    async def fetch(self, p, manifest):
        session = self.bridge.session(p['session_id'], operation='artifacts')
        prior, fingerprint, request = self.request(p, 'fetch')
        if prior:
            return prior['result']
        if request and request['status'] == 'aborted':
            raise BridgeError('transfer_closed', 'Transfer was cancelled; choose a new request ID')
        self.check_metadata(manifest)
        offer = {'session_id': session['session_id'], 'request_id': p['request_id'], 'artifact_id': p['artifact_id'],
                 'destination': p['destination'], 'size': manifest['size'], 'sha256': manifest['sha256']}
        async with self.receive_lock:
            reply = await self.begin(session, offer, self.bridge.node_id)
        request = request or {**p, 'digest': fingerprint, 'direction': 'fetch', 'status': 'transferring',
                             'peer_id': session['peer_id'], 'created_at': now()}
        if self.bridge.store.get('artifact_requests', p['request_id'])['status'] == 'aborted':
            async with self.receive_lock:
                self.abort(session, {'transfer_id': reply['transfer_id']}, self.bridge.node_id)
        self.check_active(p['request_id'])
        request.update(transfer_id=reply['transfer_id'], size=manifest['size'], sha256=manifest['sha256'], status='transferring')
        self.record('artifact_requests', p['request_id'], request)
        for index in range((manifest['size'] + CHUNK_BYTES - 1) // CHUNK_BYTES):
            if index in reply['received']:
                continue
            block = await self.bridge.remote(session['peer_id'], 'peer.artifact_read', {
                'session_id': session['session_id'], 'artifact_id': p['artifact_id'], 'index': index})
            self.bridge.session(session['session_id'], operation='artifacts')
            self.check_active(p['request_id'])
            async with self.receive_lock:
                received = await self.chunk(session, {'transfer_id': reply['transfer_id'], 'index': index,
                                          'sha256': block['sha256'], 'data': block['data']}, self.bridge.node_id)
            request['transferred_bytes'] = received['received_bytes']
            self.record('artifact_requests', p['request_id'], request)
        async with self.receive_lock:
            result = await self.commit(session, {'transfer_id': reply['transfer_id']}, self.bridge.node_id)
        result = {**result, 'path': str(safe_path(self.bridge.project(session['project_id'], session['peer_id'])['import_root'], p['destination']))}
        request.update(status='completed', result=result)
        self.record('artifact_requests', p['request_id'], request)
        self.bridge.store.put('mutations', p['request_id'], {'digest': fingerprint, 'result': result})
        return result

    async def status(self, p):
        self.bridge.session(p['session_id'], operation='artifacts')
        value = self.bridge.store.get('artifact_requests', identifier(p['request_id'], 'request_id'))
        if not value or value['session_id'] != p['session_id']:
            raise BridgeError('transfer_not_found', 'No locally initiated chunked transfer in this session')
        return {k: value[k] for k in ('request_id', 'session_id', 'transfer_id', 'artifact_id', 'direction',
                'status', 'size', 'sha256', 'transferred_bytes', 'created_at', 'updated_at', 'result', 'error',
                'remote_abort_pending') if k in value}

    async def cancel(self, p):
        await self.status(p)
        # Retain a local reference across the await; weak entries disappear
        # after all callers finish, without leaking one lock per past request.
        lock = self.cancel_locks.setdefault(p['request_id'], asyncio.Lock())
        async with lock:
            return await self._cancel(p)

    async def _cancel(self, p):
        session = self.bridge.session(p['session_id'], operation='artifacts')
        await self.status(p)
        value = self.bridge.store.get('artifact_requests', p['request_id'])
        if value['status'] == 'completed' and not value.get('remote_abort_pending'):
            return await self.status(p)
        if value['status'] != 'completed':
            value['status'] = 'aborted'
        self.record('artifact_requests', p['request_id'], value)
        if value.get('transfer_id'):
            args = {'session_id': p['session_id'], 'transfer_id': value['transfer_id']}
            if value['direction'] == 'send':
                value['remote_abort_pending'] = True
                self.record('artifact_requests', p['request_id'], value)
                try:
                    reply = await self.bridge.remote(session['peer_id'], 'peer.artifact_abort', args)
                    if not isinstance(reply, dict) or reply.get('status') not in ('aborted', 'completed') or (
                            reply['status'] == 'completed' and not isinstance(reply.get('result'), dict)):
                        raise BridgeError('invalid_response', 'Peer did not acknowledge artifact cancellation', True)
                    value = self.bridge.store.get('artifact_requests', p['request_id'])
                    if reply['status'] == 'completed':
                        value.update(status='completed', result=reply['result'])
                        self.bridge.store.put('mutations', p['request_id'], {'digest': value['digest'], 'result': reply['result']})
                    value.pop('remote_abort_pending', None)
                    value.pop('error', None)
                    self.record('artifact_requests', p['request_id'], value)
                except BridgeError as exc:
                    # A concurrently finishing send may already have its
                    # commit receipt; a late abort failure cannot undo it.
                    value = self.bridge.store.get('artifact_requests', p['request_id'])
                    if value.get('remote_abort_pending'):
                        value['error'] = exc.as_dict()
                        self.record('artifact_requests', p['request_id'], value)
            else:
                async with self.receive_lock:
                    self.abort(session, args, self.bridge.node_id)
        return await self.status(p)

    async def close(self):
        jobs = list(self.running.values())
        for task in jobs:
            task.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
