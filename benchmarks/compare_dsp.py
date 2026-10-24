"""Compare stateful processing against an explicitly supplied earlier DSP file."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import numpy as np

parser = argparse.ArgumentParser()
parser.add_argument('--reference', type=Path, required=True)
parser.add_argument('--candidate', type=Path, default=Path(__file__).resolve().parents[1]/'dsp.py')
parser.add_argument('--output', type=Path, required=True)
args = parser.parse_args()

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module

reference = load('reference_dsp', args.reference)
candidate = load('candidate_dsp', args.candidate)
old, new = reference.VoiceProcessor(), candidate.VoiceProcessor()
rng = np.random.default_rng(47)
phases = [
    dict(enhance=40, noise=70, compression=35),
    dict(enhance=100, noise=0, warmth=6, clarity=6, compression=100, gain_db=12),
    dict(enhance=0, noise=0, warmth=-6, clarity=-6, compression=0, gain_db=-12),
    dict(enhance=70, noise=100, warmth=4, clarity=3, compression=50),
    dict(bypass=True),
    dict(enhance=100, noise=91, warmth=5, clarity=-2, compression=40, gain_db=-4),
]
maximum_error = 0.0
squared_error = 0.0
identical = True
frames = 0
try:
    for phase_index, settings in enumerate(phases):
        old.settings = reference.Settings(**settings)
        new.settings = candidate.Settings(**settings)
        for index in range(100):
            t = (np.arange(reference.FRAME)+frames*reference.FRAME)/reference.RATE
            amplitude = (.001,.03,.2,.95)[index % 4]
            signal = (amplitude*np.sin(2*np.pi*137*t)+rng.normal(0,.015,len(t))).astype(np.float32)
            if index < 4:
                signal[:] = 0
            if index == 50:
                signal[:3] = [np.nan,np.inf,-np.inf]
            untouched = signal.copy()
            expected, actual = old.process(signal), new.process(signal)
            np.testing.assert_array_equal(signal, untouched)
            assert np.isfinite(actual).all()
            np.testing.assert_allclose(actual, expected, rtol=1e-6, atol=1e-7)
            error = actual.astype(np.float64)-expected
            maximum_error = max(maximum_error,float(np.max(np.abs(error))))
            squared_error += float(np.dot(error,error))
            identical = identical and np.array_equal(actual,expected)
            frames += 1
    result = {'passed':True,'frames':frames,'samples':frames*reference.FRAME,
              'setting_transitions':len(phases)-1,'bit_identical':bool(identical),
              'max_sample_error':maximum_error,
              'rms_sample_error':float(np.sqrt(squared_error/(frames*reference.FRAME))),
              'reference_sha256':hashlib.sha256(args.reference.read_bytes()).hexdigest(),
              'candidate_sha256':hashlib.sha256(args.candidate.read_bytes()).hexdigest(),
              'opened_audio_streams':False}
    args.output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result))
finally:
    old.close()
    new.close()
