"""Create restart-safe social text inside a confirmed monthly analysis snapshot.

The caller supplies a processed Dataset version containing the index/results in
``month_dir``. It publishes this directory again only when ``changed`` is true.
No function in this module uploads data or submits posts to a social platform.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import re
import shutil
import stat
import tempfile
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path

from polymarket_kaggle_reports import ReportPeriod, due_periods, render_bundle, validate_bundle
from polymarket_publish_state import _validate_text_manifest, load_manifest


SOCIAL_ARCHIVE_FILENAME = "social.archive.json"
SEED_ARCHIVE_FILENAME = "months.archive.json"


def _encode_archive(payload: bytes) -> bytes:
    """Hide archive magic from Dataset extraction using an explicit JSON format."""
    envelope = {"schema_version": 1, "encoding": "base64+zip",
                "sha256": hashlib.sha256(payload).hexdigest(),
                "payload": base64.b64encode(payload).decode("ascii")}
    return (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def _decode_archive(path: Path) -> bytes:
    envelope = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(envelope, dict)
            or set(envelope) != {"schema_version", "encoding", "sha256", "payload"}
            or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1
            or envelope["encoding"] != "base64+zip"
            or not isinstance(envelope["payload"], str)
            or not isinstance(envelope["sha256"], str)):
        raise ValueError("Unsupported cloud text archive container")
    try:
        payload = base64.b64decode(envelope["payload"], validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Invalid cloud text archive encoding") from None
    if hashlib.sha256(payload).hexdigest() != envelope["sha256"]:
        raise ValueError("Cloud text archive checksum differs")
    return payload


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
                         encoding="utf-8")
    temporary.replace(path)


def _save_if_changed(path: Path, value: dict) -> bool:
    if path.exists() and json.loads(path.read_text(encoding="utf-8")) == value:
        return False
    _write_json(path, value)
    return True


def _pack_directory(directory: Path, archive: Path) -> bool:
    """Pack one directory deterministically so unchanged state has identical bytes."""
    directory, archive = Path(directory), Path(archive)
    if not directory.exists():
        return False
    if directory.is_symlink():
        raise ValueError("Archive source must not be a symbolic link")
    files = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError("Archive source contains a symbolic link")
        if path.is_file():
            files.append(path)
    with tempfile.TemporaryDirectory(prefix=".social-archive-", dir=archive.parent) as temporary:
        staged = Path(temporary) / archive.name
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_DEFLATED) as output:
            for path in files:
                info = zipfile.ZipInfo(path.relative_to(directory.parent).as_posix(), date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                output.writestr(info, path.read_bytes())
        # Kaggle extracts recognized archives on upload. JSON is stored as-is
        # with --keep-tabular; its decoded ZIP never reaches that upload path.
        staged.write_bytes(_encode_archive(staged.read_bytes()))
        if archive.exists() and archive.read_bytes() == staged.read_bytes():
            return False
        staged.replace(archive)
    return True


def _restore_archive(archive: Path, destination: Path, *, prefix: str) -> bool:
    """Validate every member and conflict before restoring any archived file."""
    pending = []
    names = set()
    destination = Path(destination)
    with zipfile.ZipFile(io.BytesIO(_decode_archive(Path(archive)))) as source:
        for info in source.infolist():
            relative = Path(info.filename)
            if (relative.is_absolute() or ".." in relative.parts or "\\" in info.filename
                    or not relative.parts or relative.parts[0] != prefix):
                raise ValueError("Invalid cloud text archive path")
            canonical = relative.as_posix()
            if canonical in names:
                raise ValueError("Duplicate cloud text archive member")
            names.add(canonical)
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ValueError("Cloud text archive contains a symbolic link or special file")
            target = destination / relative
            for path in [target, *list(target.parents)[:len(relative.parts)]]:
                if path.is_symlink():
                    raise ValueError("Cloud text restore path contains a symbolic link")
            if info.is_dir():
                if target.exists() and not target.is_dir():
                    raise ValueError("Cloud text archive directory conflicts with an existing file")
                continue
            if len(relative.parts) < 2:
                raise ValueError("Cloud text archive file is outside its directory")
            content = source.read(info)
            if target.exists():
                if not target.is_file() or target.read_bytes() != content:
                    raise ValueError("Cloud text archive conflicts with existing content")
            else:
                for parent in target.parents:
                    if parent == destination:
                        break
                    if parent.exists() and not parent.is_dir():
                        raise ValueError("Cloud text archive parent is not a directory")
                pending.append((target, content))
    # Detect file/parent collisions within the archive, including absent paths.
    file_paths = {path for path, _ in pending}
    if any(any(parent in file_paths for parent in path.parents) for path in file_paths):
        raise ValueError("Cloud text archive contains conflicting file paths")
    destination.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".social-restore-", dir=destination) as temporary:
        for number, (target, content) in enumerate(pending):
            staged = Path(temporary) / str(number)
            staged.write_bytes(content)
            target.parent.mkdir(parents=True, exist_ok=True)
            staged.replace(target)
    return bool(pending)


def pack_cloud_social(month_dir: Path) -> bool:
    """Create/update the flat JSON archive uploaded with a monthly Dataset."""
    month_dir = Path(month_dir)
    return _pack_directory(month_dir / "social", month_dir / SOCIAL_ARCHIVE_FILENAME)


def restore_cloud_social(month_dir: Path) -> bool:
    """Restore portable text/state after downloading a monthly Dataset."""
    month_dir = Path(month_dir)
    archive = month_dir / SOCIAL_ARCHIVE_FILENAME
    return _restore_archive(archive, month_dir, prefix="social") if archive.exists() else False


def _package_manifest(month_dir: Path, period: ReportPeriod) -> Path:
    return month_dir / "social" / period.kind / period.target / (period.stem + "_publish.json")


def _existing_package(manifest_path: Path, period: ReportPeriod) -> dict:
    manifest = load_manifest(manifest_path)
    for platform in ("x", "linkedin"):
        _validate_text_manifest(manifest, platform)
    source = json.loads(Path(manifest["source_path"]).read_text(encoding="utf-8"))
    validate_bundle(source, period)
    return manifest


def _portable_manifest(manifest: dict) -> dict:
    manifest["path_base"] = "manifest_directory"
    manifest["source_path"] = Path(manifest["source_path"]).name
    for platform in manifest["platforms"].values():
        platform["text_paths"] = [Path(path).name for path in platform["text_paths"]]
    return manifest


def seed_cloud_social_state(month_dir: Path, *, month: str, local_state_path: Path,
                            local_output_dir: Path) -> dict:
    """Prepare a one-time cloud seed without changing any local source files.

    Copy validated text packages byte-for-byte (only manifest paths change).
    Terminal legacy reports with no text package retain a ``preserved`` record,
    so the migration does not regenerate previously handled periods. The caller
    transports this seed as part of a cloud snapshot before normal generation.
    """
    month_dir, local_output_dir = Path(month_dir), Path(local_output_dir)
    original = json.loads(Path(local_state_path).read_text(encoding="utf-8"))
    rollout_date = date.fromisoformat(original["rollout_date"])
    state_path = month_dir / "social/state.json"
    state = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else
             {"schema_version": 1, "rollout_date": str(rollout_date), "reports": {}})
    outcome = {"changed": False, "imported": [], "preserved": []}
    for identity, old in original["reports"].items():
        period = ReportPeriod.from_identity(identity)
        if period.month != month or identity in state["reports"]:
            continue
        source_manifest_path = local_output_dir / (period.stem + "_publish.json")
        item = {"status": "pending"}
        if source_manifest_path.exists():
            manifest = _existing_package(source_manifest_path, period)
            destination = _package_manifest(month_dir, period)
            if destination.parent.exists():
                manifest = _existing_package(destination, period)
            else:
                destination.parent.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix=".social-seed-", dir=destination.parent.parent) as temporary:
                    staged = Path(temporary) / "package"
                    staged.mkdir()
                    sources = [manifest["source_path"], *[p for item in manifest["platforms"].values()
                                                          for p in item["text_paths"]]]
                    for source in sources:
                        shutil.copyfile(source, staged / Path(source).name)
                    _write_json(staged / destination.name, _portable_manifest(manifest))
                    _existing_package(staged / destination.name, period)
                    staged.rename(destination.parent)
            item.update(status="generated", format="text", source=manifest["source_reference"],
                        manifest=destination.relative_to(month_dir).as_posix(),
                        imported_from_local=True)
            if old.get("generated_at"):
                item["generated_at"] = old["generated_at"]
            outcome["imported"].append(identity)
        elif old.get("status") in {"generated", "preserved"}:
            item.update(status="preserved", reason="Preserved pre-migration local publication assets")
            outcome["preserved"].append(identity)
        state["reports"][identity] = item
    outcome["changed"] = _save_if_changed(state_path, state)
    return outcome


def build_cloud_text_seed(destination: Path, *, local_state_path: Path,
                          local_output_dir: Path) -> dict:
    """Build a small, portable migration Dataset containing text and state only."""
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Seed destination must not exist")
    original = json.loads(Path(local_state_path).read_text(encoding="utf-8"))
    months = sorted({ReportPeriod.from_identity(identity).month for identity in original["reports"]})
    destination.mkdir(parents=True)
    summary = {"schema_version": 2, "archive_filename": SEED_ARCHIVE_FILENAME,
               "rollout_date": original["rollout_date"], "months": months,
               "reports": {}, "files_sha256": {}}
    for month in months:
        summary["reports"][month] = seed_cloud_social_state(destination / "months" / month,
            month=month, local_state_path=local_state_path, local_output_dir=local_output_dir)
    for path in sorted(destination.rglob("*")):
        if path.is_file():
            summary["files_sha256"][path.relative_to(destination).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    _pack_directory(destination / "months", destination / SEED_ARCHIVE_FILENAME)
    summary["archive_sha256"] = hashlib.sha256((destination / SEED_ARCHIVE_FILENAME).read_bytes()).hexdigest()
    _write_json(destination / "seed.json", summary)
    shutil.rmtree(destination / "months")
    return summary


def import_cloud_text_state(month_dir: Path, seed_dir: Path, *, month: str) -> dict:
    """Merge a fixed, downloaded seed into a monthly snapshot without overwrite.

    The caller records migration completion only after the changed monthly
    snapshot has been published and confirmed. Repeating this merge is safe.
    """
    month_dir, seed_dir = Path(month_dir), Path(seed_dir)
    metadata = json.loads((seed_dir / "seed.json").read_text(encoding="utf-8"))
    if (metadata.get("schema_version") != 2
            or metadata.get("archive_filename") != SEED_ARCHIVE_FILENAME):
        raise ValueError("Unsupported cloud social seed")
    if month not in metadata["months"]:
        return {"changed": False, "imported": [], "preserved": []}
    archive = seed_dir / SEED_ARCHIVE_FILENAME
    if hashlib.sha256(archive.read_bytes()).hexdigest() != metadata.get("archive_sha256"):
        raise ValueError("Cloud social seed archive changed")
    with tempfile.TemporaryDirectory(prefix="social-seed-import-") as temporary:
        extracted = Path(temporary)
        _restore_archive(archive, extracted, prefix="months")
        return _import_cloud_text_state(month_dir, extracted, month=month, metadata=metadata)


def _import_cloud_text_state(month_dir: Path, seed_dir: Path, *, month: str, metadata: dict) -> dict:
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("Invalid seed month")
    date.fromisoformat(month + "-01")
    for filename, expected in metadata["files_sha256"].items():
        relative = Path(filename)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Invalid seed filename")
        if hashlib.sha256((seed_dir / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Cloud social seed file changed: " + filename)
    source_month = seed_dir / "months" / month
    source_state = json.loads((source_month / "social/state.json").read_text(encoding="utf-8"))
    destination_state = month_dir / "social/state.json"
    state = (json.loads(destination_state.read_text(encoding="utf-8")) if destination_state.exists() else
             {"schema_version": 1, "rollout_date": metadata["rollout_date"], "reports": {}})
    outcome = {"changed": False, "imported": [], "preserved": []}
    for identity, item in source_state["reports"].items():
        period = ReportPeriod.from_identity(identity)
        if period.month != month:
            raise ValueError("Seed report belongs to a different month")
        destination = _package_manifest(month_dir, period)
        previous = state["reports"].get(identity, {})
        if previous.get("status") == "preserved":
            outcome["preserved"].append(identity)
            continue
        if destination.parent.exists():
            manifest = _existing_package(destination, period)
            previous.update(status="generated", format="text", source=manifest["source_reference"],
                            manifest=destination.relative_to(month_dir).as_posix())
            previous.pop("last_error", None)
            state["reports"][identity] = previous
            outcome["preserved"].append(identity)
            continue
        if previous.get("status") == "generated":
            raise ValueError("Existing generated state has no cloud package")
        if item.get("status") == "generated":
            source_manifest = _package_manifest(source_month, period)
            _existing_package(source_manifest, period)
            destination.parent.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix=".social-import-", dir=destination.parent.parent) as temporary:
                stage = Path(temporary) / "package"
                shutil.copytree(source_manifest.parent, stage)
                _existing_package(stage / destination.name, period)
                stage.rename(destination.parent)
            outcome["imported"].append(identity)
            outcome["changed"] = True
        elif item.get("status") == "preserved":
            outcome["preserved"].append(identity)
        elif item.get("status") != "pending":
            raise ValueError("Invalid cloud social seed report status")
        state["reports"][identity] = dict(item)
    outcome["changed"] = _save_if_changed(destination_state, state) or outcome["changed"]
    return outcome


def generate_cloud_text(month_dir: Path, bundle: dict, *, source_reference: dict) -> Path:
    """Atomically install a complete package; never replace an existing package.

    Eligibility is checked by ``render_cloud_month``. This lower-level function
    also supports a caller that has already selected one eligible result.
    """
    period = validate_bundle(bundle)
    destination = _package_manifest(month_dir, period)
    if destination.parent.exists():
        _existing_package(destination, period)
        return destination
    destination.parent.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".social-stage-", dir=destination.parent.parent) as temporary:
        staged = Path(temporary) / "package"
        paths = render_bundle(bundle, staged, source_reference=source_reference)
        manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
        # Dataset snapshots can be restored under any path in a new session.
        _write_json(paths["manifest"], _portable_manifest(manifest))
        _existing_package(paths["manifest"], period)
        staged.rename(destination.parent)
    return destination


def render_cloud_month(month_dir: Path, *, month: str, source_reference: dict,
                       rollout_date: date, now: datetime | None = None) -> dict:
    """Generate only due periods from an already confirmed monthly snapshot.

    ``rollout_date`` uses the existing report scheduler's Tokyo calendar. An
    existing cloud state retains its original rollout date. Results that have
    not arrived stay pending, and malformed packages are preserved with an error
    instead of being overwritten. Only text generation is recorded here.
    """
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        raise ValueError("Invalid analysis month")
    date.fromisoformat(month + "-01")
    reference = dict(source_reference)
    if (not isinstance(reference.get("dataset"), str) or "/" not in reference["dataset"]
            or type(reference.get("version")) is not int or reference["version"] < 1):
        raise ValueError("Cloud text requires a confirmed analysis Dataset version")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Cloud text time must include a timezone")
    month_dir = Path(month_dir)
    state_path = month_dir / "social" / "state.json"
    state = (json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else
             {"schema_version": 1, "rollout_date": str(rollout_date), "reports": {}})
    if state.get("schema_version") != 1 or not isinstance(state.get("reports"), dict):
        raise ValueError("Unsupported cloud text state")
    periods = [period for period in due_periods(date.fromisoformat(state["rollout_date"]), now)
               if period.month == month]
    index_path = month_dir / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    outcome = {"changed": False, "generated": [], "preserved": [], "pending": []}
    for period in periods:
        item = state["reports"].setdefault(period.identity, {"status": "pending"})
        manifest_path = _package_manifest(month_dir, period)
        try:
            if item.get("status") == "preserved":
                item.pop("last_error", None)
                outcome["preserved"].append(period.identity)
                continue
            if manifest_path.parent.exists():
                manifest = _existing_package(manifest_path, period)
                # Recover an atomic package even if the previous process died
                # before its state update. Never regenerate an existing draft.
                item.update(status="generated", format="text",
                            manifest=manifest_path.relative_to(month_dir).as_posix(),
                            source=manifest["source_reference"])
                item.pop("last_error", None)
                outcome["preserved"].append(period.identity)
                continue
            if item.get("status") == "generated":
                raise ValueError("Cloud text state references a missing package; restore its Dataset version")
            entry = index.get("results", {}).get(period.identity)
            if not entry or entry.get("status") != "complete":
                raise ValueError("Completed cloud result not available")
            filename = entry["filename"]
            relative = Path(filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("Invalid cloud result filename")
            bundle = json.loads((month_dir / relative).read_text(encoding="utf-8"))
            validate_bundle(bundle, period)
            ref = {**reference, "filename": filename}
            manifest_path = generate_cloud_text(month_dir, bundle, source_reference=ref)
            item.update(status="generated", format="text", source=ref,
                        manifest=manifest_path.relative_to(month_dir).as_posix(),
                        generated_at=now.isoformat())
            item.pop("last_error", None)
            outcome["generated"].append(period.identity)
            outcome["changed"] = True
        except (ValueError, KeyError, TypeError, OSError) as exc:
            # Preserve fixed period targets and existing bytes. A later cloud
            # session can retry pending results without local timers.
            item["last_error"] = f"{type(exc).__name__}: {exc}"
            outcome["pending"].append(period.identity)
    if periods or state_path.exists():
        outcome["changed"] = _save_if_changed(state_path, state) or outcome["changed"]
    return outcome


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build a text-only cloud migration seed; no uploads")
    parser.add_argument("--build-seed", type=Path, required=True)
    parser.add_argument("--local-state", type=Path, required=True)
    parser.add_argument("--local-output", type=Path, required=True)
    args = parser.parse_args()
    summary = build_cloud_text_seed(args.build_seed, local_state_path=args.local_state,
                                    local_output_dir=args.local_output)
    print(json.dumps({"months": summary["months"], "reports": summary["reports"],
                      "file_count": len(summary["files_sha256"])}, ensure_ascii=False, indent=2))
