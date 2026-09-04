"""Experimental hooks for agent-assisted concolic scheduling.

The core framework stays deterministic: no model call is made from the fuzzer.
An external agent can consume the exported JSONL tasks, then write hints keyed
by input SHA-256 or path. The MPI master applies those hints as ordinary
scheduler overrides.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
import hashlib
import json
import math
import os
import selectors
import shlex
import signal
import subprocess
import time
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest


_MAX_AGENT_OUTPUT_BYTES = 1024 * 1024


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number {value!r}")


def _json_object_without_duplicates(
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member {key!r}")
        result[key] = value
    return result


def _strict_json_loads(value: str) -> Any:
    return json.loads(
        value,
        object_pairs_hook=_json_object_without_duplicates,
        parse_constant=_reject_json_constant,
    )


def load_hints(path: str | None) -> dict[str, dict[str, Any]]:
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            raw = _strict_json_loads(stream.read())
    except (OSError, ValueError, TypeError):
        return {}
    if isinstance(raw, dict) and isinstance(raw.get("hints"), list):
        raw = raw["hints"]
    hints: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, list):
        return hints
    for item in raw:
        if not isinstance(item, dict):
            continue
        key = str(item.get("sha256") or item.get("path") or "")
        if not key:
            continue
        hints[key] = item
    return hints


def apply_hint(message: dict[str, Any], hint: dict[str, Any],
               strategy_count: int) -> None:
    focus = hint.get("focus_bytes")
    if isinstance(focus, str):
        message["focus_bytes"] = focus
    try:
        target_branch = int(hint.get("target_branch", 0))
    except (TypeError, ValueError):
        target_branch = 0
    if target_branch > 0:
        message["target_branch"] = target_branch
    try:
        strategy = int(hint.get("strategy", -1))
    except (TypeError, ValueError):
        strategy = -1
    if 0 <= strategy < strategy_count:
        message["strategy"] = strategy
    focus_set = hint.get("focus_set")
    if isinstance(focus_set, str) and focus_set:
        message["focus_set"] = focus_set
    actions = hint.get("s2f_actions")
    if isinstance(actions, (list, tuple)):
        normalized = []
        seen = set()
        for item in actions:
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            try:
                branch = max(0, int(item[0]))
            except (TypeError, ValueError):
                continue
            action = str(item[1]).strip().lower()
            if branch > 0 and branch not in seen and action in {
                    "solve", "sample", "skip"}:
                normalized.append((branch, action))
                seen.add(branch)
        if normalized:
            message["s2f_actions"] = tuple(normalized)
    route = hint.get("route")
    if isinstance(route, str) and route:
        message["agentic_route"] = route[:32]


def append_task(path: str | None, task: dict[str, Any]) -> None:
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "a", encoding="utf-8") as stream:
            json.dump(task, stream, sort_keys=True)
            stream.write("\n")
    except OSError:
        return


def _run_bounded_command(
    argv: list[str], payload: bytes, timeout: float,
    output_limit: int = _MAX_AGENT_OUTPUT_BYTES,
) -> bytes | None:
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            start_new_session=True,
        )
    except (OSError, ValueError):
        return None
    assert process.stdin is not None and process.stdout is not None
    selector = selectors.DefaultSelector()
    output = bytearray()
    written = 0
    failed = False
    deadline = time.monotonic() + max(0.1, float(timeout))

    def terminate() -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            try:
                process.kill()
            except OSError:
                pass

    try:
        os.set_blocking(process.stdin.fileno(), False)
        os.set_blocking(process.stdout.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failed = True
                break
            for key, _ in selector.select(min(remaining, 0.1)):
                if key.data == "stdin":
                    try:
                        count = os.write(
                            process.stdin.fileno(), payload[written:])
                        written += count
                    except (BlockingIOError, BrokenPipeError, OSError):
                        written = len(payload)
                    if written >= len(payload):
                        selector.unregister(process.stdin)
                        process.stdin.close()
                else:
                    try:
                        chunk = os.read(process.stdout.fileno(), 64 * 1024)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(process.stdout)
                        process.stdout.close()
                        continue
                    output.extend(chunk)
                    if len(output) > output_limit:
                        failed = True
                        break
            if failed:
                break
        if failed:
            terminate()
        try:
            return_code = process.wait(
                timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            failed = True
            terminate()
            process.wait()
            return_code = process.returncode
        if failed or return_code != 0:
            return None
        return bytes(output)
    finally:
        selector.close()
        for stream in (process.stdin, process.stdout):
            if stream is not None and not stream.closed:
                stream.close()
        terminate()
        try:
            process.wait(timeout=0.1)
        except subprocess.TimeoutExpired:
            pass


def query_agent(command: str | None, task: dict[str, Any],
                timeout: float = 2.0) -> dict[str, Any]:
    """Ask an external bounded command for one scheduling hint.

    The command receives one JSON task on stdin and should print one JSON object
    compatible with apply_hint() on stdout. All failures degrade to no hint.
    """
    if not command:
        return {}
    try:
        argv = shlex.split(command)
    except ValueError:
        return {}
    if not argv:
        return {}
    try:
        payload = json.dumps(task, sort_keys=True).encode("utf-8")
        output = _run_bounded_command(argv, payload, timeout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    if not output:
        return {}
    try:
        raw = _strict_json_loads(output.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def sanitize_hint(raw: Any, strategy_count: int) -> dict[str, Any]:
    """Return only bounded scheduling fields accepted by the control plane."""
    if not isinstance(raw, dict):
        return {}
    probe: dict[str, Any] = {}
    apply_hint(probe, raw, strategy_count)
    result: dict[str, Any] = {}
    for name in (
        "focus_bytes", "focus_set", "target_branch", "strategy",
        "agentic_route", "s2f_actions",
    ):
        if name in probe:
            result["route" if name == "agentic_route" else name] = probe[name]
    return result


def _decode_backend_hint(raw: Any) -> dict[str, Any]:
    """Normalize direct, task-envelope, and chat-completion responses."""
    if isinstance(raw, dict) and isinstance(raw.get("hint"), dict):
        return raw["hint"]
    if isinstance(raw, dict) and isinstance(raw.get("choices"), list):
        try:
            content = raw["choices"][0]["message"]["content"]
            if isinstance(content, str):
                decoded = _strict_json_loads(content)
                return decoded if isinstance(decoded, dict) else {}
        except (IndexError, KeyError, TypeError, ValueError):
            return {}
    if isinstance(raw, dict) and isinstance(raw.get("content"), list):
        for block in raw["content"]:
            if not isinstance(block, dict) or not isinstance(
                    block.get("text"), str):
                continue
            try:
                decoded = _strict_json_loads(block["text"])
            except ValueError:
                continue
            if isinstance(decoded, dict):
                return decoded
    return raw if isinstance(raw, dict) else {}


def _load_backend_specs(raw: str | None) -> list[dict[str, Any]]:
    if not raw:
        return []
    try:
        if os.path.isfile(raw):
            with open(raw, encoding="utf-8") as stream:
                decoded = _strict_json_loads(stream.read())
        else:
            decoded = _strict_json_loads(raw)
    except (OSError, ValueError, TypeError):
        return []
    if isinstance(decoded, dict):
        decoded = decoded.get("backends", [decoded])
    return [item for item in decoded if isinstance(item, dict)] \
        if isinstance(decoded, list) else []


class AgenticBackend:
    name = "backend"

    def query(self, task: dict[str, Any], timeout: float) -> dict[str, Any]:
        raise NotImplementedError

    def query_with_metadata(
        self, task: dict[str, Any], timeout: float,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        """Return a response plus provider-reported token accounting.

        Legacy command backends do not have a trusted provider envelope, so
        their metadata is empty. Structured controllers still account for a
        conservative byte-derived token estimate in that case.
        """
        return self.query(task, timeout), {}


@dataclass
class CommandAgenticBackend(AgenticBackend):
    command: str
    name: str = "command"
    provider: str = "command"
    model: str = ""
    prompt_sha256: str = ""

    def query(self, task: dict[str, Any], timeout: float) -> dict[str, Any]:
        return query_agent(self.command, task, timeout)


@dataclass
class HttpAgenticBackend(AgenticBackend):
    url: str
    headers: dict[str, str]
    protocol: str = "task-json"
    model: str = ""
    system_prompt: str = (
        "Return one JSON scheduling hint using only strategy, target_branch, "
        "focus_bytes, focus_set, s2f_actions, and route.")
    name: str = "http"
    provider: str = "http"

    @staticmethod
    def _resolve_header(value: str) -> str:
        if value.startswith("env:"):
            return os.environ.get(value[4:], "")
        return value

    def _perform_request(
        self, task: dict[str, Any], timeout: float,
    ) -> dict[str, Any]:
        if self.protocol in {"chat", "chat-completions"}:
            payload: dict[str, Any] = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": self.system_prompt},
                    {"role": "user", "content": json.dumps(
                        task, sort_keys=True)},
                ],
                "temperature": 0,
            }
        else:
            payload = {"schema": 1, "task": task}
        headers = {
            "Content-Type": "application/json",
            **{
                str(name): self._resolve_header(str(value))
                for name, value in self.headers.items()
            },
        }
        request = urlrequest.Request(
            self.url,
            data=json.dumps(payload, sort_keys=True).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urlrequest.urlopen(
                    request, timeout=max(0.1, float(timeout))) as response:
                body = response.read(_MAX_AGENT_OUTPUT_BYTES + 1)
            if len(body) > _MAX_AGENT_OUTPUT_BYTES:
                return {}
            decoded = _strict_json_loads(body.decode("utf-8"))
        except (OSError, ValueError, UnicodeDecodeError, urlerror.URLError):
            return {}
        return decoded if isinstance(decoded, dict) else {}

    def query(self, task: dict[str, Any], timeout: float) -> dict[str, Any]:
        return _decode_backend_hint(self._perform_request(task, timeout))

    def query_with_metadata(
        self, task: dict[str, Any], timeout: float,
    ) -> tuple[dict[str, Any], dict[str, int]]:
        decoded = self._perform_request(task, timeout)
        usage = decoded.get("usage", {}) if isinstance(decoded, dict) else {}
        metadata: dict[str, int] = {}
        if isinstance(usage, dict):
            for source, target in (
                ("prompt_tokens", "input_tokens"),
                ("input_tokens", "input_tokens"),
                ("completion_tokens", "output_tokens"),
                ("output_tokens", "output_tokens"),
            ):
                value = usage.get(source)
                if isinstance(value, int) and not isinstance(value, bool) \
                        and value >= 0:
                    metadata[target] = max(metadata.get(target, 0), value)
        return _decode_backend_hint(decoded), metadata


@dataclass
class _BackendState:
    backend: AgenticBackend
    failures: int = 0
    open_until: float = 0.0
    calls: int = 0
    successes: int = 0


class AgenticBackendManager:
    """Asynchronous provider portfolio with validation and circuit breaking."""

    def __init__(
        self,
        backends: list[AgenticBackend],
        strategy_count: int,
        *,
        timeout: float = 2.0,
        workers: int = 2,
        failure_threshold: int = 3,
        cooldown: float = 30.0,
        cache_size: int = 2048,
    ):
        self.backends = [_BackendState(backend) for backend in backends]
        self.strategy_count = max(1, int(strategy_count))
        self.timeout = max(0.1, float(timeout))
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown = max(1.0, float(cooldown))
        self.cache_size = max(1, int(cache_size))
        self.executor = ThreadPoolExecutor(
            max_workers=max(1, int(workers)),
            thread_name_prefix="symcc-agent")
        self.pending: dict[
            str, tuple[Future[dict[str, Any]], tuple[str, ...]]] = {}
        self.ready: list[tuple[tuple[str, ...], dict[str, Any]]] = []
        self.cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.submitted = 0
        self.completed = 0

    @classmethod
    def from_environment(
        cls,
        strategy_count: int,
        *,
        legacy_command: str = "",
        timeout: float = 2.0,
    ) -> "AgenticBackendManager | None":
        specs = _load_backend_specs(os.environ.get("SYMCC_AGENTIC_BACKENDS"))
        backends: list[AgenticBackend] = []
        for index, spec in enumerate(specs):
            kind = str(spec.get("type", "command")).strip().lower()
            name = str(spec.get("name", f"{kind}-{index}"))[:64]
            if kind == "command" and spec.get("command"):
                backends.append(CommandAgenticBackend(
                    str(spec["command"]),
                    name=name,
                    provider=str(spec.get("provider", "command")),
                    model=str(spec.get("model", "")),
                    prompt_sha256=str(spec.get("prompt_sha256", "")),
                ))
            elif kind in {"http", "http-json", "chat-completions"} \
                    and spec.get("url"):
                protocol = str(spec.get(
                    "protocol",
                    "chat-completions" if kind == "chat-completions"
                    else "task-json"))
                headers = spec.get("headers", {})
                backends.append(HttpAgenticBackend(
                    str(spec["url"]),
                    {
                        str(key): str(value)
                        for key, value in headers.items()
                    } if isinstance(headers, dict) else {},
                    protocol=protocol,
                    model=str(spec.get("model", "")),
                    system_prompt=str(spec.get(
                        "system_prompt",
                        HttpAgenticBackend.system_prompt)),
                    name=name,
                    provider=str(spec.get("provider", "http")),
                ))
        if legacy_command:
            backends.append(CommandAgenticBackend(
                legacy_command, name="legacy-command"))
        if not backends:
            return None
        try:
            workers = int(os.environ.get("SYMCC_AGENTIC_WORKERS", "2"))
            threshold = int(os.environ.get(
                "SYMCC_AGENTIC_FAILURE_THRESHOLD", "3"))
            cooldown = float(os.environ.get(
                "SYMCC_AGENTIC_COOLDOWN", "30"))
            cache_size = int(os.environ.get(
                "SYMCC_AGENTIC_CACHE", "2048"))
        except ValueError:
            workers, threshold, cooldown, cache_size = 2, 3, 30.0, 2048
        return cls(
            backends, strategy_count, timeout=timeout, workers=workers,
            failure_threshold=threshold, cooldown=cooldown,
            cache_size=cache_size)

    @staticmethod
    def _task_key(task: dict[str, Any]) -> str:
        payload = json.dumps(
            task, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _task_aliases(task: dict[str, Any]) -> tuple[str, ...]:
        values = (
            str(task.get("sha256", "") or ""),
            str(task.get("input_path", "") or ""),
        )
        return tuple(dict.fromkeys(value for value in values if value))

    def query(self, task: dict[str, Any]) -> dict[str, Any]:
        key = self._task_key(task)
        cached = self.cache.get(key)
        if cached is not None:
            self.cache.move_to_end(key)
            return dict(cached)
        now = time.monotonic()
        for state in self.backends:
            if state.open_until > now:
                continue
            state.calls += 1
            try:
                raw = state.backend.query(task, self.timeout)
            except Exception:
                raw = {}
            hint = sanitize_hint(raw, self.strategy_count)
            if hint:
                state.failures = 0
                state.successes += 1
                self.cache[key] = hint
                while len(self.cache) > self.cache_size:
                    self.cache.popitem(last=False)
                return dict(hint)
            state.failures += 1
            if state.failures >= self.failure_threshold:
                state.open_until = now + self.cooldown
                state.failures = 0
        return {}

    def submit(self, task: dict[str, Any]) -> bool:
        key = self._task_key(task)
        aliases = self._task_aliases(task)
        if key in self.cache:
            self.ready.append((aliases, dict(self.cache[key])))
            return True
        if key in self.pending:
            return False
        self.pending[key] = (
            self.executor.submit(self.query, dict(task)), aliases)
        self.submitted += 1
        return True

    def drain(self) -> list[tuple[tuple[str, ...], dict[str, Any]]]:
        completed = self.ready
        self.ready = []
        for key, (future, aliases) in list(self.pending.items()):
            if not future.done():
                continue
            del self.pending[key]
            try:
                hint = future.result()
            except Exception:
                hint = {}
            if hint:
                completed.append((aliases, hint))
            self.completed += 1
        return completed

    def snapshot(self) -> dict[str, Any]:
        return {
            "submitted": self.submitted,
            "completed": self.completed,
            "pending": len(self.pending),
            "cache": len(self.cache),
            "backends": {
                state.backend.name: {
                    "calls": state.calls,
                    "successes": state.successes,
                    "circuit_open": state.open_until > time.monotonic(),
                }
                for state in self.backends
            },
        }

    def close(self) -> None:
        self.executor.shutdown(wait=True, cancel_futures=True)


def _nonnegative_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _finite_float(value: Any, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return parsed if math.isfinite(parsed) else default


class BuiltinAgenticPlanner:
    """Deterministic stateful planner for concolic scheduling hints."""

    def __init__(self, state_path: str | None, strategy_count: int,
                 exploration: float = 0.35,
                 route_mode: str | None = None) -> None:
        self.state_path = state_path or ""
        self.strategy_count = max(1, int(strategy_count))
        self.exploration = max(0.0, float(exploration))
        self.route_mode = (route_mode or os.environ.get(
            "SYMCC_AGENTIC_ROUTE", "hybrid")).strip().lower()
        if self.route_mode in {"0", "false", "off", "none"}:
            self.route_mode = "off"
        if self.route_mode not in {
                "off", "hybrid", "cottontail", "concollmic", "gordian"}:
            self.route_mode = "hybrid"
        self.state: dict[str, Any] = {
            "schema": 2,
            "updates": 0,
            "strategies": {},
            "branches": {},
            "inputs": {},
            "saved_at": 0.0,
        }
        self.load()

    def load(self) -> None:
        if not self.state_path or not os.path.isfile(self.state_path):
            return
        try:
            with open(self.state_path, encoding="utf-8") as stream:
                raw = json.load(stream)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(raw, dict):
            return
        if isinstance(raw.get("strategies"), dict):
            self.state["strategies"] = raw["strategies"]
        if isinstance(raw.get("branches"), dict):
            self.state["branches"] = raw["branches"]
        if isinstance(raw.get("inputs"), dict):
            self.state["inputs"] = raw["inputs"]
        self.state["updates"] = _nonnegative_int(raw.get("updates", 0))

    def save(self) -> None:
        if not self.state_path:
            return
        self.state["saved_at"] = time.time()
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self.state_path)),
                        exist_ok=True)
            tmp = f"{self.state_path}.{os.getpid()}.tmp"
            with open(tmp, "w", encoding="utf-8") as stream:
                json.dump(self.state, stream, sort_keys=True)
                stream.write("\n")
            os.replace(tmp, self.state_path)
        except OSError:
            return

    def _strategy_stats(self, strategy: int) -> dict[str, Any]:
        strategies = self.state.setdefault("strategies", {})
        key = str(strategy)
        stats = strategies.get(key)
        if not isinstance(stats, dict):
            stats = {"n": 0, "reward": 0.0, "cost": 1.0, "success": 0}
            strategies[key] = stats
        return stats

    def _branch_stats(self, branch: int) -> dict[str, Any]:
        branches = self.state.setdefault("branches", {})
        key = str(branch)
        stats = branches.get(key)
        if not isinstance(stats, dict):
            stats = {"n": 0, "reward": 0.0, "success": 0, "last_seen": 0}
            branches[key] = stats
        return stats

    def _input_key(self, task: dict[str, Any]) -> str:
        key = str(task.get("sha256") or task.get("input_path") or "")
        return key[:256]

    def _input_stats(self, key: str) -> dict[str, Any]:
        inputs = self.state.setdefault("inputs", {})
        stats = inputs.get(key)
        if not isinstance(stats, dict):
            stats = {
                "n": 0,
                "route": "",
                "focus_bytes": "",
                "target_branch": 0,
                "strategy": -1,
                "reward": 0.0,
                "last_seen": 0,
            }
            inputs[key] = stats
        return stats

    @staticmethod
    def _best_comparison_taint(
        telemetry: dict[str, Any],
    ) -> tuple[int, str, float] | None:
        taints = telemetry.get("comparison_taints", ())
        if not isinstance(taints, (list, tuple)):
            return None
        best: tuple[float, int, str] | None = None
        for entry in taints[:128]:
            if not isinstance(entry, (list, tuple)) or len(entry) < 7:
                continue
            site, branch, count, lo, hi, _taken, interesting = (
                _nonnegative_int(value) for value in entry[:7])
            if not branch or lo > hi:
                continue
            span = max(1, hi - lo + 1)
            locality = min(1.0, count / span)
            score = locality + 0.5 * int(bool(interesting))
            focus = f"{max(0, lo - 2)}-{hi + 2}"
            candidate = (score, branch, focus)
            if best is None or candidate > best:
                best = candidate
        if best is None:
            return None
        return best[1], best[2], best[0]

    def _strategy_for_route(self, route: str) -> int:
        if route == "gordian":
            if self.strategy_count > 6:
                return 6
            if self.strategy_count > 5:
                return 5
        if route == "cottontail":
            if self.strategy_count > 4:
                return 4
            if self.strategy_count > 2:
                return 2
        if route == "concollmic" and self.strategy_count > 5:
            return 5
        return max(
            range(self.strategy_count),
            key=lambda strategy: self._score_strategy(strategy, 0))

    def _route_allowed(self, route: str) -> bool:
        return self.route_mode == "hybrid" or self.route_mode == route

    def _learn_route(
        self,
        task: dict[str, Any],
        telemetry: dict[str, Any],
        result: dict[str, Any],
        reward: float,
    ) -> None:
        if self.route_mode == "off":
            return
        key = self._input_key(task)
        if not key:
            return
        route = ""
        focus = ""
        target = _nonnegative_int(
            telemetry.get("target_branch", task.get("target_branch", 0)))

        taint = self._best_comparison_taint(telemetry)
        if taint is not None and self._route_allowed("cottontail"):
            target, focus, _score = taint
            route = "cottontail"

        hostile = (
            _nonnegative_int(telemetry.get("generated", 0)) == 0 and
            (_nonnegative_int(telemetry.get("solver_unknown", 0)) > 0 or
             _nonnegative_int(telemetry.get("z3_timeouts", 0)) > 0 or
             _nonnegative_int(telemetry.get("backsolver_z3_fallbacks", 0)) > 0)
        )
        if hostile and self._route_allowed("gordian"):
            route = "gordian"

        actions = telemetry.get("s2f_action_branches", 0)
        if not route and _nonnegative_int(actions) > 0 and self._route_allowed(
                "concollmic"):
            route = "concollmic"

        if not route:
            return
        stats = self._input_stats(key)
        stats["n"] = _nonnegative_int(stats.get("n", 0)) + 1
        stats["route"] = route
        if focus:
            stats["focus_bytes"] = focus
        if target:
            stats["target_branch"] = target
        stats["strategy"] = self._strategy_for_route(route)
        stats["reward"] = 0.75 * _finite_float(stats.get("reward", 0.0)) + \
            0.25 * reward
        stats["last_seen"] = int(time.time())

    def _route_hint(self, task: dict[str, Any]) -> dict[str, Any]:
        if self.route_mode == "off":
            return {}
        key = self._input_key(task)
        if not key:
            return {}
        stats = self.state.setdefault("inputs", {}).get(key)
        if not isinstance(stats, dict):
            return {}
        route = str(stats.get("route", ""))
        if route not in {"cottontail", "concollmic", "gordian"}:
            return {}
        if not self._route_allowed(route):
            return {}
        hint: dict[str, Any] = {"route": route}
        focus = stats.get("focus_bytes")
        if isinstance(focus, str) and focus:
            hint["focus_bytes"] = focus
        target = _nonnegative_int(stats.get("target_branch", 0))
        if target:
            hint["target_branch"] = target
            if route == "concollmic":
                hint["s2f_actions"] = [[target, "solve"]]
            elif route == "gordian":
                hint["s2f_actions"] = [[target, "sample"]]
        try:
            strategy = int(stats.get("strategy", -1))
        except (TypeError, ValueError, OverflowError):
            strategy = -1
        if 0 <= strategy < self.strategy_count:
            hint["strategy"] = strategy
        return hint

    def _score_strategy(self, strategy: int, target_branch: int) -> float:
        stats = self._strategy_stats(strategy)
        n = _nonnegative_int(stats.get("n", 0))
        total = max(1, _nonnegative_int(self.state.get("updates", 0)))
        reward = _finite_float(stats.get("reward", 0.0))
        cost = max(1.0, _finite_float(stats.get("cost", 1.0), 1.0))
        exploitation = reward / cost
        exploration = self.exploration * math.sqrt(math.log(total + 1) / (n + 1))
        branch_bonus = 0.0
        if target_branch:
            bstats = self._branch_stats(target_branch)
            branch_bonus = 0.05 * _finite_float(bstats.get("reward", 0.0))
        return exploitation + exploration + branch_bonus

    def _select_target(self, task: dict[str, Any]) -> int:
        current = _nonnegative_int(task.get("target_branch", 0))
        if current:
            return current
        raw = task.get("open_branches", ())
        if not isinstance(raw, (list, tuple)):
            return 0
        candidates = [
            branch for branch in (_nonnegative_int(value) for value in raw[:64])
            if branch > 0
        ]
        if not candidates:
            return 0
        total = max(1, _nonnegative_int(self.state.get("updates", 0)))

        def score(branch: int) -> float:
            stats = self._branch_stats(branch)
            n = _nonnegative_int(stats.get("n", 0))
            reward = _finite_float(stats.get("reward", 0.0))
            return reward + self.exploration * math.sqrt(
                math.log(total + 1) / (n + 1))

        return max(candidates, key=score)

    def suggest(self, task: dict[str, Any]) -> dict[str, Any]:
        target = self._select_target(task)
        best_strategy = max(
            range(self.strategy_count),
            key=lambda strategy: self._score_strategy(strategy, target))
        hint: dict[str, Any] = {"strategy": best_strategy}
        if target:
            hint["target_branch"] = target
        focus = task.get("focus_bytes")
        if isinstance(focus, str) and focus:
            hint["focus_bytes"] = focus
        route_hint = self._route_hint(task)
        if route_hint:
            hint.update(route_hint)
        return hint

    def observe(self, task: dict[str, Any], telemetry: dict[str, Any] | None,
                result: dict[str, Any] | None = None) -> None:
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        result = result if isinstance(result, dict) else {}
        strategy = _nonnegative_int(
            result.get("strategy", task.get("strategy", 0)))
        if strategy >= self.strategy_count:
            return
        generated = _nonnegative_int(
            telemetry.get("generated", result.get("total_generated", 0)))
        solver_sat = _nonnegative_int(telemetry.get("solver_sat", 0))
        solver_unknown = _nonnegative_int(telemetry.get("solver_unknown", 0))
        poly = (
            _nonnegative_int(telemetry.get("poly_cache_hits", 0)) +
            _nonnegative_int(telemetry.get("poly_samples", 0))
        )
        data_features = telemetry.get("data_features", ())
        data_bonus = min(1.0, len(data_features) / 16.0) \
            if isinstance(data_features, (list, tuple)) else 0.0
        comparison_taints = telemetry.get("comparison_taints", ())
        comparison_bonus = min(1.0, len(comparison_taints) / 32.0) \
            if isinstance(comparison_taints, (list, tuple)) else 0.0
        target_reached = 1.0 if telemetry.get("target_reached") else 0.0
        reward = (
            float(generated) + 0.35 * min(4, solver_sat) +
            0.25 * min(4, poly) + data_bonus + 0.5 * comparison_bonus
            + 2.0 * target_reached -
            0.2 * min(4, solver_unknown)
        )
        self._learn_route(task, telemetry, result, reward)
        cost = max(
            1.0,
            _finite_float(telemetry.get("solver_time_us", 0.0)) / 1_000_000.0,
            _finite_float(result.get("elapsed", 0.0), 0.0),
        )
        stats = self._strategy_stats(strategy)
        n = _nonnegative_int(stats.get("n", 0))
        alpha = 1.0 / min(32, n + 1)
        stats["n"] = n + 1
        stats["reward"] = (
            (1.0 - alpha) * _finite_float(stats.get("reward", 0.0)) +
            alpha * reward)
        stats["cost"] = (
            (1.0 - alpha) * _finite_float(stats.get("cost", 1.0), 1.0) +
            alpha * cost)
        stats["success"] = _nonnegative_int(stats.get("success", 0)) + (
            1 if generated > 0 or target_reached else 0)

        branch = _nonnegative_int(
            telemetry.get("target_branch", task.get("target_branch", 0)))
        if branch:
            bstats = self._branch_stats(branch)
            bn = _nonnegative_int(bstats.get("n", 0))
            balpha = 1.0 / min(32, bn + 1)
            bstats["n"] = bn + 1
            bstats["reward"] = (
                (1.0 - balpha) * _finite_float(bstats.get("reward", 0.0)) +
                balpha * reward)
            bstats["success"] = _nonnegative_int(bstats.get("success", 0)) + (
                1 if target_reached or generated > 0 else 0)
            bstats["last_seen"] = int(time.time())

        self.state["updates"] = _nonnegative_int(
            self.state.get("updates", 0)) + 1
        if self.state["updates"] % 16 == 0:
            self.save()
