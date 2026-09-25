"""Execution authority comes only from this computer's selected project policy.

No network listeners, SSH, model runtimes, or installed configuration are used.
Calls go directly through Bridge with the existing harmless FakeAdapter.
"""
import asyncio
import contextlib
import copy
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import uuid

from codex_bridge import cli, onboarding
from codex_bridge.codex_adapter import AdapterError
from codex_bridge.core import Bridge, BridgeError
from test_core import FakeAdapter


class ExecutionPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='bridge-execution-policy-')
        self.root = Path(self.temporary.name).resolve()
        self.path = self.root / 'config.json'
        projects = {}
        for name, policy in [('default', None), ('read', 'read-only'),
                             ('write', 'workspace-write'), ('full', 'full-access')]:
            workspace = self.root / name
            workspace.mkdir()
            (workspace / 'export').mkdir()
            (workspace / 'import').mkdir()
            projects[name] = {'name': name, 'workspace': str(workspace),
                'export_root': str(workspace / 'export'), 'import_root': str(workspace / 'import'),
                'allowed_peers': ['peer'], 'allowed_ops': ['tasks', 'messages', 'artifacts', 'context']}
            if policy is not None:
                projects[name]['policy'] = policy
        self.config = {'version': 1, 'peer_id': 'owner', 'state_dir': str(self.root / 'state'),
            'listen_port': 42123, 'local_token': 'owner-fixture-' + 'x' * 32,
            'codex_path': str(self.root / 'unused-fake'),
            'peers': {'peer': {'enabled': True, 'url': 'http://127.0.0.1:1',
                'incoming_token': 'peer-fixture-' + 'x' * 32, 'outgoing_token': 'outgoing-fixture-' + 'x' * 32}},
            'projects': projects}
        self.save()
        self.adapter = FakeAdapter('owner')
        self.bridge = Bridge(self.path, adapter=self.adapter)
        self.bridge.remote = mock.AsyncMock(side_effect=AssertionError('Unexpected network operation'))
        self.bridge.remote_direct = mock.AsyncMock(side_effect=AssertionError('Unexpected network operation'))

    async def asyncTearDown(self):
        for task in list(self.bridge.running.values()):
            task.cancel()
        await asyncio.gather(*list(self.bridge.running.values()), return_exceptions=True)
        await self.bridge.adapter.close()
        await self.bridge.artifact_transfers.close()
        await self.bridge.connections.close()
        self.bridge.store.db.close()
        self.bridge.remote.assert_not_awaited()
        self.bridge.remote_direct.assert_not_awaited()
        self.temporary.cleanup()

    def save(self):
        self.path.write_text(json.dumps(self.config), encoding='utf-8')

    @staticmethod
    def isolated_save(path, value):
        # Exercise owner-local configuration selection without invoking Windows
        # ACL commands. Every path in these tests belongs to TemporaryDirectory.
        Path(path).write_text(json.dumps(value), encoding='utf-8')

    def select(self, project, policy=None):
        with mock.patch.object(cli, 'save', side_effect=self.isolated_save):
            result = onboarding.project_select(self.path, project_id=project,
                peer_id='peer', policy=policy, batch=True,
                prompt=mock.Mock(side_effect=AssertionError('No extra confirmation needed')))
        self.config = json.loads(self.path.read_text())
        return result

    async def offer(self, project='read', **extra):
        request = {'project_id': project, 'session_id': 'session-' + uuid.uuid4().hex,
            'name': 'Execution policy fixture', 'goal': 'Verify local execution authority',
            'context': 'Fixture context', 'responsibilities': {'owner': 'test'},
            'workspace': '/peer/workspace', 'allowed_ops': ['tasks', 'messages', 'artifacts', 'context']}
        request.update(extra)
        return await self.bridge.dispatch('peer.session_offer', request, actor='peer')

    async def execute(self, session, request_id=None, prompt='Harmless fixture task', **extra):
        request = {'session_id': session['session_id'], 'request_id': request_id or str(uuid.uuid4()),
                   'prompt': prompt, **extra}
        await self.bridge.dispatch('peer.task_accept', request, actor='peer')
        running = self.bridge.running.get(request['request_id'])
        if running:
            await running
        return self.bridge.store.get('incoming', request['request_id'])

    def assert_execution(self, task, policy, workspace):
        self.assertEqual(task['status'], 'completed', task.get('error'))
        self.assertEqual(task['execution_policy'], policy)
        call = self.adapter.runs[-1]
        self.assertEqual(call['policy'], policy)
        self.assertEqual(call['workspace'], workspace)
        self.assertEqual(call['writable_roots'], [workspace] if policy == 'workspace-write' else [])

    async def test_default_and_restricted_policies_keep_existing_execution_behavior(self):
        for project, policy in [('default', 'read-only'), ('read', 'read-only'), ('write', 'workspace-write')]:
            with self.subTest(project=project):
                session = await self.offer(project)
                task = await self.execute(session)
                self.assert_execution(task, policy, self.config['projects'][project]['workspace'])

    async def test_explicit_local_full_access_uses_no_fake_confined_write_roots(self):
        session = await self.offer('full')
        task = await self.execute(session)
        self.assert_execution(task, 'full-access', self.config['projects']['full']['workspace'])

    async def test_session_offer_cannot_select_or_inject_execution_policy(self):
        before = self.path.read_bytes()
        session = await self.offer('read', policy='full-access', execution_policy='full-access',
            sandbox='danger-full-access', sandboxPolicy={'type': 'dangerFullAccess'},
            writable_roots=[str(self.root)], project_policy='full-access',
            projects={'read': {'policy': 'full-access'}})
        self.assertNotIn('policy', session)
        self.assertNotIn('execution_policy', session)
        task = await self.execute(session)
        self.assert_execution(task, 'read-only', self.config['projects']['read']['workspace'])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_peer_context_sync_cannot_expand_project_authority(self):
        session = await self.offer('read')
        before = self.path.read_bytes()
        await self.bridge.dispatch('peer.context_sync', {'session_id': session['session_id'],
            'context': 'Updated fixture context', 'goal': 'Updated goal', 'responsibilities': {},
            'revision': 2, 'policy': 'full-access', 'execution_policy': 'full-access',
            'workspace': str(self.root), 'writable_roots': [str(self.root)],
            'project_id': 'full'}, actor='peer')
        task = await self.execute(session)
        self.assert_execution(task, 'read-only', self.config['projects']['read']['workspace'])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_peer_context_update_cannot_expand_project_authority(self):
        session = await self.offer('read')
        # A locally coordinated session permits the peer to propose context.
        stored = self.bridge.store.get('sessions', session['session_id'])
        stored['coordinator_id'] = 'owner'
        self.bridge.store.put('sessions', session['session_id'], stored)
        before = self.path.read_bytes()
        await self.bridge.dispatch('peer.context_update', {'session_id': session['session_id'],
            'request_id': str(uuid.uuid4()), 'expected_revision': 1,
            'context': 'Updated fixture context', 'policy': 'full-access',
            'execution_policy': 'full-access', 'project_id': 'full'}, actor='peer')
        task = await self.execute(session)
        self.assert_execution(task, 'read-only', self.config['projects']['read']['workspace'])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_task_payload_cannot_switch_project_policy_or_workspace(self):
        session = await self.offer('read')
        before = self.path.read_bytes()
        task = await self.execute(session, project_id='full', policy='full-access',
            execution_policy='full-access', workspace=self.config['projects']['full']['workspace'],
            writable_roots=[str(self.root)], sandbox='danger-full-access')
        self.assert_execution(task, 'read-only', self.config['projects']['read']['workspace'])
        self.assertEqual(self.path.read_bytes(), before)

    async def test_peer_cannot_override_locally_selected_full_access_with_another_policy(self):
        session = await self.offer('full')
        task = await self.execute(session, policy='read-only', execution_policy='workspace-write',
                                  writable_roots=[self.config['projects']['full']['workspace']])
        self.assert_execution(task, 'full-access', self.config['projects']['full']['workspace'])

    async def test_explicit_local_selection_changes_only_selected_project(self):
        before = copy.deepcopy(self.config)
        result = self.select('read', 'full-access')
        self.assertEqual(result['policy'], 'full-access')
        self.assertTrue(result['new_session_required'])
        self.assertEqual(self.config['peers'], before['peers'])
        for name in ('default', 'write', 'full'):
            self.assertEqual(self.config['projects'][name], before['projects'][name])
        full_task = await self.execute(await self.offer('read'))
        self.assert_execution(full_task, 'full-access', self.config['projects']['read']['workspace'])
        other_task = await self.execute(await self.offer('write'))
        self.assert_execution(other_task, 'workspace-write', self.config['projects']['write']['workspace'])

    async def test_omitted_local_policy_preserves_selection_and_new_project_defaults(self):
        self.assertEqual(self.select('full')['policy'], 'full-access')
        workspace = self.root / 'new'
        workspace.mkdir()
        with mock.patch.object(cli, 'save', side_effect=self.isolated_save):
            result = onboarding.project_select(self.path, project_id='new', name='New fixture',
                workspace=workspace, peer_id='peer', batch=True,
                prompt=mock.Mock(side_effect=AssertionError('Unexpected prompt')))
        self.assertEqual(result['policy'], 'read-only')

    async def test_completed_request_retries_remain_deduplicated_after_local_policy_change(self):
        session = await self.offer('read')
        request_id = str(uuid.uuid4())
        completed = await self.execute(session, request_id=request_id)
        self.assertEqual(completed['execution_policy'], 'read-only')
        conversation = self.bridge.store.get('sessions', session['session_id'])['conversation_ids']
        self.select('read', 'full-access')
        replay = await self.execute(session, request_id=request_id, policy='full-access')
        self.assertEqual(replay, completed)
        self.assertEqual(len(self.adapter.runs), 1)
        self.assertEqual(self.bridge.store.get('sessions', session['session_id'])['conversation_ids'], conversation)
        with self.assertRaises(BridgeError) as changed:
            await self.execute(session, request_id=request_id, prompt='Different intent')
        self.assertEqual(changed.exception.code, 'duplicate_conflict')
        self.assertEqual(len(self.adapter.runs), 1)

    async def test_malformed_owner_policies_fail_before_configuration_changes(self):
        before = self.path.read_bytes()
        for invalid in ('', 'FULL-ACCESS', 'danger-full-access', 'workspaceWrite', True, False, 0, [], {}):
            with self.subTest(policy=invalid):
                with self.assertRaises(BridgeError) as rejected:
                    self.select('read', invalid)
                self.assertEqual(rejected.exception.code, 'configuration')
                self.assertEqual(self.path.read_bytes(), before)

    async def test_malformed_stored_policies_fail_before_task_is_accepted(self):
        session = await self.offer('read')
        for invalid in (None, '', 'danger-full-access', 'unrestricted', True, 0, [], {}):
            with self.subTest(policy=invalid):
                self.config['projects']['read']['policy'] = invalid
                self.save()
                with self.assertRaises(BridgeError) as rejected:
                    await self.execute(session)
                self.assertEqual(rejected.exception.code, 'configuration')
                self.assertEqual(self.bridge.store.all('incoming'), [])
                self.assertEqual(self.adapter.runs, [])

    async def test_peer_cannot_call_owner_project_configuration_commands(self):
        before = self.path.read_bytes()
        for method in ('project_select', 'project_add', 'project-add', 'peer.project_select'):
            with self.subTest(method=method):
                with self.assertRaises(BridgeError) as rejected:
                    await self.bridge.dispatch(method, {'project_id': 'read', 'policy': 'full-access'}, actor='peer')
                self.assertEqual(rejected.exception.code, 'scope_denied')
        self.assertEqual(self.path.read_bytes(), before)

    async def test_full_access_does_not_grant_disallowed_bridge_operations(self):
        self.config['projects']['full']['allowed_ops'] = ['messages']
        self.save()
        session = await self.offer('full')
        with self.assertRaises(BridgeError) as rejected:
            await self.execute(session)
        self.assertEqual(rejected.exception.code, 'scope_denied')
        self.assertEqual(self.adapter.runs, [])

    async def test_cli_project_add_accepts_explicit_full_access_without_global_change(self):
        workspace = self.root / 'cli-project'
        workspace.mkdir()
        before = copy.deepcopy(self.config['projects'])
        with mock.patch.object(cli, 'save', side_effect=self.isolated_save), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(['--config', str(self.path), 'project-add', '--id', 'cli-project',
                '--name', 'CLI fixture', '--workspace', str(workspace), '--peer-id', 'peer',
                '--export-root', str(workspace / 'export'), '--import-root', str(workspace / 'import'),
                '--policy', 'full-access'])
        self.assertEqual(code, 0)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['projects']['cli-project']['policy'], 'full-access')
        self.assertTrue(all(saved['projects'][name] == value for name, value in before.items()))

    async def test_cli_project_select_accepts_full_access_with_no_second_confirmation_flag(self):
        before = copy.deepcopy(self.config['projects'])
        with mock.patch.object(cli, 'save', side_effect=self.isolated_save), contextlib.redirect_stdout(io.StringIO()):
            code = cli.main(['--config', str(self.path), 'project-select', '--id', 'read',
                             '--peer-id', 'peer', '--policy', 'full-access', '--batch'])
        self.assertEqual(code, 0)
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['projects']['read']['policy'], 'full-access')
        for project in ('default', 'write', 'full'):
            self.assertEqual(saved['projects'][project], before[project])

    async def test_adapter_policy_error_codes_survive_start_and_run_failures(self):
        for stage in ('start', 'run'):
            for code in ('unsupported_full_access', 'policy_mismatch', 'policy_change_requires_new_session'):
                with self.subTest(stage=stage, code=code):
                    session = await self.offer('full')
                    failure = AdapterError(code, 'Harmless fixture policy refusal')

                    async def begin_runtime(**kwargs):
                        # Real adapter.run awaits start before opening a turn.
                        # Exercise that propagation without starting a process.
                        await self.adapter.start()
                        raise AssertionError('The rejected runtime must not start a turn')

                    with contextlib.ExitStack() as patches:
                        if stage == 'start':
                            patches.enter_context(mock.patch.object(self.adapter, 'start', side_effect=failure))
                            invoked = patches.enter_context(mock.patch.object(self.adapter, 'run', side_effect=begin_runtime))
                        else:
                            invoked = patches.enter_context(mock.patch.object(self.adapter, 'run', side_effect=failure))
                        task = await self.execute(session)
                        self.assertEqual(task['status'], 'failed')
                        self.assertEqual(task['execution_policy'], 'full-access')
                        self.assertEqual(task['error'], {'code': code,
                            'message': 'Harmless fixture policy refusal', 'retryable': False})
                        replay = await self.execute(session, request_id=task['request_id'])
                        self.assertEqual(replay, task)
                        self.assertEqual(invoked.await_count, 1)
                    self.assertEqual(self.adapter.runs, [])

    async def test_unrelated_runtime_exception_remains_generic_even_with_code_attribute(self):
        ordinary = RuntimeError('Harmless runtime failure')
        misleading = RuntimeError('Harmless runtime failure')
        misleading.code = 'unsupported_full_access'
        for failure in (ordinary, misleading):
            with self.subTest(has_code=hasattr(failure, 'code')):
                session = await self.offer('full')
                with mock.patch.object(self.adapter, 'run', side_effect=failure) as invoked:
                    task = await self.execute(session)
                    self.assertEqual(task['status'], 'failed')
                    self.assertEqual(task['error'], {'code': 'execution_error',
                        'message': 'Harmless runtime failure', 'retryable': False})
                    self.assertEqual(invoked.await_count, 1)
                self.assertEqual(self.adapter.runs, [])

    def preflight(self, project, support_fields):
        runtime = {'ready': True, 'schema_ready': True, 'codex_version': '0.153.4',
                   'runtime_bundle': {'status': 'complete'}, **support_fields}
        runtime_probe = mock.Mock(return_value=runtime)
        runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
        local_call = mock.Mock(return_value={'ok': True})
        probe = mock.Mock(return_value={'ok': True, 'peer_id': 'peer', 'runtime_ready': True,
            'projects': [{'project_id': name} for name in self.config['projects']]})
        tcp_probe = mock.Mock(side_effect=AssertionError('No actual TCP probe is allowed'))
        before = self.path.read_bytes()
        result = onboarding.preflight(self.path, peer_id='peer', project_id=project,
            runtime_probe=runtime_probe, runner=runner, local_call=local_call,
            probe=probe, tcp_probe=tcp_probe)
        runtime_probe.assert_called_once_with(self.config['codex_path'])
        runner.assert_called_once()
        local_call.assert_called_once()
        probe.assert_called_once()
        tcp_probe.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        return result

    async def test_full_access_preflight_rejects_missing_or_false_runtime_support(self):
        for fields in ({}, {'full_access_supported': False}):
            with self.subTest(support=fields):
                result = self.preflight('full', fields)
                self.assertFalse(result['ok'])
                policy_check = next(item for item in result['checks'] if item['category'] == 'execution_policy')
                self.assertEqual(policy_check['state'], 'fail')
                self.assertEqual(policy_check['code'], 'full_access_unsupported')
                self.assertEqual(policy_check['policy'], 'full-access')
                self.assertEqual(policy_check['project_id'], 'full')
                self.assertEqual([item['category'] for item in result['checks'] if item['state'] == 'fail'],
                                 ['execution_policy'])

    async def test_full_access_preflight_passes_explicit_runtime_support(self):
        result = self.preflight('full', {'full_access_supported': True})
        self.assertTrue(result['ok'], result)
        policy_check = next(item for item in result['checks'] if item['category'] == 'execution_policy')
        self.assertEqual(policy_check['state'], 'pass')
        self.assertEqual(policy_check['code'], 'full_access_supported')
        self.assertEqual(result['native_tool_execution'], 'not_checked')

    async def test_restricted_preflight_remains_compatible_without_optional_full_access_support(self):
        for project in ('default', 'read', 'write'):
            for fields in ({}, {'full_access_supported': False}):
                with self.subTest(project=project, support=fields):
                    result = self.preflight(project, fields)
                    self.assertTrue(result['ok'], result)
                    self.assertNotIn('execution_policy', [item['category'] for item in result['checks']])
                    self.assertEqual(result['native_tool_execution'], 'not_checked')


if __name__ == '__main__':
    unittest.main()
