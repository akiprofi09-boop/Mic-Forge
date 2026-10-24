"""UI state checks with no microphone capture or playback."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import unittest
import time
import tempfile
from concurrent.futures import Future
from dataclasses import replace
from unittest.mock import patch

import numpy as np
from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QScrollArea
from PySide6.QtCore import QEvent, QPointF, Qt, QSettings
from PySide6.QtGui import QMouseEvent
from PySide6.QtTest import QTest

from dsp import Settings
from audio import Device
from main import Window, STYLE


class UIStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.app.setStyle('Fusion')
        cls.app.setStyleSheet(STYLE)

    def setUp(self):
        self.settings_dir = tempfile.TemporaryDirectory()
        self.settings = QSettings(self.settings_dir.name + '/test.ini', QSettings.Format.IniFormat)
        with patch('main.QSettings', return_value=self.settings):
            self.window = Window()
        self.window.show_error = lambda *args: self.fail(str(args))

    def tearDown(self):
        self.window.close()
        self.settings_dir.cleanup()

    def test_compact_window_fits_all_controls_without_scrolling(self):
        self.window.show()
        self.app.processEvents()
        scroll = self.window.findChild(QScrollArea)
        self.assertLessEqual(self.window.width(), 800)
        self.assertLessEqual(self.window.height(), 780)
        self.assertEqual(scroll.verticalScrollBar().maximum(), 0)
        self.assertEqual(set(self.window.sliders), {'enhance', 'noise', 'compression', 'gain_db', 'warmth', 'clarity'})

    def test_tray_skips_paint_updates_but_handles_audio_errors(self):
        window = self.window
        window._in_tray = True
        with patch.object(window.input_meter, 'set_peak') as meter:
            window.tick()
            meter.assert_not_called()
        window.engine.processor = object()
        window.engine.error = 'Disconnected test endpoint'
        # Keep the synthetic sentinel away from native cleanup.
        with patch.object(window, 'stop_audio') as stop, patch.object(window, 'show_error') as error:
            window.tick()
            stop.assert_called_once()
            error.assert_called_once()
        window.engine.processor = None
        window.engine.error = None
        window.restore_from_tray()
        self.assertFalse(window._in_tray)

    def test_startup_has_no_capture_recording_or_monitoring(self):
        window = self.window
        self.assertFalse(window.engine.running)
        self.assertIsNone(window.engine.processor)
        self.assertFalse(window.engine.recording)
        self.assertFalse(window.monitor.isChecked())
        self.assertFalse(window.record_button.isEnabled())
        self.assertFalse(window.original_button.isEnabled())
        self.assertFalse(window.processed_button.isEnabled())
        self.assertFalse(window.export_button.isEnabled())

    def test_minimize_keeps_processing_and_tray_open_restores_window(self):
        window = self.window
        window.show()
        window.engine.running = True  # No streams are opened by this test.
        with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=True), \
             patch.object(window.tray, 'isVisible', return_value=True), \
             patch.object(window, 'stop_audio') as stop:
            window.showMinimized()
            self.app.processEvents()
            self.app.processEvents()
            self.assertFalse(window.isVisible())
            self.assertTrue(window.engine.running)
            self.assertTrue(window.timer.isActive())
            stop.assert_not_called()
            window.tray_menu.actions()[0].trigger()
            self.assertTrue(window.isVisible())
            self.assertFalse(window.isMinimized())
        with patch.object(window, 'stop_audio') as stop:
            window.tray_menu.actions()[-1].trigger()
            stop.assert_called_once()
            self.assertFalse(window.timer.isActive())
            self.assertFalse(window.tray.isVisible())

    def test_minimize_without_system_tray_keeps_window_accessible(self):
        window = self.window
        window.show()
        with patch.object(QSystemTrayIcon, 'isSystemTrayAvailable', return_value=False):
            window.showMinimized()
            self.app.processEvents()
            self.app.processEvents()
            self.assertTrue(window.isVisible())
            self.assertTrue(window.isMinimized())

    def test_login_launch_requires_cable_and_physical_microphone(self):
        window = self.window
        with patch.object(window, 'toggle_audio') as toggle, \
             patch.object(window.cable, 'currentData', return_value=None):
            window.start_connected_microphone()
            toggle.assert_not_called()
        with patch.object(window, 'toggle_audio') as toggle, \
             patch.object(window.cable, 'currentData', return_value=Device(42, 'CABLE Input (VB-Audio Virtual Cable)', 0, 2)), \
             patch.object(window.microphone, 'currentData', return_value=Device(20, 'Microphone (MARVO HG9067)', 2, 0)):
            window.start_connected_microphone()
            toggle.assert_called_once()

    def test_controls_reach_audio_settings_and_bypass(self):
        window = self.window
        old = {key: value.value() for key, value in window.sliders.items()}
        try:
            window.sliders['enhance'].setValue(75)
            window.sliders['gain_db'].setValue(-4)
            window.bypass.setChecked(True)
            self.assertEqual(window.engine.settings.enhance, 75)
            self.assertEqual(window.engine.settings.gain_db, -4)
            self.assertTrue(window.engine.settings.bypass)
        finally:
            for key, value in old.items():
                window.sliders[key].setValue(value)
            window.bypass.setChecked(False)

    def test_tone_pad_drag_updates_both_axes_as_one_settings_change(self):
        window = self.window
        old = window.tone_pad.values
        window.show()
        try:
            window.set_tone(0, 0)
            with patch('main.Settings', wraps=Settings) as settings:
                QTest.mouseClick(window.tone_pad, Qt.MouseButton.LeftButton,
                                 pos=window.tone_pad._plot_rect().topRight().toPoint())
                self.assertEqual(settings.call_count, 1)
            self.assertEqual(window.tone_pad.values, (6, 6))
            self.assertEqual(window.engine.settings.warmth, 6)
            self.assertEqual(window.engine.settings.clarity, 6)
            self.assertEqual(window.sliders['warmth'].value(), 6)
            self.assertEqual(window.sliders['clarity'].value(), 6)
            QTest.mousePress(window.tone_pad, Qt.MouseButton.LeftButton,
                             pos=window.tone_pad._plot_rect().center().toPoint())
            move = QMouseEvent(QEvent.Type.MouseMove, QPointF(-100, 1000), QPointF(-100, 1000),
                               Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(window.tone_pad, move)
            QTest.mouseRelease(window.tone_pad, Qt.MouseButton.LeftButton)
            self.assertEqual(window.tone_pad.values, (-6, -6))
            self.assertEqual((window.engine.settings.warmth, window.engine.settings.clarity), (-6, -6))
        finally:
            window.set_tone(*old)

    def test_tone_pad_keyboard_sliders_and_reset_stay_linked(self):
        window = self.window
        old = window.tone_pad.values
        original = window.engine.settings
        try:
            window.set_tone(0, 0)
            QTest.keyClick(window.tone_pad, Qt.Key.Key_Right)
            QTest.keyClick(window.tone_pad, Qt.Key.Key_Up)
            self.assertEqual(window.tone_pad.values, (1, 1))
            window.sliders['warmth'].setValue(-3)
            window.sliders['clarity'].setValue(4)
            self.assertEqual(window.tone_pad.values, (-3, 4))
            QTest.mouseClick(window.tone_reset, Qt.MouseButton.LeftButton)
            self.assertEqual(window.tone_pad.values, (0, 0))
            self.assertEqual(window.engine.settings, replace(original, warmth=0, clarity=0))
            window.set_tone(3, -2)
            QTest.keyClick(window.tone_pad, Qt.Key.Key_Home)
            self.assertEqual(window.tone_pad.values, (0, 0))
            window.set_tone(-2, 2)
            QTest.mouseDClick(window.tone_pad, Qt.MouseButton.LeftButton,
                              pos=window.tone_pad._plot_rect().center().toPoint())
            self.assertEqual(window.tone_pad.values, (0, 0))
        finally:
            window.set_tone(*old)

    def test_tone_change_keeps_saved_recording_and_stops_stale_preview(self):
        window = self.window
        old = window.tone_pad.values
        raw = np.zeros(4800, dtype=np.float32)
        window.engine.last_test = (raw, raw.copy())
        try:
            window.set_tone(0, 0)
            with patch.object(window.playback, 'stop') as stop:
                window.set_tone(4, 3)
                stop.assert_called_once()
            self.assertIs(window.engine.last_test[0], raw)
            self.assertIn('click Processed', window.status.text())
            self.assertEqual((window.engine.settings.warmth, window.engine.settings.clarity), (4, 3))
        finally:
            window.set_tone(*old)

    def test_tone_pad_is_disabled_during_preview_rendering(self):
        window = self.window
        with patch.object(window, 'render_future', Future()):
            window.tick()
            self.assertFalse(window.tone_pad.isEnabled())
            self.assertFalse(window.tone_reset.isEnabled())
            self.assertFalse(window.sliders['warmth'].isEnabled())
        window.tick()
        self.assertTrue(window.tone_pad.isEnabled())
        self.assertTrue(window.tone_reset.isEnabled())

    def test_comparison_playback_stops_capture_and_uses_headphones(self):
        window = self.window
        if window.headphones.currentData() is None:
            self.skipTest('No physical headphone output available')
        raw = np.zeros(4800, dtype=np.float32)
        wet = np.ones(4800, dtype=np.float32)*0.1
        window.engine.last_test = (raw, wet)
        window.engine.running = True  # No streams are opened by this test.
        with patch.object(window.playback, 'play') as play:
            window.play_test(0)
            self.assertFalse(window.engine.running)
            self.assertFalse(window.engine.monitor_enabled)
            self.assertIs(play.call_args.args[0], raw)
            self.assertEqual(play.call_args.args[1], window.headphones.currentData())

    def test_processed_preview_uses_current_settings_after_recording(self):
        window = self.window
        if window.headphones.currentData() is None:
            self.skipTest('No physical headphone output available')
        raw = (0.02*np.sin(2*np.pi*1000*np.arange(4800)/48000)).astype(np.float32)
        window.engine.last_test = (raw, raw.copy())
        # Simulate changing output level after the test was recorded.
        window.engine.settings = Settings(enhance=0, noise=0, compression=0, gain_db=12)
        with patch.object(window.playback, 'play') as play:
            window.play_test(1)
            deadline = time.monotonic()+3
            while not play.called and time.monotonic() < deadline:
                window.tick()
                time.sleep(0.01)
            self.assertTrue(play.called)
            self.assertGreater(float(np.std(play.call_args.args[0])), float(np.std(raw))*2)
            np.testing.assert_array_equal(window.engine.last_test[0], raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
