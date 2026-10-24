from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

# Mono 10 ms audio frames do not use threaded BLAS. Keep its idle worker
# pools and stacks out of this process; this does not change audio threading.
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

import numpy as np
from PySide6.QtCore import Qt, QTimer, QSettings, QSize, QEvent
from PySide6.QtGui import QColor, QDesktopServices, QFont, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton, QScrollArea,
    QSlider, QVBoxLayout, QWidget, QMenu, QSystemTrayIcon,
)

from audio import AudioEngine, TestPlayback, devices, render_preview
from dsp import RATE, Settings
from tone_pad import TonePad

ROOT = Path(__file__).resolve().parent
LOG_DIR = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'MicForge'

VERSION = '0.2.0'

STYLE = """
QWidget { color: #dedfe3; font-family: 'Segoe UI'; font-size: 12px; }
QMainWindow, QWidget#root { background: #191b20; }
QDialog, QMessageBox { background: #22252b; }
QFrame#footer { background: #1e2127; border-top: 1px solid #373b44; }
QFrame#card { background: #22252b; border: 1px solid #373b44; border-radius: 5px; }
QLabel#muted { color: #9da4b1; }
QLabel#section { font-size: 13px; font-weight: 600; }
QLabel#brand { font-size: 21px; font-weight: 600; }
QLabel#value { color: #a9c6f4; font-weight: 600; }
QLabel#badge { color: #a2a9b5; background: #292d35; padding: 5px 9px; border-radius: 4px; }
QLabel#notice { color: #aab5c6; padding-top: 3px; }
QPushButton { background: #30353e; border: 1px solid #454c59; border-radius: 4px; padding: 6px 10px; }
QPushButton:hover { background: #3c4350; border-color: #62738e; }
QPushButton:pressed { background: #252a32; }
QPushButton:disabled { color: #707784; background: #272b32; border-color: #363b44; }
QPushButton#primary { background: #4977bb; color: white; border: 1px solid #6390d0; padding: 8px 18px; font-weight: 600; }
QPushButton#primary:hover { background: #5989ce; }
QPushButton#primary:disabled { background: #33435a; color: #8f9daf; }
QComboBox { background: #1a1d23; border: 1px solid #454c59; border-radius: 4px; padding: 6px 8px; min-height: 18px; }
QComboBox:disabled { color: #7e8797; }
QComboBox QAbstractItemView { background: #272b32; selection-background-color: #3d5e8c; padding: 4px; }
QSlider::groove:horizontal { height: 4px; background: #414854; border-radius: 2px; }
QSlider::sub-page:horizontal { background: #739bd4; border-radius: 2px; }
QSlider::handle:horizontal { width: 12px; margin: -5px 0; border-radius: 6px; background: #c4d7f4; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #667181; border-radius: 3px; background: #1a1d23; }
QCheckBox::indicator:checked { background: #739bd4; border: 3px solid #374b68; }
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { width: 7px; background: #191b20; }
QScrollBar::handle:vertical { background: #464e5b; border-radius: 3px; min-height: 25px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #303844; color: white; border: 1px solid #697a92; }
"""


def label(text, kind=None, wrap=False):
    widget = QLabel(text)
    if kind:
        widget.setObjectName(kind)
    widget.setWordWrap(wrap)
    return widget


def card(kind='card'):
    frame = QFrame()
    frame.setObjectName(kind)
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(12, 10, 12, 10)
    layout.setSpacing(6)
    return frame, layout


class Meter(QWidget):
    def __init__(self):
        super().__init__()
        self.peak = 0
        self.setMinimumHeight(13)

    def set_peak(self, value):
        peak = max(value, self.peak*0.80)
        if peak != self.peak:
            self.peak = peak
            self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fraction = max(0, min(1, (20*math.log10(max(self.peak, 1e-6))+60)/60))
        width = self.width()
        for i in range(40):
            lit = i/40 < fraction
            color = '#ef7e83' if i > 36 else '#d7c381' if i > 31 else '#75dfc3'
            painter.setBrush(QColor(color if lit else '#263342'))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawRoundedRect(int(i*width/40), 1, max(2, int(width/40)-3), 10, 2, 2)


def app_icon():
    pixmap = QPixmap(128, 128)
    pixmap.fill(QColor('#252b36'))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(QPen(QColor('#a9c6f4'), 9, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    for i, height in enumerate([23, 48, 73, 48, 23]):
        painter.drawLine(28+i*18, (128-height)//2, 28+i*18, (128+height)//2)
    painter.end()
    return QIcon(pixmap)


class Window(QMainWindow):
    def __init__(self):
        super().__init__()
        self.engine = AudioEngine()
        self.playback = TestPlayback()
        self.saved = QSettings('MicForge', 'MicForge')
        self.was_recording = False
        self.preview_was_playing = False
        self.render_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='MicForgePreview')
        self.render_future = None
        self.render_action = None
        self.render_raw = None
        self.render_output = None
        self._closing = False
        self.sliders = {}
        self.setWindowTitle('Mic Forge')
        self.setWindowIcon(app_icon())
        self._in_tray = False
        self.resize(800, 780)
        self.setMinimumSize(720, 620)
        root = QWidget()
        root.setObjectName('root')
        self.setCentralWidget(root)
        panel_layout = QVBoxLayout(root)
        panel_layout.setContentsMargins(0, 0, 0, 0)
        panel_layout.setSpacing(0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setObjectName('root')
        scroll.setWidget(body)
        panel_layout.addWidget(scroll, 1)
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        top = QHBoxLayout()
        top.addWidget(label('Mic Forge', 'brand'))
        top.addWidget(label(VERSION, 'muted'))
        top.addStretch()
        self.badge = label('MICROPHONE OFF', 'badge')
        top.addWidget(self.badge)
        guide = QPushButton('Connection guide')
        guide.clicked.connect(self.guide)
        top.addWidget(guide)
        layout.addLayout(top)

        routing, route_layout = card()
        route_top = QHBoxLayout()
        route_top.addWidget(label('Audio routing', 'section'))
        route_top.addStretch()
        self.refresh_button = QPushButton('Refresh')
        self.refresh_button.clicked.connect(self.refresh_devices)
        route_top.addWidget(self.refresh_button)
        route_layout.addLayout(route_top)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        self.microphone, self.cable, self.headphones = QComboBox(), QComboBox(), QComboBox()
        for col, (title, combo) in enumerate([
            ('Microphone', self.microphone), ('Voice output', self.cable), ('Headphones / preview', self.headphones)
        ]):
            combo.setMinimumWidth(100)
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(10)
            combo.setAccessibleName(title)
            grid.addWidget(label(title, 'muted'), 0, col)
            grid.addWidget(combo, 1, col)
            grid.setColumnStretch(col, 1)
        route_layout.addLayout(grid)
        self.route_notice = label('', 'notice', True)
        route_layout.addWidget(self.route_notice)
        layout.addWidget(routing)

        controls, control_layout = card()
        control_header = QHBoxLayout()
        control_header.addWidget(label('Voice processing', 'section'))
        control_header.addStretch()
        self.bypass = QCheckBox('Bypass processing')
        self.bypass.toggled.connect(self.update_settings)
        control_header.addWidget(self.bypass)
        control_layout.addLayout(control_header)
        columns = QHBoxLayout()
        columns.setSpacing(22)
        tone_layout, processing = QVBoxLayout(), QVBoxLayout()
        tone_layout.setSpacing(4)
        processing.setSpacing(6)
        tone_header = QHBoxLayout()
        tone_header.addWidget(label('Tone', 'muted'))
        tone_header.addStretch()
        self.tone_reset = QPushButton('Reset tone')
        self.tone_reset.setToolTip('Set Warmth and Clarity to 0 dB. Other controls stay as set.')
        self.tone_reset.clicked.connect(lambda: self.set_tone(0, 0))
        tone_header.addWidget(self.tone_reset)
        tone_layout.addLayout(tone_header)
        self.tone_pad = TonePad()
        self.tone_pad.values_changed.connect(self.set_tone)
        tone_layout.addWidget(self.tone_pad)
        self.tone_values = label('', 'value')
        self.tone_values.setAlignment(Qt.AlignmentFlag.AlignCenter)
        tone_layout.addWidget(self.tone_values)
        self.add_slider(tone_layout, 'warmth', 'Warmth', -6, 6, 0, ' dB', 'Voice body around 180 Hz, in addition to Voice Enhance.')
        self.add_slider(tone_layout, 'clarity', 'Clarity', -6, 6, 0, ' dB', 'Speech presence around 3.2 kHz, in addition to Voice Enhance.')
        self.add_slider(processing, 'enhance', 'Voice Enhance', 0, 100, 40, '%', 'Fuller lows, clearer speech and steadier volume. Preserves natural pitch.')
        self.add_slider(processing, 'noise', 'Noise suppression', 0, 100, 70, '%', 'RNNoise reduces background noise. High settings may affect voice texture.')
        self.add_slider(processing, 'compression', 'Compression', 0, 100, 35, '%', 'Evens out loud and quiet speech. Voice Enhance adds gentle compression too.')
        self.add_slider(processing, 'gain_db', 'Output level', -12, 12, 0, ' dB', 'Processed voice volume before the peak limiter.')
        processing.addWidget(label('Peak protection on while processing.\nBypass plays the original signal.', 'muted', True))
        processing.addStretch()
        columns.addLayout(tone_layout, 1)
        columns.addLayout(processing, 1)
        control_layout.addLayout(columns)
        layout.addWidget(controls)

        levels, levels_layout = card()
        meter_grid = QGridLayout()
        self.in_db, self.out_db = label('-inf dBFS', 'muted'), label('-inf dBFS', 'muted')
        self.input_meter, self.output_meter = Meter(), Meter()
        for row, (text, db, meter) in enumerate([
            ('Mic input', self.in_db, self.input_meter), ('Processed', self.out_db, self.output_meter)
        ]):
            meter_grid.addWidget(label(text, 'muted'), row, 0)
            meter_grid.addWidget(meter, row, 1)
            meter_grid.addWidget(db, row, 2)
        meter_grid.setColumnStretch(1, 1)
        levels_layout.addLayout(meter_grid)
        self.health = label('Start your mic to check its level.', 'muted', True)
        levels_layout.addWidget(self.health)
        layout.addWidget(levels)

        test, test_layout = card()
        test_top = QHBoxLayout()
        test_top.addWidget(label('Recording test', 'section'))
        test_top.addStretch()
        test_top.addWidget(label('Local audio only', 'muted'))
        test_layout.addLayout(test_top)
        self.test_info = label('Record up to 12 seconds. Compare through headphones.', 'muted', True)
        test_layout.addWidget(self.test_info)
        test_buttons = QHBoxLayout()
        self.record_button = QPushButton('Record test')
        self.record_button.clicked.connect(self.record_test)
        self.original_button = QPushButton('Original')
        self.processed_button = QPushButton('Processed')
        self.export_button = QPushButton('Save WAVs')
        self.original_button.clicked.connect(lambda: self.play_test(0))
        self.processed_button.clicked.connect(lambda: self.play_test(1))
        self.export_button.clicked.connect(self.save_test)
        for button in (self.record_button, self.original_button, self.processed_button, self.export_button):
            test_buttons.addWidget(button)
        test_layout.addLayout(test_buttons)
        layout.addWidget(test)
        layout.addStretch()

        footer_frame = QFrame()
        footer_frame.setObjectName('footer')
        footer = QHBoxLayout(footer_frame)
        footer.setContentsMargins(16, 10, 16, 10)
        self.start_button = QPushButton('Start microphone')
        self.start_button.setObjectName('primary')
        self.start_button.clicked.connect(self.toggle_audio)
        footer.addWidget(self.start_button)
        self.monitor = QCheckBox('Hear myself')
        self.monitor.setToolTip('Use headphones. Monitoring adds delay and starts off.')
        self.monitor.toggled.connect(self.toggle_monitor)
        footer.addWidget(self.monitor)
        footer.addStretch()
        self.status = label('Ready.', 'muted')
        footer.addWidget(self.status)
        panel_layout.addWidget(footer_frame)
        self.cable.currentIndexChanged.connect(self.route_changed)
        self.refresh_devices()
        self.update_settings()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(80)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('Mic Forge')
        self.tray_menu = QMenu(self)
        self.tray_menu.addAction('Open Mic Forge', self.restore_from_tray)
        self.tray_menu.addSeparator()
        self.tray_menu.addAction('Quit Mic Forge', self.close)
        self.tray.setContextMenu(self.tray_menu)
        self.tray.activated.connect(self.tray_activated)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        self.tick()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and self.isMinimized():
            QTimer.singleShot(0, self.minimize_to_tray)

    def minimize_to_tray(self):
        if (self.isMinimized() and QSystemTrayIcon.isSystemTrayAvailable()
                and self.tray.isVisible()):
            self._in_tray = True
            self.hide()

    def restore_from_tray(self):
        self._in_tray = False
        self.tick()
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def tray_activated(self, reason):
        if reason in (QSystemTrayIcon.ActivationReason.Trigger,
                      QSystemTrayIcon.ActivationReason.DoubleClick):
            self.restore_from_tray()

    def add_slider(self, target, key, title, low, high, default, suffix, tooltip=''):
        top = QHBoxLayout()
        top.setSpacing(6)
        top.addWidget(label(title))
        top.addStretch()
        value_label = label('', 'value')
        top.addWidget(value_label)
        target.addLayout(top)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(low, high)
        try:
            value = int(self.saved.value(key, default))
        except (TypeError, ValueError):
            value = default
        slider.setValue(max(low, min(high, value)))
        slider.setToolTip(tooltip or title)
        slider.setAccessibleName(title)
        def update(value):
            value_label.setText(f'{value}{suffix}')
            self.update_settings()
        slider.valueChanged.connect(update)
        self.sliders[key] = slider
        value_label.setText(f'{slider.value()}{suffix}')
        target.addWidget(slider)

    def set_tone(self, warmth, clarity):
        if (warmth, clarity) == (self.sliders['warmth'].value(), self.sliders['clarity'].value()):
            return
        self._updating_tone = True
        try:
            self.sliders['warmth'].setValue(warmth)
            self.sliders['clarity'].setValue(clarity)
        finally:
            self._updating_tone = False
        self.update_settings()

    def update_settings(self, *_):
        if getattr(self, '_updating_tone', False):
            return
        values = {key: slider.value() for key, slider in self.sliders.items()}
        values['bypass'] = self.bypass.isChecked() if hasattr(self, 'bypass') else False
        self.engine.settings = Settings(**values)
        if hasattr(self, 'tone_pad'):
            warmth, clarity = values['warmth'], values['clarity']
            self.tone_pad.set_values(warmth, clarity)
            self.tone_values.setText(f'Warmth {warmth:+d} dB   ·   Clarity {clarity:+d} dB')
        if self.engine.last_test is not None and hasattr(self, 'status'):
            self.playback.stop()
            self.preview_was_playing = False
            self.status.setText('Settings changed · click Processed to hear them')

    def refresh_devices(self):
        try:
            microphones, cables, outputs = devices()
        except Exception as exc:
            self.show_error('Cannot enumerate audio devices', exc)
            return
        for combo, entries, saved_key, preferred in [
            (self.microphone, microphones, 'microphone', 'MARVO'),
            (self.cable, cables, 'cable', 'CABLE Input'),
            (self.headphones, outputs, 'headphones', 'MARVO'),
        ]:
            previous = combo.currentData()
            wanted = previous.name if previous else str(self.saved.value(saved_key, ''))
            combo.blockSignals(True)
            combo.clear()
            if combo is self.cable:
                combo.addItem('Local test only (no cable)', None)
            for device in entries:
                combo.addItem(device.name, device)
                combo.setItemData(combo.count()-1, device.name, Qt.ItemDataRole.ToolTipRole)
            exact = next((i for i in range(combo.count()) if combo.itemData(i) and combo.itemData(i).name == wanted), None)
            preferred_index = next((i for i in range(combo.count()) if preferred.lower() in combo.itemText(i).lower()), 0)
            combo.setCurrentIndex(exact if exact is not None else preferred_index)
            combo.blockSignals(False)
        self.route_changed()

    def route_changed(self):
        if self.cable.currentData() is None:
            self.route_notice.setText('Local test only. Choose a virtual cable to send your voice to another app.')
        else:
            self.route_notice.setText('Discord input: CABLE Output. Keep Windows and Discord playback on your headset.')

    def toggle_audio(self):
        self.playback.stop()
        if self.engine.running or self.engine.processor is not None:
            self.stop_audio()
            return
        microphone = self.microphone.currentData()
        if microphone is None:
            self.show_error('No microphone selected', 'Connect your headset and refresh devices.')
            return
        try:
            self.engine.start(microphone, self.cable.currentData())
            self.status.setText('Local test mode' if self.cable.currentData() is None else 'Sending voice to VB-CABLE')
            for key, combo in [('microphone', self.microphone), ('cable', self.cable), ('headphones', self.headphones)]:
                selected = combo.currentData()
                self.saved.setValue(key, selected.name if selected else '')
        except Exception as exc:
            self.show_error('Could not start microphone', exc)
        self.tick()

    def start_connected_microphone(self):
        """Opt-in login launch: never fall back to a physical playback output."""
        if self.cable.currentData() is None:
            self.status.setText('VB-CABLE unavailable · microphone remains off')
            return
        if self.microphone.currentData() is None:
            self.status.setText('Microphone unavailable · reconnect your headset')
            return
        self.toggle_audio()

    def stop_audio(self):
        self.monitor.blockSignals(True)
        self.monitor.setChecked(False)
        self.monitor.blockSignals(False)
        try:
            self.engine.stop()
        except Exception as exc:
            self.show_error('Could not stop audio cleanly', exc)
        self.status.setText('Microphone stopped')

    def toggle_monitor(self, checked):
        try:
            self.playback.stop()
            self.engine.set_monitor(checked, self.headphones.currentData())
        except Exception as exc:
            self.monitor.blockSignals(True)
            self.monitor.setChecked(False)
            self.monitor.blockSignals(False)
            self.show_error('Monitoring unavailable', exc)

    def record_test(self):
        self.playback.stop()
        try:
            if self.engine.recording:
                self.engine.finish_recording()
            else:
                self.engine.start_recording()
                self.was_recording = True
        except Exception as exc:
            self.show_error('Could not record test', exc)

    def play_test(self, which):
        if not self.engine.last_test or self.render_future is not None:
            return
        output = self.headphones.currentData()
        if output is None:
            self.show_error('No headphones selected', 'Connect your headset and refresh devices.')
            return
        # Stop microphone capture/routing during A/B playback so earcup leakage
        # cannot accidentally rebroadcast the test into a call.
        if self.engine.running:
            self.stop_audio()
        if which == 1:
            self.render_test('play', output)
            return
        try:
            self.playback.play(self.engine.last_test[which], output)
            self.status.setText('Playing original' if which == 0 else 'Playing processed')
        except Exception as exc:
            self.show_error('Could not play test', exc)

    def save_test(self):
        if not self.engine.last_test or self.render_future is not None:
            return
        if self.engine.running:
            self.stop_audio()
        self.render_test('save')

    def render_test(self, action, output=None):
        self.playback.stop()
        self.preview_was_playing = False
        self.render_raw = self.engine.last_test[0]
        self.render_output = output
        self.render_action = action
        self.render_future = self.render_pool.submit(render_preview, self.render_raw, self.engine.settings)
        self.status.setText('Applying current settings…')
        self.tick()

    def finish_render(self):
        future = self.render_future
        self.render_future = None
        try:
            processed = future.result()
            self.engine.last_test = (self.render_raw, processed)
            if self.render_action == 'play':
                self.playback.play(processed, self.render_output)
                self.status.setText('Playing processed · current settings')
            else:
                self.save_test_files()
        except Exception as exc:
            self.show_error('Could not apply preview settings', exc)
        finally:
            self.render_raw = None
            self.render_output = None
            self.render_action = None

    def save_test_files(self):
        name, _ = QFileDialog.getSaveFileName(self, 'Save test (original and processed WAVs)', str(Path.home() / 'Documents' / 'Mic Forge test.wav'), 'WAV audio (*.wav)')
        if not name:
            return
        path = Path(name).with_suffix('.wav')
        original = path.with_stem(path.stem+' - original')
        if original.exists():
            answer = QMessageBox.question(self, 'Replace original recording?', f'Also replace {original.name}?')
            if answer != QMessageBox.StandardButton.Yes:
                return
        try:
            for target, samples in [(original, self.engine.last_test[0]), (path, self.engine.last_test[1])]:
                with wave.open(str(target), 'wb') as wav:
                    wav.setnchannels(1)
                    wav.setsampwidth(2)
                    wav.setframerate(RATE)
                    wav.writeframes((np.clip(samples, -1, 1)*32767).astype('<i2').tobytes())
            self.status.setText('Saved original + processed WAVs')
        except Exception as exc:
            self.show_error('Could not save test', exc)

    def tick(self):
        if self.render_future is not None and self.render_future.done():
            self.finish_render()
        rendering = self.render_future is not None
        running = self.engine.running
        if self.playback.done and self.preview_was_playing:
            self.status.setText('Preview finished · start microphone to resume')
        self.preview_was_playing = not self.playback.done
        if self.engine.error and self.engine.processor is not None:
            error = self.engine.error
            self.stop_audio()
            self.show_error('Audio device or processing error', error)
            running = False
        # A disconnected endpoint may stop a WASAPI stream without raising in DSP.
        if running and any(not stream.active for stream in self.engine._streams):
            self.stop_audio()
            self.show_error('Audio device disconnected', 'Reconnect your devices, refresh the device list, then start again.')
            running = False
        if self._in_tray:
            return  # Audio, errors and preview completion remain active above.
        self.badge.setText('MICROPHONE LIVE' if running else 'MICROPHONE OFF')
        self.badge.setStyleSheet('color: #a9d9bd;' if running else '')
        self.start_button.setText('Stop microphone' if running else 'Start microphone')
        self.start_button.setEnabled(not rendering and (running or self.microphone.currentData() is not None))
        for combo in (self.microphone, self.cable, self.headphones):
            combo.setEnabled(not rendering and not running and self.playback.done)
        self.refresh_button.setEnabled(not rendering and not running and self.playback.done)
        for slider in self.sliders.values():
            slider.setEnabled(not rendering)
        self.tone_pad.setEnabled(not rendering)
        self.tone_reset.setEnabled(not rendering)
        self.bypass.setEnabled(not rendering)
        self.monitor.setEnabled(running and self.headphones.currentData() is not None)
        self.record_button.setEnabled(running)
        self.record_button.setText('Finish recording' if self.engine.recording else 'Record test')
        available = self.engine.last_test is not None and not self.engine.recording and not rendering
        self.processed_button.setText('Applying…' if rendering else 'Processed')
        for button in (self.original_button, self.processed_button, self.export_button):
            button.setEnabled(available)
        if rendering:
            self.test_info.setText('Applying your current controls to the recorded original…')
        elif self.engine.recording:
            self.test_info.setText(f'Recording {self.engine.record_frames / RATE:.1f} / 12 seconds… speak naturally.')
        elif available:
            seconds = len(self.engine.last_test[0])/RATE
            self.test_info.setText(f'{seconds:.1f}s ready. Adjust controls, then click Processed to hear the changes. Playback pauses your microphone.')
        for peak, meter, text in [(self.engine.raw_peak, self.input_meter, self.in_db), (self.engine.wet_peak, self.output_meter, self.out_db)]:
            meter.set_peak(peak if running else 0)
            text.setText(f'{20*math.log10(peak):.1f} dBFS' if running and peak > 1e-6 else '−∞ dBFS')
        if running:
            buffer = self.engine.cable_buffer
            if time.monotonic() < self.engine.clipped_until:
                self.health.setText('Input clipping: reduce the headset microphone level in Windows.')
            elif self.engine.overflows or buffer.drops or buffer.underruns > 3:
                self.health.setText('Audio interruptions detected. Close heavy apps and restart the mic.')
            elif self.engine.settings.bypass:
                self.health.setText('Bypass active · original mic signal')
            else:
                self.health.setText(f'Processing {self.engine.process_ms:.1f} ms / 10 ms frame' + (' · limiter active' if self.engine.limiting else ' · peak protection on'))
        else:
            self.health.setText('Microphone is off. Playback stays in your headphones.')

    def guide(self):
        dialog = QMessageBox(self)
        dialog.setWindowTitle('Connect Mic Forge to Discord or a game')
        dialog.setTextFormat(Qt.TextFormat.RichText)
        dialog.setText('<h3>Your voice, on its own path.</h3>'
            '<p>1. Install <b>VB-CABLE</b> from VB-Audio, then restart Windows when its installer asks.</p>'
            '<p>2. Refresh devices here. Select <b>your physical microphone</b> as microphone and <b>CABLE Input</b> as voice output.</p>'
            '<p>3. In Discord or your game, select <b>CABLE Output</b> as the microphone.</p>'
            '<p>4. Keep Windows playback and Discord output on <b>your headset</b>. Do not set CABLE Input as your default speakers.</p>'
            '<p>5. Start the microphone in Mic Forge. Other apps hear processed audio only while it runs.</p>'
            '<p>Close Voicemeeter while testing. Leave Windows “Listen to this device” off. '
            'If Discord adds its own suppression or automatic gain, compare with those effects off to avoid double processing.</p>'
            '<p>If music still leaks, record a test while staying silent and playing music. '
            'Compare with the music paused. That helps distinguish microphone pickup or headset crosstalk from mixing.</p>')
        download = dialog.addButton('Open official VB-CABLE page', QMessageBox.ButtonRole.ActionRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        if dialog.clickedButton() is download:
            QDesktopServices.openUrl(QUrl('https://vb-audio.com/Cable/'))

    def show_error(self, title, error):
        logging.error('%s: %s', title, error)
        QMessageBox.warning(self, title, str(error))

    def closeEvent(self, event):
        self.tray.hide()
        self.timer.stop()
        self.playback.stop()
        self.stop_audio()
        self.render_pool.shutdown(wait=True, cancel_futures=True)
        for key, slider in self.sliders.items():
            self.saved.setValue(key, slider.value())
        self.saved.sync()
        event.accept()


def run():
    parser = argparse.ArgumentParser()
    parser.add_argument('--screenshot', help='Render idle UI to a PNG, without opening the mic.')
    parser.add_argument('--self-test', help='Write a synthetic runtime check report; does not open the microphone.')
    parser.add_argument('--start-microphone', action='store_true', help='Start the selected physical mic only when a cable output is available.')
    args = parser.parse_args()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(filename=LOG_DIR/'mic-forge.log', level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s')
    if args.self_test:
        from runtime_check import check
        check(args.self_test)
        return 0
    app = QApplication(sys.argv[:1])
    app.setApplicationName('Mic Forge')
    app.setOrganizationName('MicForge')
    app.setStyle('Fusion')
    app.setStyleSheet(STYLE)
    window = Window()
    window.show()
    if args.screenshot:
        def screenshot():
            window.grab().save(args.screenshot)
            window.close()
        QTimer.singleShot(600, screenshot)
    elif args.start_microphone:
        QTimer.singleShot(600, window.start_connected_microphone)
    return app.exec()


if __name__ == '__main__':
    sys.exit(run())
