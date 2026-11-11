import argparse,sys,uuid
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from runtime.service import Manager
from runtime import engines
p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--example',choices=['selenium','todomvc'],default='todomvc');args=p.parse_args()
m=Manager(args.root);source=engines.ROOT/'examples'/args.example
id=args.example+'-demo-'+uuid.uuid4().hex[:5]
print(m.create_project(id,engines.core.read(source/'requirements.json'),engines.core.read(source/'ui-cases.json')+engines.core.read(source/'api-cases.json')))
print('Imported with execution_authorized=false. Configure allowed origins and scope in the dashboard before executing.');m.close()
