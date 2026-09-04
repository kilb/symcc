"""Crash-recoverable publication for globally triaged AFL queue entries."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
import re
import stat
import tempfile
from typing import Mapping, Sequence

from distributed_state import stable_regular_file_snapshot


_SCHEMA = 1
_MAX_RECORDS = 4096
_MAX_MANIFEST_BYTES = 8 * 1024 * 1024
_MAX_DIRECTORY_ENTRIES = _MAX_RECORDS * 4 + 1024
_TRANSACTION_ID = re.compile(r"[0-9a-f]{64}")
_STAGE_NAME = re.compile(r"([0-9a-f]{64})-([0-9]{6})\.candidate")
_SOURCE_ID = re.compile(r"[0-9]{1,10}")
_MAX_AFL_QUEUE_ID = (1 << 32) - 1


def _sync_directory(path: str) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path or ".", flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: str) -> None:
    if os.path.isdir(path):
        return
    os.makedirs(path, exist_ok=True)
    _sync_directory(os.path.dirname(path) or ".")


def _atomic_publish(path: str, content: bytes) -> None:
    directory = os.path.dirname(path) or "."
    descriptor, temporary = tempfile.mkstemp(
        prefix=".coverage-queue-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = ""
        _sync_directory(directory)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _object_without_duplicate_keys(
    pairs: list[tuple[str, object]],
) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate coverage queue transaction JSON key")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"invalid coverage queue transaction constant {value}")


def _manifest_digest(manifest: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical({
        key: value for key, value in manifest.items() if key != "sha256"
    })).hexdigest()


def _regular_file_sha256(path: str) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("coverage queue transaction object is not regular")
        digest = hashlib.sha256()
        while True:
            block = os.read(descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            raise OSError("coverage queue transaction object changed while reading")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class CoverageQueueCommit:
    destinations: tuple[str | None, ...]
    next_queue_id: int
    redundant: int
    conservatively_recovered: int = 0


class CoverageQueueTransactionStore:
    """Bind staged corpus bytes to a recoverable global-coverage decision."""

    def __init__(self, symcc_dir: str, queue_dir: str) -> None:
        self.root = os.path.join(symcc_dir, ".coverage_queue_transactions")
        self.queue_dir = queue_dir
        _ensure_directory(self.root)
        _ensure_directory(self.queue_dir)

    def _manifest_path(self, transaction_id: str) -> str:
        if _TRANSACTION_ID.fullmatch(transaction_id) is None:
            raise ValueError("invalid coverage queue transaction identity")
        return os.path.join(self.root, f"{transaction_id}.json")

    def _stage_path(self, stage_name: str) -> str:
        if _STAGE_NAME.fullmatch(stage_name) is None:
            raise ValueError("invalid coverage queue transaction stage")
        return os.path.join(self.root, stage_name)

    @staticmethod
    def _validate_manifest(raw: object) -> dict[str, object]:
        if not isinstance(raw, dict) or set(raw) != {
            "schema",
            "transaction_id",
            "state",
            "first_queue_id",
            "claim_deltas",
            "records",
            "sha256",
        }:
            raise ValueError("invalid coverage queue transaction manifest")
        transaction_id = raw.get("transaction_id")
        state = raw.get("state")
        first_queue_id = raw.get("first_queue_id")
        deltas = raw.get("claim_deltas")
        records = raw.get("records")
        if (
            raw.get("schema") != _SCHEMA
            or not isinstance(transaction_id, str)
            or _TRANSACTION_ID.fullmatch(transaction_id) is None
            or state not in {"prepared", "decided"}
            or not isinstance(deltas, list)
            or not isinstance(records, list)
            or not 1 <= len(records) <= _MAX_RECORDS
            or raw.get("sha256") != _manifest_digest(raw)
        ):
            raise ValueError("invalid coverage queue transaction manifest")
        for index, record in enumerate(records):
            expected_stage = f"{transaction_id}-{index:06d}.candidate"
            if (
                not isinstance(record, dict)
                or set(record) != {"stage", "src_id", "content_sha256"}
                or record.get("stage") != expected_stage
                or not isinstance(record.get("src_id"), str)
                or _SOURCE_ID.fullmatch(record["src_id"]) is None
                or int(record["src_id"]) > _MAX_AFL_QUEUE_ID
                or not isinstance(record.get("content_sha256"), str)
                or _TRANSACTION_ID.fullmatch(record["content_sha256"]) is None
            ):
                raise ValueError("invalid coverage queue transaction record")
        if state == "prepared":
            if first_queue_id is not None or deltas:
                raise ValueError("invalid prepared coverage queue transaction")
        elif (
            type(first_queue_id) is not int
            or first_queue_id < 0
            or first_queue_id > _MAX_AFL_QUEUE_ID
            or len(deltas) != len(records)
            or any(type(delta) is not int or delta < 0 for delta in deltas)
            or sum(delta > 0 for delta in deltas)
            > _MAX_AFL_QUEUE_ID - first_queue_id + 1
        ):
            raise ValueError("invalid decided coverage queue transaction")
        return raw

    def _read_manifest(self, transaction_id: str) -> dict[str, object]:
        try:
            snapshot = stable_regular_file_snapshot(
                self._manifest_path(transaction_id),
                max_bytes=_MAX_MANIFEST_BYTES,
                retain_content=True,
            )
            if snapshot.content is None:
                raise ValueError("coverage queue transaction manifest is empty")
            raw = json.loads(
                snapshot.content.decode("ascii"),
                object_pairs_hook=_object_without_duplicate_keys,
                parse_constant=_reject_json_constant,
            )
        except (UnicodeError, ValueError, TypeError) as error:
            raise ValueError("invalid coverage queue transaction manifest") from error
        validated = self._validate_manifest(raw)
        if snapshot.content != _canonical(validated) + b"\n":
            raise ValueError("coverage queue transaction manifest is not canonical")
        return validated

    def _write_manifest(self, manifest: Mapping[str, object]) -> None:
        payload = dict(manifest)
        payload["sha256"] = _manifest_digest(payload)
        validated = self._validate_manifest(payload)
        encoded = _canonical(validated) + b"\n"
        if len(encoded) > _MAX_MANIFEST_BYTES:
            raise ValueError("coverage queue transaction manifest exceeds byte budget")
        _atomic_publish(
            self._manifest_path(str(validated["transaction_id"])), encoded
        )

    def prepare(self, records: Sequence[tuple[bytes, str]]) -> str:
        if not 1 <= len(records) <= _MAX_RECORDS:
            raise ValueError("coverage queue transaction exceeds record budget")
        material = (
            f"{os.getpid()}\x00{os.getppid()}\x00".encode("ascii") + os.urandom(32)
        )
        transaction_id = hashlib.sha256(material).hexdigest()
        manifest_records: list[dict[str, str]] = []
        staged: list[str] = []
        try:
            for index, item in enumerate(records):
                if (
                    not isinstance(item, tuple)
                    or len(item) != 2
                    or not isinstance(item[0], bytes)
                    or not isinstance(item[1], str)
                    or _SOURCE_ID.fullmatch(item[1]) is None
                    or int(item[1]) > _MAX_AFL_QUEUE_ID
                ):
                    raise ValueError("invalid coverage queue transaction input")
                content, src_id = item
                stage_name = f"{transaction_id}-{index:06d}.candidate"
                stage_path = self._stage_path(stage_name)
                _atomic_publish(stage_path, content)
                staged.append(stage_path)
                manifest_records.append({
                    "stage": stage_name,
                    "src_id": src_id,
                    "content_sha256": hashlib.sha256(content).hexdigest(),
                })
            self._write_manifest({
                "schema": _SCHEMA,
                "transaction_id": transaction_id,
                "state": "prepared",
                "first_queue_id": None,
                "claim_deltas": [],
                "records": manifest_records,
            })
        except BaseException:
            # _atomic_publish() may have completed os.replace() before a
            # directory fsync reported failure.  In that case the manifest is
            # already visible and must be removed with its staged objects;
            # otherwise recovery observes a prepared transaction whose
            # candidates were deliberately rolled back below.
            try:
                os.unlink(self._manifest_path(transaction_id))
            except FileNotFoundError:
                pass
            for path in staged:
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
            _sync_directory(self.root)
            raise
        return transaction_id

    def decide(
        self,
        transaction_id: str,
        claim_deltas: Sequence[int],
        *,
        first_queue_id: int,
    ) -> None:
        manifest = self._read_manifest(transaction_id)
        if manifest["state"] == "decided":
            if (
                manifest["claim_deltas"] != list(claim_deltas)
                or manifest["first_queue_id"] != first_queue_id
            ):
                raise ValueError("coverage queue transaction decision changed")
            return
        manifest["state"] = "decided"
        manifest["first_queue_id"] = first_queue_id
        manifest["claim_deltas"] = list(claim_deltas)
        manifest.pop("sha256", None)
        self._write_manifest(manifest)

    def commit(
        self,
        transaction_id: str,
        *,
        conservatively_recovered: int = 0,
    ) -> CoverageQueueCommit:
        manifest = self._read_manifest(transaction_id)
        if manifest["state"] != "decided":
            raise ValueError("coverage queue transaction has no durable decision")
        first_queue_id = int(manifest["first_queue_id"])
        records = manifest["records"]
        deltas = manifest["claim_deltas"]
        assert isinstance(records, list) and isinstance(deltas, list)
        destinations: list[str | None] = []
        winner_offset = 0
        namespace_changed = False
        for record, delta in zip(records, deltas):
            assert isinstance(record, dict) and isinstance(delta, int)
            stage_path = self._stage_path(str(record["stage"]))
            if delta <= 0:
                try:
                    os.unlink(stage_path)
                    namespace_changed = True
                except FileNotFoundError:
                    pass
                destinations.append(None)
                continue
            queue_id = first_queue_id + winner_offset
            winner_offset += 1
            destination = os.path.join(
                self.queue_dir,
                f"id:{queue_id:06d},src:{record['src_id']}",
            )
            if os.path.exists(destination):
                if _regular_file_sha256(destination) != record["content_sha256"]:
                    raise FileExistsError(
                        "coverage queue recovery would overwrite another testcase"
                    )
                try:
                    os.unlink(stage_path)
                    namespace_changed = True
                except FileNotFoundError:
                    pass
            else:
                if not os.path.exists(stage_path):
                    raise FileNotFoundError(
                        "coverage queue transaction lost its staged testcase"
                    )
                os.replace(stage_path, destination)
                namespace_changed = True
            destinations.append(destination)
        if namespace_changed:
            _sync_directory(self.queue_dir)
            _sync_directory(self.root)
        os.unlink(self._manifest_path(transaction_id))
        _sync_directory(self.root)
        return CoverageQueueCommit(
            destinations=tuple(destinations),
            next_queue_id=first_queue_id + winner_offset,
            redundant=len(records) - winner_offset,
            conservatively_recovered=conservatively_recovered,
        )

    def recover(self, next_queue_id: int) -> CoverageQueueCommit:
        if type(next_queue_id) is not int or next_queue_id < 0:
            raise ValueError("invalid next coverage queue id")
        manifest_ids: list[str] = []
        stage_paths: dict[str, str] = {}
        temporary_paths: list[str] = []
        with os.scandir(self.root) as entries:
            scanned = 0
            for entry in entries:
                scanned += 1
                if scanned > _MAX_DIRECTORY_ENTRIES:
                    raise ValueError(
                        "coverage queue transaction directory exceeds budget"
                    )
                match = re.fullmatch(r"([0-9a-f]{64})\.json", entry.name)
                if match is not None:
                    if not entry.is_file(follow_symlinks=False):
                        raise ValueError(
                            "coverage queue transaction manifest is not regular"
                        )
                    manifest_ids.append(match.group(1))
                    continue
                stage_match = _STAGE_NAME.fullmatch(entry.name)
                if stage_match is not None:
                    if not entry.is_file(follow_symlinks=False):
                        raise ValueError(
                            "coverage queue transaction stage is not regular"
                        )
                    stage_paths[entry.name] = entry.path
                    continue
                if (
                    entry.name.startswith(".coverage-queue-")
                    and entry.name.endswith(".tmp")
                ):
                    if not entry.is_file(follow_symlinks=False):
                        raise ValueError(
                            "coverage queue transaction temporary is not regular"
                        )
                    temporary_paths.append(entry.path)
                    continue
                raise ValueError(
                    "coverage queue transaction directory has an invalid entry"
                )
        manifests = [
            (transaction_id, self._read_manifest(transaction_id))
            for transaction_id in sorted(manifest_ids)
        ]
        referenced_stages = {
            str(record["stage"])
            for _transaction_id, manifest in manifests
            for record in manifest["records"]
            if isinstance(record, dict)
        }
        orphan_paths = [
            path for stage, path in stage_paths.items()
            if stage not in referenced_stages
        ]
        removed_orphan = False
        for path in [*orphan_paths, *temporary_paths]:
            try:
                os.unlink(path)
                removed_orphan = True
            except FileNotFoundError:
                pass
        if removed_orphan:
            _sync_directory(self.root)
        decided = sorted(
            (
                (transaction_id, manifest)
                for transaction_id, manifest in manifests
                if manifest["state"] == "decided"
            ),
            key=lambda item: (int(item[1]["first_queue_id"]), item[0]),
        )
        prepared = [
            (transaction_id, manifest)
            for transaction_id, manifest in manifests
            if manifest["state"] == "prepared"
        ]
        all_destinations: list[str | None] = []
        redundant = 0
        conservative = 0
        # Finish already decided ranges before assigning IDs to ambiguous
        # prepared transactions. Otherwise a prepared transaction could reserve
        # an ID that an older, only partially promoted decision already owns.
        for transaction_id, manifest in [*decided, *prepared]:
            if manifest["state"] == "prepared":
                records = manifest["records"]
                assert isinstance(records, list)
                # A crash may have happened after the global owner committed but
                # before its return value became durable locally. Retaining every
                # staged input is the only recovery choice that cannot lose the
                # corpus corresponding to an authoritative coverage claim.
                self.decide(
                    transaction_id,
                    [1 for _ in records],
                    first_queue_id=next_queue_id,
                )
                conservative += len(records)
            committed = self.commit(
                transaction_id,
                conservatively_recovered=conservative,
            )
            all_destinations.extend(committed.destinations)
            next_queue_id = max(next_queue_id, committed.next_queue_id)
            redundant += committed.redundant
        return CoverageQueueCommit(
            destinations=tuple(all_destinations),
            next_queue_id=next_queue_id,
            redundant=redundant,
            conservatively_recovered=conservative,
        )
