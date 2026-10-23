from __future__ import annotations

from concurrent.futures import Future
import json
from pathlib import Path
import threading
import time
from typing import Any, Mapping

# Validators built ahead for the captures Cloud is heading into (see
# prewarm_artifact); a route over one of them takes it instead of building.
MAX_WARM_VALIDATORS = 2


def _pool_key(key: Any) -> str:
    """A hashable form of a validator cache key (it holds stat dicts)."""
    return json.dumps(key, sort_keys=True, default=str)


class PrewarmedIndependentJsonValidatorMixin:
    """Serialize validator construction and allow identity-keyed prewarming.

    This mixin deliberately does not change the standalone validator source or
    its validation algorithm.  It only constructs the existing independent
    analyzer before the first route asks for it.  The normal topology binding
    still recomputes/verifies all artifact and validator identities and drops
    the warm object if any cache-key component differs.
    """

    def _init_prewarm_state(self) -> None:
        self._prewarm_lock = threading.RLock()
        self.prewarm_status: dict[str, Any] = {"state": "not_started"}
        self._pool_lock = threading.Lock()
        self._warm_pool: dict[str, Future] = {}

    def _manifest_cache_key(self, manifest_path: str | Path) -> tuple[tuple[Any, ...], Path]:
        """The validator cache key bind_topology_session will form for a capture."""
        from .live_route_adapter import _path_stat_identity_for_validator
        from .topology_cache import resolve_manifest_artifact, validator_identity

        path = Path(manifest_path).resolve()
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(manifest, Mapping):
            raise ValueError("manifest_not_object")
        artifact_path = resolve_manifest_artifact(path, manifest).resolve()
        artifact_stat = _path_stat_identity_for_validator(artifact_path)
        if not artifact_stat.get("exists"):
            raise ValueError("artifact_missing")
        raw_sha = str(manifest.get("raw_artifact_sha256") or "").upper()
        static_fingerprint = str(manifest.get("static_topology_fingerprint") or "").upper()
        if len(raw_sha) != 64 or len(static_fingerprint) != 64:
            raise ValueError("manifest_topology_identity_incomplete")
        validation_path = self._validation_source_path().resolve()
        validation_stat = _path_stat_identity_for_validator(validation_path)
        source_sha = validator_identity(validation_path).upper()
        if source_sha != str(manifest.get("validator_source_sha256") or "").upper():
            raise ValueError("validator_source_identity_mismatch")
        key = (str(artifact_path), artifact_stat, raw_sha, static_fingerprint,
               str(validation_path), validation_stat, source_sha)
        return key, artifact_path

    def prewarm_artifact(self, manifest_path: str | Path) -> Mapping[str, Any]:
        """Build a validator for a capture in the background, into the warm pool.

        Unlike prewarm_from_manifest this leaves the bound validator alone, so
        it can run while a route elsewhere holds one: live, Kalm's capture took
        74 s to build with the game running, and each route there stood still
        for it. The route that later binds this capture takes the built one
        (take_warm); every identity is still checked at binding.
        """
        from .validator_process import ValidatorProcess

        try:
            key, artifact_path = self._manifest_cache_key(manifest_path)
        except Exception as exc:
            return {"state": "failed", "reason": type(exc).__name__, "detail": str(exc)[:300]}
        pool_key = _pool_key(key)
        with self._pool_lock:
            if getattr(self, "_validator", None) is not None and getattr(self, "_validator_cache_key", None) == key:
                return {"state": "current", "artifact_path": str(artifact_path)}
            if pool_key in self._warm_pool:
                return {"state": "ready" if self._warm_pool[pool_key].done() else "building",
                        "artifact_path": str(artifact_path)}
            future: Future = Future()
            self._warm_pool[pool_key] = future
            for stale in [item for item in self._warm_pool if item != pool_key][:max(0, len(self._warm_pool) - MAX_WARM_VALIDATORS)]:
                old = self._warm_pool.pop(stale)
                if old.done() and old.exception() is None:
                    old.result().close()
        started = time.perf_counter()
        try:
            proxy = ValidatorProcess(Path(key[4]), artifact_path, source_sha=key[6], artifact_sha=key[2])
            proxy.wait_ready()
        except Exception as exc:
            future.set_exception(exc)
            with self._pool_lock:
                if self._warm_pool.get(pool_key) is future:
                    del self._warm_pool[pool_key]
            return {"state": "failed", "reason": type(exc).__name__, "detail": str(exc)[:300]}
        future.set_result(proxy)
        return {"state": "ready", "artifact_path": str(artifact_path),
                "elapsed_ms": round((time.perf_counter() - started) * 1000.0)}

    def take_warm(self, key: Any, timeout: float) -> Any:
        """The pool's validator for ``key``, waiting for one still building."""
        with self._pool_lock:
            future = self._warm_pool.pop(_pool_key(key), None)
        if future is None:
            return None
        try:
            return future.result(timeout=timeout)
        except Exception:
            return None

    def _load(self) -> Any:
        with self._prewarm_lock:
            return super()._load()

    def bind_topology_session(self, topology: Any) -> Mapping[str, Any]:
        with self._prewarm_lock:
            return super().bind_topology_session(topology)

    def prewarm_from_manifest(self, manifest_path: str | Path) -> Mapping[str, Any]:
        """Construct the existing validator without granting movement authority.

        Manifest hashes are used only to form the speculative warm cache key.
        They are *not* trusted for authorization here.  Later
        ``bind_topology_session`` performs the ordinary provider-backed hash,
        fingerprint, source, world/map and stat-identity checks.  A mismatch
        clears this warm object and falls back to normal fail-closed loading.
        """
        from .live_route_adapter import _path_stat_identity_for_validator
        from .topology_cache import resolve_manifest_artifact, validator_identity

        path = Path(manifest_path).resolve()
        started = time.perf_counter()
        with self._prewarm_lock:
            if getattr(self, "_bound_topology_session_id", None) is not None:
                self.prewarm_status = {
                    "state": "skipped",
                    "reason": "validator_already_bound",
                }
                return dict(self.prewarm_status)
            try:
                manifest = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(manifest, Mapping):
                    raise ValueError("manifest_not_object")
                artifact_path = resolve_manifest_artifact(path, manifest).resolve()
                artifact_stat = _path_stat_identity_for_validator(artifact_path)
                if not artifact_stat.get("exists"):
                    raise ValueError("artifact_missing")

                raw_sha = str(manifest.get("raw_artifact_sha256") or "").upper()
                static_fingerprint = str(
                    manifest.get("static_topology_fingerprint") or ""
                ).upper()
                if len(raw_sha) != 64 or len(static_fingerprint) != 64:
                    raise ValueError("manifest_topology_identity_incomplete")

                validation_path = self._validation_source_path().resolve()
                validation_stat = _path_stat_identity_for_validator(validation_path)
                source_sha = validator_identity(validation_path).upper()
                manifest_source_sha = str(
                    manifest.get("validator_source_sha256") or ""
                ).upper()
                if source_sha != manifest_source_sha:
                    raise ValueError("validator_source_identity_mismatch")

                # This is exactly the key assembled later by
                # IndependentJsonValidator.bind_topology_session.  Setting it
                # before construction makes a concurrent bind safe: both bind
                # and load are serialized by this mixin's lock.
                self.snapshot_path = artifact_path
                self._validator_cache_key = (
                    str(artifact_path),
                    artifact_stat,
                    raw_sha,
                    static_fingerprint,
                    str(validation_path),
                    validation_stat,
                    source_sha,
                )
                require_binding = bool(getattr(self, "require_topology_binding", False))
                self.require_topology_binding = False
                try:
                    validator = super()._load()
                finally:
                    self.require_topology_binding = require_binding
                if validator is None:
                    raise ValueError("validator_construction_returned_none")
                elapsed_ms = (time.perf_counter() - started) * 1000.0
                self.prewarm_status = {
                    "state": "ready",
                    "artifact_path": str(artifact_path),
                    "raw_artifact_sha256": raw_sha,
                    "static_topology_fingerprint": static_fingerprint,
                    "validator_source_sha256": source_sha,
                    "elapsed_ms": elapsed_ms,
                    "construction_process_pid": getattr(getattr(self, "_prewarm_process", None), "worker_pid", None),
                }
            except Exception as exc:
                # Prewarming is only an optimization.  Never let a speculative
                # warm-up failure weaken or replace the ordinary route-time
                # fail-closed admission/binding path.
                self.close()
                self._validator_cache_key = None
                self.prewarm_status = {
                    "state": "failed",
                    "reason": type(exc).__name__,
                    "detail": " ".join(str(exc).split())[:512],
                    "elapsed_ms": (time.perf_counter() - started) * 1000.0,
                }
            return dict(self.prewarm_status)


def build_prewarmed_validator_class() -> type:
    """Return a lock/prewarm-enabled subclass without changing validator SHA."""
    from .live_route_adapter import IndependentJsonValidator

    class PrewarmedIndependentJsonValidator(
        PrewarmedIndependentJsonValidatorMixin,
        IndependentJsonValidator,
    ):
        def __init__(self, *args: Any, isolated: bool = False, **kwargs: Any) -> None:
            IndependentJsonValidator.__init__(self, *args, **kwargs)
            self._init_prewarm_state()
            self._isolated = isolated
            self._prewarm_process = None

        def _construct_validator(self, validation_path: Path) -> Any:
            if not self._isolated:
                return super()._construct_validator(validation_path)
            from .validator_process import VALIDATOR_READY_TIMEOUT_SECONDS, ValidatorProcess
            from .topology_cache import sha256_file
            key = self._validator_cache_key
            warm = self.take_warm(key, VALIDATOR_READY_TIMEOUT_SECONDS) if key else None
            if warm is not None:
                self._prewarm_process = warm
                return warm
            proxy = ValidatorProcess(
                validation_path, self.snapshot_path,
                source_sha=key[6] if key else sha256_file(validation_path),
                artifact_sha=key[2] if key else sha256_file(self.snapshot_path),
            )
            self._prewarm_process = proxy
            proxy.wait_ready()
            return proxy

        def close(self) -> None:
            super().close()
            proxy, self._prewarm_process = self._prewarm_process, None
            if proxy is not None:
                proxy.close()

    return PrewarmedIndependentJsonValidator


def validated_navigation_resource_key(
    manifest_path: Any,
    probe_dll_path: Any,
    native_probe_sha256: Any = None,
    validator_source_sha256: Any = None,
    validator_corpus: Any = None,
    allow_scoped_partial_topology: bool = False,
) -> tuple[str | None, ...]:
    """The key under which a worker keeps its validated navigation resources.

    runtime_commands and the prewarm both build it here. They used to build
    it separately and drifted (the prewarm lacked the scoped-partial flag),
    so every first route rebuilt the validator cold: 11.9 s live at Kalm.
    """
    return tuple(str(value) if value is not None else None for value in (
        Path(manifest_path).resolve(),
        Path(probe_dll_path).resolve(),
        native_probe_sha256,
        validator_source_sha256,
        Path(validator_corpus).resolve() if validator_corpus else None,
        allow_scoped_partial_topology,
    ))


def default_validated_navigation_resource_key() -> tuple[str | None, ...]:
    """runtime_commands' resource key for its default arguments."""
    from .live_route_adapter import (
        DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
        DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
    )

    return validated_navigation_resource_key(DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH, DEFAULT_TOPOLOGY_PROBE_DLL_PATH)


def prepare_default_validator_prewarm(service: Any) -> Mapping[str, Any]:
    """Attach default cache resources to a long-lived real-input service.

    The background worker reads no live telemetry and sends no input.  If the
    default cache is unavailable or stale, the later route command uses its
    existing provider/binding checks and either reconstructs normally or fails
    closed exactly as before.
    """
    from .live_route_adapter import (
        DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH,
        DEFAULT_TOPOLOGY_PROBE_DLL_PATH,
        ValidatedTopologyCacheProvider,
    )

    manifest_path = Path(DEFAULT_TOPOLOGY_CACHE_MANIFEST_PATH).resolve()
    if not manifest_path.is_file():
        status = {"state": "skipped", "reason": "default_manifest_missing"}
        setattr(service, "_independent_validator_prewarm_status", status)
        return status

    validator_type = build_prewarmed_validator_class()
    validator = validator_type(None, None, require_topology_binding=True, isolated=True)
    provider = ValidatedTopologyCacheProvider(
        manifest_path,
        native_probe_path=Path(DEFAULT_TOPOLOGY_PROBE_DLL_PATH),
    )
    resource_key = default_validated_navigation_resource_key()
    setattr(
        service,
        "_validated_navigation_resources",
        (resource_key, provider, validator),
    )

    status: dict[str, Any] = {
        "state": "started",
        "manifest_path": str(manifest_path),
    }
    setattr(service, "_independent_validator_prewarm_status", status)

    def run() -> None:
        result = validator.prewarm_from_manifest(manifest_path)
        setattr(service, "_independent_validator_prewarm_status", dict(result))
        # Then the planning graph for the capture covering Cloud now.
        try:
            position = (service.observe().get("player") or {}).get("position") or {}
            covering = (float(position["x"]), float(position["y"])) if position else None
        except Exception:
            covering = None
        try:
            graph_status = provider.prewarm_graph(covering)
        except Exception as exc:
            graph_status = {"state": "failed", "reason": type(exc).__name__, "detail": str(exc)[:256]}
        setattr(service, "_topology_graph_prewarm_status", graph_status)

    worker = threading.Thread(
        target=run,
        name="ff7r-independent-validator-prewarm",
        daemon=True,
    )
    setattr(service, "_independent_validator_prewarm_thread", worker)
    worker.start()
    return dict(status)
