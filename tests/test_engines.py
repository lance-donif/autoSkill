import tempfile,unittest,sys,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime import engines
class EngineTests(unittest.TestCase):
 def test_unknown_origin_denied(self):
  with self.assertRaises(ValueError):engines.safe_origin('https://other.invalid', ['https://qa.invalid'])
 def test_script_mode_does_not_invoke_ai(self):
  with patch.object(engines,'prepare_run',return_value=([],{})),patch.object(engines.core,'run_api',return_value=[]),patch.object(engines.core,'export'),patch.object(engines.core,'report'),patch.object(engines,'run_ai') as ai:
   engines.execute(Path('p'),Path('o'),'api','script');ai.assert_not_called()
 def test_ai_requires_complete_results(self):
  with tempfile.TemporaryDirectory() as tmp:
   p=Path(tmp)
   with patch.object(engines.subprocess,'run') as run,patch.object(engines.core,'require_valid'),patch.object(engines.core,'read',return_value=[]),patch.object(engines.core,'load_cases',return_value=[{'id':'a','kind':'api'}]):
    run.return_value.returncode=0
    with self.assertRaisesRegex(ValueError,'全部用例'):engines.run_ai(p,'api',{})
if __name__=='__main__':unittest.main()
