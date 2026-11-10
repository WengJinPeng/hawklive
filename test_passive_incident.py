import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
import passive_incident as p


class PassiveTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
    def tearDown(self):self.temp.cleanup()
    def policy(self):
        (self.root/'passive-incident-policy.json').write_text(json.dumps(dict(enabled=True,scope=dict(customer_id='qa',site_id='site'),expires_at=time.time()+60)))
    def test_default_off_wrong_scope_and_unregistered_devices_do_not_start_queries(self):
        snap=Mock();w=p.PassiveIncident(self.root,snap)
        self.assertIsNone(w.thread)
        self.policy()
        with patch.object(p.threading.Thread,'start'):
            w=p.PassiveIncident(self.root,snap)
        w.configure([({'customer_id':'qa'},{'id':'1','site_id':'site','host':'10.0.0.1'})])
        for customer,site,host in [('other','site','10.0.0.1'),('qa','other','10.0.0.1'),('qa','site','10.0.0.2')]:
            w.submit(customer,site,{'id':'1','host':host},{'failure_count':3})
        self.assertTrue(w.jobs.empty());snap.assert_not_called()
    def test_failure_trigger_is_async_and_excludes_unapproved_fields(self):
        self.policy();snap=Mock(return_value={'state':'observed','endpoints':[]})
        w=p.PassiveIncident(self.root,snap)
        try:
            w.configure([({'customer_id':'qa'},{'id':'1','site_id':'site','host':'10.0.0.1'})])
            w.submit('qa','site',{'id':'1','host':'10.0.0.1'},{'failure_count':3,'token':'secret'})
            deadline=time.monotonic()+2
            while not snap.called and time.monotonic()<deadline:time.sleep(.02)
            self.assertEqual(snap.call_args.args[0],['10.0.0.1'])
        finally:w.close()
        text=(self.root/'passive-incidents'/'incident-0.jsonl').read_text()
        self.assertNotIn('secret',text)
        self.assertIn('wall_time',text);self.assertIn('monotonic',text)
    def test_real_helper_exits_without_probe_on_non_windows(self):
        if os.name=='nt':self.skipTest('Native Windows cache check is covered separately')
        result=p.bounded_snapshot(['10.0.0.1'])
        self.assertEqual(result['state'],'unsupported_platform')
    def test_timeout_reaps_exact_owned_process(self):
        import subprocess,sys
        original=subprocess.Popen;children=[]
        def spawn(*args,**kwargs):
            child=original([sys.executable,'-c','import time;time.sleep(60)'],**kwargs)
            children.append(child);return child
        with patch.object(p.subprocess,'Popen',side_effect=spawn),patch.object(p,'BUDGET_SECONDS',.1):
            with self.assertRaises(TimeoutError):p.bounded_snapshot(['10.0.0.1'])
        self.assertIsNotNone(children[0].poll())
    def test_files_rotate_under_total_quota_and_expired_files_are_removed(self):
        w=p.PassiveIncident(self.root);w.root.mkdir()
        for i in range(5):(w.root/f'incident-{i}.jsonl').write_bytes(b'x'*(2*1024*1024))
        stale=w.root/'incident-old.jsonl';stale.write_text('old');os.utime(stale,(0,0))
        w._write({'event':'new'})
        self.assertFalse(stale.exists())
        self.assertLessEqual(sum(f.stat().st_size for f in w.root.glob('incident-*.jsonl')),10*1024*1024)
    def test_cache_code_contains_no_active_resolver_or_socket_calls(self):
        import ast,inspect
        tree=ast.parse(inspect.getsource(p.cached_network))
        called={n.func.attr for n in ast.walk(tree) if isinstance(n,ast.Call) and isinstance(n.func,ast.Attribute)}
        self.assertFalse(called & {'ResolveIpNetEntry2','SendARP','connect','send','sendto'})
        self.assertTrue({'GetBestRoute','GetIpNetEntry2','GetIfEntry'}<=called)

    def test_three_budget_failures_disable_only_optional_worker(self):
        self.policy();clock=[0.0]
        snap=Mock(side_effect=TimeoutError('fixture native stall'))
        with patch.object(p.threading.Thread,'start'):
            w=p.PassiveIncident(self.root,snap)
        w.configure([({'customer_id':'qa'},{'id':'1','site_id':'site','host':'10.0.0.1'})])
        def event(timeout):
            clock[0]+=61
            return {'device_id':'1','host':'10.0.0.1','failure_count':3,'monotonic':clock[0]}
        w.jobs=Mock();w.jobs.get.side_effect=event
        with patch.object(p.time,'monotonic',side_effect=lambda:clock[0]):w._run()
        self.assertTrue(w.closed.is_set());self.assertEqual(snap.call_count,3)
        self.assertEqual(w.registered,(('1','10.0.0.1'),))

    def test_daily_window_budget_survives_restart_and_does_not_exceed_six(self):
        first=p.PassiveIncident(self.root)
        for _ in range(6):self.assertTrue(first._reserve_window())
        self.assertFalse(p.PassiveIncident(self.root)._reserve_window())

    def test_helper_entrypoint_runs_before_normal_launcher_or_single_instance(self):
        import windows_launcher
        with patch.object(p,'helper_main',return_value=0) as helper,patch.object(windows_launcher,'parse_args') as parse:
            self.assertEqual(windows_launcher.main(['--passive-cache-helper','10.0.0.1']),0)
        helper.assert_called_once_with(['10.0.0.1']);parse.assert_not_called()
