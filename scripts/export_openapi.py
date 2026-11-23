"""Run with the backend environment to export its actual, implemented routes."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "backend"))
from app.main import app  # noqa: E402

target = root / "artifacts/runtime-openapi.json"
target.parent.mkdir(exist_ok=True)
document = app.openapi()
# Named response aliases are part of the existing TypeScript public contract,
# although FastAPI itself emits only the underlying envelope names.
declared = root / 'contracts/openapi.p2.json'
if declared.exists():
    for name, schema in json.loads(declared.read_text(encoding='utf-8'))['components']['schemas'].items():
        if set(schema) == {'$ref'} and name not in document['components']['schemas']:
            document['components']['schemas'][name] = schema
target.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(target)
