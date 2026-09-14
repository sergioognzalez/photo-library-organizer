from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest
from collections import namedtuple
from datetime import datetime
from pathlib import Path
from unittest import mock

from photo_organizer.config import COLLECTION_DIR, DUPLICATES_DIR, OTHER_FILES_DIR, AppConfig
from photo_organizer.date_resolver import resolve_media_date
from photo_organizer.database import MediaDatabase
from photo_organizer.exceptions import CopyVerificationError, OperationCancelled, ReuseNotSafeError, ValidationError
from photo_organizer.models import DateSource, MediaFile, MediaType
from photo_organizer.organizer import OrganizerService, validate_paths
from photo_organizer.safe_copy import copy_file_verified, copy_file_verified_exact
from photo_organizer.scanner import scan_source
from photo_organizer.selection_review import PhotoSelectionService, REVIEW_TRASH_DIR


def write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def snapshot_tree(path: Path) -> dict[str, tuple[bytes, float]]:
    snapshot: dict[str, tuple[bytes, float]] = {}
    for file_path in sorted(item for item in path.rglob("*") if item.is_file()):
        snapshot[str(file_path.relative_to(path))] = (file_path.read_bytes(), file_path.stat().st_mtime)
    return snapshot


def media_files(path: Path) -> list[Path]:
    return [item for item in path.rglob("*") if item.is_file() and item.name != "origenes.json"]


PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR"
    b"\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde"
    b"\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf\xc0\x00\x00\x03\x01\x01\x00\xc9\xfe\x92\xef"
    b"\x00\x00\x00\x00IEND\xaeB`\x82"
)


class AppFotoCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "origen"
        self.destination = self.root / "destino"
        self.source.mkdir()
        self.config = AppConfig(block_size=64 * 1024, metadata_enabled=False)
        self.service = OrganizerService(self.config)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def analyze(self):
        return self.service.analyze(self.source, self.destination)

    def test_unique_jpg_and_mov_are_organized_by_date_and_type(self) -> None:
        jpg = write_file(self.source / "Fotos" / "IMG_20260715_183000.JPG", b"photo")
        mov = write_file(self.source / "Videos" / "2026-07-16 18.30.00.MOV", b"movie-data")
        before = snapshot_tree(self.source)

        analysis = self.analyze()
        self.assertEqual(analysis.summary["fotos"], 1)
        self.assertEqual(analysis.summary["videos"], 1)
        self.assertEqual(analysis.summary["archivos_unicos"], 2)
        self.assertEqual(analysis.summary["grupos_duplicados"], 0)

        result = self.service.organize(analysis)

        self.assertEqual(result.copied_files, 2)
        self.assertTrue(
            (self.destination / COLLECTION_DIR / "2026" / "07 - Julio" / "Fotos" / jpg.name).exists()
        )
        self.assertTrue(
            (self.destination / COLLECTION_DIR / "2026" / "07 - Julio" / "Videos" / mov.name).exists()
        )
        self.assertEqual(before, snapshot_tree(self.source))

    def test_identical_files_go_only_to_duplicates(self) -> None:
        content = b"same-binary-content"
        write_file(self.source / "DispositivoA" / "IMG_20260715_183000.HEIC", content)
        write_file(self.source / "DispositivoB" / "IMG_20260715_183000.HEIC", content)
        write_file(self.source / "WhatsApp" / "IMG-20260715-WA0023.HEIC", content)
        write_file(self.source / "Unicas" / "otra.png", b"unique")

        analysis = self.analyze()
        self.assertEqual(analysis.summary["grupos_duplicados"], 1)
        self.assertEqual(analysis.summary["archivos_en_grupos_duplicados"], 3)
        duplicate_plans = [file for file in analysis.files if file.is_duplicate]
        self.assertTrue(all(DUPLICATES_DIR in str(file.planned_destination) for file in duplicate_plans))
        self.assertTrue(all(COLLECTION_DIR not in str(file.planned_destination) for file in duplicate_plans))

        self.service.organize(analysis)

        duplicate_group = self.destination / DUPLICATES_DIR / "Duplicado 0001"
        copied_duplicates = [path for path in media_files(duplicate_group) if path.suffix.lower() == ".heic"]
        self.assertEqual(len(copied_duplicates), 3)
        self.assertTrue((duplicate_group / "origenes.json").exists())
        collection_bytes = [path.read_bytes() for path in media_files(self.destination / COLLECTION_DIR)]
        self.assertNotIn(content, collection_bytes)

    def test_same_name_with_different_content_gets_safe_counter(self) -> None:
        write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"abc")
        write_file(self.source / "B" / "IMG_20260715_183000.jpg", b"abd")

        analysis = self.analyze()
        self.assertEqual(analysis.summary["archivos_unicos"], 2)
        self.assertEqual(analysis.summary["grupos_duplicados"], 0)
        self.service.organize(analysis)

        folder = self.destination / COLLECTION_DIR / "2026" / "07 - Julio" / "Fotos"
        names = sorted(path.name for path in media_files(folder))
        self.assertEqual(names, ["IMG_20260715_183000.jpg", "IMG_20260715_183000__2.jpg"])

    def test_formats_case_and_auxiliary_files_are_handled(self) -> None:
        write_file(self.source / "pic.HEIC", b"heic")
        write_file(self.source / "clip.mp4", b"mp4")
        write_file(self.source / "image.PnG", b"png")
        write_file(self.source / "origenes.json", b"{}")
        write_file(self.source / "Thumbs.db", b"thumbs")
        write_file(self.source / "temp.tmp", b"tmp")

        analysis = self.analyze()

        self.assertEqual(analysis.summary["total_archivos_compatibles"], 3)
        self.assertEqual(analysis.summary["fotos"], 2)
        self.assertEqual(analysis.summary["videos"], 1)
        self.assertEqual(analysis.summary["archivos_ignorados"], 3)
        self.assertTrue((self.destination / "INFORMES" / "archivos_ignorados.csv").exists())

    def test_date_sources_filename_filesystem_and_unknown(self) -> None:
        filename_date = write_file(self.source / "Screenshot_20260715-183000.png", b"screen")
        filesystem_date = write_file(self.source / "random_name.jpg", b"fs")
        timestamp = datetime(2022, 3, 5, 10, 15, 0).timestamp()
        os.utime(filesystem_date, (timestamp, timestamp))

        analysis = self.analyze()
        by_name = {file.name: file for file in analysis.files}
        self.assertEqual(by_name[filename_date.name].date_source, DateSource.FILENAME)
        self.assertEqual(by_name[filename_date.name].detected_date.year, 2026)
        self.assertEqual(by_name[filesystem_date.name].date_source, DateSource.FILESYSTEM)
        self.assertEqual(by_name[filesystem_date.name].detected_date.year, 2022)

        media_file = MediaFile(
            id="x",
            absolute_path=Path("no_date.jpg"),
            relative_path=Path("no_date.jpg"),
            name="no_date.jpg",
            extension=".jpg",
            media_type=MediaType.PHOTO,
            size=1,
            modified_time=timestamp,
        )
        resolution = resolve_media_date(media_file, {}, AppConfig(use_filesystem_dates=False))
        self.assertIsNone(resolution.value)
        self.assertEqual(resolution.source, DateSource.UNKNOWN)

    def test_path_validation_rejects_unsafe_relationships(self) -> None:
        with self.assertRaises(ValidationError):
            validate_paths(self.source, self.source)
        with self.assertRaises(ValidationError):
            validate_paths(self.source, self.source / "destino")
        with self.assertRaises(ValidationError):
            validate_paths(self.source, self.root)

    def test_insufficient_space_blocks_organization(self) -> None:
        write_file(self.source / "IMG_20260715_183000.jpg", b"photo")
        usage = namedtuple("usage", "total used free")
        with mock.patch("photo_organizer.organizer.shutil.disk_usage", return_value=usage(10, 10, 0)):
            analysis = self.analyze()
            self.assertFalse(analysis.summary["espacio_suficiente"])
            with self.assertRaises(ValidationError):
                self.service.organize(analysis)

    def test_safe_copy_verification_failure_removes_temp(self) -> None:
        source = write_file(self.source / "file.jpg", b"content")
        target = self.destination / "file.jpg"

        with self.assertRaises(CopyVerificationError):
            copy_file_verified(source, target, self.config, expected_hash="bad-hash")

        self.assertFalse(target.exists())
        self.assertEqual([], list(self.destination.rglob("*.codexcopy-*.tmp")))
        self.assertEqual(source.read_bytes(), b"content")

    def test_cancel_analysis_before_scan(self) -> None:
        write_file(self.source / "IMG_20260715_183000.jpg", b"photo")
        cancel_event = threading.Event()
        cancel_event.set()

        with self.assertRaises(OperationCancelled):
            self.service.analyze(self.source, self.destination, cancel_event=cancel_event)

        self.assertTrue((self.source / "IMG_20260715_183000.jpg").exists())

    def test_cancel_during_copy_removes_partial_temp(self) -> None:
        for index in range(3):
            write_file(self.source / f"IMG_2026071{index}_183000.jpg", bytes([index]) * 200_000)
        before = snapshot_tree(self.source)
        analysis = self.analyze()
        cancel_event = threading.Event()

        def progress(event: dict) -> None:
            if event.get("bytes_copied_delta"):
                cancel_event.set()

        result = self.service.organize(analysis, cancel_event=cancel_event, progress_callback=progress)

        self.assertTrue(result.cancelled)
        self.assertEqual(result.copied_files, 0)
        self.assertEqual(before, snapshot_tree(self.source))
        self.assertEqual([], list(self.destination.rglob("*.codexcopy-*.tmp")))

    def test_disappearing_file_is_reported_as_error(self) -> None:
        disappearing = write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"abc")
        write_file(self.source / "B" / "IMG_20260716_183000.jpg", b"abd")

        class DeletingReader:
            def __init__(self) -> None:
                self.deleted = False

            def read(self, path: Path) -> dict:
                if not self.deleted:
                    disappearing.unlink()
                    self.deleted = True
                return {}

        analysis = self.service.analyze(
            self.source,
            self.destination,
            metadata_reader=DeletingReader(),
        )

        errored = [file for file in analysis.files if file.analysis_status == "error"]
        self.assertEqual(len(errored), 1)
        self.assertGreaterEqual(analysis.summary["errores"], 1)

    def test_reviewed_duplicate_folder_can_be_reanalyzed_as_unique(self) -> None:
        write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"same")
        write_file(self.source / "B" / "IMG_20260715_183000.jpg", b"same")
        analysis = self.analyze()
        self.service.organize(analysis)

        group_dir = self.destination / DUPLICATES_DIR / "Duplicado 0001"
        duplicate_media = media_files(group_dir)
        self.assertEqual(len(duplicate_media), 2)
        duplicate_media[1].unlink()

        second_destination = self.root / "segundo_destino"
        second_analysis = self.service.analyze(group_dir, second_destination)

        self.assertEqual(second_analysis.summary["total_archivos_compatibles"], 1)
        self.assertEqual(second_analysis.summary["archivos_ignorados"], 1)
        self.assertEqual(second_analysis.summary["archivos_unicos"], 1)
        self.assertEqual(second_analysis.summary["grupos_duplicados"], 0)

    def test_unicode_and_long_paths_are_copied_without_original_changes(self) -> None:
        nested = self.source / "Persona Demo" / "Viaje familiar" / ("ruta_larga_" * 8)
        original = write_file(nested / "foto_con_emoji_😀_20260715_183000.JPG", b"unicode")
        before = snapshot_tree(self.source)

        analysis = self.analyze()
        result = self.service.organize(analysis)

        self.assertEqual(result.copied_files, 1)
        self.assertEqual(before, snapshot_tree(self.source))
        copied = media_files(self.destination / COLLECTION_DIR)
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].read_bytes(), original.read_bytes())

    def test_large_file_copy_and_hash_uses_block_workflow(self) -> None:
        large = write_file(self.source / "IMG_20260715_183000.mp4", b"0123456789abcdef" * 400_000)
        analysis = self.analyze()
        result = self.service.organize(analysis)

        self.assertEqual(result.copied_files, 1)
        copied = media_files(self.destination / COLLECTION_DIR)
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0].stat().st_size, large.stat().st_size)
        self.assertEqual(copied[0].read_bytes(), large.read_bytes())

    def test_summary_separates_duplicate_space_and_safety_margin(self) -> None:
        service = OrganizerService(AppConfig(block_size=64 * 1024, metadata_enabled=False, safety_margin_percent=10))
        write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"0123456789")
        write_file(self.source / "B" / "IMG_20260715_183000.jpg", b"0123456789")
        write_file(self.source / "C" / "IMG_20260715_183000.jpg", b"0123456789")
        write_file(self.source / "U" / "IMG_20260716_183000.jpg", b"abcd")

        analysis = service.analyze(self.source, self.destination)
        summary = analysis.summary

        self.assertEqual(summary["grupos_duplicados"], 1)
        self.assertEqual(summary["archivos_en_grupos_duplicados"], 3)
        self.assertEqual(summary["copias_redundantes"], 2)
        self.assertEqual(summary["tamano_total_archivos_en_grupos_duplicados"], 30)
        self.assertEqual(summary["tamano_copia_representativa_grupos_duplicados"], 10)
        self.assertEqual(summary["espacio_redundante_recuperable"], 20)
        self.assertEqual(summary["tamano_final_coleccion_ordenada"], 4)
        self.assertEqual(summary["tamano_final_duplicados"], 30)
        self.assertEqual(summary["espacio_total_sin_margen_destino"], 34)
        self.assertEqual(summary["margen_seguridad_bytes"], 4)
        self.assertEqual(summary["espacio_total_necesario_destino"], 38)
        self.assertIn("GB", summary["espacio_total_necesario_destino_gb"])

    def test_recalculate_previous_analysis_reuses_hashes_for_new_destination(self) -> None:
        write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"same")
        write_file(self.source / "B" / "IMG_20260715_183000.jpg", b"same")
        analysis = self.analyze()
        hashes = {file.relative_path: file.hash for file in analysis.files}
        second_destination = self.root / "destino_2"

        with mock.patch("photo_organizer.duplicate_detector.compute_hash", side_effect=AssertionError):
            recalculated = self.service.recalculate_from_previous_analysis(analysis, second_destination)

        self.assertEqual(recalculated.destination, second_destination.resolve(strict=False))
        self.assertTrue(recalculated.database_path.exists())
        self.assertEqual(recalculated.summary["grupos_duplicados"], 1)
        self.assertTrue(all(str(file.planned_destination).startswith(str(second_destination)) for file in recalculated.files))
        self.assertEqual({file.relative_path: file.hash for file in recalculated.files}, hashes)

    def test_recalculate_previous_analysis_rejects_changed_source(self) -> None:
        write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"same")
        analysis = self.analyze()
        write_file(self.source / "B" / "IMG_20260716_183000.jpg", b"new")

        with self.assertRaises(ReuseNotSafeError):
            self.service.recalculate_from_previous_analysis(analysis, self.root / "destino_2")

    def test_ignored_files_are_reported_by_extension_and_copyable_to_other_files(self) -> None:
        write_file(self.source / "Album" / "foto.AAE", b"aae")
        write_file(self.source / "Album" / "foto.XMP", b"xmp")
        write_file(self.source / "Album" / "foto.JSON", b"json")
        write_file(self.source / "Album" / "thumb.THM", b"thm")
        write_file(self.source / "Audio" / "nota.M4A", b"audio")
        write_file(self.source / "Subtitulos" / "clip.SRT", b"subs")
        write_file(self.source / "Texto" / "nota.TXT", b"text")
        write_file(self.source / "Thumbs.db", b"thumbs")
        write_file(self.source / "desktop.ini", b"desktop")

        analysis = self.analyze()
        summary = analysis.summary

        self.assertEqual(summary["archivos_ignorados"], 9)
        self.assertEqual(summary["archivos_ignorados_copiables"], 7)
        self.assertEqual(summary["archivos_ignorados_no_copiables"], 2)
        self.assertEqual(summary["archivos_ignorados_por_extension"][".AAE"], 1)
        self.assertEqual(summary["archivos_ignorados_por_extension"][".JSON"], 1)
        self.assertEqual(summary["archivos_ignorados_por_extension"][".M4A"], 1)
        self.assertEqual(summary["archivos_ignorados_por_extension"][".TXT"], 1)
        ignored_csv = analysis.report_paths["ignorados_csv"]
        csv_text = ignored_csv.read_text(encoding="utf-8-sig")
        self.assertIn("foto.AAE", csv_text)
        self.assertIn("desktop.ini", csv_text)
        self.assertIn("archivo auxiliar ignorado", csv_text)

        result = self.service.organize(analysis)

        self.assertEqual(result.copied_ignored_files, 7)
        self.assertEqual(result.failed_ignored_files, 0)
        self.assertTrue((self.destination / "OTROS_ARCHIVOS" / "Album" / "foto.AAE").exists())
        self.assertTrue((self.destination / "OTROS_ARCHIVOS" / "Audio" / "nota.M4A").exists())
        self.assertFalse((self.destination / "OTROS_ARCHIVOS" / "Thumbs.db").exists())
        self.assertFalse((self.destination / "OTROS_ARCHIVOS" / "desktop.ini").exists())

    def test_copy_ignored_files_can_be_disabled(self) -> None:
        service = OrganizerService(AppConfig(block_size=64 * 1024, metadata_enabled=False, copy_ignored_files=False))
        write_file(self.source / "Album" / "foto.AAE", b"aae")

        analysis = service.analyze(self.source, self.destination)
        result = service.organize(analysis)

        self.assertEqual(analysis.summary["archivos_ignorados_copiables"], 1)
        self.assertFalse(analysis.summary["copiar_archivos_ignorados"])
        self.assertEqual(analysis.summary["tamano_final_otros_archivos"], 0)
        self.assertEqual(result.copied_ignored_files, 0)
        self.assertFalse((self.destination / "OTROS_ARCHIVOS" / "Album" / "foto.AAE").exists())

    def test_exact_copy_refuses_existing_destination_without_suffix(self) -> None:
        source = write_file(self.source / "IMG_20260715_183000.jpg", b"new-content")
        target = write_file(self.destination / "IMG_20260715_183000.jpg", b"old-content")

        with self.assertRaises(CopyVerificationError):
            copy_file_verified_exact(source, target, self.config)

        self.assertEqual(target.read_bytes(), b"old-content")
        self.assertFalse((self.destination / "IMG_20260715_183000__2.jpg").exists())
        self.assertEqual([], list(self.destination.rglob("*.codexcopy-*.tmp")))

    def test_resume_interrupted_organization_reuses_valid_and_recopies_invalid(self) -> None:
        valid = write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"valid-copy")
        pending = write_file(self.source / "B" / "IMG_20260716_183000.jpg", b"pending-copy")
        incomplete = write_file(self.source / "C" / "IMG_20260717_183000.jpg", b"incomplete-copy")
        corrupt = write_file(self.source / "D" / "IMG_20260718_183000.jpg", b"original-four")
        sidecar = write_file(self.source / "Album" / "ajuste.AAE", b"sidecar")
        before = snapshot_tree(self.source)

        analysis = self.analyze()
        planned = {file.name: file.planned_destination for file in analysis.files}
        ignored_plan = {file.name: file.planned_destination for file in analysis.ignored}

        write_file(planned[valid.name], valid.read_bytes())
        write_file(planned[incomplete.name], b"partial")
        write_file(planned[corrupt.name], b"modified-four")
        write_file(
            self.destination
            / COLLECTION_DIR
            / "interrumpido.codexcopy-123.tmp",
            b"temporal",
        )
        extra = write_file(
            self.destination
            / COLLECTION_DIR
            / "archivo_no_planificado.jpg",
            b"extra",
        )

        inspection = self.service.inspect_interrupted_organization(self.source, self.destination)
        summary = inspection.summary

        self.assertEqual(summary["verificados_reutilizables"], 1)
        self.assertEqual(summary["pendientes"], 2)
        self.assertEqual(summary["incompletos"], 1)
        self.assertEqual(summary["corruptos"], 1)
        self.assertEqual(summary["errores"], 0)
        self.assertEqual(summary["temporales_detectados"], 1)
        self.assertEqual(summary["archivos_no_planificados_detectados"], 1)
        self.assertTrue(summary["espacio_suficiente_para_reanudar"])

        result = self.service.resume_interrupted_organization(inspection)

        self.assertFalse(result.cancelled)
        self.assertEqual(result.reused_files, 1)
        self.assertEqual(result.copied_files, 3)
        self.assertEqual(result.copied_ignored_files, 1)
        self.assertEqual(result.failed_files, 0)
        self.assertEqual(result.failed_ignored_files, 0)
        self.assertEqual(planned[pending.name].read_bytes(), pending.read_bytes())
        self.assertEqual(planned[incomplete.name].read_bytes(), incomplete.read_bytes())
        self.assertEqual(planned[corrupt.name].read_bytes(), corrupt.read_bytes())
        self.assertEqual(ignored_plan[sidecar.name].read_bytes(), sidecar.read_bytes())
        self.assertEqual(before, snapshot_tree(self.source))
        self.assertEqual([], list(self.destination.rglob("*.codexcopy-*.tmp")))
        self.assertTrue(extra.exists())
        self.assertEqual([], [path for path in self.destination.rglob("*__2*")])

        database = MediaDatabase(analysis.database_path)
        stored_files = [file for file in database.load_files() if file.analysis_status != "error"]
        stored_ignored = [file for file in database.load_ignored_files() if file.copyable]
        self.assertTrue(all(file.verification_status == "verified" for file in stored_files))
        self.assertTrue(all(file.verification_status == "verified" for file in stored_ignored))

        second_inspection = self.service.inspect_interrupted_organization(self.source, self.destination)
        self.assertEqual(second_inspection.summary["verificados_reutilizables"], 5)
        self.assertEqual(second_inspection.summary["pendientes"], 0)
        self.assertEqual(second_inspection.summary["incompletos"], 0)
        self.assertEqual(second_inspection.summary["corruptos"], 0)

    def test_final_verification_passes_only_after_fresh_source_and_destination_hashes(self) -> None:
        write_file(self.source / "Unica" / "IMG_20260715_183000.jpg", b"unique")
        write_file(self.source / "DupA" / "IMG_20260716_183000.jpg", b"same")
        write_file(self.source / "DupB" / "IMG_20260716_183000.jpg", b"same")
        write_file(self.source / "Sidecars" / "IMG_20260715_183000.AAE", b"sidecar")
        before = snapshot_tree(self.source)

        analysis = self.analyze()
        self.service.organize(analysis)

        result = self.service.verify_final_before_delete(self.source, self.destination)
        summary = result.summary

        self.assertTrue(result.passed)
        self.assertEqual(summary["estado"], "VERIFICACIÓN FINAL SUPERADA")
        self.assertEqual(summary["archivos_originales_encontrados"], 4)
        self.assertEqual(summary["archivos_con_destino_localizado"], 4)
        self.assertEqual(summary["archivos_identicos_por_hash"], 4)
        self.assertEqual(summary["archivos_ausentes"], 0)
        self.assertEqual(summary["tamaños_diferentes"], 0)
        self.assertEqual(summary["hashes_diferentes"], 0)
        self.assertEqual(summary["archivos_sin_planificar"], 0)
        self.assertEqual(summary["temporales"], 0)
        self.assertEqual(summary["errores_lectura"], 0)
        self.assertEqual(summary["bytes_originales"], summary["bytes_verificados_destino"])
        self.assertEqual(before, snapshot_tree(self.source))
        self.assertTrue((self.destination / "INFORMES" / "verificacion_final_completa.csv").exists())
        self.assertTrue((self.destination / "INFORMES" / "verificacion_final_completa.json").exists())
        self.assertTrue((self.destination / "INFORMES" / "verificacion_final_tecnica.log").exists())

    def test_final_verification_reports_missing_truncated_corrupt_temp_and_extra_files(self) -> None:
        good = write_file(self.source / "A" / "IMG_20260715_183000.jpg", b"good-copy")
        missing = write_file(self.source / "B" / "IMG_20260716_183000.jpg", b"missing-copy")
        truncated = write_file(self.source / "C" / "IMG_20260717_183000.jpg", b"truncated-copy")
        corrupt = write_file(self.source / "D" / "IMG_20260718_183000.jpg", b"same-size-data")
        sidecar = write_file(self.source / "Sidecars" / "IMG_20260715_183000.AAE", b"sidecar")
        before = snapshot_tree(self.source)

        analysis = self.analyze()
        for media_file in analysis.files:
            media_file.hash = "estado_sqlite_no_fiable"
            media_file.copy_status = "copied"
            media_file.verification_status = "verified"
        database = MediaDatabase(analysis.database_path)
        database.save_files(analysis.files)

        planned = {file.name: file.planned_destination for file in analysis.files}
        ignored_plan = {file.name: file.planned_destination for file in analysis.ignored}
        write_file(planned[good.name], good.read_bytes())
        write_file(planned[truncated.name], b"truncated")
        write_file(planned[corrupt.name], b"diff-size-data")
        write_file(ignored_plan[sidecar.name], sidecar.read_bytes())
        write_file(self.destination / COLLECTION_DIR / "resto.codexcopy-abc.tmp", b"temp")
        write_file(self.destination / COLLECTION_DIR / "copia_extra_misma.jpg", good.read_bytes())

        result = self.service.verify_final_before_delete(self.source, self.destination)
        summary = result.summary

        self.assertFalse(result.passed)
        self.assertEqual(summary["estado"], "NO ES SEGURO BORRAR EL ORIGEN")
        self.assertEqual(summary["archivos_originales_encontrados"], 5)
        self.assertEqual(summary["archivos_con_destino_localizado"], 4)
        self.assertEqual(summary["archivos_identicos_por_hash"], 2)
        self.assertEqual(summary["archivos_ausentes"], 1)
        self.assertEqual(summary["tamaños_diferentes"], 1)
        self.assertEqual(summary["hashes_diferentes"], 1)
        self.assertEqual(summary["archivos_sin_planificar"], 0)
        self.assertEqual(summary["temporales"], 1)
        self.assertEqual(summary["errores_lectura"], 0)
        self.assertEqual(summary["archivos_destino_no_contemplados"], 1)
        self.assertEqual(summary["archivos_duplicados_inesperados"], 1)
        self.assertNotEqual(summary["bytes_originales"], summary["bytes_verificados_destino"])
        self.assertEqual(before, snapshot_tree(self.source))

        csv_text = result.report_paths["csv"].read_text(encoding="utf-8-sig")
        self.assertIn("ruta_origen", csv_text)
        self.assertIn("hash_diferente", csv_text)
        self.assertIn("tamaño_diferente", csv_text)
        payload = json.loads(result.report_paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(payload["resumen"]["temporales"], 1)
        self.assertEqual(len(payload["destinos_no_planificados"]), 1)

    def test_multimedia_verification_validates_photos_and_reports_missing_video_tool(self) -> None:
        write_file(self.source / "IMG_20260715_183000.png", PNG_1X1)
        write_file(self.source / "VID_20260715_183000.mp4", b"not-a-real-video")
        analysis = self.analyze()
        self.service.organize(analysis)

        with mock.patch("photo_organizer.final_verification.shutil.which", return_value=None):
            result = self.service.verify_destination_multimedia(self.source, self.destination)

        self.assertEqual(result.summary["archivos_multimedia_revisados"], 2)
        self.assertEqual(result.summary["fotografias_revisadas"], 1)
        self.assertEqual(result.summary["videos_revisados"], 1)
        self.assertEqual(result.summary["correctos"], 1)
        self.assertEqual(result.summary["errores"], 1)
        errors = [row for row in result.rows if row.result != "correcto"]
        self.assertEqual(errors[0].error_reason, "ffprobe/ffmpeg no encontrado")
        self.assertTrue((self.destination / "INFORMES" / "verificacion_multimedia.csv").exists())
        self.assertTrue((self.destination / "INFORMES" / "verificacion_multimedia.json").exists())
        self.assertTrue((self.destination / "INFORMES" / "verificacion_multimedia_tecnica.log").exists())

    def test_selection_keep_marks_without_modifying_file(self) -> None:
        album = self.destination / COLLECTION_DIR / "2025" / "07 - Julio" / "Fotos"
        photo = write_file(album / "IMG_0001.JPG", b"photo-data")
        before_bytes = photo.read_bytes()
        before_mtime = photo.stat().st_mtime
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        session = service.keep_current(session)

        self.assertEqual(session.files[0].decision, "kept")
        self.assertTrue(photo.exists())
        self.assertEqual(photo.read_bytes(), before_bytes)
        self.assertEqual(photo.stat().st_mtime, before_mtime)

    def test_selection_discard_moves_to_review_trash_with_relative_structure(self) -> None:
        album = self.destination / COLLECTION_DIR / "2025" / "07 - Julio" / "Fotos"
        photo = write_file(album / "IMG_0002.JPG", b"discard-me")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        session = service.discard_current(session)

        expected = self.destination / REVIEW_TRASH_DIR / COLLECTION_DIR / "2025" / "07 - Julio" / "Fotos" / "IMG_0002.JPG"
        self.assertFalse(photo.exists())
        self.assertTrue(expected.exists())
        self.assertEqual(expected.read_bytes(), b"discard-me")
        self.assertEqual(session.files[0].decision, "discarded")

    def test_selection_discard_never_overwrites_existing_trash_name(self) -> None:
        album = self.destination / COLLECTION_DIR / "Fotos"
        photo = write_file(album / "IMG_0003.JPG", b"new")
        existing = write_file(self.destination / REVIEW_TRASH_DIR / COLLECTION_DIR / "Fotos" / "IMG_0003.JPG", b"old")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        service.discard_current(session)

        self.assertEqual(existing.read_bytes(), b"old")
        suffixed = self.destination / REVIEW_TRASH_DIR / COLLECTION_DIR / "Fotos" / "IMG_0003__2.JPG"
        self.assertTrue(suffixed.exists())
        self.assertEqual(suffixed.read_bytes(), b"new")

    def test_selection_undo_restores_discarded_file(self) -> None:
        album = self.destination / COLLECTION_DIR / "Fotos"
        photo = write_file(album / "IMG_0004.JPG", b"restore")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        session = service.discard_current(session)
        self.assertFalse(photo.exists())

        session = service.undo_last(session)

        self.assertTrue(photo.exists())
        self.assertEqual(photo.read_bytes(), b"restore")
        self.assertEqual(session.files[0].decision, "undecided")

    def test_selection_session_can_continue_without_repeating_decided_files(self) -> None:
        album = self.destination / COLLECTION_DIR / "Fotos"
        write_file(album / "IMG_0005.JPG", b"first")
        write_file(album / "IMG_0006.JPG", b"second")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        service.keep_current(session)

        resumed = service.latest_session_for_album(album)
        self.assertIsNotNone(resumed)
        self.assertEqual(resumed.current_index, 1)
        self.assertEqual(service.current_file(resumed).name, "IMG_0006.JPG")

    def test_selection_audits_interrupted_move_without_losing_file(self) -> None:
        album = self.destination / COLLECTION_DIR / "Fotos"
        photo = write_file(album / "IMG_0007.JPG", b"interrupt")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        interrupted = service.discard_current(session, simulate_interrupt_after_register=True)
        audits = service.audit_incomplete_movements(interrupted)

        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].state, "en_origen")
        self.assertTrue(photo.exists())
        self.assertEqual(photo.read_bytes(), b"interrupt")

    def test_selection_moves_auxiliary_files_when_enabled(self) -> None:
        album = self.destination / COLLECTION_DIR / "Fotos"
        photo = write_file(album / "IMG_0008.JPG", b"photo")
        sidecar = write_file(album / "IMG_0008.AAE", b"edit")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="all", ordering="name")
        service.discard_current(session, move_auxiliaries=True)

        expected_photo = self.destination / REVIEW_TRASH_DIR / COLLECTION_DIR / "Fotos" / "IMG_0008.JPG"
        expected_sidecar = self.destination / REVIEW_TRASH_DIR / COLLECTION_DIR / "Fotos" / "IMG_0008.AAE"
        self.assertFalse(photo.exists())
        self.assertFalse(sidecar.exists())
        self.assertTrue(expected_photo.exists())
        self.assertTrue(expected_sidecar.exists())

    def test_selection_video_enumeration_does_not_load_contents(self) -> None:
        album = self.destination / COLLECTION_DIR / "Videos"
        video = write_file(album / "VID_0001.MP4", b"video-bytes")
        service = PhotoSelectionService()

        session = service.prepare_album(album, include_subfolders=True, media_filter="videos", ordering="name")

        self.assertEqual(len(session.files), 1)
        self.assertEqual(session.files[0].media_type, "video")
        self.assertTrue(video.exists())

    def test_review_trash_permanent_delete_requires_all_confirmations(self) -> None:
        trash_file = write_file(self.destination / REVIEW_TRASH_DIR / COLLECTION_DIR / "Fotos" / "IMG_0009.JPG", b"trash")
        service = PhotoSelectionService()

        with self.assertRaises(ValidationError):
            service.permanently_delete_trash(self.destination, False, "ELIMINAR", True)
        with self.assertRaises(ValidationError):
            service.permanently_delete_trash(self.destination, True, "BORRAR", True)
        with self.assertRaises(ValidationError):
            service.permanently_delete_trash(self.destination, True, "ELIMINAR", False)

        self.assertTrue(trash_file.exists())

    def test_selection_tab_is_disabled_during_active_operations(self) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication
        from photo_organizer.ui.main_window import MainWindow

        app = QApplication.instance() or QApplication([])
        window = MainWindow()
        window._set_busy(True, copying=False)
        self.assertFalse(window.selection_tab.isEnabled())
        window._set_busy(False, copying=False)
        self.assertTrue(window.selection_tab.isEnabled())
        window.close()

    def test_analysis_emits_progress_before_path_validation_failure(self) -> None:
        events: list[dict] = []

        with self.assertRaises(ValidationError):
            self.service.analyze(
                self.source / "no_existe",
                self.destination,
                progress_callback=events.append,
            )

        self.assertGreaterEqual(len(events), 1)
        self.assertEqual(events[0]["phase"], "validando carpetas")
        self.assertIn(str(self.source / "no_existe"), events[0]["current_file"])

    def test_scan_reports_activity_even_without_compatible_files(self) -> None:
        write_file(self.source / "Documentos" / "nota.txt", b"texto")
        write_file(self.source / "Thumbs.db", b"thumbs")
        events: list[dict] = []

        result = scan_source(self.source, progress_callback=events.append)

        self.assertEqual(len(result.files), 0)
        self.assertEqual(len(result.ignored), 2)
        self.assertTrue(any(event["phase"] == "enumerando archivos" for event in events))
        self.assertTrue(any("current_file" in event for event in events))
        self.assertTrue(any(event.get("ignored") == 2 for event in events))

    def test_analysis_creates_persistent_technical_log(self) -> None:
        write_file(self.source / "IMG_20260715_183000.jpg", b"photo")
        events: list[dict] = []

        analysis = self.service.analyze(self.source, self.destination, progress_callback=events.append)

        log_events = [event for event in events if event.get("log_path")]
        self.assertEqual(len(log_events), 1)
        log_path = Path(log_events[0]["log_path"])
        self.assertTrue(log_path.exists())
        self.assertIn("INFORMES", str(log_path))
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("enumerando archivos", log_text)
        self.assertIn("analisis completado", log_text)
        self.assertEqual(analysis.summary["total_archivos_compatibles"], 1)


if __name__ == "__main__":
    unittest.main()
