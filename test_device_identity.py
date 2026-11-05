import ctypes
import json
import os
import threading
import time
import unittest
from unittest.mock import patch
from device_identity import IdentityVerifier,IdentityError,normalize_mac,NeighborRow,resolve_windows_neighbor
import test_device_recovery as recovery

A='02:00:00:00:00:02'
B='02:00:00:00:00:03'
class IdentityRules(unittest.TestCase):
    def test_normalization_and_invalid_bindings(self):
        self.assertEqual(normalize_mac('02-00-00-00-00-02'),A)
        for value in ('',None,'00:00:00:00:00:00','ff:ff:ff:ff:ff:ff','01:00:5e:00:00:01','x'):
            with self.assertRaises(ValueError):normalize_mac(value)
    def test_never_enroll_on_first_observation(self):
        observation=dict(mac=A,host='10.12.0.115',interface=14)
        with self.assertRaises(IdentityError):IdentityVerifier.check(None,observation,observation)
    def test_same_ip_other_device_and_mid_read_change(self):
        before=dict(mac=A,host='10.12.0.115',interface=14)
        after=dict(before,mac=B)
        with self.assertRaises(IdentityError):IdentityVerifier.check(A,after,after)
        with self.assertRaises(IdentityError):IdentityVerifier.check(A,before,after)
        with self.assertRaises(IdentityError):IdentityVerifier.check(A,before,dict(before,interface=2))
        IdentityVerifier.check(A,before,before)
    def test_timeout_bounded_and_late_result_never_reused(self):
        release=threading.Event();self.addCleanup(release.set)
        def blocked(host):release.wait(3);return dict(mac=A,host=host,interface=14)
        verifier=IdentityVerifier(blocked,timeout=.02,capacity=1)
        with self.assertRaises(IdentityError):verifier.observe('10.12.0.115')
        with self.assertRaisesRegex(IdentityError,'busy'):verifier.observe('10.12.0.115')
        release.set()
    def test_windows_structure_layout(self):
        self.assertEqual(ctypes.sizeof(NeighborRow),88)
        self.assertEqual(NeighborRow.physical.offset,40)
        self.assertEqual(NeighborRow.length.offset,72)
        self.assertEqual(NeighborRow.reachability.offset,84)
    @unittest.skipUnless(os.name=='nt','Windows API ABI check')
    def test_windows_native_api_rejects_non_neighbor(self):
        with self.assertRaises(IdentityError):resolve_windows_neighbor('127.0.0.1')
        # Exercise the native route/neighbor API against the documentation-only
        # network; a routed destination must fail closed, not return gateway MAC.
        with self.assertRaises(IdentityError):resolve_windows_neighbor('192.0.2.1')

class GuardedSampling(unittest.TestCase):
    setUp=recovery.DeviceRecoveryTests.setUp
    sample=recovery.DeviceRecoveryTests.sample
    rows=recovery.DeviceRecoveryTests.rows
    def enable(self,mac=A):
        with self.service.connect() as db:
            db.execute('UPDATE devices SET identity_required=1,expected_mac=?,site_id=? WHERE id=?',(mac,'site-test',self.device['id']))
        self.room=self.service.configuration()[0];self.device=self.room['devices'][0]
    def observe(self,mac=A):return dict(mac=mac,host=self.device['host'],interface=14)
    def isolated(self):
        with self.service.connect() as db:return [dict(r) for r in db.execute('SELECT * FROM identity_unverified_samples')]
    def poll(self,client_type,before,after):
        client_type.return_value.read_realtime.return_value=self.sample(time.time())
        with patch.object(self.service._identity_verifier,'observe',side_effect=[before,after]):
            self.service._poll_device(self.room,self.device)
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_matching_binding_retains_readings_and_recovery(self,client_type):
        self.enable();self.poll(client_type,self.observe(),self.observe())
        self.assertEqual(len(self.rows()),1);self.assertFalse(self.isolated())
        self.assertTrue(self.service.latest[self.device['id']]['online'])
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_other_mac_at_same_ip_is_retained_without_history_latest_or_alarm(self,client_type):
        self.enable()
        with patch.object(self.service,'_evaluate_alarms') as alarm:
            self.poll(client_type,self.observe(B),self.observe(B));alarm.assert_not_called()
        self.assertFalse(self.rows());self.assertFalse(self.service.latest[self.device['id']]['online']);self.assertNotIn('particles',self.service.latest[self.device['id']])
        self.assertEqual(len(self.isolated()),1)
        self.assertEqual(json.loads(self.isolated()[0]['observed_before'])['mac'],B)
        self.assertTrue(any(row[1]=='device_identity_blocked' for row in self.logs))
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_change_during_read_or_unavailable_postcheck_never_publishes(self,client_type):
        self.enable();self.poll(client_type,self.observe(),IdentityError('unavailable'))
        self.assertFalse(self.rows());self.assertEqual(len(self.isolated()),1)
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_unbound_raw_sample_does_not_establish_identity(self,client_type):
        self.enable(None);self.poll(client_type,self.observe(),self.observe())
        self.assertFalse(self.rows());self.assertEqual(len(self.isolated()),1)
        self.assertIsNone(self.service.configuration()[0]['devices'][0]['expected_mac'])
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_good_history_unchanged_after_swap_and_restore(self,client_type):
        self.enable();self.poll(client_type,self.observe(),self.observe());original=self.rows()[0]
        self.poll(client_type,self.observe(B),self.observe(B))
        self.assertEqual(self.rows(),[original])
        self.assertFalse(self.service.latest[self.device['id']]['online']);self.assertNotIn('particles',self.service.latest[self.device['id']])
        self.service._next_poll_at.clear()
        self.poll(client_type,self.observe(),self.observe())
        self.assertEqual(len(self.rows()),2);self.assertEqual(self.rows()[0],original)
        self.assertEqual(len(self.isolated()),1)
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_ip_change_with_same_bound_identity_recovers(self,client_type):
        self.enable()
        with self.service.connect() as db:db.execute('UPDATE devices SET host=? WHERE id=?',('192.168.2.31',self.device['id']))
        self.room=self.service.configuration()[0];self.device=self.room['devices'][0]
        self.poll(client_type,self.observe(),self.observe());self.assertEqual(len(self.rows()),1)
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_retention_failure_cannot_fall_through_to_normal_storage(self,client_type):
        self.enable()
        with patch.object(self.service,'_retain_identity_sample',side_effect=OSError('disk full')):
            self.poll(client_type,self.observe(B),self.observe(B))
        self.assertFalse(self.rows());self.assertFalse(self.service.latest[self.device['id']]['online']);self.assertNotIn('particles',self.service.latest[self.device['id']])
    @patch('monitoring_service.Dcp8001TcpClient')
    def test_cloud_enables_guard_during_legacy_read_blocks_publication(self,client_type):
        sample=self.sample(time.time())
        def read():
            with self.service.connect() as db:
                db.execute('UPDATE devices SET identity_required=1,expected_mac=? WHERE id=?',(A,self.device['id']))
            return sample
        client_type.return_value.read_realtime.side_effect=read
        self.service._poll_device(self.room,self.device)
        self.assertFalse(self.rows());self.assertEqual(len(self.isolated()),1)
        self.assertFalse(self.service.latest[self.device['id']]['online'])
    def test_cloud_omission_cannot_remove_binding_and_explicit_rebind_is_scoped(self):
        self.enable()
        payload=dict(customer_id=self.room['customer_id'],site_id='site-test',rooms=[self.room])
        payload['rooms'][0]['devices'][0].pop('identity_required')
        payload['rooms'][0]['devices'][0].pop('expected_mac')
        self.service.apply_edge_configuration(payload)
        device=self.service.configuration()[0]['devices'][0]
        self.assertTrue(device['identity_required']);self.assertEqual(device['expected_mac'],A)
        payload['rooms'][0]['devices'][0].update(identity_required=True,expected_mac=B)
        self.service.apply_edge_configuration(payload)
        self.assertEqual(self.service.configuration()[0]['devices'][0]['expected_mac'],B)
