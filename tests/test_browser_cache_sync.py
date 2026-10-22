import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock

import browser_cache_sync as sync


class CacheSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.cache, self.state, self.key = root/'cache.json', root/'state', root/'key'
        self.key.write_text('synthetic test key')
        self.cache.write_text(json.dumps({'sources':{'Meta':{'jobs':[]}},'fetched_at':'2026-10-21T12:00:00Z'}))
        self.run = Mock()

    def call(self, host='vm.example.org'):
        return sync.sync(self.cache,self.state,host,self.key,self.run)

    def test_changed_upload_once_unchanged_skips_and_transfer_is_atomic(self):
        self.assertIn('uploaded atomically',self.call())
        self.assertEqual(self.run.call_count,2)
        scp, ssh = self.run.call_args_list
        self.assertEqual(scp.args[0][0], 'scp')
        self.assertIn('StrictHostKeyChecking=yes',scp.args[0])
        self.assertIn('sudo -n mv -f',ssh.args[0][-1])
        self.assertEqual(scp.kwargs['timeout'],30)
        self.assertIn('unchanged',self.call())
        self.assertEqual(self.run.call_count,2)
        self.call('other.example.org')
        self.assertEqual(self.run.call_count,4)

    def test_failed_transfer_is_not_marked_or_retried_within_invocation(self):
        self.run.side_effect = subprocess.TimeoutExpired('scp',30)
        with self.assertRaises(subprocess.TimeoutExpired):self.call()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.run.call_count,1)

    def test_invalid_host_or_cache_never_connects(self):
        with self.assertRaises(ValueError):self.call('-oProxyCommand=bad')
        self.cache.write_text('{}')
        with self.assertRaises(ValueError):self.call()
        self.run.assert_not_called()

    def test_agent_runs_at_login_and_hourly_without_launching_browser(self):
        agent=sync.launch_agent()
        self.assertTrue(agent['RunAtLoad'])
        self.assertEqual(agent['StartInterval'],3600)
        self.assertNotIn('browser_reader.py', ' '.join(agent['ProgramArguments']))
        self.assertNotIn('--headless',' '.join(agent['ProgramArguments']))

    def test_failed_remote_install_never_advances_marker(self):
        self.run.side_effect = [None, subprocess.CalledProcessError(1,'ssh')]
        with self.assertRaises(subprocess.CalledProcessError):self.call()
        self.assertFalse(self.state.exists())
        self.assertEqual(self.run.call_count,2)
