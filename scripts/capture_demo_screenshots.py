"""Generate privacy-safe screenshots for the README using synthetic media files."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QApplication

from photo_organizer.config import AppConfig, COLLECTION_DIR
from photo_organizer.organizer import OrganizerService
from photo_organizer.selection_review import PhotoSelectionService
from photo_organizer.ui.main_window import MainWindow

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCREENSHOTS_DIR = PROJECT_ROOT / "docs" / "screenshots"
DISPLAY_SOURCE = r"D:\Demo\AppFoto\Biblioteca_Ficticia"
DISPLAY_DESTINATION = r"D:\Demo\AppFoto\Resultado_Seguro"


def _create_demo_library(source: Path) -> None:
    colors = [QColor("#2563eb"), QColor("#0891b2"), QColor("#16a34a"), QColor("#9333ea")]
    for index in range(20):
        image = QImage(1200, 800, QImage.Format.Format_RGB32)
        image.fill(colors[index % len(colors)])
        painter = QPainter(image)
        painter.fillRect(80 + index * 18, 120 + index * 13, 340, 260, QColor("#f8fafc"))
        painter.fillRect(120, 520 - index * 9, 760, 60, QColor("#0f172a"))
        painter.end()
        image.save(str(source / f"IMG_2026{(index % 8) + 1:02d}_{index + 1:06d}.jpg"), "JPG")

    duplicate = source / "IMG_20260715_220001.jpg"
    shutil.copyfile(source / "IMG_202601_000001.jpg", duplicate)
    shutil.copyfile(duplicate, source / "Copia_telefono_20260715.jpg")
    (source / "CLIP_20260801_120000.mov").write_bytes(b"synthetic-video-a")
    (source / "CLIP_20260802_120000.mov").write_bytes(b"synthetic-video-b")


def _save(window: MainWindow, filename: str) -> None:
    QApplication.processEvents()
    if not window.grab().save(str(SCREENSHOTS_DIR / filename), "PNG"):
        raise RuntimeError(f"No se pudo guardar {filename}")


def _show_analysis(window: MainWindow, result) -> None:
    window.source_input.setText(DISPLAY_SOURCE)
    window.destination_input.setText(DISPLAY_DESTINATION)
    summary = dict(result.summary)
    # Display-only demo value: never reveal the real disk's free capacity.
    summary["espacio_libre_destino"] = 20 * 1024**3
    summary["espacio_libre_destino_gb"] = "20.00 GB"
    window.progress.setValue(100)
    window.phase_label.setText("Fase: análisis completado")
    window.reviewed_label.setText(f"Revisados: {summary['total_archivos_encontrados']}")
    window.photos_label.setText(f"Fotos: {summary['fotos']}")
    window.videos_label.setText(f"Videos: {summary['videos']}")
    window.compatible_label.setText(f"Compatibles: {summary['total_archivos_compatibles']}")
    window.ignored_label.setText(f"Ignorados: {summary['archivos_ignorados']}")
    window.errors_label.setText(f"Errores: {summary['errores']}")
    window.messages.setPlainText(window._format_summary(summary))
    window.copy_button.setEnabled(True)


def _show_verification(window: MainWindow, result) -> None:
    summary = result.summary
    window.progress.setValue(100)
    window.phase_label.setText("Fase: verificación final completada")
    window.reviewed_label.setText(f"Revisados: {summary['archivos_originales_encontrados']}")
    window.verified_label.setText(f"Verificados: {summary['archivos_identicos_por_hash']}")
    window.errors_label.setText(f"Errores: {summary['errores_lectura']}")
    window.messages.setPlainText(window._format_final_verification_summary(result))


def _show_review(window: MainWindow, collection: Path) -> None:
    service = PhotoSelectionService()
    session = service.prepare_album(collection, media_filter="photos", ordering="name")
    for _ in range(3):
        session = service.discard_current(session)

    window.tabs.setCurrentWidget(window.selection_tab)
    window.review_service = service
    window._review_session_ready(session)
    window.review_album_input.setText(r"D:\Demo\AppFoto\Resultado_Seguro\COLECCION_ORDENADA")
    window.review_date_label.setText("2026-08-01T12:00:00")
    window.review_album_summary.setText(
        "Sesión de revisión activa. Los descartes se pueden restaurar antes de cualquier eliminación permanente."
    )
    window.review_trash_list.clear()
    for index in range(3):
        window.review_trash_list.addItem(
            rf"PAPELERA_REVISION\COLECCION_ORDENADA\2026\Demo\foto_descartada_{index + 1:02d}.jpg"
        )
    window.review_trash_stats.setText("3 archivos recuperables en papelera")


def main() -> None:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="app_foto_screenshots_") as temporary:
        root = Path(temporary)
        source = root / "source"
        destination = root / "destination"
        source.mkdir()
        _create_demo_library(source)
        # Make filesystem fallback dates fictitious and independent of this PC.
        import os
        from datetime import datetime, timezone

        demo_timestamp = datetime(2026, 8, 1, 12, tzinfo=timezone.utc).timestamp()
        for media in source.iterdir():
            os.utime(media, (demo_timestamp, demo_timestamp))

        app = QApplication([])
        window = MainWindow()
        window.resize(1280, 900)
        window.show()
        _save(window, "01-main-window.png")

        service = OrganizerService(AppConfig(metadata_enabled=False))
        analysis = service.analyze(source, destination)
        _show_analysis(window, analysis)
        _save(window, "02-analysis.png")

        copy_result = service.organize(analysis)
        if copy_result.failed_files:
            raise RuntimeError("La organización de la biblioteca sintética ha fallado")
        verification = service.verify_final_before_delete(source, destination)
        if not verification.passed:
            raise RuntimeError("La verificación de la biblioteca sintética ha fallado")
        window.tabs.setCurrentIndex(0)
        _show_verification(window, verification)
        _save(window, "03-final-verification.png")

        _show_review(window, destination / COLLECTION_DIR)
        _save(window, "04-review-trash.png")
        window.close()
        app.quit()


if __name__ == "__main__":
    main()
