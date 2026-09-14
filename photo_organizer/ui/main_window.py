from __future__ import annotations

import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QPixmap, QShortcut
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..config import AppConfig, COLLECTION_DIR, DUPLICATES_DIR, OTHER_FILES_DIR
from ..exceptions import OperationCancelled, OrganizerError, ReuseNotSafeError
from ..models import (
    AnalysisResult,
    CopyResult,
    FinalVerificationResult,
    MultimediaVerificationResult,
    ResumeInspection,
)
from ..organizer import OrganizerService
from ..selection_review import (
    PhotoSelectionService,
    REVIEW_TRASH_DIR,
    ReviewSession,
    related_auxiliary_files,
)


def _same_path(left: str | Path, right: str | Path) -> bool:
    return Path(left).expanduser().resolve(strict=False) == Path(right).expanduser().resolve(strict=False)


def _format_ms(value: int) -> str:
    seconds = max(0, int(value / 1000))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


class AnalysisWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        source: str,
        destination: str,
        config: AppConfig,
        cancel_event: threading.Event,
        previous_analysis: AnalysisResult | None = None,
    ):
        super().__init__()
        self.source = source
        self.destination = destination
        self.config = config
        self.cancel_event = cancel_event
        self.previous_analysis = previous_analysis

    @Slot()
    def run(self) -> None:
        try:
            service = OrganizerService(self.config)
            if self.previous_analysis is not None and _same_path(self.source, self.previous_analysis.source):
                try:
                    result = service.recalculate_from_previous_analysis(
                        self.previous_analysis,
                        self.destination,
                        cancel_event=self.cancel_event,
                        progress_callback=self.progress.emit,
                    )
                except ReuseNotSafeError as exc:
                    self.progress.emit(
                        {
                            "phase": "analisis completo",
                            "message": f"No se reutiliza el analisis previo: {exc}",
                            "progress_mode": "busy",
                        }
                    )
                    result = service.analyze(
                        self.source,
                        self.destination,
                        cancel_event=self.cancel_event,
                        progress_callback=self.progress.emit,
                    )
            else:
                result = service.analyze(
                    self.source,
                    self.destination,
                    cancel_event=self.cancel_event,
                    progress_callback=self.progress.emit,
                )
            self.finished.emit(result)
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class CopyWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, analysis: AnalysisResult, config: AppConfig, cancel_event: threading.Event):
        super().__init__()
        self.analysis = analysis
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            service = OrganizerService(self.config)
            result = service.organize(
                self.analysis,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(result)
        except OrganizerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class ResumeInspectionWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        source: str | None,
        destination: str,
        config: AppConfig,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.source = source
        self.destination = destination
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            service = OrganizerService(self.config)
            result = service.inspect_interrupted_organization(
                self.source,
                self.destination,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(result)
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except OrganizerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class ResumeCopyWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        inspection: ResumeInspection,
        config: AppConfig,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.inspection = inspection
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            service = OrganizerService(self.config)
            result = service.resume_interrupted_organization(
                self.inspection,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(result)
        except OrganizerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class FinalVerificationWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, source: str, destination: str, config: AppConfig, cancel_event: threading.Event):
        super().__init__()
        self.source = source
        self.destination = destination
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            service = OrganizerService(self.config)
            result = service.verify_final_before_delete(
                self.source,
                self.destination,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(result)
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except OrganizerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class MultimediaVerificationWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        source: str | None,
        destination: str,
        config: AppConfig,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.source = source
        self.destination = destination
        self.config = config
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            service = OrganizerService(self.config)
            result = service.verify_destination_multimedia(
                self.source,
                self.destination,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(result)
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except OrganizerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class ReviewPrepareWorker(QObject):
    progress = Signal(dict)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        album_root: str,
        include_subfolders: bool,
        media_filter: str,
        ordering: str,
        autoplay_videos: bool,
        cancel_event: threading.Event,
    ):
        super().__init__()
        self.album_root = album_root
        self.include_subfolders = include_subfolders
        self.media_filter = media_filter
        self.ordering = ordering
        self.autoplay_videos = autoplay_videos
        self.cancel_event = cancel_event

    @Slot()
    def run(self) -> None:
        try:
            service = PhotoSelectionService()
            session = service.prepare_album(
                self.album_root,
                include_subfolders=self.include_subfolders,
                media_filter=self.media_filter,
                ordering=self.ordering,
                autoplay_videos=self.autoplay_videos,
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.finished.emit(session)
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except OrganizerError as exc:
            self.failed.emit(str(exc))
        except Exception as exc:
            self.failed.emit(str(exc))


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("App Foto - Organizador local")
        self.resize(900, 680)
        self.analysis_result: AnalysisResult | None = None
        self.resume_inspection: ResumeInspection | None = None
        self.final_verification_result: FinalVerificationResult | None = None
        self.multimedia_verification_result: MultimediaVerificationResult | None = None
        self.review_service = PhotoSelectionService()
        self.review_session: ReviewSession | None = None
        self.review_album_path: Path | None = None
        self.review_include_aux_checkbox: QCheckBox | None = None
        self.review_pixmap_cache: dict[str, QPixmap] = {}
        self.current_thread: QThread | None = None
        self.current_worker: QObject | None = None
        self.cancel_event = threading.Event()
        self.last_progress_monotonic = time.monotonic()
        self.last_progress_phase = "-"
        self.last_progress_path = "-"
        self.last_watchdog_notice = 0.0
        self.technical_log_path: Path | None = None
        self._build_ui()
        self.watchdog_timer = QTimer(self)
        self.watchdog_timer.setInterval(5000)
        self.watchdog_timer.timeout.connect(self._watchdog_tick)
        self.watchdog_timer.start()

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        organizer_tab = QWidget()
        layout = QVBoxLayout(organizer_tab)

        form = QGridLayout()
        self.source_input = QLineEdit()
        self.destination_input = QLineEdit()
        source_button = QPushButton("Seleccionar origen")
        destination_button = QPushButton("Seleccionar destino")
        source_button.clicked.connect(self._choose_source)
        destination_button.clicked.connect(self._choose_destination)

        form.addWidget(QLabel("Carpeta de origen"), 0, 0)
        form.addWidget(self.source_input, 0, 1)
        form.addWidget(source_button, 0, 2)
        form.addWidget(QLabel("Carpeta de destino"), 1, 0)
        form.addWidget(self.destination_input, 1, 1)
        form.addWidget(destination_button, 1, 2)
        layout.addLayout(form)

        safety = QLabel(
            "Los archivos originales no seran modificados. "
            "Todo el resultado se creara mediante copias en la carpeta de destino."
        )
        safety.setWordWrap(True)
        layout.addWidget(safety)

        config_box = QGroupBox("Configuracion")
        config_layout = QHBoxLayout(config_box)
        self.block_size_spin = QSpinBox()
        self.block_size_spin.setRange(1, 64)
        self.block_size_spin.setValue(4)
        self.parallel_spin = QSpinBox()
        self.parallel_spin.setRange(1, 4)
        self.parallel_spin.setValue(1)
        self.margin_spin = QSpinBox()
        self.margin_spin.setRange(0, 100)
        self.margin_spin.setValue(5)
        self.copy_ignored_checkbox = QCheckBox("Copiar archivos no compatibles a OTROS_ARCHIVOS")
        self.copy_ignored_checkbox.setChecked(True)
        config_layout.addWidget(QLabel("Bloque MB"))
        config_layout.addWidget(self.block_size_spin)
        config_layout.addWidget(QLabel("Operaciones simultaneas"))
        config_layout.addWidget(self.parallel_spin)
        config_layout.addWidget(QLabel("Margen espacio %"))
        config_layout.addWidget(self.margin_spin)
        config_layout.addWidget(self.copy_ignored_checkbox)
        config_layout.addStretch(1)
        layout.addWidget(config_box)

        actions = QHBoxLayout()
        self.analyze_button = QPushButton("Analizar")
        self.cancel_button = QPushButton("Cancelar analisis")
        self.copy_button = QPushButton("Comenzar organizacion")
        self.export_button = QPushButton("Exportar informe")
        self.resume_button = QPushButton("Reanudar organizacion interrumpida")
        self.cancel_button.setEnabled(False)
        self.copy_button.setEnabled(False)
        self.export_button.setEnabled(False)
        self.analyze_button.clicked.connect(self._start_analysis)
        self.cancel_button.clicked.connect(self._cancel_current)
        self.copy_button.clicked.connect(self._start_copy)
        self.export_button.clicked.connect(self._open_reports)
        self.resume_button.clicked.connect(self._start_resume_inspection)
        actions.addWidget(self.analyze_button)
        actions.addWidget(self.cancel_button)
        actions.addWidget(self.export_button)
        actions.addWidget(self.copy_button)
        actions.addWidget(self.resume_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        verification_actions = QHBoxLayout()
        self.final_verify_button = QPushButton("Verificación final antes de borrar el origen")
        self.multimedia_verify_button = QPushButton("Verificar integridad multimedia del destino")
        self.final_verify_button.clicked.connect(self._start_final_verification)
        self.multimedia_verify_button.clicked.connect(self._start_multimedia_verification)
        verification_actions.addWidget(self.final_verify_button)
        verification_actions.addWidget(self.multimedia_verify_button)
        verification_actions.addStretch(1)
        layout.addLayout(verification_actions)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.current_file = QLabel("Archivo actual: -")
        self.current_file.setWordWrap(True)
        layout.addWidget(self.progress)
        layout.addWidget(self.current_file)

        counters = QGroupBox("Contadores")
        counters_layout = QGridLayout(counters)
        self.phase_label = QLabel("Fase: -")
        self.reviewed_label = QLabel("Revisados: 0")
        self.photos_label = QLabel("Fotos: 0")
        self.videos_label = QLabel("Videos: 0")
        self.compatible_label = QLabel("Compatibles: 0")
        self.ignored_label = QLabel("Ignorados: 0")
        self.errors_label = QLabel("Errores: 0")
        self.copied_label = QLabel("Copiados: 0")
        self.verified_label = QLabel("Verificados: 0")
        labels = [
            self.phase_label,
            self.reviewed_label,
            self.photos_label,
            self.videos_label,
            self.compatible_label,
            self.ignored_label,
            self.errors_label,
            self.copied_label,
            self.verified_label,
        ]
        for index, label in enumerate(labels):
            counters_layout.addWidget(label, index // 4, index % 4)
        layout.addWidget(counters)

        self.messages = QTextEdit()
        self.messages.setReadOnly(True)
        layout.addWidget(self.messages, stretch=1)

        open_actions = QHBoxLayout()
        open_collection = QPushButton("Abrir COLECCION_ORDENADA")
        open_duplicates = QPushButton("Abrir DUPLICADOS")
        open_other_files = QPushButton("Abrir OTROS_ARCHIVOS")
        open_destination = QPushButton("Abrir destino")
        open_collection.clicked.connect(lambda: self._open_subdir(COLLECTION_DIR))
        open_duplicates.clicked.connect(lambda: self._open_subdir(DUPLICATES_DIR))
        open_other_files.clicked.connect(lambda: self._open_subdir(OTHER_FILES_DIR))
        open_destination.clicked.connect(lambda: self._open_path(Path(self.destination_input.text())))
        open_actions.addWidget(open_collection)
        open_actions.addWidget(open_duplicates)
        open_actions.addWidget(open_other_files)
        open_actions.addWidget(open_destination)
        open_actions.addStretch(1)
        layout.addLayout(open_actions)

        tabs.addTab(organizer_tab, "Organizador local")
        self.selection_tab = self._build_selection_tab()
        tabs.addTab(self.selection_tab, "Seleccion de fotos")
        self.tabs = tabs
        self.setCentralWidget(tabs)

    def _build_selection_tab(self) -> QWidget:
        tab = QWidget()
        tab.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(tab)

        album_form = QGridLayout()
        self.review_album_input = QLineEdit()
        choose_album_button = QPushButton("Elegir carpeta")
        choose_album_button.clicked.connect(self._choose_review_album)
        self.review_include_subfolders = QCheckBox("Incluir subcarpetas")
        self.review_include_subfolders.setChecked(True)
        self.review_autoplay_videos = QCheckBox("Autoplay video")
        self.review_filter_combo = QComboBox()
        self.review_filter_combo.addItem("Revisar fotos y videos", "all")
        self.review_filter_combo.addItem("Solo fotos", "photos")
        self.review_filter_combo.addItem("Solo videos", "videos")
        self.review_order_combo = QComboBox()
        self.review_order_combo.addItem("Fecha ascendente", "date_asc")
        self.review_order_combo.addItem("Fecha descendente", "date_desc")
        self.review_order_combo.addItem("Nombre", "name")
        self.review_order_combo.addItem("Aleatorio", "random")
        self.review_start_button = QPushButton("Cargar album")
        self.review_continue_button = QPushButton("Continuar sesion anterior")
        self.review_start_button.clicked.connect(self._start_review_prepare)
        self.review_continue_button.clicked.connect(self._continue_review_session)
        album_form.addWidget(QLabel("Album"), 0, 0)
        album_form.addWidget(self.review_album_input, 0, 1)
        album_form.addWidget(choose_album_button, 0, 2)
        album_form.addWidget(self.review_include_subfolders, 1, 0)
        album_form.addWidget(self.review_filter_combo, 1, 1)
        album_form.addWidget(self.review_order_combo, 1, 2)
        album_form.addWidget(self.review_autoplay_videos, 2, 0)
        album_form.addWidget(self.review_start_button, 2, 1)
        album_form.addWidget(self.review_continue_button, 2, 2)
        layout.addLayout(album_form)

        self.review_album_summary = QLabel("Selecciona una carpeta para comenzar.")
        self.review_album_summary.setWordWrap(True)
        layout.addWidget(self.review_album_summary)

        self.review_viewer_stack = QStackedWidget()
        self.review_image_label = QLabel("Sin archivo cargado")
        self.review_image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.review_image_label.setStyleSheet("background:#111; color:#ddd;")
        self.review_image_label.setMinimumHeight(300)
        self.review_image_label.setWordWrap(True)
        self.review_video_widget = QVideoWidget()
        self.review_video_widget.setStyleSheet("background:#111;")
        self.review_viewer_stack.addWidget(self.review_image_label)
        self.review_viewer_stack.addWidget(self.review_video_widget)
        layout.addWidget(self.review_viewer_stack, stretch=1)

        self.review_player = QMediaPlayer(self)
        self.review_audio = QAudioOutput(self)
        self.review_player.setAudioOutput(self.review_audio)
        self.review_player.setVideoOutput(self.review_video_widget)
        self.review_player.positionChanged.connect(self._review_video_position_changed)
        self.review_player.durationChanged.connect(self._review_video_duration_changed)

        video_controls = QHBoxLayout()
        self.review_play_button = QPushButton("Play/Pausa")
        self.review_play_button.clicked.connect(self._toggle_review_video)
        self.review_video_slider = QSlider(Qt.Orientation.Horizontal)
        self.review_video_slider.sliderMoved.connect(self.review_player.setPosition)
        self.review_volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.review_volume_slider.setRange(0, 100)
        self.review_volume_slider.setValue(70)
        self.review_audio.setVolume(0.7)
        self.review_volume_slider.valueChanged.connect(lambda value: self.review_audio.setVolume(value / 100))
        self.review_duration_label = QLabel("00:00 / 00:00")
        video_controls.addWidget(self.review_play_button)
        video_controls.addWidget(self.review_video_slider, stretch=1)
        video_controls.addWidget(QLabel("Volumen"))
        video_controls.addWidget(self.review_volume_slider)
        video_controls.addWidget(self.review_duration_label)
        layout.addLayout(video_controls)

        info_box = QGroupBox("Archivo actual")
        info_layout = QGridLayout(info_box)
        self.review_name_label = QLabel("-")
        self.review_date_label = QLabel("-")
        self.review_resolution_label = QLabel("-")
        self.review_size_label = QLabel("-")
        self.review_relative_label = QLabel("-")
        self.review_position_label = QLabel("0 / 0")
        self.review_kept_label = QLabel("Conservados: 0")
        self.review_discarded_label = QLabel("Descartados: 0")
        self.review_pending_label = QLabel("Pendientes: 0")
        self.review_trash_size_label = QLabel("Papelera: 0 bytes")
        info_layout.addWidget(QLabel("Nombre"), 0, 0)
        info_layout.addWidget(self.review_name_label, 0, 1)
        info_layout.addWidget(QLabel("Fecha"), 1, 0)
        info_layout.addWidget(self.review_date_label, 1, 1)
        info_layout.addWidget(QLabel("Resolucion"), 2, 0)
        info_layout.addWidget(self.review_resolution_label, 2, 1)
        info_layout.addWidget(QLabel("Tamano"), 3, 0)
        info_layout.addWidget(self.review_size_label, 3, 1)
        info_layout.addWidget(QLabel("Ruta relativa"), 4, 0)
        info_layout.addWidget(self.review_relative_label, 4, 1)
        info_layout.addWidget(self.review_position_label, 0, 2)
        info_layout.addWidget(self.review_kept_label, 1, 2)
        info_layout.addWidget(self.review_discarded_label, 2, 2)
        info_layout.addWidget(self.review_pending_label, 3, 2)
        info_layout.addWidget(self.review_trash_size_label, 4, 2)
        layout.addWidget(info_box)

        self.review_progress = QProgressBar()
        self.review_progress.setRange(0, 100)
        layout.addWidget(self.review_progress)

        aux_layout = QHBoxLayout()
        self.review_aux_label = QLabel("Sin auxiliares relacionados.")
        self.review_move_aux = QCheckBox("Moverlos junto al archivo principal")
        self.review_move_aux.setChecked(True)
        aux_layout.addWidget(self.review_aux_label)
        aux_layout.addWidget(self.review_move_aux)
        aux_layout.addStretch(1)
        layout.addLayout(aux_layout)

        action_layout = QHBoxLayout()
        self.review_keep_button = QPushButton("CONSERVAR")
        self.review_discard_button = QPushButton("DESCARTAR")
        self.review_pending_button = QPushButton("PENDIENTE")
        self.review_undo_button = QPushButton("DESHACER")
        self.review_open_button = QPushButton("ABRIR EN EXPLORADOR")
        self.review_keep_button.clicked.connect(self._review_keep)
        self.review_discard_button.clicked.connect(self._review_discard)
        self.review_pending_button.clicked.connect(self._review_skip)
        self.review_undo_button.clicked.connect(self._review_undo)
        self.review_open_button.clicked.connect(self._review_open_current)
        action_layout.addWidget(self.review_keep_button)
        action_layout.addWidget(self.review_discard_button)
        action_layout.addWidget(self.review_pending_button)
        action_layout.addWidget(self.review_undo_button)
        action_layout.addWidget(self.review_open_button)
        action_layout.addStretch(1)
        layout.addLayout(action_layout)

        trash_box = QGroupBox(f"{REVIEW_TRASH_DIR}")
        trash_layout = QGridLayout(trash_box)
        self.review_trash_search = QLineEdit()
        self.review_trash_search.setPlaceholderText("Buscar por nombre o ruta")
        self.review_trash_search.textChanged.connect(self._refresh_review_trash)
        self.review_trash_list = QListWidget()
        self.review_trash_stats = QLabel("0 archivos, 0 bytes")
        open_trash_button = QPushButton("Abrir papelera")
        restore_selected_button = QPushButton("Restaurar seleccionados")
        restore_all_button = QPushButton("Restaurar todos")
        review_discarded_button = QPushButton("Revisar descartados")
        delete_forever_button = QPushButton("Eliminar definitivamente")
        open_trash_button.clicked.connect(self._open_review_trash)
        restore_selected_button.clicked.connect(self._restore_selected_trash)
        restore_all_button.clicked.connect(self._restore_all_trash)
        review_discarded_button.clicked.connect(self._review_discarded_only)
        delete_forever_button.clicked.connect(self._delete_trash_forever)
        trash_layout.addWidget(self.review_trash_search, 0, 0, 1, 3)
        trash_layout.addWidget(self.review_trash_list, 1, 0, 1, 3)
        trash_layout.addWidget(self.review_trash_stats, 2, 0, 1, 3)
        trash_layout.addWidget(open_trash_button, 3, 0)
        trash_layout.addWidget(restore_selected_button, 3, 1)
        trash_layout.addWidget(restore_all_button, 3, 2)
        trash_layout.addWidget(review_discarded_button, 4, 0)
        trash_layout.addWidget(delete_forever_button, 4, 1)
        layout.addWidget(trash_box)

        self._install_review_shortcuts(tab)
        return tab

    def _config(self) -> AppConfig:
        return AppConfig(
            block_size=self.block_size_spin.value() * 1024 * 1024,
            max_parallel_operations=self.parallel_spin.value(),
            safety_margin_percent=self.margin_spin.value(),
            copy_ignored_files=self.copy_ignored_checkbox.isChecked(),
        )

    def _install_review_shortcuts(self, parent: QWidget) -> None:
        shortcuts = [
            (Qt.Key.Key_Right, self._review_keep),
            (Qt.Key.Key_Left, self._review_discard),
            (Qt.Key.Key_Down, self._review_skip),
            (Qt.Key.Key_Up, self._review_previous),
            (Qt.Key.Key_Space, self._toggle_review_video),
            (Qt.Key.Key_Escape, self._pause_review_session),
        ]
        self.review_shortcuts = []
        for key, slot in shortcuts:
            shortcut = QShortcut(QKeySequence(key), parent)
            shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
            shortcut.activated.connect(slot)
            self.review_shortcuts.append(shortcut)
        undo_shortcut = QShortcut(QKeySequence("Ctrl+Z"), parent)
        undo_shortcut.setContext(Qt.ShortcutContext.ApplicationShortcut)
        undo_shortcut.activated.connect(self._review_undo)
        self.review_shortcuts.append(undo_shortcut)

    def _choose_review_album(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleccionar album para revisar")
        if path:
            self.review_album_input.setText(path)
            self._update_review_continue_hint()

    def _start_review_prepare(self) -> None:
        if self.current_thread is not None:
            QMessageBox.warning(self, "Operacion activa", "Espera a que termine la operacion actual.")
            return
        if not self.review_album_input.text():
            QMessageBox.warning(self, "Falta carpeta", "Selecciona un album.")
            return
        self._pause_review_video_only()
        self.review_album_summary.setText("Enumerando album en segundo plano...")
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=False)
        worker = ReviewPrepareWorker(
            self.review_album_input.text(),
            self.review_include_subfolders.isChecked(),
            str(self.review_filter_combo.currentData()),
            str(self.review_order_combo.currentData()),
            self.review_autoplay_videos.isChecked(),
            self.cancel_event,
        )
        self._run_worker(worker, worker.run, self._review_session_ready)

    def _continue_review_session(self) -> None:
        if self.current_thread is not None:
            QMessageBox.warning(self, "Operacion activa", "Espera a que termine la operacion actual.")
            return
        if not self.review_album_input.text():
            QMessageBox.warning(self, "Falta carpeta", "Selecciona un album.")
            return
        try:
            session = self.review_service.latest_session_for_album(Path(self.review_album_input.text()))
            if session is None:
                QMessageBox.information(self, "Sin sesion", "No hay una sesion anterior para esa carpeta.")
                return
            audits = self.review_service.audit_incomplete_movements(session)
            if audits:
                details = ", ".join(f"{audit.source_path.name}: {audit.state}" for audit in audits[:5])
                self.review_album_summary.setText(f"Movimientos interrumpidos auditados: {details}")
            self._review_session_ready(session)
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo continuar", str(exc))

    @Slot(object)
    def _review_session_ready(self, session: ReviewSession) -> None:
        self.review_session = session
        self.review_pixmap_cache.clear()
        self._set_busy(False, copying=False)
        self.review_album_path = session.album_root
        summary = self.review_service.summary(session)
        self.review_album_summary.setText(
            f"Encontrados {summary.total_files} archivos ({summary.total_size} bytes, "
            f"{summary.total_size_gb}). Continuar sesion: {min(session.current_index + 1, summary.total_files)} "
            f"de {summary.total_files}."
        )
        self._display_review_current()
        self._refresh_review_trash()

    def _update_review_continue_hint(self) -> None:
        try:
            session = self.review_service.latest_session_for_album(Path(self.review_album_input.text()))
        except Exception:
            session = None
        if session is None:
            self.review_album_summary.setText("No hay sesion anterior detectada para esta carpeta.")
            return
        self.review_album_summary.setText(
            f"Sesion anterior disponible: {min(session.current_index + 1, session.total_files)} de {session.total_files}."
        )

    def _display_review_current(self) -> None:
        session = self.review_session
        if session is None:
            return
        summary = self.review_service.summary(session)
        self.review_kept_label.setText(f"Conservados: {summary.kept}")
        self.review_discarded_label.setText(f"Descartados: {summary.discarded}")
        self.review_pending_label.setText(f"Pendientes: {summary.pending}")
        self.review_trash_size_label.setText(f"Papelera: {summary.trash_size} bytes ({summary.trash_size_gb})")
        self.review_progress.setValue(int((summary.kept + summary.discarded + summary.pending) * 100 / summary.total_files) if summary.total_files else 0)

        file = self.review_service.current_file(session)
        if file is None or session.current_index >= len(session.files):
            self._pause_review_video_only()
            self.review_image_label.setText("Revision completada.")
            self.review_viewer_stack.setCurrentWidget(self.review_image_label)
            self.review_position_label.setText(f"{summary.total_files} / {summary.total_files}")
            return

        self.review_position_label.setText(f"{file.position + 1} / {summary.total_files}")
        self.review_name_label.setText(file.name)
        self.review_size_label.setText(f"{file.size} bytes")
        self.review_relative_label.setText(str(file.relative_path))
        self.review_date_label.setText(datetime.fromtimestamp(file.modified_time).isoformat(timespec="seconds"))
        self.review_resolution_label.setText("-")

        aux = related_auxiliary_files(file.current_path) if file.current_path.exists() else []
        if aux:
            self.review_aux_label.setText(f"Tambien se han encontrado {len(aux)} archivos auxiliares relacionados.")
            self.review_move_aux.setEnabled(True)
            self.review_move_aux.setChecked(True)
        else:
            self.review_aux_label.setText("Sin auxiliares relacionados.")
            self.review_move_aux.setEnabled(False)

        if file.media_type == "video":
            self.review_viewer_stack.setCurrentWidget(self.review_video_widget)
            self.review_player.setSource(QUrl.fromLocalFile(str(file.current_path)))
            if session.autoplay_videos:
                self.review_player.play()
            return

        self._pause_review_video_only()
        self.review_viewer_stack.setCurrentWidget(self.review_image_label)
        pixmap = self._review_pixmap(file.current_path)
        if pixmap.isNull():
            self.review_image_label.setText(
                f"No se pudo visualizar este formato.\n{file.current_path}\n"
                "Puedes conservar, descartar, dejar pendiente o abrir externamente."
            )
            self.review_resolution_label.setText("No disponible")
            return
        self.review_resolution_label.setText(f"{pixmap.width()} x {pixmap.height()}")
        scaled = pixmap.scaled(
            self.review_image_label.size(),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self.review_image_label.setPixmap(scaled)
        self._preload_review_images(file.position + 1)

    def _review_pixmap(self, path: Path) -> QPixmap:
        key = str(path)
        pixmap = self.review_pixmap_cache.get(key)
        if pixmap is None:
            pixmap = QPixmap(str(path))
            if not pixmap.isNull():
                self.review_pixmap_cache[key] = pixmap
                self._trim_review_cache()
        return pixmap

    def _preload_review_images(self, start_index: int) -> None:
        session = self.review_session
        if session is None:
            return
        loaded = 0
        for file in session.files[start_index : start_index + 4]:
            if loaded >= 3:
                break
            if file.media_type != "foto":
                continue
            if str(file.current_path) not in self.review_pixmap_cache:
                pixmap = QPixmap(str(file.current_path))
                if not pixmap.isNull():
                    self.review_pixmap_cache[str(file.current_path)] = pixmap
                    loaded += 1
        self._trim_review_cache()

    def _trim_review_cache(self) -> None:
        if self.review_session is None or len(self.review_pixmap_cache) <= 6:
            return
        keep_paths = {
            str(file.current_path)
            for file in self.review_session.files[
                max(0, self.review_session.current_index - 1) : self.review_session.current_index + 4
            ]
        }
        for key in list(self.review_pixmap_cache):
            if key not in keep_paths:
                self.review_pixmap_cache.pop(key, None)

    def _review_keep(self) -> None:
        if not self._review_action_allowed():
            return
        self.review_session = self.review_service.keep_current(self.review_session)
        self._display_review_current()

    def _review_skip(self) -> None:
        if not self._review_action_allowed():
            return
        self.review_session = self.review_service.skip_current(self.review_session)
        self._display_review_current()

    def _review_discard(self) -> None:
        if not self._review_action_allowed():
            return
        try:
            self.review_session = self.review_service.discard_current(
                self.review_session,
                move_auxiliaries=self.review_move_aux.isChecked(),
            )
            self._display_review_current()
            self._refresh_review_trash()
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo descartar", str(exc))

    def _review_previous(self) -> None:
        if self.review_session is None:
            return
        self.review_session.current_index = self.review_service.previous_index(self.review_session)
        self._display_review_current()

    def _review_undo(self) -> None:
        if self.review_session is None:
            return
        try:
            self.review_session = self.review_service.undo_last(self.review_session)
            self._display_review_current()
            self._refresh_review_trash()
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo deshacer", str(exc))

    def _review_action_allowed(self) -> bool:
        if self.current_thread is not None:
            return False
        if hasattr(self, "tabs") and self.tabs.currentWidget() is not self.selection_tab:
            return False
        return self.review_session is not None and self.review_service.current_file(self.review_session) is not None

    def _review_open_current(self) -> None:
        if self.review_session is None:
            return
        file = self.review_service.current_file(self.review_session)
        if file is not None:
            self._open_path(file.current_path.parent)

    def _pause_review_session(self) -> None:
        self._pause_review_video_only()
        if self.review_session is not None:
            self.review_album_summary.setText("Sesion pausada y guardada.")

    def _pause_review_video_only(self) -> None:
        if hasattr(self, "review_player"):
            self.review_player.pause()

    def _toggle_review_video(self) -> None:
        if not hasattr(self, "review_player"):
            return
        if hasattr(self, "tabs") and self.tabs.currentWidget() is not self.selection_tab:
            return
        if self.review_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.review_player.pause()
        else:
            self.review_player.play()

    def _review_video_position_changed(self, position: int) -> None:
        self.review_video_slider.setValue(position)
        self._update_review_duration_label()

    def _review_video_duration_changed(self, duration: int) -> None:
        self.review_video_slider.setRange(0, duration)
        self._update_review_duration_label()

    def _update_review_duration_label(self) -> None:
        position = self.review_player.position()
        duration = self.review_player.duration()
        self.review_duration_label.setText(f"{_format_ms(position)} / {_format_ms(duration)}")

    def _refresh_review_trash(self) -> None:
        self.review_trash_list.clear()
        if self.review_session is None:
            self.review_trash_stats.setText("0 archivos, 0 bytes")
            return
        query = self.review_trash_search.text().lower()
        files = self.review_service.trash_files(self.review_session.review_root)
        shown = []
        for path in files:
            text = str(path)
            if query and query not in text.lower():
                continue
            self.review_trash_list.addItem(text)
            shown.append(path)
        total_size = sum(path.stat().st_size for path in files if path.exists())
        self.review_trash_stats.setText(f"{len(files)} archivos, {total_size} bytes")

    def _open_review_trash(self) -> None:
        if self.review_session is not None:
            self._open_path(self.review_session.trash_root)

    def _restore_selected_trash(self) -> None:
        if self.review_session is None:
            return
        for item in self.review_trash_list.selectedItems():
            try:
                self.review_service.restore_trash_file(self.review_session.review_root, Path(item.text()))
            except Exception as exc:
                QMessageBox.warning(self, "No se pudo restaurar", str(exc))
                break
        self._refresh_review_trash()

    def _restore_all_trash(self) -> None:
        if self.review_session is None:
            return
        answer = QMessageBox.question(
            self,
            "Restaurar todos",
            "Quieres restaurar todos los archivos de PAPELERA_REVISION?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.review_service.restore_all_trash(self.review_session.review_root)
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo restaurar todo", str(exc))
        self._refresh_review_trash()

    def _review_discarded_only(self) -> None:
        if self.review_session is None:
            return
        self.review_album_input.setText(str(self.review_session.trash_root))
        self._start_review_prepare()

    def _delete_trash_forever(self) -> None:
        if self.review_session is None:
            return
        files = self.review_service.trash_files(self.review_session.review_root)
        total_size = sum(path.stat().st_size for path in files if path.exists())
        box = QMessageBox(self)
        box.setWindowTitle("Eliminar definitivamente")
        box.setText(f"Se eliminaran definitivamente {len(files)} archivos ({total_size} bytes).")
        confirmation_checkbox = QCheckBox("Entiendo que esta accion no se puede deshacer.")
        box.setCheckBox(confirmation_checkbox)
        box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
        if box.exec() != QMessageBox.StandardButton.Ok or not confirmation_checkbox.isChecked():
            return
        typed, ok = QInputDialog.getText(self, "Confirmacion", "Escribe ELIMINAR:")
        if not ok or typed != "ELIMINAR":
            return
        second = QMessageBox.question(
            self,
            "Confirmacion final",
            "Confirmas la eliminacion definitiva?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        try:
            deleted = self.review_service.permanently_delete_trash(
                self.review_session.review_root,
                confirmation_checkbox.isChecked(),
                typed,
                second == QMessageBox.StandardButton.Yes,
            )
            QMessageBox.information(self, "Papelera", f"Eliminados definitivamente: {deleted}")
        except Exception as exc:
            QMessageBox.warning(self, "No se pudo eliminar", str(exc))
        self._refresh_review_trash()

    def _choose_source(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de origen")
        if path:
            self.source_input.setText(path)

    def _choose_destination(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Seleccionar carpeta de destino")
        if path:
            self.destination_input.setText(path)

    def _start_analysis(self) -> None:
        if not self.source_input.text() or not self.destination_input.text():
            QMessageBox.warning(self, "Faltan carpetas", "Selecciona origen y destino.")
            return
        previous_analysis = self.analysis_result
        self.analysis_result = None
        self.resume_inspection = None
        self.messages.clear()
        self.progress.setValue(0)
        self.progress.setRange(0, 0)
        self.technical_log_path = None
        self.last_progress_monotonic = time.monotonic()
        self.last_progress_phase = "arrancando analisis"
        self.last_progress_path = self.source_input.text()
        self._append_message("Arrancando analisis en segundo plano...")
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=False)
        worker = AnalysisWorker(
            self.source_input.text(),
            self.destination_input.text(),
            self._config(),
            self.cancel_event,
            previous_analysis,
        )
        self._run_worker(worker, worker.run, self._analysis_finished)

    def _start_copy(self) -> None:
        if self.analysis_result is None:
            return
        if not self.analysis_result.summary.get("espacio_suficiente", False):
            QMessageBox.warning(self, "Espacio insuficiente", "No hay espacio libre suficiente en el destino.")
            return
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=True)
        worker = CopyWorker(self.analysis_result, self._config(), self.cancel_event)
        self._run_worker(worker, worker.run, self._copy_finished)

    def _start_resume_inspection(self) -> None:
        if not self.destination_input.text():
            QMessageBox.warning(self, "Falta destino", "Selecciona la carpeta de destino anterior.")
            return
        self.analysis_result = None
        self.resume_inspection = None
        self.messages.clear()
        self.progress.setValue(0)
        self.progress.setRange(0, 0)
        self.technical_log_path = None
        self.last_progress_monotonic = time.monotonic()
        self.last_progress_phase = "arrancando reanudacion"
        self.last_progress_path = self.destination_input.text()
        self._append_message("Inspeccionando una organizacion interrumpida en segundo plano...")
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=False)
        source = self.source_input.text().strip() or None
        worker = ResumeInspectionWorker(
            source,
            self.destination_input.text(),
            self._config(),
            self.cancel_event,
        )
        self._run_worker(worker, worker.run, self._resume_inspection_finished)

    def _start_resume_copy(self) -> None:
        if self.resume_inspection is None:
            return
        if not self.resume_inspection.summary.get("espacio_suficiente_para_reanudar", False):
            QMessageBox.warning(self, "Espacio insuficiente", "No hay espacio libre suficiente para reanudar.")
            return
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=True)
        worker = ResumeCopyWorker(self.resume_inspection, self._config(), self.cancel_event)
        self._run_worker(worker, worker.run, self._copy_finished)

    def _start_final_verification(self) -> None:
        if not self.source_input.text() or not self.destination_input.text():
            QMessageBox.warning(self, "Faltan carpetas", "Selecciona origen y destino.")
            return
        answer = QMessageBox.question(
            self,
            "Verificación final",
            (
                "Esta comprobación volverá a leer todos los archivos del origen y sus "
                "destinos completos para comparar tamaño y hash. No copia, mueve, "
                "renombra ni borra archivos.\n\n"
                "Quieres iniciar la verificación final ahora?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.analysis_result = None
        self.resume_inspection = None
        self.final_verification_result = None
        self.multimedia_verification_result = None
        self.messages.clear()
        self.progress.setValue(0)
        self.progress.setRange(0, 0)
        self.technical_log_path = None
        self.last_progress_monotonic = time.monotonic()
        self.last_progress_phase = "arrancando verificación final"
        self.last_progress_path = self.source_input.text()
        self._append_message("Iniciando verificación final independiente en segundo plano...")
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=False)
        worker = FinalVerificationWorker(
            self.source_input.text(),
            self.destination_input.text(),
            self._config(),
            self.cancel_event,
        )
        self._run_worker(worker, worker.run, self._final_verification_finished)

    def _start_multimedia_verification(self) -> None:
        if not self.destination_input.text():
            QMessageBox.warning(self, "Falta destino", "Selecciona la carpeta de destino.")
            return
        answer = QMessageBox.question(
            self,
            "Verificación multimedia",
            (
                "Esta prueba intentará validar la estructura de fotos y leer vídeos con "
                "ffprobe o ffmpeg si están instalados. Puede tardar mucho y no modifica archivos.\n\n"
                "Quieres iniciar la verificación multimedia ahora?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.multimedia_verification_result = None
        self.messages.clear()
        self.progress.setValue(0)
        self.progress.setRange(0, 0)
        self.technical_log_path = None
        self.last_progress_monotonic = time.monotonic()
        self.last_progress_phase = "arrancando verificación multimedia"
        self.last_progress_path = self.destination_input.text()
        self._append_message("Iniciando verificación multimedia opcional en segundo plano...")
        self.cancel_event = threading.Event()
        self._set_busy(True, copying=False)
        source = self.source_input.text().strip() or None
        worker = MultimediaVerificationWorker(
            source,
            self.destination_input.text(),
            self._config(),
            self.cancel_event,
        )
        self._run_worker(worker, worker.run, self._multimedia_verification_finished)

    def _run_worker(self, worker: QObject, run_slot, finished_slot) -> None:
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(run_slot)
        worker.progress.connect(self._handle_progress)
        worker.finished.connect(finished_slot)
        worker.failed.connect(self._worker_failed)
        worker.finished.connect(thread.quit)
        worker.failed.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        thread.finished.connect(self._worker_thread_finished)
        self.current_thread = thread
        self.current_worker = worker
        thread.start()

    def _set_busy(self, busy: bool, copying: bool) -> None:
        self.analyze_button.setEnabled(not busy)
        self.resume_button.setEnabled(not busy)
        self.final_verify_button.setEnabled(not busy)
        self.multimedia_verify_button.setEnabled(not busy)
        if hasattr(self, "selection_tab"):
            self.selection_tab.setEnabled(not busy)
        self.copy_button.setEnabled(not busy and self.analysis_result is not None)
        self.export_button.setEnabled(not busy and self.analysis_result is not None)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setText("Cancelar copia" if copying else "Cancelar analisis")

    def _cancel_current(self) -> None:
        self.cancel_event.set()
        self._append_message("Cancelacion solicitada. La operacion actual terminara antes de parar.")

    @Slot(dict)
    def _handle_progress(self, event: dict[str, Any]) -> None:
        self.last_progress_monotonic = time.monotonic()
        phase = event.get("phase")
        if phase:
            self.last_progress_phase = str(phase)
            self.phase_label.setText(f"Fase: {phase}")
        current_file = event.get("current_file")
        if current_file:
            self.last_progress_path = str(current_file)
            self.current_file.setText(f"Archivo actual: {current_file}")
        if event.get("log_path"):
            self.technical_log_path = Path(str(event["log_path"]))
        if "reviewed" in event:
            self.reviewed_label.setText(f"Revisados: {event['reviewed']}")
        if "compatible" in event:
            self.compatible_label.setText(f"Compatibles: {event['compatible']}")
        if "photos" in event:
            self.photos_label.setText(f"Fotos: {event['photos']}")
        if "videos" in event:
            self.videos_label.setText(f"Videos: {event['videos']}")
        if "ignored" in event:
            self.ignored_label.setText(f"Ignorados: {event['ignored']}")
        if "errors" in event:
            self.errors_label.setText(f"Errores: {event['errors']}")
        if "copied_files" in event:
            self.copied_label.setText(f"Copiados: {event['copied_files']}")
        if "verified_files" in event:
            self.verified_label.setText(f"Verificados: {event['verified_files']}")
        if event.get("progress_mode") == "busy":
            self.progress.setRange(0, 0)
        elif event.get("progress_mode") == "percent":
            self.progress.setRange(0, 100)
        if "current" in event and "total" in event and event["total"]:
            self.progress.setRange(0, 100)
            self.progress.setValue(int(event["current"] * 100 / event["total"]))
        if event.get("message") and event.get("ui_message", True):
            self._append_message(str(event["message"]))

    @Slot(object)
    def _analysis_finished(self, result: AnalysisResult) -> None:
        self.analysis_result = result
        self._set_busy(False, copying=False)
        self.progress.setRange(0, 100)
        summary = result.summary
        self.photos_label.setText(f"Fotos: {summary['fotos']}")
        self.videos_label.setText(f"Videos: {summary['videos']}")
        self.compatible_label.setText(f"Compatibles: {summary['total_archivos_compatibles']}")
        self.ignored_label.setText(f"Ignorados: {summary['archivos_ignorados']}")
        self.errors_label.setText(f"Errores: {summary['errores']}")
        self.progress.setValue(100)
        self._append_message(self._format_summary(summary))
        self._append_message(f"Informe JSON: {result.report_paths.get('json')}")
        self.export_button.setEnabled(True)
        self.copy_button.setEnabled(summary.get("espacio_suficiente", False))
        if not summary.get("espacio_suficiente", False):
            self._append_message("Organizacion bloqueada: no hay espacio libre suficiente.")

    @Slot(object)
    def _resume_inspection_finished(self, inspection: ResumeInspection) -> None:
        self.resume_inspection = inspection
        self._set_busy(False, copying=False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self._append_message(self._format_resume_summary(inspection))
        self.export_button.setEnabled(True)

        summary = inspection.summary
        actionable = (
            summary.get("pendientes", 0)
            + summary.get("incompletos", 0)
            + summary.get("corruptos", 0)
        )
        if not summary.get("espacio_suficiente_para_reanudar", False):
            self._append_message("Reanudacion bloqueada: no hay espacio libre suficiente.")
            return
        if actionable == 0:
            self._append_message("No quedan copias pendientes para reanudar.")
            return

        answer = QMessageBox.question(
            self,
            "Continuar reanudacion",
            (
                "La inspeccion ha terminado. Si continuas, se reutilizaran los archivos "
                "verificados, se eliminaran solo temporales/copias invalidas del destino "
                "y se copiaran las rutas pendientes. El origen no se modificara.\n\n"
                f"Copias por crear o rehacer: {actionable}\n"
                "Quieres continuar ahora?"
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start_resume_copy()

    @Slot(object)
    def _copy_finished(self, result: CopyResult) -> None:
        self._set_busy(False, copying=True)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.copied_label.setText(f"Copiados: {result.copied_files}")
        self.verified_label.setText(f"Verificados: {result.verified_files}")
        self._append_message("Organizacion finalizada." if not result.cancelled else "Organizacion cancelada de forma segura.")
        self._append_message(f"Archivos copiados: {result.copied_files}")
        self._append_message(f"Archivos verificados: {result.verified_files}")
        self._append_message(f"Archivos reutilizados ya verificados: {result.reused_files}")
        self._append_message(f"Archivos fallidos: {result.failed_files}")
        self._append_message(f"Archivos no compatibles copiados: {result.copied_ignored_files}")
        self._append_message(f"Archivos no compatibles reutilizados: {result.reused_ignored_files}")
        self._append_message(f"Archivos no compatibles fallidos: {result.failed_ignored_files}")
        self._append_message(f"Espacio total copiado: {result.copied_bytes} bytes")
        self._append_message(f"Informe final: {result.report_paths.get('json')}")
        if result.report_paths:
            self.export_button.setEnabled(True)

    @Slot(object)
    def _final_verification_finished(self, result: FinalVerificationResult) -> None:
        self.final_verification_result = result
        self._set_busy(False, copying=False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.reviewed_label.setText(f"Revisados: {result.summary['archivos_originales_encontrados']}")
        self.errors_label.setText(f"Errores: {result.summary['errores_lectura']}")
        self.verified_label.setText(f"Verificados: {result.summary['archivos_identicos_por_hash']}")
        self._append_message(self._format_final_verification_summary(result))
        self._append_message(f"CSV: {result.report_paths.get('csv')}")
        self._append_message(f"JSON: {result.report_paths.get('json')}")
        self._append_message(f"Registro tecnico: {result.report_paths.get('log')}")
        self.export_button.setEnabled(True)
        if result.passed:
            QMessageBox.information(self, "Verificación final", "VERIFICACIÓN FINAL SUPERADA")
        else:
            QMessageBox.warning(self, "Verificación final", "NO ES SEGURO BORRAR EL ORIGEN")

    @Slot(object)
    def _multimedia_verification_finished(self, result: MultimediaVerificationResult) -> None:
        self.multimedia_verification_result = result
        self._set_busy(False, copying=False)
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        self.reviewed_label.setText(f"Revisados: {result.summary['archivos_multimedia_revisados']}")
        self.errors_label.setText(f"Errores: {result.summary['errores']}")
        self.verified_label.setText(f"Verificados: {result.summary['correctos']}")
        self._append_message(self._format_multimedia_summary(result))
        self._append_message(f"CSV: {result.report_paths.get('csv')}")
        self._append_message(f"JSON: {result.report_paths.get('json')}")
        self._append_message(f"Registro tecnico: {result.report_paths.get('log')}")
        self.export_button.setEnabled(True)

    @Slot(str)
    def _worker_failed(self, message: str) -> None:
        self._set_busy(False, copying=False)
        self.progress.setRange(0, 100)
        self._append_message(f"Error: {message}")
        QMessageBox.warning(self, "Operacion no completada", message)

    @Slot()
    def _worker_thread_finished(self) -> None:
        self.current_worker = None
        self.current_thread = None

    @Slot()
    def _watchdog_tick(self) -> None:
        if not self.cancel_button.isEnabled():
            return
        now = time.monotonic()
        idle_seconds = int(now - self.last_progress_monotonic)
        if idle_seconds < 15 or now - self.last_watchdog_notice < 15:
            return
        self.last_watchdog_notice = now
        self._append_message(
            "Vigilancia: sin nuevos eventos durante "
            f"{idle_seconds}s. Ultima fase: {self.last_progress_phase}. "
            f"Ruta/operacion: {self.last_progress_path}"
        )

    def _append_message(self, message: str) -> None:
        self.messages.append(message)
        if self.technical_log_path is None:
            return
        try:
            with self.technical_log_path.open("a", encoding="utf-8") as handle:
                handle.write(f"[UI] {message}\n")
        except OSError:
            pass

    def _format_summary(self, summary: dict[str, Any]) -> str:
        def size_line(label: str, key: str) -> str:
            return f"- {label}: {summary[key]} bytes ({summary.get(f'{key}_gb', '0.00 GB')})"

        lines = [
            "Resumen del analisis:",
            f"- Total encontrados: {summary['total_archivos_encontrados']}",
            f"- Compatibles: {summary['total_archivos_compatibles']}",
            f"- Fotos: {summary['fotos']}",
            f"- Videos: {summary['videos']}",
            f"- Archivos unicos: {summary['archivos_unicos']}",
            f"- Grupos duplicados: {summary['grupos_duplicados']}",
            f"- Archivos dentro de grupos duplicados: {summary['archivos_en_grupos_duplicados']}",
            f"- Copias redundantes: {summary['copias_redundantes']}",
            f"- Sin fecha: {summary['archivos_sin_fecha']}",
            f"- Ignorados: {summary['archivos_ignorados']}",
            f"- Ignorados copiables: {summary['archivos_ignorados_copiables']}",
            f"- Ignorados no copiables: {summary['archivos_ignorados_no_copiables']}",
            f"- Copiar ignorados a OTROS_ARCHIVOS: {'si' if summary['copiar_archivos_ignorados'] else 'no'}",
            f"- Errores: {summary['errores']}",
            "",
            "Duplicados y espacio:",
            size_line("Tamano total de archivos en grupos duplicados", "tamano_total_archivos_en_grupos_duplicados"),
            size_line("Una copia representativa de cada grupo", "tamano_copia_representativa_grupos_duplicados"),
            size_line("Espacio redundante/recuperable", "espacio_redundante_recuperable"),
            size_line("Tamano final de COLECCION_ORDENADA", "tamano_final_coleccion_ordenada"),
            size_line("Tamano final de DUPLICADOS", "tamano_final_duplicados"),
            size_line("Tamano de ignorados copiables", "tamano_archivos_ignorados_copiables"),
            size_line("Tamano de ignorados no copiables", "tamano_archivos_ignorados_no_copiables"),
            size_line("Tamano final de OTROS_ARCHIVOS", "tamano_final_otros_archivos"),
            size_line("Espacio sin margen", "espacio_total_sin_margen_destino"),
            f"- Margen de seguridad: {summary['margen_seguridad_porcentaje']}%",
            size_line("Margen de seguridad", "margen_seguridad_bytes"),
            size_line("Espacio total necesario en destino", "espacio_total_necesario_destino"),
            size_line("Espacio libre disponible", "espacio_libre_destino"),
            size_line("Espacio faltante", "espacio_faltante_destino"),
            f"- Espacio suficiente: {'si' if summary['espacio_suficiente'] else 'no'}",
            "",
            "Ignorados por extension:",
        ]
        ignored_by_extension = summary.get("archivos_ignorados_por_extension", {})
        if ignored_by_extension:
            for extension, count in ignored_by_extension.items():
                lines.append(f"- {extension}: {count} archivos")
        else:
            lines.append("- ninguno")
        return "\n".join(lines)

    def _format_resume_summary(self, inspection: ResumeInspection) -> str:
        summary = inspection.summary

        def size_line(label: str, key: str) -> str:
            return f"- {label}: {summary[key]} bytes ({summary.get(f'{key}_gb', '0.00 GB')})"

        lines = [
            "Resumen de reanudacion:",
            f"- Origen del plan: {inspection.source}",
            f"- Destino: {inspection.destination}",
            f"- SQLite: {inspection.database_path}",
            f"- Informe previo: {inspection.latest_report_path or '-'}",
            f"- Registros tecnicos previos: {len(inspection.technical_log_paths)}",
            "",
            "Estado de copias planificadas:",
            f"- Verificados y reutilizables: {summary['verificados_reutilizables']}",
            f"- Pendientes: {summary['pendientes']}",
            f"- Incompletos: {summary['incompletos']}",
            f"- Corruptos: {summary['corruptos']}",
            f"- Errores: {summary['errores']}",
            f"- Temporales detectados: {summary['temporales_detectados']}",
            f"- Archivos no planificados detectados: {summary['archivos_no_planificados_detectados']}",
            "",
            "Espacio para continuar:",
            size_line("Bytes restantes por copiar", "bytes_restantes_por_copiar"),
            size_line("Margen de seguridad para reanudacion", "margen_seguridad_reanudacion"),
            size_line("Espacio necesario restante", "espacio_necesario_restante"),
            size_line("Espacio libre actual", "espacio_libre_actual"),
            size_line("Recuperable por destinos invalidos", "espacio_recuperable_de_destinos_invalidos"),
            size_line("Recuperable por temporales", "espacio_recuperable_de_temporales"),
            size_line("Tamano de archivos no planificados", "tamano_archivos_no_planificados"),
            size_line("Disponible estimado tras limpieza", "espacio_disponible_estimado_tras_limpieza"),
            size_line("Espacio faltante para reanudar", "espacio_faltante_para_reanudar"),
            f"- Espacio suficiente para reanudar: {'si' if summary['espacio_suficiente_para_reanudar'] else 'no'}",
        ]
        if inspection.temp_files:
            lines.append("")
            lines.append("Temporales que se eliminaran del destino si continuas:")
            for path in inspection.temp_files[:25]:
                lines.append(f"- {path}")
            if len(inspection.temp_files) > 25:
                lines.append(f"- ... {len(inspection.temp_files) - 25} mas")
        if inspection.extra_files:
            lines.append("")
            lines.append("Archivos no planificados detectados; no se tocaran automaticamente:")
            for path in inspection.extra_files[:25]:
                lines.append(f"- {path}")
            if len(inspection.extra_files) > 25:
                lines.append(f"- ... {len(inspection.extra_files) - 25} mas")
        return "\n".join(lines)

    def _format_final_verification_summary(self, result: FinalVerificationResult) -> str:
        summary = result.summary

        def size_line(label: str, key: str) -> str:
            return f"- {label}: {summary[key]} bytes ({summary.get(f'{key}_gb', '0.00 GB')})"

        lines = [
            summary["estado"],
            f"- Archivos originales encontrados: {summary['archivos_originales_encontrados']}",
            f"- Archivos con destino localizado: {summary['archivos_con_destino_localizado']}",
            f"- Archivos idénticos por hash: {summary['archivos_identicos_por_hash']}",
            f"- Archivos ausentes: {summary['archivos_ausentes']}",
            f"- Tamaños diferentes: {summary['tamaños_diferentes']}",
            f"- Hashes diferentes: {summary['hashes_diferentes']}",
            f"- Archivos sin planificar: {summary['archivos_sin_planificar']}",
            f"- Temporales: {summary['temporales']}",
            f"- Errores de lectura: {summary['errores_lectura']}",
            f"- Destinos no contemplados: {summary['archivos_destino_no_contemplados']}",
            f"- Duplicados inesperados: {summary['archivos_duplicados_inesperados']}",
            f"- Destinos planificados duplicados: {summary['destinos_planificados_duplicados']}",
            f"- Planificados no encontrados en origen: {summary['planificados_no_en_origen']}",
            size_line("Bytes originales", "bytes_originales"),
            size_line("Bytes verificados en destino", "bytes_verificados_destino"),
            size_line("Bytes de destinos planificados localizados", "bytes_destinos_planificados"),
            f"- Suma de tamaños origen/destino coincide: {'si' if summary['suma_tamaños_original_destino_coincide'] else 'no'}",
        ]
        if result.temp_files:
            lines.append("")
            lines.append("Temporales detectados:")
            for path in result.temp_files[:25]:
                lines.append(f"- {path}")
            if len(result.temp_files) > 25:
                lines.append(f"- ... {len(result.temp_files) - 25} mas")
        if result.unplanned_destination_files:
            lines.append("")
            lines.append("Destinos no contemplados en el plan:")
            for path in result.unplanned_destination_files[:25]:
                lines.append(f"- {path}")
            if len(result.unplanned_destination_files) > 25:
                lines.append(f"- ... {len(result.unplanned_destination_files) - 25} mas")
        return "\n".join(lines)

    def _format_multimedia_summary(self, result: MultimediaVerificationResult) -> str:
        summary = result.summary
        lines = [
            summary["estado"],
            f"- Archivos multimedia revisados: {summary['archivos_multimedia_revisados']}",
            f"- Fotografías revisadas: {summary['fotografias_revisadas']}",
            f"- Vídeos revisados: {summary['videos_revisados']}",
            f"- Correctos: {summary['correctos']}",
            f"- Errores: {summary['errores']}",
            f"- ffprobe disponible: {'si' if summary['ffprobe_disponible'] else 'no'}",
            f"- ffmpeg disponible: {'si' if summary['ffmpeg_disponible'] else 'no'}",
        ]
        problem_rows = [row for row in result.rows if row.result != "correcto"]
        if problem_rows:
            lines.append("")
            lines.append("Primeros errores:")
            for row in problem_rows[:25]:
                lines.append(f"- {row.path}: {row.error_reason}")
            if len(problem_rows) > 25:
                lines.append(f"- ... {len(problem_rows) - 25} mas")
        return "\n".join(lines)

    def _open_reports(self) -> None:
        if self.analysis_result:
            self._open_path(self.analysis_result.destination / "INFORMES")
            return
        if self.resume_inspection:
            self._open_path(self.resume_inspection.destination / "INFORMES")
            return
        if self.final_verification_result:
            self._open_path(self.final_verification_result.destination / "INFORMES")
            return
        if self.multimedia_verification_result:
            self._open_path(self.multimedia_verification_result.destination / "INFORMES")
            return
        if self.destination_input.text():
            self._open_path(Path(self.destination_input.text()) / "INFORMES")

    def _open_subdir(self, subdir: str) -> None:
        destination = Path(self.destination_input.text())
        self._open_path(destination / subdir)

    def _open_path(self, path: Path) -> None:
        if path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def main() -> None:
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
