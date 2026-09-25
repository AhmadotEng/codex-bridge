"""Owner-demanded, bounded SSH connections with authenticated two-end selection.

The durable decision is independent of projects and request payloads.  Only this
module owns the SSH children it creates; legacy routes are never adopted/killed.
Control RPCs use existing directional pairing credentials and a signed response
bound to each fresh request. Project RPCs require the committed generation.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import copy
import hashlib
import hmac
import os
import secrets
import subprocess
import sys
import time
import uuid
from pathlib import Path

from .core import BridgeError, VERSION, canonical, digest, identifier, now
from .transport import configured_transports, transport_args, _spawn_ssh, _BoundedErrors
from . import processes


CAPABILITY = 'on_demand_tunnel_v1'
OWNER_CAPABILITY = 'retained_ssh_owner_v1'
CONTROL_PREFIX = 'peer.connection_'
STAGES = ('local_bridge', 'network_ssh', 'ssh_authentication', 'forwarding',
          'remote_bridge', 'remote_codex')
HARD_ERRORS = {'ssh_host_key_failed', 'ssh_authentication_failed', 'forwarding_port_in_use',
               'forwarding_denied', 'unauthorized', 'scope_denied', 'access_revoked', 'peer_mismatch',
               'configuration', 'peer_stopped', 'connection_stopped', 'protocol_error',
               'capability_missing', 'decision_unresolved', 'bridge_unavailable', 'ssh_cleanup_unconfirmed'}

# Configure parent-death cleanup in a fresh interpreter, avoiding preexec_fn in
# this multithreaded HTTP daemon. The child checks the spawning parent's PID
# after setting PDEATHSIG, closing the fork/prctl race, then execs SSH in place.
_LINUX_CHILD = (
    'import ctypes,os,signal,sys\n'
    'libc=ctypes.CDLL(None,use_errno=True)\n'
    'if libc.prctl(1,signal.SIGKILL,0,0,0)!=0: raise OSError(ctypes.get_errno(),"Cannot establish SSH child ownership")\n'
    'if os.getppid()!=int(sys.argv[1]): sys.exit(125)\n'
    'os.execv(sys.argv[2],sys.argv[2:])\n')


def _uuid(value):
    if not isinstance(value, str):
        raise BridgeError('invalid_argument', 'A canonical UUID request ID is required')
    try:
        if str(uuid.UUID(value)) != value:
            raise ValueError()
    except ValueError as exc:
        raise BridgeError('invalid_argument', 'A canonical UUID request ID is required') from exc
    return value


def _number(settings, key, default, low, high):
    value = settings.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
        raise BridgeError('configuration', 'Invalid connection setting: ' + key)
    return value


def validate_connections(cfg):
    """Validate the shared lane map without opening keys, sockets, or services."""
    entries = cfg.get('connections', {})
    if not isinstance(entries, dict):
        raise BridgeError('configuration', 'connections must map peer IDs to settings')
    own = identifier(cfg['peer_id'])
    occupied = {cfg.get('listen_port')}
    for peer, settings in entries.items():
        identifier(peer)
        if peer == own or peer not in cfg.get('peers', {}) or not isinstance(settings, dict):
            raise BridgeError('configuration', 'A connection must identify a paired other computer')
        if settings.get('mode', 'on_demand') == 'legacy':
            continue
        if settings.get('mode', 'on_demand') != 'on_demand':
            raise BridgeError('configuration', 'Connection mode must be on_demand or legacy')
        lanes = settings.get('lanes')
        nodes = {own, peer}
        if not isinstance(lanes, dict) or set(lanes) != nodes:
            raise BridgeError('configuration', 'Approve one candidate lane for each originating computer')
        remote_ports = set()
        for origin, ports in lanes.items():
            if not isinstance(ports, dict) or set(ports) != nodes:
                raise BridgeError('configuration', 'Each candidate lane needs both computers loopback ports')
            for port in ports.values():
                if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                    raise BridgeError('configuration', 'Candidate ports must be integers between 1 and 65535')
            if ports[own] in occupied or ports[peer] in remote_ports:
                raise BridgeError('configuration', 'Candidate listeners must use distinct reserved ports')
            occupied.add(ports[own]); remote_ports.add(ports[peer])
        for key, default, low, high in (
                ('initial_attempts', 2, 1, 2), ('initial_seconds', 45, 1, 45),
                ('recovery_attempts', 3, 1, 3), ('recovery_seconds', 120, 1, 120),
                ('idle_seconds', 900, 1, 86400)):
            value = _number(settings, key, default, low, high)
            if key.endswith('attempts') and not isinstance(value, int):
                raise BridgeError('configuration', 'Attempt limits must be whole numbers')
        if not isinstance(settings.get('persistent', False), bool):
            raise BridgeError('configuration', 'persistent must be a boolean')
    return entries


class ConnectionManager:
    def __init__(self, bridge):
        self.bridge = bridge
        self.boot = str(uuid.uuid4())
        self.locks = {}
        self.demands = {}
        self.owners = {}
        self.owner_closures = {}
        self.cleanup_failures = set()
        self.local_candidates = {}
        self.verified = {}
        self.challenges = {}
        self.proposals = {}
        self.elections = {}
        self.inflight = {}
        self.last_activity = {}
        self.last_progress = {}
        self.last_probe = {}
        self.deadlines = {}
        self.healthy_since = {}
        self.recovery = {}
        self.drains = {}
        self.cleanup_handles = set()
        self.cleanup_tasks = set()
        self.closed = False
        self.legacy_status = {}
        # A persisted monotonic clock is not valid in another process/boot.
        for state in self.bridge.store.all('connections'):
            if state.get('episode', {}).get('state') in ('attempting', 'recovering'):
                state['episode']['state'] = 'suspended'
            decision = state.get('decision')
            if decision:
                decision['dispatch_ready'] = False
            self._save(state)

    def configured(self, peer):
        entry = self.bridge.config().get('connections', {}).get(peer)
        return bool(entry and entry.get('mode', 'on_demand') == 'on_demand')

    enabled = configured

    def _settings(self, peer):
        self.bridge.peer(peer)
        cfg = self.bridge.config()
        settings = validate_connections(cfg).get(peer)
        if not settings or settings.get('mode', 'on_demand') != 'on_demand':
            raise BridgeError('legacy_connection', 'This peer uses its existing legacy route')
        return settings

    def _lock(self, peer):
        return self.locks.setdefault(peer, asyncio.Lock())

    def _cleanup_pending(self, peer):
        attempts = {attempt for owner_peer, attempt in self.cleanup_failures if owner_peer == peer}
        attempts.update(attempt for (owner_peer, attempt), task in self.owner_closures.items()
                        if owner_peer == peer and not task.done())
        attempts.update(item['attempt_id'] for item in self.bridge.store.all('connection_children')
                        if item.get('peer_id') == peer and not item.get('closed') and item.get('close_error'))
        return sorted(attempts)

    def _require_clean(self, peer):
        if self._cleanup_pending(peer):
            raise BridgeError('ssh_cleanup_unconfirmed',
                'An owned SSH child has not confirmed exit; retry local disconnect before connecting again', True)

    def _state(self, peer):
        state = self.bridge.store.get('connections', peer) or {
            'peer_id': peer, 'state': 'idle', 'generation': 0, 'stop_requested': False,
            'stages': {stage: {'state': 'pass' if stage == 'local_bridge' else 'not_checked',
                               'evidence_at': now() if stage == 'local_bridge' else None}
                       for stage in STAGES}, 'updated_at': now()}
        # The old explicit stop marker remains authoritative through migration.
        if (Path(self.bridge.config()['state_dir']) / 'transports' / peer / 'stop').exists():
            state['stop_requested'] = True
        return state

    def _save(self, state):
        state['updated_at'] = now()
        self.bridge.store.put('connections', state['peer_id'], state)

    def _stage(self, peer, stage, value, code=None):
        state = self._state(peer)
        state['stages'][stage] = {'state': value, 'evidence_at': now(), 'code': code}
        self._save(state)

    def _coordinator(self, peer):
        return min((self.bridge.node_id, peer), key=lambda v: v.encode('utf-8'))

    def _url(self, peer, candidate):
        self._validate_candidate(peer, candidate)
        ports = self._settings(peer)['lanes'][candidate['initiator']]
        return 'http://127.0.0.1:' + str(ports[self.bridge.node_id])

    def _validate_candidate(self, peer, candidate):
        settings = self._settings(peer)
        if (not isinstance(candidate, dict) or candidate.get('initiator') not in (peer, self.bridge.node_id)
                or candidate.get('nodes') != sorted((peer, self.bridge.node_id))
                or candidate.get('map_digest') != digest(settings['lanes'])):
            raise BridgeError('protocol_error', 'Candidate does not match the approved pairing and forwarding map')
        _uuid(candidate.get('attempt_id'))
        return candidate

    def _validate_decision(self, peer, decision):
        if not isinstance(decision, dict):
            raise BridgeError('protocol_error', 'Connection decision must be an object')
        candidate = self._validate_candidate(peer, decision.get('candidate'))
        generation = decision.get('generation')
        if (isinstance(generation, bool) or not isinstance(generation, int) or generation < 1
                or decision.get('previous_generation') != generation-1
                or decision.get('connection_id') != candidate['attempt_id']
                or decision.get('state') not in ('prepared', 'committed', 'draining', 'closed')):
            raise BridgeError('protocol_error', 'Malformed connection generation or decision identity')
        return decision

    def _candidate(self, peer):
        return {'attempt_id': str(uuid.uuid4()), 'initiator': self.bridge.node_id,
                'nodes': sorted((peer, self.bridge.node_id)), 'map_digest': digest(self._settings(peer)['lanes'])}

    def _activity(self, peer):
        return bool(self.inflight.get(peer, 0) or self.bridge.connection_activity(peer))

    def _demand_active(self, peer):
        task = self.demands.get(peer)
        return bool(task and not task.done())

    def _owned_alive(self, peer, candidate):
        # Only a retained child handle is ownership. A journal PID or a newly
        # reopened listener at the same address cannot revive an older carrier.
        key = (peer, candidate['attempt_id'])
        item = self.owners.get(key); closing = self.owner_closures.get(key)
        return bool(item and key not in self.cleanup_failures and not (closing and not closing.done())
                    and item[0].process.poll() is None)

    async def _carrier_alive(self, peer, candidate):
        self._validate_candidate(peer, candidate)
        if candidate['initiator'] == self.bridge.node_id:
            return self._owned_alive(peer, candidate)
        try:
            proof = await self._rpc(peer, 'owner', {'candidate': candidate}, candidate, timeout=1)
        except BridgeError as exc:
            if exc.code in ('peer_unavailable', 'connection_timeout'): return False
            raise
        except (asyncio.TimeoutError, OSError): return False
        if proof.get('ownership_proof') != OWNER_CAPABILITY:
            raise BridgeError('capability_missing', 'The peer must support exact retained SSH owner verification')
        return (proof.get('peer_id') == peer and proof.get('attempt_id') == candidate['attempt_id']
                and proof.get('alive') is True)

    async def _require_carrier(self, peer, candidate):
        if not await self._carrier_alive(peer, candidate):
            raise BridgeError('carrier_unavailable', 'The selected SSH origin no longer owns this exact live carrier', True)

    def _same_decision(self, peer, decision, states=('committed',)):
        state = self._state(peer); current = state.get('decision') or {}
        return bool(not state['stop_requested'] and current.get('state') in states
                    and current.get('connection_id') == decision.get('connection_id')
                    and current.get('generation') == decision.get('generation'))

    def _persistent(self, peer):
        # The saved startup choice is the one authority. Merely editing a
        # connection example's `persistent` field cannot authorize dialing.
        from . import autostart
        path = getattr(self.bridge, 'config_path', None)
        return bool(path and autostart.enabled(path, 'transport', peer_id=peer))

    def record_codex(self, peer, result=None, error=None):
        if not self.configured(peer): return
        adapter = result or {}
        if isinstance(adapter.get('codex'), dict): adapter = adapter['codex']
        if isinstance(adapter.get('adapter'), dict): adapter = adapter['adapter']
        ready = adapter.get('ready', adapter.get('available'))
        detail = error or adapter.get('error')
        self._stage(peer, 'remote_codex', 'fail' if detail or ready is False else 'pass' if ready is True else 'not_checked',
                    (detail.get('code') if isinstance(detail, dict) else getattr(detail, 'code', None)) if detail else None)

    def record_progress(self, peer):
        """A substantive RPC/result was acknowledged, not merely polled."""
        self.last_progress[peer] = time.monotonic()

    def record_probe_failure(self, peer, error):
        """A failed authenticated Bridge probe is not a Codex runtime failure."""
        detail = error.as_dict() if isinstance(error, BridgeError) else error
        if not isinstance(detail, dict):
            detail = {'code': 'peer_unavailable', 'message': 'Peer Bridge could not be verified', 'retryable': True}
        if not self.configured(peer):
            self.legacy_status[peer] = {'peer_id': peer, 'legacy': True, 'available': False,
                'state': 'disconnected', 'error': detail, 'updated_at': now()}
            return
        state = self._state(peer)
        if state.get('decision'): state['decision']['dispatch_ready'] = False
        state['state'] = 'disconnected'; state['error'] = copy.deepcopy(detail)
        state['stages']['remote_bridge'] = {'state': 'fail', 'evidence_at': now(), 'code': detail.get('code')}
        if detail.get('code') in ('peer_unavailable', 'connection_timeout'):
            for stage in ('network_ssh', 'ssh_authentication', 'forwarding'):
                if state['stages'][stage]['state'] == 'pass':
                    state['stages'][stage].update(state='stale', evidence_at=now())
        state['stages']['remote_codex'] = {'state': 'not_checked', 'evidence_at': None, 'code': None}
        self._save(state)

    def _defer_cleanup(self, peer, generation, winner):
        # A delayed old commit/close must not sweep up a candidate created
        # afterwards while the *same* generation still awaits replacement.
        # Capture exact owned attempts at authorization, never future children.
        attempts = set(self.local_candidates.get(peer, ())) - {winner}
        async def cleanup():
            if self.closed: return
            decision = self._state(peer).get('decision') or {}
            if decision.get('generation') == generation:
                for attempt in attempts:
                    await self._close_owner(peer, attempt)
                    self.local_candidates.get(peer, set()).discard(attempt)
        def schedule():
            self.cleanup_handles.discard(handle)
            if self.closed: return
            task = asyncio.create_task(cleanup()); self.cleanup_tasks.add(task)
            def completed(value):
                self.cleanup_tasks.discard(value)
                # Failure is retained on the child journal/status. Consume the
                # background exception without retiring its owned handle.
                if not value.cancelled(): value.exception()
            task.add_done_callback(completed)
        handle = asyncio.get_running_loop().call_later(.5, schedule)
        self.cleanup_handles.add(handle)

    async def _rpc(self, peer, action, payload, candidate, timeout=10):
        nonce = secrets.token_hex(32)
        request = {'payload': payload, 'nonce': nonce}
        result = await asyncio.wait_for(self.bridge.remote_direct(peer, CONTROL_PREFIX + action,
            request, self._url(peer, candidate)), timeout)
        if not isinstance(result, dict) or not isinstance(result.get('signature'), str):
            raise BridgeError('protocol_error', 'Peer does not support authenticated connection control')
        signed = {'nonce': nonce, 'method': action, 'result': result.get('result')}
        secret = self.bridge.peer(peer)['incoming_token'].encode()
        expected = hmac.new(secret, canonical(signed).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, result['signature']):
            raise BridgeError('unauthorized', 'Connection control response authentication failed')
        return result['result']

    async def peer_rpc(self, method, params, actor):
        self._settings(actor)
        action = method.removeprefix(CONTROL_PREFIX)
        allowed = {'probe', 'callback', 'propose', 'prepare', 'commit', 'decision', 'health', 'owner',
                   'abort', 'drain', 'undrain', 'close', 'disconnect'}
        if action not in allowed or not isinstance(params, dict):
            raise BridgeError('scope_denied', 'Unknown connection control operation')
        nonce, payload = params.get('nonce'), params.get('payload')
        if not isinstance(nonce, str) or len(nonce) != 64 or not isinstance(payload, dict):
            raise BridgeError('protocol_error', 'Invalid connection control envelope')
        self.bridge.peer(actor)
        if self._state(actor)['stop_requested'] and action not in ('decision', 'disconnect', 'close', 'abort'):
            raise BridgeError('peer_stopped', 'The receiving owner deliberately stopped this peer; local resume is required')
        result = await getattr(self, '_receive_' + action)(actor, payload)
        self.bridge.peer(actor)
        signed = {'nonce': nonce, 'method': action, 'result': result}
        signature = hmac.new(self.bridge.peer(actor)['outgoing_token'].encode(),
                             canonical(signed).encode(), hashlib.sha256).hexdigest()
        return {'result': result, 'signature': signature}

    async def _receive_owner(self, peer, p):
        candidate = self._validate_candidate(peer, p.get('candidate'))
        if candidate['initiator'] != self.bridge.node_id:
            raise BridgeError('protocol_error', 'Only the carrier origin can attest retained child ownership')
        return {'peer_id': self.bridge.node_id, 'attempt_id': candidate['attempt_id'],
                'alive': self._owned_alive(peer, candidate), 'version': VERSION,
                'ownership_proof': OWNER_CAPABILITY}

    async def _verify(self, peer, candidate, generation=0):
        self._validate_candidate(peer, candidate)
        await self._require_carrier(peer, candidate)
        nonce = secrets.token_hex(32)
        key = (peer, candidate['attempt_id'], generation)
        self.challenges[key] = {'nonce': nonce, 'expires': time.monotonic() + 30,
                                'boot': self.boot, 'returned': False}
        try:
            answer = await self._rpc(peer, 'probe', {'candidate': candidate, 'challenge': nonce,
                 'generation': generation, 'issuer_boot': self.boot}, candidate)
            if answer.get('ownership_proof') != OWNER_CAPABILITY:
                raise BridgeError('capability_missing', 'Both peers must support retained SSH owner verification')
            challenge = self.challenges[key]
            if (answer.get('peer_id') != peer or answer.get('challenge') != nonce
                    or not challenge['returned'] or challenge['expires'] < time.monotonic()):
                raise BridgeError('peer_mismatch', 'Mutual forwarding challenge failed')
            await self._require_carrier(peer, candidate)
            self.verified[(peer, candidate['attempt_id'])] = time.monotonic() + 30
            for stage in ('ssh_authentication', 'forwarding', 'remote_bridge'):
                self._stage(peer, stage, 'pass')
            return answer
        finally:
            self.challenges.pop(key, None)

    async def _receive_probe(self, peer, p):
        candidate = self._validate_candidate(peer, p.get('candidate'))
        nonce = p.get('challenge')
        if (not isinstance(nonce, str) or len(nonce) != 64 or not isinstance(p.get('generation'), int)
                or not isinstance(p.get('issuer_boot'), str)):
            raise BridgeError('protocol_error', 'Malformed mutual challenge')
        await self._require_carrier(peer, candidate)
        # Callback can use only the locally approved return lane, never a URL supplied by a peer.
        answer = await self._rpc(peer, 'callback', p, candidate)
        if answer.get('challenge') != nonce:
            raise BridgeError('protocol_error', 'Reverse forwarding callback failed')
        await self._require_carrier(peer, candidate)
        self.verified[(peer, candidate['attempt_id'])] = time.monotonic() + 30
        if p['generation'] and answer.get('committed'):
            async with self._lock(peer):
                state = self._state(peer); decision = state.get('decision') or {}
                if (decision.get('state') == 'committed' and decision.get('generation') == p['generation']
                        and decision.get('connection_id') == candidate['attempt_id']):
                    decision['dispatch_ready'] = True; state['state'] = 'connected'; self._save(state)
        for stage in ('ssh_authentication', 'forwarding', 'remote_bridge'):
            self._stage(peer, stage, 'pass')
        return {'peer_id': self.bridge.node_id, 'challenge': nonce, 'capability': CAPABILITY,
                'ownership_proof': OWNER_CAPABILITY, 'version': VERSION}

    async def _receive_callback(self, peer, p):
        candidate = self._validate_candidate(peer, p.get('candidate'))
        await self._require_carrier(peer, candidate)
        key = (peer, candidate['attempt_id'], p.get('generation'))
        challenge = self.challenges.get(key)
        if (not challenge or challenge['expires'] < time.monotonic() or challenge['returned']
                or challenge['nonce'] != p.get('challenge') or p.get('issuer_boot') != self.boot):
            raise BridgeError('protocol_error', 'Expired, replayed, or unissued connection challenge')
        challenge['returned'] = True
        decision = self._state(peer).get('decision') or {}
        return {'challenge': challenge['nonce'], 'peer_id': self.bridge.node_id,
                'committed': decision.get('state') == 'committed'
                    and decision.get('generation') == p.get('generation')
                    and decision.get('connection_id') == candidate['attempt_id']}

    def _verified(self, peer, candidate):
        self._validate_candidate(peer, candidate)
        if self.verified.get((peer, candidate['attempt_id']), 0) < time.monotonic():
            raise BridgeError('protocol_error', 'Candidate needs fresh mutual verification')

    async def _healthy(self, peer, decision):
        if not decision or decision.get('state') != 'committed':
            return False
        try:
            if not await self._carrier_alive(peer, decision['candidate']): return False
            result = await self._rpc(peer, 'health', {'connection_id': decision['connection_id'],
                'generation': decision['generation']}, decision['candidate'], timeout=1)
            if not self._same_decision(peer, decision): return False
            if result.get('committed') and (not result.get('dispatch_ready') or not decision.get('dispatch_ready')):
                await self._verify(peer, decision['candidate'], decision['generation'])
                async with self._lock(peer):
                    state = self._state(peer)
                    if not self._same_decision(peer, decision):
                        return False
                    state['decision']['dispatch_ready'] = True; state['state'] = 'connected'; self._save(state)
            alive = await self._carrier_alive(peer, decision['candidate'])
            healthy = result.get('committed') is True and alive and self._same_decision(peer, decision)
            if healthy:
                # A restarted non-origin can retain the proven carrier, but
                # still needs a finite idle clock. Polls never renew it.
                self.last_activity.setdefault(peer, time.monotonic())
            return healthy
        except (BridgeError, asyncio.TimeoutError, OSError):
            return False

    async def _receive_health(self, peer, p):
        state = self._state(peer); decision = state.get('decision')
        if (not decision or decision.get('state') != 'committed'
                or decision['connection_id'] != p.get('connection_id')
                or decision['generation'] != p.get('generation')):
            raise BridgeError('stale_generation', 'Connection generation is not committed here')
        # The caller separately proves the physical origin. If this endpoint
        # is that origin, also check its handle while answering the health RPC.
        if decision['candidate']['initiator'] == self.bridge.node_id:
            await self._require_carrier(peer, decision['candidate'])
        if not self._same_decision(peer, decision):
            raise BridgeError('stale_generation', 'Selected connection changed during health verification')
        return {'committed': True, 'dispatch_ready': decision.get('dispatch_ready', False),
                'peer_id': self.bridge.node_id, 'active': self._activity(peer)}

    async def _receive_decision(self, peer, p):
        return {'decision': self._state(peer).get('decision'), 'stop_requested': self._state(peer)['stop_requested'],
                'version': VERSION, 'ownership_proof': OWNER_CAPABILITY}

    async def _reconcile_commit(self, peer, decision):
        # A surviving origin permits a restarted non-origin coordinator to
        # finish its durable commit without first competing for occupied ports.
        if (self._coordinator(peer) != self.bridge.node_id or not decision
                or decision.get('state') != 'committed'
                or not await self._carrier_alive(peer, decision['candidate'])):
            return False
        await self._verify(peer, decision['candidate'], decision['generation'])
        await self._rpc(peer, 'commit', {'decision': decision}, decision['candidate'])
        async with self._lock(peer):
            if not self._same_decision(peer, decision):
                raise BridgeError('decision_unresolved', 'Selected connection changed during commit recovery')
            state = self._state(peer); state['decision']['dispatch_ready'] = True
            state['state'] = 'connected'; self._save(state)
        self.last_activity.setdefault(peer, time.monotonic())
        return True

    async def _select(self, peer, candidate):
        if self._coordinator(peer) == self.bridge.node_id:
            return await self._receive_propose(peer, {'candidate': candidate})
        return await self._rpc(peer, 'propose', {'candidate': candidate}, candidate, timeout=20)

    async def _receive_propose(self, peer, p):
        self._require_clean(peer)
        if self._coordinator(peer) != self.bridge.node_id:
            raise BridgeError('protocol_error', 'Only the immutable-ID coordinator selects a connection')
        candidate = p['candidate']; self._verified(peer, candidate)
        async with self._lock(peer):
            self.proposals.setdefault(peer, {})[candidate['attempt_id']] = candidate
            task = self.elections.get(peer)
            if task is None or task.done():
                task = self.elections[peer] = asyncio.create_task(self._elect(peer))
        return await asyncio.shield(task)

    async def _elect(self, peer):
        await asyncio.sleep(.25)  # Coordinator-local contention window, never wall-clock ordering.
        async with self._lock(peer):
            candidates = list(self.proposals.pop(peer, {}).values())
            candidate = min(candidates, key=lambda c: (c['initiator'].encode(), c['attempt_id']))
            prior = self._state(peer).get('decision')
        if prior and prior.get('state') == 'draining':
            # A fresh authenticated candidate supplies a recovery control path
            # even if the old lane disappeared halfway through idle closure.
            other = (await self._rpc(peer, 'decision', {}, candidate)).get('decision') or {}
            if other.get('generation') != prior['generation'] or other.get('connection_id') != prior['connection_id']:
                raise BridgeError('decision_unresolved', 'Idle closure endpoints disagree about the selected generation')
            if other.get('state') == 'closed':
                async with self._lock(peer):
                    state = self._state(peer); state['decision']['state'] = 'closed'; self._save(state)
                prior = self._state(peer)['decision']
            elif other.get('state') in ('committed', 'draining'):
                await self._rpc(peer, 'undrain', {'generation': prior['generation'],
                    'connection_id': prior['connection_id']}, candidate)
                async with self._lock(peer):
                    state = self._state(peer); state['decision'].update(state='committed', dispatch_ready=False); self._save(state)
                prior = self._state(peer)['decision']
            else:
                raise BridgeError('decision_unresolved', 'Idle closure state requires owner inspection')
        if prior and prior.get('state') == 'prepared':
            # No timeout may overthrow an unresolved durable prepare. Reconcile on this fresh lane.
            other = await self._rpc(peer, 'decision', {}, candidate)
            if (other.get('decision') or {}).get('state') == 'committed':
                raise BridgeError('decision_unresolved', 'Peer has a committed decision absent locally; inspect durable state')
            await self._rpc(peer, 'abort', {'decision': prior}, candidate)
            async with self._lock(peer):
                state = self._state(peer); state['decision']['state'] = 'closed'; self._save(state)
        if prior and prior.get('state') == 'committed':
            # Recover a lost commit ACK idempotently before considering replacement.
            # Verify the *old physical owner* first. The fresh candidate can
            # occupy the old port pair and answer HTTP for an otherwise dead ID.
            if not prior.get('dispatch_ready'):
                await self._reconcile_commit(peer, prior)
            if await self._healthy(peer, prior):
                return self._state(peer)['decision']
        async with self._lock(peer):
            state = self._state(peer)
            if state['stop_requested']:
                raise BridgeError('connection_stopped', 'Owner stopped this connection')
            if self.inflight.get(peer, 0):
                raise BridgeError('decision_unresolved', 'In-flight operations must drain before replacing their connection', True)
            generation = state['generation'] + 1
            decision = {'connection_id': candidate['attempt_id'], 'candidate': candidate,
                        'generation': generation, 'state': 'prepared', 'dispatch_ready': False,
                        'prepared_at': now(), 'previous_generation': state['generation']}
            state.update(generation=generation, decision=decision, state='prepared'); self._save(state)
        await self._rpc(peer, 'prepare', {'decision': decision}, candidate)
        async with self._lock(peer):
            state = self._state(peer)
            if state['stop_requested']:
                raise BridgeError('connection_stopped', 'Owner stopped while preparing this connection')
            decision = state['decision']; decision.update(state='committed', committed_at=now())
            self._save(state)  # Durable coordinator selection point precedes sending commit.
        try:
            await self._rpc(peer, 'commit', {'decision': decision}, candidate)
        except (BridgeError, asyncio.TimeoutError) as exc:
            if isinstance(exc, BridgeError) and exc.code not in ('peer_unavailable', 'connection_timeout'):
                raise
            # Lost acknowledgment is reconciled using the exact durable decision;
            # this cannot create another generation or another task execution.
            other = await self._rpc(peer, 'decision', {}, candidate)
            recorded = other.get('decision') or {}
            if (recorded.get('connection_id') != decision['connection_id']
                    or recorded.get('generation') != decision['generation']
                    or recorded.get('state') != 'committed'):
                await self._rpc(peer, 'commit', {'decision': decision}, candidate)
        async with self._lock(peer):
            state = self._state(peer)
            if state['stop_requested']:
                raise BridgeError('connection_stopped', 'Owner stopped before commit acknowledgment')
            state['decision']['dispatch_ready'] = True; state['state'] = 'connected'
            state.pop('peer_paused', None); self._save(state)
        # A newly selected generation receives its own idle interval. Keeping
        # the old generation's expired timer would immediately close it again.
        self.last_activity[peer] = time.monotonic()
        await self._close_losers(peer, candidate['attempt_id'])
        return self._state(peer)['decision']

    async def _receive_prepare(self, peer, p):
        self._require_clean(peer)
        if self._coordinator(peer) != peer:
            raise BridgeError('protocol_error', 'Prepare must come from the pairing coordinator')
        decision = self._validate_decision(peer, p.get('decision')); self._verified(peer, decision['candidate'])
        await self._require_carrier(peer, decision['candidate'])
        prior = self._state(peer).get('decision')
        if prior and prior.get('state') == 'committed' and prior['generation'] != decision['generation']:
            if await self._healthy(peer, prior):
                raise BridgeError('decision_unresolved', 'Existing jointly committed connection is still healthy')
        async with self._lock(peer):
            state = self._state(peer)
            if state['stop_requested']:
                raise BridgeError('peer_stopped', 'Receiving owner stopped this peer')
            existing = state.get('decision')
            if existing and existing['generation'] == decision['generation']:
                if existing['connection_id'] != decision['connection_id']:
                    raise BridgeError('protocol_error', 'Generation already belongs to another candidate')
                return {'prepared': True}
            if decision['previous_generation'] != state['generation'] or decision['generation'] != state['generation'] + 1:
                raise BridgeError('decision_unresolved', 'Endpoints have different durable generations')
            if self.inflight.get(peer, 0):
                raise BridgeError('decision_unresolved', 'Existing requests must drain before a new generation', True)
            state.update(generation=decision['generation'], decision=copy.deepcopy(decision), state='prepared')
            self._save(state)
        return {'prepared': True}

    async def _receive_commit(self, peer, p):
        if self._coordinator(peer) != peer:
            raise BridgeError('protocol_error', 'Commit must come from the pairing coordinator')
        proposed = self._validate_decision(peer, p.get('decision'))
        await self._require_carrier(peer, proposed['candidate'])
        async with self._lock(peer):
            state = self._state(peer); existing = state.get('decision')
            if (state['stop_requested'] or not existing or existing['state'] not in ('prepared', 'committed')
                    or existing['generation'] != proposed['generation']
                    or existing['connection_id'] != proposed['connection_id']):
                raise BridgeError('decision_unresolved', 'No matching prepared decision can be committed')
            newly_committed = existing['state'] == 'prepared'
            existing.update(state='committed', dispatch_ready=True, committed_at=now())
            state['state'] = 'connected'; state.pop('peer_paused', None); self._save(state)
        if newly_committed:
            self.last_activity[peer] = time.monotonic()
        else:
            # A duplicate commit or health reconciliation is not new activity.
            self.last_activity.setdefault(peer, time.monotonic())
        # Defer loser closure until the commit response has travelled back.
        if newly_committed:
            self._defer_cleanup(peer, proposed['generation'], proposed['candidate']['attempt_id'])
        return {'committed': True, 'connection_id': existing['connection_id'], 'generation': existing['generation']}

    async def _receive_abort(self, peer, p):
        if self._coordinator(peer) != peer:
            raise BridgeError('protocol_error', 'Only the coordinator can abort preparation')
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision')
            proposed = self._validate_decision(peer, p.get('decision'))
            if decision and decision['generation'] == proposed['generation']:
                if decision['connection_id'] != proposed['connection_id']:
                    raise BridgeError('protocol_error', 'Abort candidate does not own this generation')
                if decision['state'] == 'committed':
                    raise BridgeError('decision_unresolved', 'A committed generation cannot be aborted as uncommitted')
                decision.update(state='closed', dispatch_ready=False); self._save(state)
            elif state['generation'] == proposed['previous_generation']:
                closed = copy.deepcopy(proposed); closed.update(state='closed', dispatch_ready=False)
                state.update(generation=closed['generation'], decision=closed); self._save(state)
            else:
                raise BridgeError('decision_unresolved', 'Cannot abort a generation inconsistent with durable state')
        return {'aborted': True}

    @asynccontextmanager
    async def route(self, peer, activity=True):
        self.bridge.peer(peer)
        if not self.configured(peer):
            yield {'url': self.bridge.peer(peer)['url'], 'connection': None}
            return
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision')
            if state['stop_requested']:
                raise BridgeError('connection_stopped', 'Owner stopped this peer; explicit local retry is required')
            if not decision or decision.get('state') != 'committed' or not decision.get('dispatch_ready'):
                raise BridgeError('not_connected', 'Not connected; remote Bridge not checked.', True)
            if decision['candidate']['initiator'] == self.bridge.node_id and not self._owned_alive(peer, decision['candidate']):
                raise BridgeError('carrier_unavailable', 'The selected SSH child has exited', True)
            selected = {'url': self._url(peer, decision['candidate']), 'connection': {
                'connection_id': decision['connection_id'], 'generation': decision['generation']}}
            self.inflight[peer] = self.inflight.get(peer, 0) + 1
            if activity: self.last_activity[peer] = time.monotonic()
        acknowledged = False
        try:
            yield selected
            acknowledged = True
        finally:
            self.inflight[peer] -= 1
            if acknowledged and activity: self.record_progress(peer)

    @asynccontextmanager
    async def accept(self, peer, connection, activity=True):
        if not self.configured(peer):
            if connection is not None:
                raise BridgeError('capability_missing', 'Peer has not enabled negotiated connection generations')
            yield
            return
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision')
            if state['stop_requested']:
                raise BridgeError('peer_stopped', 'The receiving owner stopped this peer')
            if (not decision or decision.get('state') != 'committed' or not decision.get('dispatch_ready')
                    or not isinstance(connection, dict)
                    or connection.get('connection_id') != decision['connection_id']
                    or connection.get('generation') != decision['generation']):
                raise BridgeError('stale_generation', 'Project request requires the current committed connection generation')
            self._validate_candidate(peer, decision['candidate'])
            if decision['candidate']['initiator'] == self.bridge.node_id and not self._owned_alive(peer, decision['candidate']):
                raise BridgeError('carrier_unavailable', 'The receiving origin no longer owns this SSH child', True)
            self.inflight[peer] = self.inflight.get(peer, 0) + 1
            if activity: self.last_activity[peer] = time.monotonic()
        acknowledged = False
        try:
            yield
            acknowledged = True
        finally:
            self.inflight[peer] -= 1
            if acknowledged and activity: self.record_progress(peer)

    async def ensure(self, peer, request_id, retry=False):
        self.bridge.peer(peer); _uuid(request_id)
        if not self.configured(peer):
            result = await self.bridge.remote_direct(peer, 'peer.status', {}, self.bridge.peer(peer)['url'])
            if result.get('peer_id') != peer:
                raise BridgeError('peer_mismatch', 'Legacy route belongs to another computer')
            result = {'peer_id': peer, 'state': 'legacy_connected', 'available': True, 'legacy': True,
                      'message': 'Authenticated legacy Bridge route verified.', 'updated_at': now()}
            self.legacy_status[peer] = result
            return result
        self._settings(peer)
        self._require_clean(peer)
        async with self._lock(peer):
            previous = self.bridge.store.get('connection_requests', peer + ':' + request_id)
            if previous and previous.get('finished'):
                return {**self.status(peer), 'request_id': request_id, 'deduplicated': True}
            if previous and (peer not in self.demands or self.demands[peer].done()):
                # A retry is itself idempotent. A crashed retry's ID cannot
                # authorize a second allowance; the owner must issue a NEW ID.
                return {**self.status(peer), 'request_id': request_id, 'deduplicated': True,
                        'next_action': 'This request was interrupted or already handled; use a new explicit retry request ID.'}
            if peer in self.demands and not self.demands[peer].done():
                task = self.demands[peer]
            else:
                state = self._state(peer)
                if state['stop_requested'] and not retry:
                    return self.status(peer)
                if state.get('episode', {}).get('state') in ('exhausted', 'suspended', 'failed') and not retry:
                    return self.status(peer)
                if retry:
                    (Path(self.bridge.config()['state_dir']) / 'transports' / peer / 'stop').unlink(missing_ok=True)
                    state['stop_requested'] = False; state.pop('episode', None); self._save(state)
                    self.recovery.pop(peer, None)
                task = self.demands[peer] = asyncio.create_task(self._demand(peer, request_id))
            self.bridge.store.put('connection_requests', peer + ':' + request_id,
                                 {'request_id': request_id, 'peer_id': peer, 'finished': False, 'created_at': now()})
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            # Caller cancellation does not cancel a shared demand. A disconnect does.
            raise
        self.bridge.store.put('connection_requests', peer + ':' + request_id,
            {'request_id': request_id, 'peer_id': peer, 'finished': True, 'result': result, 'updated_at': now()})
        return result

    async def _demand(self, peer, request_id, recovery=False):
        self._require_clean(peer)
        began = time.monotonic()
        draining = self.drains.get(peer)
        if draining:
            await asyncio.wait_for(draining.wait(), 30)
        settings = self._settings(peer); state = self._state(peer); decision = state.get('decision')
        if await self._healthy(peer, decision):
            # _healthy already reconciles a matching committed generation via
            # mutual proof. Do not perform a second stale asynchronous write.
            return self.status(peer)
        if decision:
            async with self._lock(peer):
                state = self._state(peer)
                if state.get('decision'): state['decision']['dispatch_ready'] = False
                for stage in STAGES[1:]:
                    if state['stages'][stage]['state'] == 'pass':
                        state['stages'][stage].update(state='stale', evidence_at=now())
                state['state'] = 'disconnected'; self._save(state)
        # A real outage after selection belongs to one durable recovery episode;
        # asking again with a new request ID cannot buy another initial budget.
        recovery = recovery or bool(decision and decision.get('state') == 'committed')
        if recovery: self.recovery.setdefault(peer, request_id)
        mode = 'recovery' if recovery else 'initial'
        limit = settings.get(mode + '_attempts', 3 if recovery else 2)
        seconds = settings.get(mode + '_seconds', 120 if recovery else 45)
        episode = self._state(peer).get('episode') if recovery else None
        if not episode or episode.get('kind') != mode or episode.get('state') not in ('recovering', 'healthy'):
            episode = {'request_id': request_id, 'kind': mode, 'attempts': 0,
                       'state': 'recovering' if recovery else 'attempting', 'started_at': now()}
            self.deadlines[peer] = began + seconds
        deadline = self.deadlines.get(peer, 0)
        state = self._state(peer); state.update(episode=episode, state='connecting'); self._save(state)
        error = BridgeError('ssh_endpoint_unreachable', 'SSH endpoint could not be reached', True)
        while episode['attempts'] < limit and time.monotonic() < deadline:
            if self._state(peer)['stop_requested']:
                break
            episode['attempts'] += 1
            state = self._state(peer); state['episode'] = episode; self._save(state)  # Charge before any spawn.
            candidate = None
            try:
                prior = self._state(peer).get('decision')
                reconciled = False
                if (prior and prior.get('state') == 'committed' and not prior.get('dispatch_ready')
                        and self._coordinator(peer) == self.bridge.node_id):
                    candidate = prior['candidate']
                    reconciled = await asyncio.wait_for(self._reconcile_commit(peer, prior), max(.01, deadline-time.monotonic()))
                if not reconciled:
                    candidate = self._candidate(peer)
                    self.local_candidates.setdefault(peer, set()).add(candidate['attempt_id'])
                    await asyncio.wait_for(self._open_candidate(peer, candidate), max(.01, deadline-time.monotonic()))
                    await asyncio.wait_for(self._select(peer, candidate), max(.01, deadline-time.monotonic()))
                async with self._lock(peer):
                    state = self._state(peer)
                    if (state.get('episode') or {}).get('request_id') != episode['request_id']:
                        raise BridgeError('decision_unresolved', 'Recovery episode changed before finalization; its budget was not reset')
                    state['episode']['state'] = 'healthy'; state['error'] = None; self._save(state)
                self.healthy_since.setdefault(peer, time.monotonic())
                await self._close_losers(peer, state['decision']['candidate']['attempt_id'])
                return self.status(peer)
            except asyncio.CancelledError:
                if candidate: await self._close_owner(peer, candidate['attempt_id'])
                raise
            except (BridgeError, OSError, asyncio.TimeoutError) as exc:
                error = exc if isinstance(exc, BridgeError) else BridgeError('connection_timeout', 'Connection verification exceeded its bounded deadline', True)
                if error.code == 'ssh_cleanup_unconfirmed':
                    # A remote refusal or concurrent local close can occur
                    # before selection. Retain only a candidate owning the
                    # durable prepared/committed decision, not an orphan lane.
                    selected = self._state(peer).get('decision') or {}
                    if candidate and (selected.get('connection_id') != candidate['attempt_id'] or
                            selected.get('state') not in ('prepared', 'committed')):
                        try:
                            await self._close_owner(peer, candidate['attempt_id'])
                        except BridgeError as cleanup:
                            if cleanup.code != 'ssh_cleanup_unconfirmed': raise
                            error = cleanup  # Exact ownership remains retained.
                    # A failed loser close preserves the selected winner and
                    # never buys another SSH attempt on an occupied lane.
                    state = self._state(peer); state['episode']['state'] = 'failed'
                    if not selected or selected.get('state') not in ('prepared', 'committed'):
                        state['state'] = 'failed'
                    state['error'] = error.as_dict(); self._save(state)
                    return self.status(peer)
                stage = ('network_ssh' if error.code == 'ssh_endpoint_unreachable' else
                         'ssh_authentication' if error.code in ('ssh_authentication_failed', 'ssh_host_key_failed') else
                         'forwarding' if error.code.startswith('forwarding') else 'remote_bridge')
                self._stage(peer, stage, 'fail', error.code)
                selected = self._state(peer).get('decision') or {}
                if candidate and (selected.get('connection_id') != candidate['attempt_id'] or selected.get('state') not in ('prepared', 'committed')):
                    await self._close_owner(peer, candidate['attempt_id'])
                if error.code in HARD_ERRORS:
                    break
                delay = 5 if episode['attempts'] == 1 else 15
                if episode['attempts'] < limit and time.monotonic() + delay < deadline:
                    await asyncio.sleep(delay)
                elif episode['attempts'] < limit:
                    break
        state = self._state(peer)
        state['episode']['state'] = 'failed' if error.code in HARD_ERRORS else 'exhausted'
        state['state'] = 'stopped' if state['stop_requested'] else 'failed'
        state['error'] = error.as_dict(); state['next_action'] = 'Explicit local retry is required; inspect the failing stage first.'
        self._save(state)
        return self.status(peer)

    async def _open_candidate(self, peer, candidate):
        cfg = self.bridge.config(); transports = configured_transports(cfg)
        if peer not in transports:
            raise BridgeError('configuration', 'Configure this computers authorized SSH receiving route for the selected peer')
        t = copy.deepcopy(transports[peer]); ports = self._settings(peer)['lanes'][self.bridge.node_id]
        t.update(local_peer_port=ports[self.bridge.node_id], remote_peer_port=ports[peer])
        cfg = copy.deepcopy(cfg); cfg.pop('ssh_transport', None); cfg['ssh_transports'] = {peer: t}
        args = transport_args(cfg, peer)
        # Let OpenSSH atomically claim the approved listeners. A plain bind
        # pre-probe mistakes Linux TIME_WAIT for a live competing listener.
        # ExitOnForwardFailure plus classified stderr reports real conflicts;
        # neither case ever authorizes killing a process that owns a port.
        self._stage(peer, 'network_ssh', 'checking')
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(t['ssh_host'], t['ssh_port']), 5)
            writer.close(); await writer.wait_closed()
        except (OSError, asyncio.TimeoutError) as exc:
            self._stage(peer, 'network_ssh', 'fail', 'ssh_endpoint_unreachable')
            raise BridgeError('ssh_endpoint_unreachable', 'SSH receiving endpoint is unreachable; check the private network and receiver', True) from exc
        self._stage(peer, 'network_ssh', 'pass')
        self._stage(peer, 'ssh_authentication', 'checking')
        args.insert(1, '-v')  # Bounded, private stderr establishes the SSH authentication stage.
        if sys.platform.startswith('linux'):
            owner = processes.OwnedProcess([sys.executable, '-c', _LINUX_CHILD, str(os.getpid()), *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        else:
            owner = _spawn_ssh(args)
        errors = _BoundedErrors(owner.process.stderr)
        self.owners[(peer, candidate['attempt_id'])] = (owner, errors)
        self.bridge.store.put('connection_children', candidate['attempt_id'], processes.record(owner.process.pid,
            peer_id=peer, attempt_id=candidate['attempt_id'], map_digest=candidate['map_digest'],
            args_digest=digest(args), creator_boot=self.boot, created_at=now(), closed=False))
        began = time.monotonic(); authenticated = False
        while time.monotonic() - began < 12:
            if owner.process.poll() is not None:
                code = errors.finish()
                self._stage(peer, 'forwarding' if code.startswith('forwarding') else 'ssh_authentication', 'fail', code)
                raise BridgeError(code, 'SSH setup failed; inspect the SSH authentication or forwarding stage', code not in HARD_ERRORS)
            if b'Authenticated to ' in bytes(errors.data) or b'Authentication succeeded' in bytes(errors.data):
                authenticated = True; self._stage(peer, 'ssh_authentication', 'pass')
                try:
                    await self._verify(peer, candidate)
                    return
                except BridgeError as exc:
                    if exc.code != 'peer_unavailable': raise
                except asyncio.TimeoutError:
                    pass
            await asyncio.sleep(.15)
        if authenticated:
            self._stage(peer, 'remote_bridge', 'fail', 'bridge_unavailable')
            raise BridgeError('bridge_unavailable', 'SSH connected; remote Bridge unavailable at the configured port.')
        raise BridgeError('ssh_endpoint_unreachable', 'SSH did not authenticate within its bounded timeout', True)

    async def _close_owner(self, peer, attempt):
        key = (peer, attempt)
        task = self.owner_closures.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._finish_owner_close(peer, attempt))
            self.owner_closures[key] = task
            # Caller cancellation does not interrupt the bounded exact-owner
            # close. A later disconnect joins it, rather than closing twice.
            def finished(value):
                if self.owner_closures.get(key) is value:
                    self.owner_closures.pop(key, None)
                if not value.cancelled(): value.exception()
            task.add_done_callback(finished)
        await asyncio.shield(task)

    async def _finish_owner_close(self, peer, attempt):
        key = (peer, attempt)
        item = self.owners.get(key)
        if item:
            owner, errors = item
            try:
                await asyncio.to_thread(owner.close)
                # Popen's retained Windows handle / POSIX waitable child stays
                # exact across PID reuse and the Linux wrapper's exec(ssh).
                if owner.process.poll() is None:
                    raise subprocess.TimeoutExpired(['owned-ssh'], 5)
                await asyncio.to_thread(errors.finish)
                record = self.bridge.store.get('connection_children', attempt)
                if record:
                    record.update(closed=True, closed_at=now()); record.pop('close_error', None)
                    self.bridge.store.put('connection_children', attempt, record)
            except Exception as exc:
                self.cleanup_failures.add(key)
                error = BridgeError('ssh_cleanup_unconfirmed',
                    'Owned SSH child cleanup is unconfirmed; its exact handle is retained for local disconnect retry', True)
                record = self.bridge.store.get('connection_children', attempt)
                if record:
                    record.update(closed=False, close_error=error.as_dict(), close_attempted_at=now())
                    self.bridge.store.put('connection_children', attempt, record)
                raise error from exc
            self.owners.pop(key, None)
            self.cleanup_failures.discard(key)
        self.local_candidates.get(peer, set()).discard(attempt)

    async def _close_losers(self, peer, winner):
        failure = None
        for p, attempt in list(self.owners):
            if p == peer and attempt != winner:
                try:
                    await self._close_owner(peer, attempt)
                except BridgeError as exc:
                    failure = failure or exc
        if failure: raise failure
        self._require_clean(peer)

    def status(self, peer_id=None):
        if peer_id is None:
            return {'peer_id': self.bridge.node_id, 'connections': [self.status(p) for p in self.bridge.config().get('peers', {})]}
        identifier(peer_id)
        if not self.configured(peer_id):
            return self.legacy_status.get(peer_id) or {'peer_id': peer_id, 'state': 'legacy', 'legacy': True,
                    'available': False, 'message': 'Not connected; remote Bridge not checked.'}
        state = self._state(peer_id); decision = state.get('decision')
        pending_cleanup = self._cleanup_pending(peer_id)
        locally_alive = (not decision or decision.get('state') != 'committed'
                         or decision['candidate'].get('initiator') == peer_id
                         or (decision['candidate'].get('initiator') == self.bridge.node_id
                             and self._owned_alive(peer_id, decision['candidate'])))
        mapping_current = not decision or decision['candidate']['map_digest'] == digest(
            self.bridge.config().get('connections', {}).get(peer_id, {}).get('lanes'))
        if pending_cleanup:
            message = 'Owned SSH child cleanup is unconfirmed; retry local disconnect before connecting again.'
        elif state['stop_requested']:
            message = 'Connection deliberately stopped; resume requires an explicit local retry.'
        elif decision and decision.get('state') == 'committed' and not locally_alive:
            message = 'The selected SSH origin no longer owns this live carrier; a new collaboration demand is required.'
        elif (state.get('error') or {}).get('code') == 'bridge_unavailable':
            message = 'SSH connected; remote Bridge unavailable at the configured port.'
        elif (state.get('error') or {}).get('code') in ('unauthorized', 'scope_denied'):
            message = 'SSH connected; remote Bridge authorization refused.'
        elif not mapping_current and decision.get('state') not in ('closed', 'failed'):
            message = 'Approved forwarding map changed; the previous connection is not verified for this map.'
        elif decision and decision.get('state') == 'committed' and decision.get('dispatch_ready'):
            message = 'Authenticated Bridge connection established.'
        elif state['state'] == 'idle':
            message = 'Not connected; remote Bridge not checked.'
        else:
            message = state.get('error', {}).get('message', 'Connection verification is incomplete.') if state.get('error') else 'Connection verification is incomplete.'
        return {**copy.deepcopy(state), 'message': message, 'available': bool(decision and
            decision.get('state') == 'committed' and decision.get('dispatch_ready') and not state['stop_requested'] and mapping_current and locally_alive and not pending_cleanup),
            'cleanup_pending': pending_cleanup,
            'remaining_seconds': round(max(0, self.deadlines.get(peer_id, 0)-time.monotonic()), 2),
            'active_operations': self.inflight.get(peer_id, 0)}

    async def disconnect(self, peer, request_id):
        _uuid(request_id); identifier(peer)
        cfg = self.bridge.config()
        paired = cfg.get('peers', {}).get(peer)
        revoked = bool(paired is not None and not paired.get('enabled', False))
        if revoked:
            # This operation is owner-local. Revocation must not prevent it
            # from closing this daemon's exact retained children. It neither
            # re-enables credentials nor authenticates/sends any remote RPC.
            settings = validate_connections(cfg).get(peer)
            if not settings or settings.get('mode', 'on_demand') != 'on_demand':
                raise BridgeError('legacy_connection', 'This peer uses its existing legacy route')
        else:
            self._settings(peer)
        old = self.bridge.store.get('connection_disconnects', peer + ':' + request_id)
        if old: return old
        async with self._lock(peer):
            state = self._state(peer); decision = copy.deepcopy(state.get('decision'))
            # Revocation already forbids this route, including for pending work.
            # Match maintenance: close only retained local children, preserving
            # all task/transfer/result journals for owner reconciliation.
            if not revoked and self._activity(peer):
                raise BridgeError('connection_busy', 'Active tasks, transfers, or pending results must finish or be cancelled before disconnect', True)
            confirmed = False
            if decision and not revoked:
                try:
                    await self._rpc(peer, 'disconnect', {'connection_id': decision['connection_id'],
                        'generation': decision['generation']}, decision['candidate'], timeout=3)
                    confirmed = True
                except BridgeError as exc:
                    if exc.code == 'connection_busy': raise
                except (OSError, asyncio.TimeoutError): pass
            state.update(stop_requested=True, state='revoked' if revoked else 'stopped')
            if state.get('decision'): state['decision'].update(state='closed', dispatch_ready=False)
            self._save(state)
        task = self.demands.get(peer)
        if task and not task.done(): task.cancel()
        await self._close_losers(peer, None)
        result = {'peer_id': peer, 'request_id': request_id, 'stop_requested': True,
                  'remote_closure_confirmed': confirmed, 'local_only': revoked, 'updated_at': now()}
        self.bridge.store.put('connection_disconnects', peer + ':' + request_id, result)
        return result

    async def _receive_disconnect(self, peer, p):
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision')
            if self._activity(peer):
                raise BridgeError('connection_busy', 'Remote active tasks, transfers, or pending results prevent disconnect', True)
            if not decision or decision['generation'] != p.get('generation') or decision['connection_id'] != p.get('connection_id'):
                raise BridgeError('stale_generation', 'A delayed stop cannot close a different selected generation')
            if decision and decision['generation'] == p.get('generation'):
                decision.update(state='closed', dispatch_ready=False)
            state['state'] = 'peer_paused'; state['peer_paused'] = True; self._save(state)
        # Peer stop is not local owner stop, but maintenance cannot start recovery.
        task = self.demands.get(peer)
        if task and not task.done(): task.cancel()
        self._defer_cleanup(peer, p.get('generation'), None)
        return {'closed': True}

    async def _receive_drain(self, peer, p):
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision')
            if (not decision or decision.get('state') not in ('committed', 'draining')
                    or decision['generation'] != p.get('generation')
                    or decision['connection_id'] != p.get('connection_id') or self._activity(peer)):
                return {'draining': False}
            decision.update(state='draining', dispatch_ready=False); state['state'] = 'draining'; self._save(state)
        return {'draining': True}

    async def _receive_undrain(self, peer, p):
        if self._coordinator(peer) != peer:
            raise BridgeError('protocol_error', 'Only the selection coordinator can resume an idle drain')
        prior = self._state(peer).get('decision') or {}
        if (prior.get('generation') != p.get('generation') or prior.get('connection_id') != p.get('connection_id')
                or prior.get('state') not in ('committed', 'draining')):
            raise BridgeError('decision_unresolved', 'No matching selected idle drain can be resumed')
        alive = await self._carrier_alive(peer, prior['candidate'])
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision') or {}
            if (decision.get('generation') != p.get('generation') or decision.get('connection_id') != p.get('connection_id')
                    or decision.get('state') not in ('committed', 'draining')):
                raise BridgeError('decision_unresolved', 'No matching selected idle drain can be resumed')
            decision.update(state='committed', dispatch_ready=alive)
            state['state'] = 'connected' if alive else 'disconnected'; self._save(state)
        return {'committed': True, 'dispatch_ready': alive}

    async def _receive_close(self, peer, p):
        async with self._lock(peer):
            state = self._state(peer); decision = state.get('decision')
            if (not decision or decision['generation'] != p.get('generation')
                    or decision['connection_id'] != p.get('connection_id') or decision['state'] not in ('draining', 'closed')):
                raise BridgeError('stale_generation', 'No matching draining connection can be closed')
            if decision:
                if decision['state'] == 'draining' and self._activity(peer):
                    raise BridgeError('connection_busy', 'New pending work prevents idle closure', True)
                decision.update(state='closed', dispatch_ready=False); state['state'] = 'idle'; self._save(state)
        self._defer_cleanup(peer, p.get('generation'), None)
        return {'closed': True}

    async def _idle_close(self, peer, decision):
        event = self.drains[peer] = asyncio.Event()
        try:
            await self._idle_close_run(peer, decision)
        finally:
            event.set(); self.drains.pop(peer, None)

    async def _idle_close_run(self, peer, decision):
        if self._coordinator(peer) != self.bridge.node_id:
            return
        async with self._lock(peer):
            if self._activity(peer): return
            state = self._state(peer); state['decision'].update(state='draining', dispatch_ready=False)
            state['state'] = 'draining'; self._save(state)
        try:
            control = {'generation': decision['generation'], 'connection_id': decision['connection_id']}
            result = await self._rpc(peer, 'drain', control, decision['candidate'])
            if not result.get('draining'):
                alive = await self._carrier_alive(peer, decision['candidate'])
                async with self._lock(peer):
                    if not self._same_decision(peer, decision, ('committed', 'draining')): return
                    state = self._state(peer); state['decision'].update(state='committed', dispatch_ready=alive)
                    state['state'] = 'connected' if alive else 'disconnected'; self._save(state)
                return
            async with self._lock(peer):
                current = self._state(peer).get('decision') or {}
                if current.get('generation') != decision['generation'] or current.get('connection_id') != decision['connection_id']:
                    raise BridgeError('decision_unresolved', 'Selected connection changed during idle drain')
                if self._activity(peer):
                    raise BridgeError('connection_busy', 'New local work prevents idle closure', True)
            await self._rpc(peer, 'close', control, decision['candidate'])
            async with self._lock(peer):
                state = self._state(peer); state['decision'].update(state='closed', dispatch_ready=False)
                state['state'] = 'idle'; self._save(state)
            await self._close_losers(peer, None)
        except (BridgeError, OSError, asyncio.TimeoutError) as exc:
            if isinstance(exc, BridgeError) and exc.code == 'connection_busy':
                try:
                    await self._rpc(peer, 'undrain', control, decision['candidate'])
                    alive = await self._carrier_alive(peer, decision['candidate'])
                    async with self._lock(peer):
                        if not self._same_decision(peer, decision, ('committed', 'draining')): return
                        state = self._state(peer); state['decision'].update(state='committed', dispatch_ready=alive)
                        state['state'] = 'connected' if alive else 'disconnected'; self._save(state)
                    return
                except (BridgeError, OSError, asyncio.TimeoutError): pass
            # Uncertain drain must fence new work, never silently revive half a connection.
            if self._same_decision(peer, decision, ('committed', 'draining')):
                state = self._state(peer); state['state'] = 'suspended'; self._save(state)

    async def maintenance(self):
        """Never dial on ordinary startup or because only queued work exists."""
        for peer in self.bridge.config().get('connections', {}):
            if not self.configured(peer): continue
            if not self.bridge.config().get('peers', {}).get(peer, {}).get('enabled'):
                task = self.demands.get(peer)
                if task and not task.done(): task.cancel()
                pending = set(self._cleanup_pending(peer))
                if pending:
                    # Do not repeatedly retry failed cleanup, but revocation
                    # still closes other retained children (including a winner).
                    for owner_peer, attempt in list(self.owners):
                        if owner_peer != peer or attempt in pending: continue
                        try:
                            await self._close_owner(peer, attempt)
                        except BridgeError as exc:
                            if exc.code != 'ssh_cleanup_unconfirmed': raise
                else:
                    try:
                        await self._close_losers(peer, None)
                    except BridgeError as exc:
                        if exc.code != 'ssh_cleanup_unconfirmed': raise
                state = self._state(peer); state['state'] = 'revoked'
                if state.get('decision'): state['decision']['dispatch_ready'] = False
                state['error'] = {'code': 'access_revoked', 'message': 'This peer authorization was revoked', 'retryable': False}
                self._save(state)
                continue
            if self._cleanup_pending(peer): continue
            state = self._state(peer); decision = state.get('decision')
            if state['stop_requested'] or state.get('peer_paused'): continue
            # Selection/finalization owns its durable episode until the demand
            # task finishes. Heartbeats must not clear it or idle-close its lane.
            if self._demand_active(peer): continue
            if not decision or decision.get('state') != 'committed' or not decision.get('dispatch_ready'): continue
            if time.monotonic() - self.last_probe.get(peer, 0) < 5: continue
            self.last_probe[peer] = time.monotonic()
            healthy = await self._healthy(peer, decision)
            # A demand may have started, or a newer selection committed, during
            # the awaited proof. Never fence/refill the replacement's state.
            if self._demand_active(peer) or not self._same_decision(peer, decision): continue
            if healthy:
                if self._activity(peer): self.last_activity[peer] = time.monotonic()
                elif not self._persistent(peer) and time.monotonic()-self.last_activity.get(peer, time.monotonic()) >= self._settings(peer).get('idle_seconds', 900):
                    await self._idle_close(peer, decision)
                if self._demand_active(peer) or not self._same_decision(peer, decision): continue
                health_began = self.healthy_since.get(peer, time.monotonic())
                healthy_with_progress = (time.monotonic()-health_began >= 300
                                         and peer in self.last_progress
                                         and self.last_progress[peer] >= health_began)
                if peer in self.recovery and (not self._activity(peer) or healthy_with_progress):
                    self.recovery.pop(peer, None)
                    state = self._state(peer)
                    if state.get('episode', {}).get('kind') == 'recovery':
                        state.pop('episode', None); self.deadlines.pop(peer, None); self._save(state)
                continue
            self.healthy_since.pop(peer, None)
            state = self._state(peer); state['decision']['dispatch_ready'] = False; state['state'] = 'disconnected'
            for stage in STAGES[1:]:
                if state['stages'][stage]['state'] == 'pass': state['stages'][stage].update(state='stale', evidence_at=now())
            self._save(state)
            if not self._activity(peer) and not self._persistent(peer): continue
            episode = state.get('episode', {})
            if episode.get('state') in ('exhausted', 'failed', 'suspended'): continue
            # Previous origin owns autonomous recovery. Opposite-side owner may explicitly ensure.
            if decision['candidate']['initiator'] != self.bridge.node_id: continue
            task = self.demands.get(peer)
            if task and not task.done(): continue
            request_id = self.recovery.setdefault(peer, str(uuid.uuid4()))
            self.demands[peer] = asyncio.create_task(self._demand(peer, request_id, recovery=True))

    async def close(self):
        self.closed = True
        for handle in self.cleanup_handles: handle.cancel()
        self.cleanup_handles.clear()
        tasks = {*self.demands.values(), *self.elections.values(), *self.cleanup_tasks}
        for task in tasks:
            if not task.done(): task.cancel()
        if tasks: await asyncio.gather(*tasks, return_exceptions=True)
        failure = None
        for peer, attempt in list(self.owners):
            try:
                await self._close_owner(peer, attempt)
            except Exception as exc:
                # A single unconfirmed exit cannot skip shutdown of other
                # exact retained children, including children of other peers.
                failure = failure or exc
        if failure: raise failure
