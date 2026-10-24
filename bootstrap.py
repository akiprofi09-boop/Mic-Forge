"""Download official PyPI wheels in resumable chunks and verify published hashes.

Supports unreliable connections. No certificate checks are bypassed.
"""
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

from pip._vendor.packaging.tags import sys_tags
from pip._vendor.packaging.utils import parse_wheel_filename

WHEELS = Path(__file__).resolve().parent / '.wheels'
PACKAGES = {
    'PySide6-Essentials': '6.12.0', 'shiboken6': '6.12.0',
    'numpy': '2.5.4', 'scipy': '1.18.1', 'sounddevice': '0.5.6',
    'cffi': '2.1.1', 'pycparser': '3.12', 'pyrnnoise': '0.4.5',
    'pyinstaller': '6.22.3', 'altgraph': '0.17.5', 'packaging': '26.3',
    'pefile': '2024.8.26', 'pyinstaller-hooks-contrib': '2026.9',
    'pywin32-ctypes': '0.2.3', 'setuptools': '84.2.1',
}
TAGS = list(sys_tags())
TAG_RANK = {tag: i for i, tag in enumerate(TAGS)}


def get_json(url):
    for attempt in range(6):
        try:
            with urllib.request.urlopen(url, timeout=45) as response:
                return json.load(response)
        except Exception:
            if attempt == 5:
                raise
            time.sleep(1)


def download(package, version):
    metadata = get_json(f'https://pypi.org/pypi/{package}/{version}/json')
    compatible = []
    for entry in metadata['urls']:
        if not entry['filename'].endswith('.whl'):
            continue
        tags = parse_wheel_filename(entry['filename'])[3]
        ranks = [TAG_RANK[tag] for tag in tags if tag in TAG_RANK]
        if ranks:
            compatible.append((min(ranks), entry))
    entry = min(compatible, key=lambda item: item[0])[1]
    target = WHEELS / entry['filename']
    expected = entry['digests']['sha256']
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
        print(f'Cached {package}', flush=True)
        return str(target)
    total = entry['size']
    chunk_size = 2*1024*1024
    def part(start):
        end = min(start+chunk_size, total)-1
        path = target.with_suffix(f'.part{start}')
        if path.exists() and path.stat().st_size == end-start+1:
            return path
        for attempt in range(10):
            try:
                req = urllib.request.Request(entry['url'], headers={'Range': f'bytes={start}-{end}'})
                with urllib.request.urlopen(req, timeout=60) as response:
                    data = response.read()
                    if response.status == 200 and len(data) == total:
                        data = data[start:end+1]
                    if len(data) != end-start+1:
                        raise ValueError('Incomplete download chunk')
                    path.write_bytes(data)
                    return path
            except Exception:
                if attempt == 9:
                    raise
                time.sleep(min(5, attempt+1))
    print(f'Downloading {package} ({total/1024**2:.1f} MB)', flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        parts = list(pool.map(part, range(0, total, chunk_size)))
    with target.open('wb') as output:
        for path in parts:
            output.write(path.read_bytes())
    if hashlib.sha256(target.read_bytes()).hexdigest() != expected:
        raise ValueError(f'Hash verification failed for {package}')
    # Only delete our own exact chunk paths after the assembled hash is checked.
    for path in parts:
        path.unlink()
    print(f'Verified {package}', flush=True)
    return str(target)


if __name__ == '__main__':
    WHEELS.mkdir(exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        paths = list(pool.map(lambda item: download(*item), PACKAGES.items()))
    subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', *paths], check=True)
