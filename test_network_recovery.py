import unittest
from unittest.mock import patch,MagicMock
from network_recovery import NetworkRecovery,device_source_address,matching_sources
from dcp8001_collector import Dcp8001TcpClient
from monitoring_service import MonitoringService

ETH=[dict(name='Ethernet',address='10.12.0.10',adapter_cidr='10.12.0.0/24')]
class NetworkRecoveryTests(unittest.TestCase):
 def test_bind_only_unambiguous_direct_adapter(self):
  with patch('network_recovery.active_adapters',return_value=ETH):
   self.assertEqual(device_source_address('10.12.0.140'),'10.12.0.10')
   self.assertIsNone(device_source_address('10.13.0.140'))
   self.assertIsNone(device_source_address('127.0.0.1'))
  with patch('network_recovery.active_adapters',return_value=ETH+[dict(name='VPN',address='10.12.0.20',adapter_cidr='10.12.0.0/24')]):
   self.assertIsNone(device_source_address('10.12.0.140'))
 def test_socket_binds_lan_without_global_network_changes(self):
  with patch('network_recovery.active_adapters',return_value=ETH),patch('dcp8001_collector.socket.create_connection') as connect:
   with Dcp8001TcpClient('10.12.0.140',connect_settle=0):pass
   connect.assert_called_once_with(('10.12.0.140',502),timeout=1.0,source_address=('10.12.0.10',0))
 def test_network_change_reprobe_is_bounded_and_not_a_fake_recovery(self):
  watch=NetworkRecovery()
  with patch('network_recovery.active_adapters',return_value=ETH):
   self.assertFalse(watch.check(0,100))
   self.assertFalse(watch.check(5,105))
   self.assertEqual(watch.state(['10.12.0.140'],1),'reconnecting')
  with patch('network_recovery.active_adapters',return_value=[]):
   self.assertTrue(watch.check(10,110))
   self.assertEqual(watch.state(['10.12.0.140'],1),'lan_unavailable')
  with patch('network_recovery.active_adapters',return_value=ETH):
   self.assertFalse(watch.check(15,115))
   self.assertEqual(watch.state(['10.12.0.140'],1),'reconnecting')
   self.assertEqual(watch.state(['10.12.0.140'],0),'healthy')
   self.assertEqual(watch.state(['10.13.0.140'],1),'no_direct_lan')
   self.assertTrue(watch.check(75,175))
   self.assertFalse(watch.check(80,180))
 def test_adapter_change_only_advances_failed_idle_devices(self):
  import threading
  monitor=MonitoringService.__new__(MonitoringService)
  monitor._lock=threading.RLock();monitor._network_recovery=MagicMock();monitor._network_recovery.check.return_value=True
  monitor._device_failures={'bad':2,'busy':2};monitor._next_poll_at={'bad':200,'busy':200,'good':200};monitor._log_diagnostic=MagicMock()
  configured=[({},dict(id=key)) for key in ['bad','busy','good']]
  with patch('monitoring_service.time.monotonic',return_value=100):monitor._watch_network(configured,{'busy'})
  self.assertEqual(monitor._next_poll_at,{'bad':100,'busy':200,'good':200})
  self.assertEqual(monitor._device_failures,{'bad':2,'busy':2})
 def test_unknown_inventory_and_stale_success_do_not_report_healthy(self):
  import threading
  from types import SimpleNamespace
  monitor=MonitoringService.__new__(MonitoringService)
  monitor._lock=threading.RLock();monitor._network_recovery=NetworkRecovery()
  monitor._network_recovery.adapters=ETH
  monitor._device_failures={};monitor._next_poll_at={}
  monitor.options=SimpleNamespace(demo=False,poll_seconds=10)
  monitor.latest={}
  devices=[dict(id='a',host='10.12.0.140')]
  self.assertEqual(monitor.network_recovery_status(devices)['network_state'],'unknown')
  monitor.latest={'a':dict(source='device',online=True,connection_checked_at=100)}
  with patch('monitoring_service.time.time',return_value=200):
   self.assertEqual(monitor.network_recovery_status(devices)['network_state'],'unknown')
  with patch('monitoring_service.time.time',return_value=110):
   self.assertEqual(monitor.network_recovery_status(devices)['network_state'],'healthy')
  with patch('network_recovery.active_adapters',return_value=None):
   self.assertFalse(monitor._network_recovery.check(100,200))
   self.assertEqual(monitor._network_recovery.state(['10.12.0.140'],1),'unknown')
if __name__=='__main__':unittest.main()
