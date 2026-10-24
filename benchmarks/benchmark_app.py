"""Paced synthetic audio benchmark. Never opens capture or playback streams."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

parser = argparse.ArgumentParser()
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--output', type=Path, required=True)
parser.add_argument('--label', required=True)
parser.add_argument('--mode', choices=('visible', 'tray'), default='tray')
parser.add_argument('--seconds', type=float, default=8)
args = parser.parse_args()
sys.path.insert(0, str(args.source.resolve()))

# Import the application's entry point first, including its runtime settings.
import main
import numpy as np
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication
from dsp import FRAME, RATE, VoiceProcessor


class MemoryCounters(ctypes.Structure):
    _fields_ = [('cb', ctypes.c_ulong), ('PageFaultCount', ctypes.c_ulong)] + [
        (name, ctypes.c_size_t) for name in (
            'PeakWorkingSetSize', 'WorkingSetSize', 'QuotaPeakPagedPoolUsage',
            'QuotaPagedPoolUsage', 'QuotaPeakNonPagedPoolUsage',
            'QuotaNonPagedPoolUsage', 'PagefileUsage', 'PeakPagefileUsage', 'PrivateUsage')]


def memory():
    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi = ctypes.WinDLL('psapi', use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {'working_set_mib': counters.WorkingSetSize/2**20,
            'private_commit_mib': counters.PrivateUsage/2**20}


with tempfile.TemporaryDirectory(prefix='micforge-benchmark-') as temp:
    settings = QSettings(str(Path(temp)/'settings.ini'), QSettings.Format.IniFormat)
    for key, value in dict(enhance=100, noise=91, warmth=5, clarity=-2,
                           compression=40, gain_db=-4).items():
        settings.setValue(key, value)
    main.QSettings = lambda *unused: settings
    app = QApplication([])
    app.setStyle('Fusion')
    app.setStyleSheet(main.STYLE)
    window = main.Window()
    window.show()
    engine = window.engine
    engine.processor = VoiceProcessor()
    engine.running = True
    engine.cable_device = object()
    original_process = engine.processor.process
    durations = []

    def measured_process(samples):
        started = time.perf_counter()
        result = original_process(samples)
        durations.append((time.perf_counter()-started)*1000)
        return result

    engine.processor.process = measured_process
    engine.thread = threading.Thread(target=engine._process, daemon=True)
    engine.thread.start()
    rng = np.random.default_rng(42)
    t = np.arange(FRAME*32)/RATE
    samples = (0.12*np.sin(2*np.pi*140*t)+0.03*np.sin(2*np.pi*280*t)
               +rng.normal(0, .008, len(t))).astype(np.float32).reshape(32, FRAME, 1)
    stopped = threading.Event()

    def feed():
        index = 0
        deadline = time.perf_counter()
        while not stopped.is_set():
            engine.cable_buffer.take(FRAME)
            engine._capture(samples[index % len(samples)], FRAME, None, False)
            index += 1
            deadline += FRAME/RATE
            stopped.wait(max(0, deadline-time.perf_counter()))

    feeder = threading.Thread(target=feed, daemon=True)
    feeder.start()
    if args.mode == 'tray':
        window.showMinimized()
    measurement = {}

    def begin():
        durations.clear()
        measurement.update(wall=time.perf_counter(), cpu=time.process_time())

    def finish():
        wall = time.perf_counter()-measurement['wall']
        cpu = time.process_time()-measurement['cpu']
        result = {'label':args.label, 'mode':args.mode, 'seconds':wall,
                  'cpu_one_core_pct':100*cpu/wall,
                  'cpu_machine_pct':100*cpu/wall/(os.cpu_count() or 1),
                  'dsp_p50_ms':float(np.percentile(durations,50)),
                  'dsp_p95_ms':float(np.percentile(durations,95)),
                  'processed_frames':len(durations), 'capture_queue_drops':engine.overflows,
                  'memory':memory(), 'opened_audio_streams':False,
                  'logical_processors':os.cpu_count()}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
        print(json.dumps(result), flush=True)
        stopped.set()
        feeder.join(timeout=2)
        window.close()
        app.quit()

    QTimer.singleShot(2000, begin)
    QTimer.singleShot(round((args.seconds+2)*1000), finish)
    app.exec()
