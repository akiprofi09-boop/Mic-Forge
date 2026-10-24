"""Retain installed third-party license files in the standalone package."""
from importlib.metadata import distributions
from pathlib import Path
import shutil
import sys

output = Path(__file__).resolve().parent / 'dist' / 'Mic Forge' / 'licenses'
output.mkdir(parents=True, exist_ok=True)
count = 0
for distribution in distributions():
    name = distribution.metadata['Name']
    for relative in distribution.files or []:
        if not any(word in relative.name.lower() for word in ('license', 'copying', 'copyright', 'notice')):
            continue
        source = Path(distribution.locate_file(relative)).resolve()
        if not source.is_file():
            continue
        safe_parts = [part for part in relative.parts if part not in ('.', '..')]
        target = output / name / Path(*safe_parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        count += 1
python_license = Path(sys.base_prefix) / 'LICENSE.txt'
if python_license.exists():
    shutil.copy2(python_license, output / 'Python-LICENSE.txt')
for source in (Path(__file__).resolve().parent / 'third_party').glob('*'):
    if source.is_file():
        shutil.copy2(source, output / source.name)
print(f'Collected {count} third-party notices plus the Python license')
