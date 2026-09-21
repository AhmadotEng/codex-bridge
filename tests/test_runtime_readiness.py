"""Startup must refuse a known incomplete bundle before opening App Server."""
import unittest
from unittest import mock

from codex_bridge.codex_adapter import AdapterError, CodexAdapter


class RuntimeReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_schema_compatible_incomplete_bundle_never_spawns_server(self):
        adapter = CodexAdapter('unused')
        report = {'ready': False, 'schema_ready': True, 'compatibility': 'schema-validated',
                  'codex_version': '0.153.4', 'runtime_bundle': {'status': 'incomplete'},
                  'errors': [{'message': 'PRIVATE-PATH'}]}
        with mock.patch('codex_bridge.codex_adapter.inspect_runtime', return_value=report), \
                mock.patch.object(adapter, '_spawn', new_callable=mock.AsyncMock) as spawn:
            with self.assertRaises(AdapterError) as failure:
                await adapter.start()
            self.assertEqual(failure.exception.code, 'runtime_bundle_incomplete')
            self.assertNotIn('PRIVATE-PATH', str(failure.exception))
            spawn.assert_not_awaited()

    async def test_static_capabilities_never_claim_a_native_command_ran(self):
        adapter = CodexAdapter('unused')
        adapter._compatibility = {'ready': True, 'schema_ready': True,
                                  'runtime_bundle': {'status': 'complete'}}
        adapter._initialized = {'platformOs': 'windows'}
        with mock.patch.object(adapter, 'start', new_callable=mock.AsyncMock):
            result = await adapter.capabilities()
        self.assertTrue(result['ready'])
        self.assertEqual(result['native_tool_execution'], 'not_checked')
        self.assertEqual(result['compatibility']['runtime_bundle']['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
