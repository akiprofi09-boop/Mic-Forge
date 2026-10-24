"""Signal and routing regression tests. Does not capture or play live audio."""
import threading
import time
import unittest
from unittest.mock import patch

import numpy as np
from scipy.signal import correlate, correlation_lags, sosfreqz

from audio import AudioEngine, Device, SampleBuffer, TestPlayback, is_cable, is_physical_input, render_preview
from dsp import FRAME, RATE, RNNOISE_DELAY_FRAMES, Settings, VoiceProcessor


class DSPTests(unittest.TestCase):
    def setUp(self):
        self.processor = VoiceProcessor()

    def tearDown(self):
        self.processor.close()

    def feed(self, samples):
        return np.concatenate([self.processor.process(samples[i:i+FRAME]) for i in range(0, len(samples), FRAME)])

    def test_silence_is_finite_and_silent(self):
        y = self.feed(np.zeros(FRAME*20, dtype=np.float32))
        self.assertTrue(np.isfinite(y).all())
        self.assertLess(float(np.max(np.abs(y))), 1e-5)

    def test_bypass_is_exact_delayed_original(self):
        self.processor.settings = Settings(bypass=True)
        source = np.random.default_rng(42).uniform(-0.2, 0.2, FRAME*10).astype(np.float32)
        result = self.feed(source)
        delay = FRAME*RNNOISE_DELAY_FRAMES
        np.testing.assert_array_equal(result[:delay], np.zeros(delay))
        np.testing.assert_array_equal(result[delay:], source[:-delay])

    def test_noise_blend_delay_matches_actual_native_library(self):
        # Speech-like voiced harmonics with a changing pitch and amplitude make
        # algorithmic delay identifiable without recording anyone's microphone.
        t = np.arange(RATE*4)/RATE
        phase = 2*np.pi*(130*t+10*np.sin(2*np.pi*0.7*t))
        envelope = 0.5+0.5*np.sin(2*np.pi*2.3*t)
        source = (0.12*envelope*sum(np.sin(k*phase)/k for k in range(1, 15))).astype(np.float32)
        native = np.concatenate([self.processor.denoiser.process(source[i:i+FRAME])[0]
                                 for i in range(0, len(source), FRAME)])
        correlation = correlate(native, source, method='fft')
        lags = correlation_lags(len(native), len(source))
        region = (lags >= 0) & (lags < 3000)
        measured = int(lags[region][np.argmax(correlation[region])])
        self.assertAlmostEqual(measured, FRAME*RNNOISE_DELAY_FRAMES, delta=8)

    def test_limiter_contains_hot_signal_even_with_maximum_gain(self):
        self.processor.settings = Settings(enhance=100, noise=0, gain_db=12, compression=0, warmth=6, clarity=6)
        t = np.arange(FRAME*30)/RATE
        result = self.feed((0.99*np.sin(2*np.pi*180*t)).astype(np.float32))
        self.assertTrue(np.isfinite(result).all())
        self.assertLessEqual(float(np.max(np.abs(result))), 10**(-1/20)+1e-7)
        self.assertGreater(float(np.max(np.abs(result))), 0.7)

    def test_noise_suppression_attenuates_stationary_noise(self):
        self.processor.settings = Settings(enhance=0, noise=100, compression=0)
        source = np.random.default_rng(4).normal(0, 0.025, FRAME*200).astype(np.float32)
        result = self.feed(source)
        before = np.sqrt(np.mean(source[RATE:]**2))
        after = np.sqrt(np.mean(result[RATE:]**2))
        reduction = 20*np.log10(before/max(after, 1e-12))
        print(f'RNNoise stationary-noise attenuation: {reduction:.1f} dB')
        self.assertGreater(reduction, 6)

    def test_tone_control_changes_low_voice_band(self):
        self.processor.settings = Settings(enhance=0, noise=0, compression=0, warmth=-6)
        source = (0.02*np.sin(2*np.pi*180*np.arange(FRAME*80)/RATE)).astype(np.float32)
        low = self.feed(source)[FRAME*20:]
        self.processor.settings = Settings(enhance=0, noise=0, compression=0, warmth=6)
        high = self.feed(source)[FRAME*20:]
        change = 20*np.log10(np.std(high)/np.std(low))
        self.assertGreater(change, 10)

    def test_voice_enhance_adds_body_and_presence_not_just_volume(self):
        responses = []
        for enhance in (0, 100):
            self.processor.settings = Settings(enhance=enhance, noise=0, compression=0)
            self.processor.process(np.zeros(FRAME, dtype=np.float32))
            _, response = sosfreqz(self.processor.sos, worN=[100, 400, 1000, 3200, 7000], fs=RATE)
            db = 20*np.log10(np.abs(response))
            responses.append(db-db[2])  # Normalize to 1 kHz to exclude volume gain.
        change = responses[1]-responses[0]
        self.assertGreater(change[0], 6, 'Voice Enhance needs a substantial low-voice lift')
        self.assertGreater(change[3], 4, 'Voice Enhance needs clearly stronger speech presence')
        self.assertLess(change[1], -2, 'Extra body must be balanced with a muddy-mid cut')

    def test_compression_reduces_loud_quiet_ratio(self):
        def level(amplitude):
            p = VoiceProcessor()
            try:
                p.settings = Settings(enhance=0, noise=0, compression=100)
                source = (amplitude*np.sin(2*np.pi*1000*np.arange(FRAME*60)/RATE)).astype(np.float32)
                y = np.concatenate([p.process(source[i:i+FRAME]) for i in range(0, len(source), FRAME)])
                return float(np.std(y[FRAME*30:]))
            finally:
                p.close()
        difference = 20*np.log10(level(0.6)/level(0.03))
        self.assertLess(difference, 20*np.log10(0.6/0.03)-5)

    def test_malformed_frame_rejected(self):
        with self.assertRaises(ValueError):
            self.processor.process(np.zeros(100))

    def test_nonfinite_input_does_not_poison_pipeline(self):
        source = np.zeros(FRAME, dtype=np.float32)
        source[:3] = [np.nan, np.inf, -np.inf]
        self.assertTrue(np.isfinite(self.processor.process(source)).all())
        self.assertTrue(np.isfinite(self.processor.process(np.zeros(FRAME))).all())

    def test_processing_budget(self):
        rng = np.random.default_rng(6)
        frames = rng.normal(0, 0.05, (150, FRAME)).astype(np.float32)
        elapsed = []
        for frame in frames:
            start = time.perf_counter()
            self.processor.process(frame)
            elapsed.append((time.perf_counter()-start)*1000)
        p95 = float(np.percentile(elapsed[10:], 95))
        print(f'DSP processing p95: {p95:.2f} ms / 10 ms frame')
        self.assertLess(p95, 10, 'DSP cannot keep up in real time on this machine')


class RoutingTests(unittest.TestCase):
    def test_preview_bypass_preserves_samples_and_partial_frame_length(self):
        original = np.random.default_rng(10).uniform(-0.2, 0.2, 1234).astype(np.float32)
        preview = render_preview(original, Settings(bypass=True))
        np.testing.assert_array_equal(preview, original)

    def test_virtual_and_loopback_inputs_are_rejected(self):
        for name in ['CABLE Output (VB-Audio Virtual Cable)', 'Voicemeeter Output', 'Stereo Mix', 'WASAPI loopback', 'What U Hear']:
            self.assertFalse(is_physical_input(name), name)
        self.assertTrue(is_physical_input('Microphone (MARVO HG9067)'))

    def test_only_cable_is_a_valid_voice_output(self):
        self.assertTrue(is_cable('CABLE Input (VB-Audio Virtual Cable)'))
        self.assertFalse(is_cable('Speakers (MARVO HG9067)'))
        self.assertFalse(is_cable('Voicemeeter Input'))

    def test_engine_revalidates_input_before_opening(self):
        engine = AudioEngine()
        with patch('audio.sd.query_devices', return_value={'name': 'Stereo Mix', 'max_input_channels': 2}):
            with self.assertRaisesRegex(ValueError, 'physical microphone'):
                engine.start(Device(1, 'Microphone', 1, 0))
        self.assertFalse(engine.running)
        self.assertIsNone(engine.processor)

    def test_engine_rejects_physical_live_output(self):
        engine = AudioEngine()
        with patch('audio.sd.query_devices', side_effect=[
            {'name': 'Microphone (MARVO HG9067)', 'max_input_channels': 1},
            {'name': 'Speakers (MARVO HG9067)', 'max_output_channels': 2},
        ]):
            with self.assertRaisesRegex(ValueError, 'VB-Audio cable'):
                engine.start(Device(1, 'Microphone', 1, 0), Device(2, 'Speakers', 0, 2))

    def test_preview_cannot_send_test_to_call(self):
        with patch('audio.sd.query_devices', return_value={'name': 'CABLE Input', 'max_output_channels': 2}):
            with self.assertRaisesRegex(ValueError, 'physical headphones'):
                TestPlayback().play(np.zeros(100), Device(1, 'CABLE Input', 0, 2))

    def test_buffer_preserves_partial_reads_and_zero_fills(self):
        buffer = SampleBuffer()
        buffer.put(np.arange(9, dtype=np.float32))
        np.testing.assert_array_equal(buffer.take(4), np.arange(4))
        np.testing.assert_array_equal(buffer.take(7), [4, 5, 6, 7, 8, 0, 0])
        self.assertEqual(buffer.available, 0)
        self.assertEqual(buffer.underruns, 1)

    def test_buffer_bounds_latency_and_keeps_newest(self):
        buffer = SampleBuffer()
        for index in range(30):
            buffer.put(np.full(FRAME, index, dtype=np.float32))
        self.assertEqual(buffer.available, FRAME*12)
        self.assertEqual(buffer.drops, 18)
        self.assertEqual(buffer.take(1)[0], 18)

    def test_stop_is_safe_before_start_and_preserves_completed_test(self):
        engine = AudioEngine()
        engine.last_test = (np.zeros(100), np.zeros(100))
        engine.stop()
        self.assertIsNotNone(engine.last_test)
        self.assertFalse(engine.monitor_enabled)

    def test_recording_is_only_explicit_and_bounded(self):
        engine = AudioEngine()
        self.assertFalse(engine.recording)
        with self.assertRaises(ValueError):
            engine.start_recording()
        engine.running = True
        engine.start_recording()
        self.assertTrue(engine.recording)
        engine.recorded_raw.append(np.zeros(FRAME))
        engine.recorded_wet.append(np.ones(FRAME))
        engine.finish_recording()
        self.assertFalse(engine.recording)
        self.assertEqual(len(engine.last_test[0]), FRAME)
        self.assertEqual(float(engine.last_test[1][0]), 1)
        engine.stop()

    def test_capture_start_failure_releases_processor_and_worker(self):
        engine = AudioEngine()
        with patch('audio.sd.query_devices', return_value={'name': 'Microphone (MARVO HG9067)', 'max_input_channels': 2}), \
             patch('audio.sd.check_input_settings'), \
             patch('audio.sd.InputStream', side_effect=RuntimeError('Device busy')):
            with self.assertRaisesRegex(RuntimeError, 'Device busy'):
                engine.start(Device(20, 'Microphone (MARVO HG9067)', 2, 0))
        self.assertFalse(engine.running)
        self.assertIsNone(engine.processor)
        self.assertIsNone(engine.thread)

    def test_worker_automatically_finishes_twelve_second_recording(self):
        class Passthrough:
            settings = Settings()
            limiting = False
            def process(self, samples):
                return samples.copy()
            def close(self):
                pass
        engine = AudioEngine()
        engine.processor = Passthrough()
        engine.running = True
        engine.start_recording()
        engine.thread = threading.Thread(target=engine._process, daemon=True)
        engine.thread.start()
        try:
            for _ in range(1200):
                engine.input_queue.put(np.zeros(FRAME, dtype=np.float32), timeout=2)
            deadline = time.monotonic()+3
            while engine.recording and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(engine.recording)
            self.assertEqual(len(engine.last_test[0]), RATE*12)
            self.assertEqual(len(engine.last_test[1]), RATE*12)
            self.assertFalse(engine.recorded_raw)
            self.assertFalse(engine.error)
        finally:
            engine.stop()


if __name__ == '__main__':
    unittest.main(verbosity=2)
