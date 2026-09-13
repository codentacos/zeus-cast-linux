"""Settings window (PySide6). Runs the Engine and mirrors what the cooler shows."""

from __future__ import annotations

import shlex
import shutil
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QSlider,
    QSpinBox,
    QSystemTrayIcon,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__, media
from .config import Config, config_dir
from .device import ROTATIONS, DeviceInfo
from .engine import Engine, Listener
from .render import THEMES, Option, composite

PREVIEW_SIZE = 300
MEDIA_FILTER = "Images and videos (*.jpg *.jpeg *.png *.bmp *.webp *.gif *.mp4 *.mov *.avi *.mkv *.webm)"
CLOCK_POSITIONS = ("Off", "Layout A", "Layout B", "Layout C")
METRIC_TITLES = {
    "cpu_usage": "CPU usage",
    "cpu_temp": "CPU temperature",
    "cpu_freq": "CPU frequency",
    "gpu_usage": "GPU usage",
    "gpu_temp": "GPU temperature",
    "vram_usage": "GPU memory",
    "ram_usage": "RAM usage",
    "disk_usage": "Disk usage (/)",
    "net_down": "Download speed",
    "net_up": "Upload speed",
    "fan": "Fan speed (fastest)",
}


def to_pixmap(image: Image.Image) -> QPixmap:
    rgba = image.convert("RGBA")
    qimage = QImage(rgba.tobytes("raw", "RGBA"), rgba.width, rgba.height, QImage.Format.Format_RGBA8888)
    return QPixmap.fromImage(qimage.copy())


def app_icon(accent: str) -> QIcon:
    pixmap = QPixmap(64, 64)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#15171c"))
    painter.drawEllipse(2, 2, 60, 60)
    pen = QPen(QColor(accent))
    pen.setWidth(7)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawArc(14, 14, 36, 36, 225 * 16, -200 * 16)
    painter.end()
    return QIcon(pixmap)


class EngineSignals(QObject):
    status = Signal(bool, str)
    info = Signal(object)
    overlay = Signal(object)
    finished = Signal(str, object)
    progress = Signal(int, int)


class SignalListener(Listener):
    """Forwards engine callbacks to the GUI thread through queued Qt signals."""

    def __init__(self, signals: EngineSignals) -> None:
        self.signals = signals

    def status_changed(self, connected, message):
        self.signals.status.emit(connected, message)

    def info_received(self, info):
        self.signals.info.emit(info)

    def overlay_rendered(self, image):
        self.signals.overlay.emit(image)

    def task_finished(self, description, error):
        self.signals.finished.emit(description, error)

    def upload_progress(self, sent, total):
        self.signals.progress.emit(int(sent * 1000 // max(total, 1)), 1000)


class MainWindow(QMainWindow):
    def __init__(self, config: Config, port: str | None = None) -> None:
        super().__init__()
        self.config = config
        self.tray: QSystemTrayIcon | None = None
        self._overlay: Image.Image | None = None
        self._background: Image.Image | None = None
        self._selected_background: str | None = config["background"]["path"]
        self._quitting = False
        self._stopped = False
        self._tray_hint_shown = False

        self.setWindowTitle("ZEUS CAST for Linux")
        self.setWindowIcon(app_icon(config["accent"]))
        self.signals = EngineSignals()
        self.engine = Engine(config, SignalListener(self.signals), port=port)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.addLayout(self._build_sidebar())
        tabs = QTabWidget()
        tabs.addTab(self._build_display_tab(), "Display")
        tabs.addTab(self._build_background_tab(), "Background")
        tabs.addTab(self._build_device_tab(), "Device")
        tabs.addTab(self._build_settings_tab(), "Settings")
        layout.addWidget(tabs, 1)
        self.setCentralWidget(central)

        self.signals.status.connect(self._on_status)
        self.signals.info.connect(self._on_info)
        self.signals.overlay.connect(self._on_overlay)
        self.signals.finished.connect(self._on_task_finished)
        self.signals.progress.connect(self._on_progress)

        self._build_tray()
        self._load_background_preview()
        self.engine.start()

    # Layout

    def _build_sidebar(self) -> QVBoxLayout:
        column = QVBoxLayout()
        self.preview = QLabel()
        self.preview.setFixedSize(PREVIEW_SIZE, PREVIEW_SIZE)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #000; border-radius: 10px;")
        self.model_label = QLabel(self.engine.model.name)
        self.model_label.setStyleSheet("font-weight: 600;")
        self.status_label = QLabel("Looking for the cooler…")
        self.status_label.setWordWrap(True)
        self.status_label.setMaximumWidth(PREVIEW_SIZE)
        column.addWidget(self.preview)
        column.addWidget(self.model_label)
        column.addWidget(self.status_label)
        column.addStretch()
        return column

    def _build_display_tab(self) -> QWidget:
        page = QWidget()
        layout = QHBoxLayout(page)
        self.theme_list = QListWidget()
        self.theme_list.setFixedWidth(230)
        for theme in THEMES.values():
            item = QListWidgetItem(theme.name)
            item.setData(Qt.ItemDataRole.UserRole, theme.key)
            self.theme_list.addItem(item)

        right = QVBoxLayout()
        self.options_box = QGroupBox("Theme options")
        self.options_form = QFormLayout(self.options_box)
        appearance = QGroupBox("Appearance")
        form = QFormLayout(appearance)
        self.accent_button = QPushButton()
        self.accent_button.clicked.connect(self._pick_accent)
        backdrop = QCheckBox("Dim the background behind the overlay")
        backdrop.setChecked(bool(self.config["backdrop"]))
        backdrop.toggled.connect(lambda checked: self._set_config("backdrop", checked))
        form.addRow("Accent colour", self.accent_button)
        form.addRow(backdrop)
        right.addWidget(self.options_box)
        right.addWidget(appearance)
        right.addStretch()

        layout.addWidget(self.theme_list)
        layout.addLayout(right, 1)
        self._update_accent_button()
        keys = list(THEMES)
        self.theme_list.currentRowChanged.connect(self._on_theme_selected)
        self.theme_list.setCurrentRow(keys.index(self.config["theme"]) if self.config["theme"] in THEMES else 0)
        return page

    def _build_background_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            "The cooler stores the background and plays it by itself: a still image, or a video "
            f"loop of up to {media.MAX_VIDEO_SECONDS} seconds. The Display theme is drawn on top."
        )
        intro.setWordWrap(True)
        row = QHBoxLayout()
        self.background_label = QLabel(self._selected_background or "No file chosen")
        self.background_label.setWordWrap(True)
        choose = QPushButton("Choose file…")
        choose.clicked.connect(self._choose_background)
        row.addWidget(self.background_label, 1)
        row.addWidget(choose)
        form = QFormLayout()
        self.background_mode = QComboBox()
        self.background_mode.addItem("Fill (crop to the panel)", "fill")
        self.background_mode.addItem("Fit (add black bars)", "fit")
        self.background_mode.setCurrentIndex(max(0, self.background_mode.findData(self.config["background"]["mode"])))
        self.background_mode.currentIndexChanged.connect(lambda _: self._load_background_preview())
        form.addRow("Scaling", self.background_mode)
        self.upload_button = QPushButton("Upload to cooler")
        self.upload_button.clicked.connect(self._upload_background)
        self.upload_progress = QProgressBar()
        self.upload_progress.hide()
        self.background_status = QLabel()
        self.background_status.setWordWrap(True)
        layout.addWidget(intro)
        layout.addLayout(row)
        layout.addLayout(form)
        layout.addWidget(self.upload_button)
        layout.addWidget(self.upload_progress)
        layout.addWidget(self.background_status)
        layout.addStretch()
        return page

    def _build_device_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        info_box = QGroupBox("Device")
        info_form = QFormLayout(info_box)
        self.info_labels: dict[str, QLabel] = {}
        for key, title in (
            ("serial", "Serial number"),
            ("app", "App version"),
            ("firmware", "Firmware"),
            ("hardware", "Hardware"),
            ("space", "Free space (raw)"),
        ):
            label = QLabel("—")
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            info_form.addRow(title, label)
            self.info_labels[key] = label

        panel_box = QGroupBox("Panel")
        panel_form = QFormLayout(panel_box)
        brightness_row = QHBoxLayout()
        self.brightness = QSlider(Qt.Orientation.Horizontal)
        self.brightness.setRange(0, 100)
        self.brightness_value = QLabel("—")
        self.brightness_value.setMinimumWidth(40)
        self._brightness_timer = QTimer(self)
        self._brightness_timer.setSingleShot(True)
        self._brightness_timer.setInterval(400)
        self._brightness_timer.timeout.connect(self._apply_brightness)
        self.brightness.valueChanged.connect(self._on_brightness_changed)
        self.brightness.sliderReleased.connect(self._apply_brightness)
        brightness_row.addWidget(self.brightness, 1)
        brightness_row.addWidget(self.brightness_value)
        panel_form.addRow("Brightness", brightness_row)

        rotation_row = QHBoxLayout()
        self.rotation_group = QButtonGroup(self)
        for degrees in ROTATIONS:
            button = QRadioButton(f"{degrees}°")
            self.rotation_group.addButton(button, degrees)
            rotation_row.addWidget(button)
        self.rotation_group.idClicked.connect(self.engine.set_rotation)
        panel_form.addRow("Rotation", rotation_row)

        startup_row = QHBoxLayout()
        self.startup_group = QButtonGroup(self)
        for option, title in ((1, "GAMDIAS logo"), (2, "Custom")):
            button = QRadioButton(title)
            self.startup_group.addButton(button, option)
            startup_row.addWidget(button)
        self.startup_group.idClicked.connect(self.engine.set_startup_logo)
        panel_form.addRow("Startup screen", startup_row)

        self.clock_position = QComboBox()
        self.clock_position.addItems(CLOCK_POSITIONS)
        self.clock_position.activated.connect(self.engine.set_clock_position)
        panel_form.addRow("Built-in clock", self.clock_position)

        actions = QHBoxLayout()
        reboot = QPushButton("Restart display")
        reboot.clicked.connect(self._confirm_reboot)
        reset = QPushButton("Factory reset…")
        reset.clicked.connect(self._confirm_factory_reset)
        actions.addWidget(reboot)
        actions.addWidget(reset)
        actions.addStretch()

        layout.addWidget(info_box)
        layout.addWidget(panel_box)
        layout.addLayout(actions)
        layout.addStretch()
        return page

    def _build_settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        form = QFormLayout()
        units = QComboBox()
        units.addItems(["Celsius", "Fahrenheit"])
        units.setCurrentIndex(1 if self.config["fahrenheit"] else 0)
        units.currentIndexChanged.connect(lambda index: self._set_config("fahrenheit", index == 1))
        interval = QDoubleSpinBox()
        interval.setRange(1.0, 10.0)
        interval.setSingleStep(0.5)
        interval.setSuffix(" s")
        interval.setValue(float(self.config["update_interval"]))
        interval.valueChanged.connect(lambda value: self._set_config("update_interval", value))
        form.addRow("Temperature unit", units)
        form.addRow("Overlay refresh", interval)

        minimized = QCheckBox("Start minimized to the system tray")
        minimized.setChecked(bool(self.config["start_minimized"]))
        minimized.toggled.connect(lambda checked: self._set_config("start_minimized", checked, refresh=False))
        autostart = QCheckBox("Launch when I log in")
        autostart.setChecked(self._autostart_file().exists())
        autostart.toggled.connect(self._set_autostart)

        tip = QLabel(
            "Prefer no window at all? Quit this app and run the background service instead:\n"
            "systemctl --user enable --now zeuscast\n"
            "Only one program can talk to the cooler at a time."
        )
        tip.setWordWrap(True)
        tip.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        about = QLabel(f"zeuscast {__version__} · settings in {config_dir()}")
        about.setStyleSheet("color: gray;")

        layout.addLayout(form)
        layout.addWidget(minimized)
        layout.addWidget(autostart)
        layout.addSpacing(12)
        layout.addWidget(tip)
        layout.addStretch()
        layout.addWidget(about)
        return page

    def _build_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self._tray_menu = QMenu()
        show = QAction("Show window", self)
        show.triggered.connect(self._show_window)
        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self.quit)
        self._tray_menu.addAction(show)
        self._tray_menu.addAction(quit_action)
        self.tray.setContextMenu(self._tray_menu)
        self.tray.setToolTip("ZEUS CAST for Linux")
        self.tray.activated.connect(
            lambda reason: self._show_window() if reason == QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.show()

    # Display tab

    def _on_theme_selected(self, row: int) -> None:
        if row < 0:
            return
        key = self.theme_list.item(row).data(Qt.ItemDataRole.UserRole)
        if key != self.config["theme"]:
            self._set_config("theme", key)
        self._rebuild_options(key)

    def _rebuild_options(self, theme_key: str) -> None:
        while self.options_form.rowCount():
            self.options_form.removeRow(0)
        theme = THEMES[theme_key]
        if not theme.options:
            note = QLabel("Nothing to configure: the cooler just plays the background.")
            note.setWordWrap(True)
            self.options_form.addRow(note)
            return
        values = self.config.theme_options(theme)
        for option in theme.options:
            widget = self._option_widget(theme_key, option, values.get(option.key, option.default))
            if option.kind == "bool":
                self.options_form.addRow(widget)
            else:
                self.options_form.addRow(option.label, widget)

    def _option_widget(self, theme_key: str, option: Option, value) -> QWidget:
        def store(new_value) -> None:
            self.config.set_theme_option(theme_key, option.key, new_value)
            self._save_and_refresh()

        if option.kind == "metric":
            combo = QComboBox()
            if option.allow_none:
                combo.addItem("None", "")
            for key, title in METRIC_TITLES.items():
                combo.addItem(title, key)
            combo.setCurrentIndex(max(0, combo.findData(value or "")))
            combo.currentIndexChanged.connect(lambda _: store(combo.currentData() or None))
            return combo
        if option.kind == "bool":
            box = QCheckBox(option.label)
            box.setChecked(bool(value))
            box.toggled.connect(store)
            return box
        if option.kind == "int":
            spin = QSpinBox()
            spin.setRange(option.minimum, option.maximum)
            spin.setValue(int(value))
            spin.valueChanged.connect(store)
            return spin
        editor = QPlainTextEdit(str(value))
        editor.setFixedHeight(110)
        debounce = QTimer(editor)
        debounce.setSingleShot(True)
        debounce.setInterval(500)
        debounce.timeout.connect(lambda: store(editor.toPlainText()))
        editor.textChanged.connect(debounce.start)
        return editor

    def _pick_accent(self) -> None:
        color = QColorDialog.getColor(QColor(self.config["accent"]), self, "Accent colour")
        if color.isValid():
            self._set_config("accent", color.name())
            self._update_accent_button()
            self.setWindowIcon(app_icon(color.name()))
            if self.tray:
                self.tray.setIcon(self.windowIcon())

    def _update_accent_button(self) -> None:
        accent = self.config["accent"]
        self.accent_button.setText(accent)
        self.accent_button.setStyleSheet(f"background: {accent}; color: black; padding: 4px 12px;")

    # Background tab

    def _choose_background(self) -> None:
        start = str(Path(self._selected_background).parent) if self._selected_background else str(Path.home())
        path, _ = QFileDialog.getOpenFileName(self, "Choose a background", start, MEDIA_FILTER)
        if path:
            self._selected_background = path
            self.background_label.setText(path)
            self.background_status.setText("Not uploaded yet.")
            self._load_background_preview()

    def _load_background_preview(self) -> None:
        path = self._selected_background
        model = self.engine.model
        if path and Path(path).exists():
            self._background = media.load_preview(path, model.width, model.height, self.background_mode.currentData())
        else:
            self._background = None
        self._update_preview()

    def _upload_background(self) -> None:
        if not self._selected_background:
            QMessageBox.information(self, "Upload background", "Choose an image or video first.")
            return
        self.upload_button.setEnabled(False)
        self.upload_progress.setRange(0, 0)
        self.upload_progress.show()
        converting = "Converting and uploading…" if media.is_video(self._selected_background) else "Uploading…"
        self.background_status.setText(converting)
        self.engine.upload_background(self._selected_background, self.background_mode.currentData())

    def _on_progress(self, value: int, maximum: int) -> None:
        self.upload_progress.setRange(0, maximum)
        self.upload_progress.setValue(value)

    # Device tab

    def _on_brightness_changed(self, value: int) -> None:
        self.brightness_value.setText(f"{value}%")
        if not self.brightness.isSliderDown():
            self._brightness_timer.start()

    def _apply_brightness(self) -> None:
        self._brightness_timer.stop()
        self.engine.set_brightness(self.brightness.value())

    def _confirm_reboot(self) -> None:
        if QMessageBox.question(self, "Restart display", "Restart the cooler's display controller?") == QMessageBox.StandardButton.Yes:
            self.engine.reboot()

    def _confirm_factory_reset(self) -> None:
        answer = QMessageBox.warning(
            self,
            "Factory reset",
            "Reset the cooler's display to factory settings? Uploaded backgrounds and settings stored on "
            "the device will be erased.",
            QMessageBox.StandardButton.Reset | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Reset:
            self.engine.factory_reset()

    # Settings tab

    @staticmethod
    def _autostart_file() -> Path:
        return Path.home() / ".config" / "autostart" / "zeuscast.desktop"

    def _set_autostart(self, enabled: bool) -> None:
        path = self._autostart_file()
        if not enabled:
            path.unlink(missing_ok=True)
            return
        binary = shutil.which("zeuscast")
        command = shlex.quote(binary) if binary else f"{shlex.quote(sys.executable)} -m zeuscast"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "[Desktop Entry]\nType=Application\nName=ZEUS CAST for Linux\n"
            f"Exec={command} gui --minimized\nX-GNOME-Autostart-enabled=true\n"
        )

    # Engine signals

    def _on_status(self, connected: bool, message: str) -> None:
        self.status_label.setText(message)
        self.status_label.setStyleSheet("color: #3a3;" if connected else "color: gray;")
        self.model_label.setText(self.engine.model.name)
        if connected:
            self._load_background_preview()

    def _on_info(self, info: DeviceInfo) -> None:
        self.info_labels["serial"].setText(info.serial_number or "—")
        self.info_labels["app"].setText(info.app_version or "—")
        self.info_labels["firmware"].setText(info.firmware_version or "—")
        self.info_labels["hardware"].setText(info.hardware_version or "—")
        self.info_labels["space"].setText("—" if info.space is None else str(info.space))
        if info.brightness is not None and not self.brightness.isSliderDown():
            self.brightness.blockSignals(True)
            self.brightness.setValue(info.brightness)
            self.brightness.blockSignals(False)
            self.brightness_value.setText(f"{info.brightness}%")
        if info.rotation in ROTATIONS:
            self.rotation_group.button(info.rotation).setChecked(True)
        if info.startup_logo in (1, 2):
            self.startup_group.button(info.startup_logo).setChecked(True)
        self.clock_position.setCurrentIndex(info.clock_position)

    def _on_overlay(self, image: Image.Image | None) -> None:
        self._overlay = image
        if self.isVisible():
            self._update_preview()

    def _on_task_finished(self, description: str, error: str | None) -> None:
        self.statusBar().showMessage(f"{description}: {error or 'done'}", 8000)
        if description.startswith("Upload background"):
            self.upload_button.setEnabled(True)
            self.upload_progress.hide()
            self.background_status.setText(f"Upload failed: {error}" if error else "Uploaded. The cooler is playing it now.")

    def _update_preview(self) -> None:
        model = self.engine.model
        image = composite(self._background, self._overlay, model.width, model.height)
        self.preview.setPixmap(
            to_pixmap(image).scaled(
                PREVIEW_SIZE, PREVIEW_SIZE, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
        )

    # Helpers and lifecycle

    def _set_config(self, key: str, value, refresh: bool = True) -> None:
        self.config[key] = value
        if refresh:
            self._save_and_refresh()
        else:
            self.config.save()

    def _save_and_refresh(self) -> None:
        self.config.save()
        self.engine.refresh()

    def _show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()
        self._update_preview()

    def closeEvent(self, event) -> None:
        if self.tray is not None and not self._quitting:
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.tray.showMessage(
                    "ZEUS CAST", "Still running in the tray so the cooler stays updated.", QSystemTrayIcon.MessageIcon.Information, 4000
                )
            return
        self.shutdown()
        event.accept()

    def quit(self) -> None:
        self._quitting = True
        self.shutdown()
        QApplication.quit()

    def shutdown(self) -> None:
        if not self._stopped:
            self._stopped = True
            self.engine.stop()


def run(config: Config, port: str | None = None, minimized: bool = False) -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setApplicationName("zeuscast")
    app.setDesktopFileName("zeuscast")
    window = MainWindow(config, port)
    app.setQuitOnLastWindowClosed(window.tray is None)
    app.aboutToQuit.connect(window.shutdown)
    if window.tray is None or not (minimized or config["start_minimized"]):
        window.show()
    return app.exec()
