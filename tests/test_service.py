import unittest,tempfile,datetime,sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.service import Manager,next_due,UTC,create_app
from fastapi.testclient import TestClient
class ServiceTests(unittest.TestCase):
 def test_daily_uses_explicit_timezone(self):
  now=datetime.datetime(2026,11,11,0,0,tzinfo=UTC)
  due=next_due({'kind':'daily','time':'09:00','timezone':'Asia/Shanghai'},now)
  self.assertEqual(due.hour,1)
 def test_interval_coalesces_missed_runs(self):
  base=datetime.datetime(2026,11,11,tzinfo=UTC)
  self.assertEqual(next_due({'kind':'interval','seconds':60},base+datetime.timedelta(seconds=185),base),base+datetime.timedelta(seconds=240))
 def test_invalid_frequency_rejected(self):
  for value in [0,59,True]:
   with self.assertRaises(ValueError):next_due({'kind':'interval','seconds':value},datetime.datetime.now(UTC))
 def test_stale_running_jobs_become_blocked(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=Manager(tmp);m.put_run({'id':'run-'+'a'*32,'status':'Running'});m.close()
   n=Manager(tmp);self.assertEqual(n.runs()[0]['status'],'Blocked');n.close()
 def test_api_blocks_cross_origin_write_and_rebinding_host(self):
  with tempfile.TemporaryDirectory() as tmp:
   app=create_app(tmp,start_worker=False)
   with TestClient(app,base_url='http://127.0.0.1:8787') as client:
    self.assertEqual(client.get('/api/status').status_code,200)
    self.assertEqual(client.post('/api/runs',json={},headers={'Origin':'https://evil.invalid'}).status_code,403)
    self.assertEqual(client.get('/api/status',headers={'Host':'evil.invalid'}).status_code,403)
 def test_one_shot_is_claimed_once_and_survives_restart(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=Manager(tmp);now=datetime.datetime.now(UTC)
   with patch.object(m,'request_ok'):
    t=m.create_task({'title':'one','project_id':'demo','engine':'script','kind':'ui','rule':{'kind':'at','at':(now+datetime.timedelta(seconds=10)).isoformat()}})
   with patch.object(m,'request_ok'),patch.object(m.pool,'submit') as queue:
    self.assertEqual(m.tick(now+datetime.timedelta(seconds=11)),1)
    self.assertEqual(m.tick(now+datetime.timedelta(seconds=12)),0);queue.assert_called_once()
    self.assertEqual(m.runs()[0]['status'],'Queued')
   m.close();n=Manager(tmp);self.assertFalse(n.tasks()[0]['enabled']);n.close()
 def test_schedule_is_persisted_and_paused(self):
  with tempfile.TemporaryDirectory() as tmp:
   m=Manager(tmp)
   with patch.object(m,'request_ok'):
    t=m.create_task({'title':'demo','project_id':'demo','engine':'script','kind':'api','rule':{'kind':'interval','seconds':60}})
   self.assertEqual(m.tasks()[0]['engine'],'script');m.toggle_task(t['id'],False);self.assertFalse(m.tasks()[0]['enabled']);m.close()
if __name__=='__main__':unittest.main()
