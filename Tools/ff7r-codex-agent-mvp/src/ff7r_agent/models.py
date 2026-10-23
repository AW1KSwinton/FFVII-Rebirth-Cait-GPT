from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import math
from typing import Any


class ValidationError(ValueError):
    pass


class PoseSkewError(ValidationError):
    """The capsule and the actor root disagree within one sample."""


def _optional_text(value: dict[str, Any], key: str) -> str | None:
    result = value.get(key)
    if result is not None and not isinstance(result, str):
        raise ValidationError(f"{key} must be text")
    return result


def _optional_bool(value: dict[str, Any], key: str) -> bool | None:
    result = value.get(key)
    if result is not None and type(result) is not bool:
        raise ValidationError(f"{key} must be Boolean")
    return result


@dataclass(frozen=True)
class Vec3:
    x: float
    y: float
    z: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Vec3":
        try:
            result = cls(float(value["x"]), float(value["y"]), float(value["z"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("position must contain numeric x, y and z") from exc
        if not all(math.isfinite(component) for component in (result.x, result.y, result.z)):
            raise ValidationError("position values must be finite")
        return result


@dataclass(frozen=True)
class Rotation:
    pitch: float
    yaw: float
    roll: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Rotation":
        try:
            result = cls(float(value["pitch"]), float(value["yaw"]), float(value["roll"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("rotation must contain numeric pitch, yaw and roll") from exc
        if not all(math.isfinite(component)
                   for component in (result.pitch, result.yaw, result.roll)):
            raise ValidationError("rotation values must be finite")
        return result


@dataclass(frozen=True)
class RelativePosition:
    x: float
    y: float
    z: float
    horizontal_distance: float
    bearing_degrees: float
    heading_error_degrees: float

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RelativePosition":
        try:
            result = cls(
                float(value["x"]),
                float(value["y"]),
                float(value["z"]),
                float(value["horizontal_distance"]),
                float(value["bearing_degrees"]),
                float(value["heading_error_degrees"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid relative position") from exc
        if not all(math.isfinite(component) for component in (
            result.x, result.y, result.z, result.horizontal_distance,
            result.bearing_degrees, result.heading_error_degrees,
        )):
            raise ValidationError("relative position values must be finite")
        if result.horizontal_distance < 0:
            raise ValidationError("relative horizontal distance cannot be negative")
        return result


@dataclass(frozen=True)
class LocomotionState:
    reference: str
    feet_position: Vec3
    capsule_radius_cm: float
    capsule_half_height_cm: float
    velocity_cm_s: Vec3
    grounded: bool | None = None
    movement_mode: int | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "LocomotionState":
        try:
            radius, half = float(value["capsule_radius_cm"]), float(value["capsule_half_height_cm"])
            grounded = value.get("grounded")
            mode = value.get("movement_mode")
            if (value.get("reference") != "upright_capsule_bottom"
                or not math.isfinite(radius) or not math.isfinite(half)
                or not 0 < radius <= 200 or not radius <= half <= 300
                or (grounded is not None and not isinstance(grounded, bool))
                or (mode is not None and (isinstance(mode, bool) or not isinstance(mode, int) or not -1 <= mode <= 255))):
                raise ValidationError("invalid measured locomotion geometry or state")
            return cls("upright_capsule_bottom", Vec3.from_dict(value["feet_position"]),
                       radius, half, Vec3.from_dict(value["velocity_cm_s"]), grounded,
                       None if mode == -1 else mode)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid locomotion snapshot") from exc


@dataclass(frozen=True)
class Character:
    name: str
    hp: int
    max_hp: int
    mp: int
    max_mp: int
    atb: float = 0.0
    position: Vec3 | None = None
    controlled_character: str | None = None
    rotation: Rotation | None = None
    controller_state: int | None = None
    actor_id: str | None = None
    locomotion: LocomotionState | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Character":
        try:
            result = cls(
                name=str(value["name"]),
                hp=int(value["hp"]),
                max_hp=int(value["max_hp"]),
                mp=int(value["mp"]),
                max_mp=int(value["max_mp"]),
                atb=float(value.get("atb", 0.0)),
                position=Vec3.from_dict(value["position"]) if value.get("position") else None,
                controlled_character=(
                    str(value["controlled_character"])
                    if value.get("controlled_character") is not None
                    else None
                ),
                rotation=Rotation.from_dict(value["rotation"]) if value.get("rotation") else None,
                controller_state=(int(value["controller_state"])
                                  if value.get("controller_state") is not None else None),
                actor_id=str(value["actor_id"]) if value.get("actor_id") is not None else None,
                locomotion=LocomotionState.from_dict(value["locomotion"]) if value.get("locomotion") is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid character data") from exc
        if min(result.hp, result.max_hp, result.mp, result.max_mp) < 0:
            raise ValidationError("HP and MP values cannot be negative")
        if result.hp > result.max_hp or result.mp > result.max_mp:
            raise ValidationError("current HP/MP cannot exceed maximum")
        if result.controller_state is not None and not 0 <= result.controller_state <= 39:
            raise ValidationError("invalid controller state")
        if result.actor_id == "":
            raise ValidationError("actor identity cannot be empty")
        if result.locomotion is not None:
            physical = result.locomotion
            if result.position is None or not result.actor_id:
                raise ValidationError("locomotion requires the synchronized actor pose and identity")
            # The sampled capsule is the actor ROOT, so the measured bottom
            # plus scaled half-height must reconstruct this exact actor pose.
            horizontal = math.hypot(physical.feet_position.x - result.position.x,
                                    physical.feet_position.y - result.position.y)
            vertical = abs(physical.feet_position.z + physical.capsule_half_height_cm - result.position.z)
            if horizontal > 1.0 or vertical > 1.0:
                raise PoseSkewError("locomotion does not match the synchronized actor root "
                                    f"(horizontal {horizontal:.1f} cm, vertical {vertical:.1f} cm)")
        return result


@dataclass(frozen=True)
class Interactable:
    id: str
    kind: str
    distance: float
    usable: bool | None = True
    position: Vec3 | None = None
    relative: RelativePosition | None = None
    label: str | None = None
    prompt: str | None = None
    visible: bool | None = None
    opened: bool | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Interactable":
        try:
            usable = value.get("usable")
            if usable is not None and not isinstance(usable, bool):
                raise TypeError("usable must be Boolean")
            result = cls(
                str(value["id"]), str(value["kind"]),
                float(value["distance"]), usable,
                Vec3.from_dict(value["position"]) if value.get("position") else None,
                RelativePosition.from_dict(value["relative"])
                if value.get("relative") else None,
                str(value["label"]) if value.get("label") is not None else None,
                _optional_text(value, "prompt"),
                _optional_bool(value, "visible"),
                _optional_bool(value, "opened"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid interactable data") from exc
        if not result.id or not result.kind or not math.isfinite(result.distance) or result.distance < 0:
            raise ValidationError("interactable distance cannot be negative")
        return result

    def with_relative_to(self, player: "Character | None") -> "Interactable":
        if self.position is None or player is None or player.position is None or player.rotation is None:
            return self
        dx = (self.position.x - player.position.x) / 100.0
        dy = (self.position.y - player.position.y) / 100.0
        dz = (self.position.z - player.position.z) / 100.0
        horizontal = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dy, dx))
        heading_error = (bearing - player.rotation.yaw + 180.0) % 360.0 - 180.0
        relative = RelativePosition(dx, dy, dz, horizontal, bearing, heading_error)
        return Interactable(
            self.id, self.kind, self.distance, self.usable,
            self.position, relative, self.label, self.prompt, self.visible, self.opened,
        )


@dataclass(frozen=True)
class RestState:
    state: str = "idle"
    interaction_id: str | int | None = None
    state_identity: str | None = None
    bench_id: str | None = None
    started_sequence: int | None = None
    completed_sequence: int | None = None
    failure_reason: str | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "RestState":
        allowed = {
            "idle", "bench_detected", "interaction_started",
            "resting", "interaction_completed", "cancelled", "failed", "unknown",
        }
        try:
            state = str(value.get("state", "unknown"))
            if state not in allowed:
                raise ValidationError("invalid rest state")
            interaction_id = value.get("interaction_id")
            if interaction_id is not None and not isinstance(interaction_id, (str, int)):
                raise ValidationError("invalid rest interaction id")
            identity = value.get("state_identity")
            if identity is not None and not isinstance(identity, str):
                raise ValidationError("invalid rest state identity")
            bench_id = value.get("bench_id")
            failure_reason = value.get("failure_reason")
            if bench_id is not None and not isinstance(bench_id, str):
                raise ValidationError("invalid rest bench id")
            if failure_reason is not None and not isinstance(failure_reason, str):
                raise ValidationError("invalid rest failure reason")
            started = value.get("started_sequence")
            completed = value.get("completed_sequence")
            if started is not None and (not isinstance(started, int) or started < 0):
                raise ValidationError("invalid rest started sequence")
            if completed is not None and (not isinstance(completed, int) or completed < 0):
                raise ValidationError("invalid rest completed sequence")
            return cls(state, interaction_id, identity, bench_id, started, completed, failure_reason)
        except (TypeError, ValueError) as exc:
            raise ValidationError("invalid rest telemetry") from exc


@dataclass(frozen=True)
class ObservedActor:
    id: str
    distance: float | None = None
    position: Vec3 | None = None
    hp: int | None = None
    max_hp: int | None = None
    kind: str | None = None
    name: str | None = None
    usable: bool | None = None
    visible: bool | None = None
    relative: RelativePosition | None = None
    prompt: str | None = None
    opened: bool | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObservedActor":
        try:
            result = cls(
                id=str(value["id"]),
                distance=float(value["distance"]) if value.get("distance") is not None else None,
                position=Vec3.from_dict(value["position"]) if value.get("position") else None,
                hp=int(value["hp"]) if value.get("hp") is not None else None,
                max_hp=int(value["max_hp"]) if value.get("max_hp") is not None else None,
                kind=str(value["kind"]) if value.get("kind") is not None else None,
                name=str(value["name"]) if value.get("name") is not None else None,
                usable=_optional_bool(value, "usable"),
                visible=_optional_bool(value, "visible"),
                relative=RelativePosition.from_dict(value["relative"])
                if value.get("relative") else None,
                prompt=_optional_text(value, "prompt"),
                opened=_optional_bool(value, "opened"),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid observed actor data") from exc
        if result.distance is not None and result.distance < 0:
            raise ValidationError("observed actor distance cannot be negative")
        if result.hp is not None and result.hp < 0:
            raise ValidationError("observed actor HP cannot be negative")
        if result.max_hp is not None and result.max_hp < 0:
            raise ValidationError("observed actor maximum HP cannot be negative")
        if result.hp is not None and result.max_hp is not None and result.hp > result.max_hp:
            raise ValidationError("observed actor HP cannot exceed maximum HP")
        return result

    def with_relative_to(self, player: "Character | None") -> "ObservedActor":
        if self.position is None or player is None or player.position is None or player.rotation is None:
            return self
        dx = (self.position.x - player.position.x) / 100.0
        dy = (self.position.y - player.position.y) / 100.0
        dz = (self.position.z - player.position.z) / 100.0
        horizontal = math.hypot(dx, dy)
        bearing = math.degrees(math.atan2(dy, dx))
        heading_error = (bearing - player.rotation.yaw + 180.0) % 360.0 - 180.0
        return ObservedActor(
            self.id, self.distance, self.position, self.hp, self.max_hp,
            self.kind, self.name, self.usable, self.visible,
            RelativePosition(dx, dy, dz, horizontal, bearing, heading_error),
            self.prompt, self.opened,
        )


@dataclass(frozen=True)
class CameraState:
    rotation: Rotation | None = None
    position: Vec3 | None = None
    forward: Vec3 | None = None
    right: Vec3 | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CameraState":
        try:
            return cls(
                rotation=Rotation.from_dict(value["rotation"])
                if value.get("rotation") else None,
                position=Vec3.from_dict(value["position"])
                if value.get("position") else None,
                forward=Vec3.from_dict(value["forward"])
                if value.get("forward") else None,
                right=Vec3.from_dict(value["right"])
                if value.get("right") else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid camera telemetry") from exc


@dataclass(frozen=True)
class GameState:
    schema_version: int
    sequence: int
    timestamp_utc: str
    running: bool
    paused: bool
    in_menu: bool
    in_combat: bool
    location: str
    objective: str
    player: Character | None
    map_id: str | None = None
    party: tuple[Character, ...] = field(default_factory=tuple)
    target: ObservedActor | None = None
    nearby_interactable: Interactable | None = None
    nearby_enemies: tuple[ObservedActor, ...] = field(default_factory=tuple)
    rest: RestState = field(default_factory=RestState)
    source: str = "unknown"
    camera: CameraState | None = None
    # Optional UI/map producer payload.  The core exporter does not currently
    # populate this, but keeping it as an opaque object lets a narrowly scoped
    # map probe add markers without weakening the validated world-state model.
    map_state: dict[str, Any] | None = None
    # Optional Queen's Blood producer payload. The read-only card-game probe
    # owns its schema until runtime field semantics have been reconciled.
    queen_blood: dict[str, Any] | None = None
    # Optional sequence-bound geometry/perception producer payload.
    navigation: dict[str, Any] | None = None
    nearby_interactables: tuple[Interactable, ...] = field(default_factory=tuple)
    nearby_actors: tuple[ObservedActor, ...] = field(default_factory=tuple)
    # Optional producer timing diagnostics.  These are additive so legacy
    # exporters and external tooling can continue to publish state.json.
    sampled_at_utc: str | None = None
    published_at_utc: str | None = None
    producer_elapsed_ms: float | None = None
    source_timestamp_utc: str | None = None
    # The UE4SS Lua producer counter is distinct from the native publication
    # counter in ``sequence`` and is required to bind sidecars to the same
    # producer snapshot.
    source_sequence: int | None = None
    # Consumer-side navigation freshness decision. This is deliberately
    # separate from navigation payload data and is never used as movement
    # authorization by itself.
    navigation_diagnostics: dict[str, Any] | None = None
    interaction: dict[str, Any] | None = None
    loading: bool = False
    nearby_entities_complete: bool = False
    # Legacy exporters default unknown battle state to in_combat=False. Only
    # this explicit producer flag makes that boolean a measured entry/exit.
    combat_state_observed: bool | None = None

    @classmethod
    def from_dict(cls, value: dict[str, Any], source: str = "file") -> "GameState":
        try:
            game = value["game"]
            timestamp = str(value["timestamp_utc"])
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValidationError("timestamp_utc must include a timezone")

            def optional_timestamp(name: str) -> str | None:
                raw = value.get(name)
                if raw is None:
                    return None
                if not isinstance(raw, str):
                    raise ValidationError(f"{name} must be a string")
                parsed_optional = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if parsed_optional.tzinfo is None:
                    raise ValidationError(f"{name} must include a timezone")
                return raw

            sampled_at_utc = optional_timestamp("sampled_at_utc")
            published_at_utc = optional_timestamp("published_at_utc")
            source_timestamp_utc = optional_timestamp("source_timestamp_utc")
            source_sequence_raw = value.get("source_sequence")
            source_sequence = (
                int(source_sequence_raw) if source_sequence_raw is not None else None
            )
            combat_state_observed = _optional_bool(game, "combat_state_observed")
            in_combat = bool(game.get("in_combat", False))
            if combat_state_observed is True:
                in_combat = _optional_bool(game, "in_combat")
                if in_combat is None:
                    raise ValidationError("observed combat state requires in_combat")
            producer_elapsed_raw = value.get("producer_elapsed_ms")
            producer_elapsed_ms = None
            if producer_elapsed_raw is not None:
                producer_elapsed_ms = float(producer_elapsed_raw)
                if not math.isfinite(producer_elapsed_ms) or producer_elapsed_ms < 0:
                    raise ValidationError("producer_elapsed_ms must be finite and non-negative")
            interaction = value.get("interaction")
            if interaction is not None:
                if (not isinstance(interaction, dict) or interaction.get("state") not in
                    {"idle", "interacting", "dialogue", "menu", "transition", "loading", "completed"}
                    or any(interaction.get(key) is not None and not isinstance(interaction[key], str)
                           for key in ("target_id", "id"))):
                    raise ValidationError("invalid interaction telemetry")
                interaction = dict(interaction)
            nearby_interactable = None
            if value.get("nearby_interactable"):
                try:
                    nearby_interactable = Interactable.from_dict(value["nearby_interactable"])
                except ValidationError:
                    nearby_interactable = None
            rest = RestState()
            if value.get("rest"):
                try:
                    rest = RestState.from_dict(value["rest"])
                except ValidationError:
                    rest = RestState("unknown")
            player = Character.from_dict(value["player"]) if value.get("player") else None
            if nearby_interactable is not None:
                nearby_interactable = nearby_interactable.with_relative_to(player)
            camera = None
            if value.get("camera"):
                try:
                    camera = CameraState.from_dict(value["camera"])
                except ValidationError:
                    camera = None
            elif value.get("camera_rotation"):
                try:
                    camera = CameraState(
                        rotation=Rotation.from_dict(value["camera_rotation"])
                    )
                except ValidationError:
                    camera = None
            map_state_value = value.get("map_state")
            if map_state_value is not None and not isinstance(map_state_value, dict):
                raise ValidationError("map_state must be an object")
            map_state = dict(map_state_value) if map_state_value is not None else None
            queen_blood_value = value.get("queen_blood")
            if queen_blood_value is not None and not isinstance(queen_blood_value, dict):
                raise ValidationError("queen_blood must be an object")
            queen_blood = dict(queen_blood_value) if queen_blood_value is not None else None
            navigation_value = value.get("navigation")
            if navigation_value is not None and not isinstance(navigation_value, dict):
                raise ValidationError("navigation must be an object")
            navigation = dict(navigation_value) if navigation_value is not None else None
            navigation_diagnostics_value = value.get("navigation_diagnostics")
            if navigation_diagnostics_value is not None and not isinstance(navigation_diagnostics_value, dict):
                raise ValidationError("navigation_diagnostics must be an object")
            navigation_diagnostics = (
                dict(navigation_diagnostics_value)
                if navigation_diagnostics_value is not None else None
            )
            entities_valid = True
            nearby_interactables: list[Interactable] = []
            for item in value.get("nearby_interactables", []):
                try:
                    nearby_interactables.append(
                        Interactable.from_dict(item).with_relative_to(player)
                    )
                except ValidationError:
                    entities_valid = False
                    continue
            nearby_actors: list[ObservedActor] = []
            for item in value.get("nearby_actors", []):
                try:
                    nearby_actors.append(
                        ObservedActor.from_dict(item).with_relative_to(player)
                    )
                except ValidationError:
                    entities_valid = False
                    continue
            return cls(
                schema_version=int(value["schema_version"]),
                sequence=int(value["sequence"]),
                timestamp_utc=timestamp,
                running=bool(game["running"]),
                paused=bool(game.get("paused", False)),
                in_menu=bool(game.get("in_menu", False)),
                in_combat=in_combat,
                map_id=str(game["map_id"]) if game.get("map_id") is not None else None,
                location=str(game.get("location", "")),
                objective=str(game.get("objective", "")),
                player=player,
                party=tuple(Character.from_dict(item) for item in value.get("party", [])),
                target=ObservedActor.from_dict(value["target"]) if value.get("target") else None,
                nearby_interactable=nearby_interactable,
                nearby_enemies=tuple(
                    ObservedActor.from_dict(item) for item in value.get("nearby_enemies", [])
                ),
                rest=rest,
                source=source,
                camera=camera,
                map_state=map_state,
                queen_blood=queen_blood,
                navigation=navigation,
                nearby_interactables=tuple(nearby_interactables),
                nearby_actors=tuple(nearby_actors),
                sampled_at_utc=sampled_at_utc,
                published_at_utc=published_at_utc,
                producer_elapsed_ms=producer_elapsed_ms,
                source_timestamp_utc=source_timestamp_utc,
                source_sequence=source_sequence,
                navigation_diagnostics=navigation_diagnostics,
                interaction=interaction,
                loading=_optional_bool(game, "loading") or False,
                nearby_entities_complete=entities_valid and (_optional_bool(value, "nearby_entities_complete") or False),
                combat_state_observed=combat_state_observed,
            )
        except ValidationError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationError("invalid telemetry snapshot") from exc

    def age_seconds(self, now: datetime | None = None) -> float:
        stamp = datetime.fromisoformat(self.timestamp_utc.replace("Z", "+00:00"))
        current = now or datetime.now(timezone.utc)
        return max(0.0, (current - stamp).total_seconds())

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        if self.combat_state_observed is None:
            result.pop("combat_state_observed")
        def clean_spatial(item: dict[str, Any]) -> None:
            if item.get("position") is None:
                item.pop("position", None)
            if item.get("relative") is None:
                item.pop("relative", None)
            for optional in ("label", "prompt", "visible", "kind", "name", "usable", "opened"):
                if item.get(optional) is None:
                    item.pop(optional, None)

        interactable = result.get("nearby_interactable")
        if interactable is not None:
            clean_spatial(interactable)
        for item in result.get("nearby_interactables", []):
            clean_spatial(item)
        for item in result.get("nearby_actors", []):
            clean_spatial(item)
        for item in result.get("nearby_enemies", []):
            clean_spatial(item)
        return result

    @property
    def camera_rotation(self) -> Rotation | None:
        """Compatibility view used by the earlier camera alignment workflow."""
        return self.camera.rotation if self.camera is not None else None
