"""Part 11.2.2 - transferable self-annealing state packs.

``hub-anneal export-state`` atomically freezes and packages the isolated
self-annealing subsystem so it restores bit-exact on new hardware:

* a SQLite WAL checkpoint of ``local_brain.sqlite``
* cognitive observer state and bit-flip history (``quipu_state.json``)
* locally annealed LoRA adapter weights and hyperparameters
* knowledge corpus graph entities and edges
* a SHA-256 manifest, hash-chained and optionally signed

``hub-anneal import-state`` verifies the manifest (and signature when the
broker public key is available), then restores.

Compression prefers zstandard when installed and falls back to gzip; the pack
records which was used, so a pack written on one host always opens on another.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import sqlite3
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .errors import StatePackError

MANIFEST_NAME = "statepack.manifest.json"
PACK_FORMAT = "v1"
DEFAULT_SUFFIX = ".statepack"

#: Files and directories that constitute the isolated annealing state.
STATE_MEMBERS = (
    "quipu_state.json",
    "local_brain.sqlite",
    "adapters",
    "corpus_entity.jsonl",
    "corpus_edge.jsonl",
)


@dataclass
class PackManifest:
    """The signed content index of a state pack."""

    format: str
    created_at: str
    source_host: str
    compression: str
    entries: List[Dict[str, Any]]
    chain_hash: str
    signature_ref: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "format": self.format,
            "createdAt": self.created_at,
            "sourceHost": self.source_host,
            "compression": self.compression,
            "entries": self.entries,
            "chainHash": self.chain_hash,
            "signatureRef": self.signature_ref,
        }


def _hostname() -> str:
    import socket

    return socket.gethostname() or "unknown"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _chain(entries: Iterable[Dict[str, Any]]) -> str:
    """Hash-chain the entry digests so reordering or omission is detectable."""
    running = hashlib.sha256(PACK_FORMAT.encode("utf-8")).hexdigest()
    for entry in sorted(entries, key=lambda e: e["path"]):
        running = hashlib.sha256(
            (running + "|" + entry["path"] + "|" + entry["sha256"]).encode("utf-8")
        ).hexdigest()
    return running


def _checkpoint_sqlite(path: Path) -> None:
    """TRUNCATE-checkpoint the WAL so the .sqlite file is self-contained."""
    if not path.exists():
        return
    try:
        connection = sqlite3.connect(str(path))
        try:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.commit()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise StatePackError("could not checkpoint " + path.name + ": " + str(exc)) from exc


def _have_zstd() -> bool:
    try:
        import zstandard  # noqa: F401

        return True
    except Exception:
        return False


def export_state(
    state_dir: Path | str,
    output: Path | str,
    signer: Optional[Any] = None,
    extra_members: Iterable[Path] = (),
) -> Dict[str, Any]:
    """Freeze and package the self-annealing state directory.

    ``signer`` may be any object exposing ``sign(bytes) -> bytes`` (the token
    broker's private key qualifies); when omitted the pack carries an unsigned
    manifest and ``signatureRef`` stays empty.
    """
    source = Path(state_dir)
    if not source.is_dir():
        raise StatePackError("self-annealing state directory not found: " + str(source))

    destination = Path(output)
    if destination.suffix not in (DEFAULT_SUFFIX, ".zst", ".gz", ".tar"):
        destination = destination.with_suffix(DEFAULT_SUFFIX)
    destination.parent.mkdir(parents=True, exist_ok=True)

    _checkpoint_sqlite(source / "local_brain.sqlite")

    staged: List[Path] = []
    for member in STATE_MEMBERS:
        candidate = source / member
        if candidate.exists():
            staged.append(candidate)
    staged.extend(Path(p) for p in extra_members if Path(p).exists())
    if not staged:
        raise StatePackError(
            "no annealing state present under " + str(source) + "; nothing to export"
        )

    entries: List[Dict[str, Any]] = []
    for path in staged:
        if path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file():
                    entries.append(
                        {
                            "path": child.relative_to(source).as_posix(),
                            "sha256": _sha256_file(child),
                            "bytes": child.stat().st_size,
                        }
                    )
        else:
            entries.append(
                {
                    "path": path.relative_to(source).as_posix(),
                    "sha256": _sha256_file(path),
                    "bytes": path.stat().st_size,
                }
            )

    compression = "zstd" if _have_zstd() else "gzip"
    manifest = PackManifest(
        format=PACK_FORMAT,
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        source_host=_hostname(),
        compression=compression,
        entries=entries,
        chain_hash=_chain(entries),
    )

    if signer is not None:
        try:
            from cryptography.hazmat.primitives import hashes
            from cryptography.hazmat.primitives.asymmetric import padding

            signature = signer.sign(
                manifest.chain_hash.encode("utf-8"), padding.PKCS1v15(), hashes.SHA256()
            )
            manifest.signature_ref = "rs256:" + hashlib.sha256(signature).hexdigest()
            _last_signature[manifest.chain_hash] = signature
        except Exception as exc:
            raise StatePackError("manifest signing failed: " + str(exc)) from exc

    _write_archive(source, staged, manifest, destination, compression)

    return {
        "output": str(destination),
        "bytes": destination.stat().st_size,
        "entries": len(entries),
        "chainHash": manifest.chain_hash,
        "compression": compression,
        "signatureRef": manifest.signature_ref,
    }


#: Detached signatures produced during this process, keyed by chain hash.
_last_signature: Dict[str, bytes] = {}


def _write_archive(
    source: Path,
    staged: List[Path],
    manifest: PackManifest,
    destination: Path,
    compression: str,
) -> None:
    """Write the tar archive atomically, compressing with zstd or gzip."""
    handle, tmp_name = tempfile.mkstemp(dir=str(destination.parent), suffix=".partial")
    os.close(handle)
    tmp_path = Path(tmp_name)
    try:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            payload = json.dumps(manifest.to_dict(), indent=2).encode("utf-8")
            info = tarfile.TarInfo(MANIFEST_NAME)
            info.size = len(payload)
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(payload))
            for path in staged:
                archive.add(str(path), arcname=path.relative_to(source).as_posix())
        raw = buffer.getvalue()

        if compression == "zstd":
            import zstandard

            compressed = zstandard.ZstdCompressor(level=10).compress(raw)
        else:
            import gzip

            compressed = gzip.compress(raw, compresslevel=6)

        tmp_path.write_bytes(compressed)
        os.replace(str(tmp_path), str(destination))
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _read_archive(pack: Path) -> bytes:
    raw = pack.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        import gzip

        return gzip.decompress(raw)
    if raw[:4] == b"\x28\xb5\x2f\xfd":
        try:
            import zstandard
        except Exception as exc:
            raise StatePackError(
                "pack is zstd-compressed but the zstandard package is not installed "
                "on this host; install it or re-export with gzip"
            ) from exc
        return zstandard.ZstdDecompressor().decompress(raw, max_output_size=1 << 31)
    return raw


def inspect(pack: Path | str) -> Dict[str, Any]:
    """Read a pack manifest without extracting anything."""
    path = Path(pack)
    if not path.exists():
        raise StatePackError("state pack not found: " + str(path))
    with tarfile.open(fileobj=io.BytesIO(_read_archive(path)), mode="r:") as archive:
        try:
            member = archive.extractfile(MANIFEST_NAME)
        except KeyError:
            member = None
        if member is None:
            raise StatePackError("state pack has no manifest; refusing to trust it")
        manifest = json.loads(member.read().decode("utf-8"))
    recomputed = _chain(manifest.get("entries", []))
    if recomputed != manifest.get("chainHash"):
        raise StatePackError(
            "manifest hash chain does not match its entries; the pack was altered"
        )
    return manifest


def import_state(
    pack: Path | str,
    state_dir: Path | str,
    verify_key: Optional[Any] = None,
    signature: Optional[bytes] = None,
) -> Dict[str, Any]:
    """Verify and restore a state pack into ``state_dir``.

    Extraction is path-checked member by member: a pack containing an absolute
    path, a ``..`` traversal, or a symlink is refused outright.
    """
    path = Path(pack)
    manifest = inspect(path)
    destination = Path(state_dir)
    destination.mkdir(parents=True, exist_ok=True)

    if verify_key is not None:
        detached = signature or _last_signature.get(manifest["chainHash"])
        if detached is None:
            raise StatePackError(
                "signature verification was requested but no detached signature was supplied"
            )
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        try:
            verify_key.verify(
                detached,
                manifest["chainHash"].encode("utf-8"),
                padding.PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise StatePackError("state pack signature is invalid; import refused") from exc

    expected = {entry["path"]: entry["sha256"] for entry in manifest["entries"]}
    staging = Path(tempfile.mkdtemp(prefix="statepack-", dir=str(destination.parent)))
    try:
        with tarfile.open(fileobj=io.BytesIO(_read_archive(path)), mode="r:") as archive:
            for member in archive.getmembers():
                if member.name == MANIFEST_NAME:
                    continue
                _assert_safe_member(member)
                archive.extract(member, path=str(staging), filter="data")

        restored: List[str] = []
        for relative, digest in expected.items():
            candidate = staging / relative
            if not candidate.is_file():
                raise StatePackError("pack is missing manifest entry " + relative)
            if _sha256_file(candidate) != digest:
                raise StatePackError("digest mismatch for " + relative + "; import refused")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(candidate), str(target))
            restored.append(relative)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return {
        "restored": sorted(restored),
        "chainHash": manifest["chainHash"],
        "sourceHost": manifest.get("sourceHost"),
        "createdAt": manifest.get("createdAt"),
        "stateDirectory": str(destination),
    }


def _assert_safe_member(member: tarfile.TarInfo) -> None:
    if member.issym() or member.islnk():
        raise StatePackError("state pack contains a link member " + member.name + "; refused")
    if member.isdev() or member.isfifo():
        raise StatePackError("state pack contains a device member " + member.name + "; refused")
    name = member.name.replace("\\", "/")
    if name.startswith("/") or ".." in Path(name).parts or ":" in name:
        raise StatePackError("state pack member " + member.name + " escapes the target; refused")
