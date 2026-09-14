from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from photo_organizer.config import COLLECTION_DIR, DUPLICATES_DIR, AppConfig
from photo_organizer.organizer import OrganizerService


def write_file(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def snapshot(path: Path) -> dict[str, bytes]:
    return {
        str(file.relative_to(path)): file.read_bytes()
        for file in sorted(item for item in path.rglob("*") if item.is_file())
    }


def media_files(path: Path) -> list[Path]:
    return [item for item in path.rglob("*") if item.is_file() and item.name != "origenes.json"]


def main() -> None:
    root = Path(tempfile.mkdtemp(prefix="app_foto_integral_"))
    source = root / "origen"
    destination = root / "destino"

    duplicate_content = b"duplicado exacto"
    write_file(source / "DispositivoA" / "Viajes" / "IMG_20260715_183000.jpg", duplicate_content)
    write_file(source / "DispositivoB" / "iPhone" / "IMG_20260715_183000.jpg", duplicate_content)
    write_file(source / "WhatsApp" / "IMG-20260715-WA0023.jpg", duplicate_content)
    write_file(source / "Unicas" / "IMG_20260801_120000.HEIC", b"heic unica")
    write_file(source / "Unicas" / "2026-08-02 10.00.00.MOV", b"mov unico")
    write_file(source / "MismoNombreA" / "IMG_20260803_120000.png", b"abc")
    write_file(source / "MismoNombreB" / "IMG_20260803_120000.png", b"abd")
    write_file(source / "origenes.json", b"{}")

    before = snapshot(source)
    service = OrganizerService(AppConfig(metadata_enabled=False, block_size=64 * 1024))
    analysis = service.analyze(source, destination)
    copy_result = service.organize(analysis)
    after = snapshot(source)

    collection_payloads = [path.read_bytes() for path in media_files(destination / COLLECTION_DIR)]
    duplicates = media_files(destination / DUPLICATES_DIR / "Duplicado 0001")
    origins_path = destination / DUPLICATES_DIR / "Duplicado 0001" / "origenes.json"

    checks = {
        "duplicados_no_en_coleccion": duplicate_content not in collection_payloads,
        "grupo_duplicado_tiene_3_archivos": len(duplicates) == 3,
        "origenes_json_creado": origins_path.exists(),
        "origen_intacto": before == after,
        "copias_verificadas": copy_result.copied_files == copy_result.verified_files,
    }
    payload = {
        "raiz_prueba": str(root),
        "resumen_analisis": analysis.summary,
        "resultado_copia": {
            "copiados": copy_result.copied_files,
            "verificados": copy_result.verified_files,
            "fallidos": copy_result.failed_files,
            "bytes": copy_result.copied_bytes,
        },
        "comprobaciones": checks,
        "informes": {key: str(path) for key, path in copy_result.report_paths.items()},
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
