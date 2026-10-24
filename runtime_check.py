"""Packaged dependency and signal smoke check; never opens an audio stream."""
import json
import time
from pathlib import Path

import numpy as np
from scipy.signal import sosfreqz

from audio import devices, is_physical_input, render_preview
from dsp import FRAME, RATE, RNNOISE_DELAY_FRAMES, Settings, VoiceProcessor


def check(report_path):
    processor = VoiceProcessor()
    try:
        t = np.arange(FRAME*100)/RATE
        source = (0.18*np.sin(2*np.pi*200*t)).astype(np.float32)
        durations = []
        output = []
        for i in range(0, len(source), FRAME):
            start = time.perf_counter()
            output.append(processor.process(source[i:i+FRAME]))
            durations.append((time.perf_counter()-start)*1000)
        result = np.concatenate(output)
        assert np.isfinite(result).all()
        assert np.max(np.abs(result)) <= 10**(-1/20)+1e-7
        assert np.max(np.abs(result)) > 1e-4
        processor.settings = Settings(bypass=True)
        for i in range(RNNOISE_DELAY_FRAMES):
            processor.process(source[i*FRAME:(i+1)*FRAME])
        np.testing.assert_array_equal(processor.process(source[2*FRAME:3*FRAME]), source[:FRAME])
        microphones, cables, headphones = devices()
        assert all(is_physical_input(d.name) for d in microphones)
        reference = (0.02*np.sin(2*np.pi*1000*np.arange(4800)/RATE)).astype(np.float32)
        quiet = render_preview(reference, Settings(enhance=0, noise=0, compression=0, gain_db=0))
        louder = render_preview(reference, Settings(enhance=0, noise=0, compression=0, gain_db=12))
        assert np.std(louder) > np.std(quiet)*2
        contours = []
        for enhance in (0, 100):
            processor.settings = Settings(enhance=enhance, noise=0, compression=0)
            processor.process(np.zeros(FRAME, dtype=np.float32))
            _, response = sosfreqz(processor.sos, worN=[100, 400, 1000, 3200], fs=RATE)
            response_db = 20*np.log10(np.abs(response))
            contours.append(response_db-response_db[2])
        change = contours[1]-contours[0]
        assert change[0] > 6 and change[3] > 4 and change[1] < -2
        report = {
            'passed': True,
            'finite_audio': True,
            'bypass_matches_original': True,
            'output_peak': float(np.max(np.abs(result))),
            'dsp_p95_ms': float(np.percentile(durations[10:], 95)),
            'microphones': [d.name for d in microphones],
            'cables': [d.name for d in cables],
            'headphone_outputs': [d.name for d in headphones],
            'opened_microphone': False,
            'recorded_audio': False,
            'preview_reprocesses_current_settings': True,
            'voice_enhance_body_change_db': float(change[0]),
            'voice_enhance_presence_change_db': float(change[3]),
        }
        Path(report_path).write_text(json.dumps(report, indent=2), encoding='utf-8')
    finally:
        processor.close()
