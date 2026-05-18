"""Target-centric aggregation for the Path 3 rebalance pass.

Walks the per-slot metadata list produced by the raid optimizer script and
groups slots by their target coord. For each coord the aggregate exposes
combined raid stats, the worst-case defense signal across instances, and a
pointer back to every slot-instance so the rebalance planner can emit
MOVE_SLOT / RELOCATE_TO_DEAD actions.

Read-only: this module does no I/O and makes no game-state changes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging_config import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class TargetAggregate:
    """Aggregated stats for one target coord across all of its slot-instances.

    Fields:
        coord: (x, y) tile on the Travian map.
        target_name: in-game village name (best-effort; some instances may be blank).
        primary_owner_village: V1..V6 label of the village whose slot-instance
            has the highest total_raids on this coord. Used as the home for the
            DEAD list when the target dies.
        total_raids_all_lists: sum of total_raids across all instances.
        total_booty_all_lists: sum of total_booty across all instances.
        avg_loot: total_booty / total_raids (0.0 when never raided).
        last_raid_time_unix: most recent last_raid timestamp across instances.
        max_def_proxy: maximum defense_proxy across instances (worst-case signal).
        any_ct2_ct3_flag: True if any instance was flagged as pushing-protection suspect.
        slot_instances: [(list_name, slot_id), ...] for every list this coord appears in.
    """

    coord: tuple[int, int]
    target_name: str
    primary_owner_village: str
    total_raids_all_lists: int
    total_booty_all_lists: int
    avg_loot: float
    last_raid_time_unix: int | None
    max_def_proxy: int
    any_ct2_ct3_flag: bool
    slot_instances: list[tuple[str, int]] = field(default_factory=list)


def build_target_inventory(
    slot_ms: list[dict[str, Any]],
    village_label_by_vid: dict[int, str],
) -> dict[tuple[int, int], TargetAggregate]:
    """Group slot-instances by coord and aggregate stats.

    slot_ms entries must have the shape produced by ``slot_metadata`` in
    ``scripts/raid_optimizer_diff_v3.py`` — coords, name, list_name, owner_village_id,
    slot_id, total_raids, total_booty, last_raid_time, defense_proxy,
    pushing_protection_suspect.

    village_label_by_vid maps Travian owner-village ids to V1..V6 labels so the
    primary_owner_village can be set without re-fetching village metadata.

    Returns a dict keyed by (x, y). Targets that appear in only one slot still
    get an aggregate (they're equivalent to a singleton group).
    """
    groups: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for sm in slot_ms:
        coord = sm.get("coords")
        if not coord:
            continue
        groups.setdefault(tuple(coord), []).append(sm)

    inventory: dict[tuple[int, int], TargetAggregate] = {}
    for coord, instances in groups.items():
        total_raids = sum(int(s.get("total_raids") or 0) for s in instances)
        total_booty = sum(int(s.get("total_booty") or 0) for s in instances)
        avg_loot = (total_booty / total_raids) if total_raids > 0 else 0.0

        raid_times = [s.get("last_raid_time") for s in instances if s.get("last_raid_time")]
        last_raid_time = max((int(t) for t in raid_times), default=None)

        max_def = max((int(s.get("defense_proxy") or 0) for s in instances), default=0)
        any_ct = any(bool(s.get("pushing_protection_suspect")) for s in instances)

        # Primary owner: the instance with the highest total_raids wins. Ties
        # break to whichever appears first in slot_ms (stable ordering).
        primary = max(instances, key=lambda s: int(s.get("total_raids") or 0))
        owner_vid = primary.get("owner_village_id")
        primary_owner_village = village_label_by_vid.get(owner_vid, "") if owner_vid else ""

        # Target name: first non-empty name across instances.
        target_name = next((s.get("name") or "" for s in instances if s.get("name")), "")

        slot_instances: list[tuple[str, int]] = []
        for s in instances:
            sid = s.get("slot_id")
            if sid is None:
                continue
            slot_instances.append((s.get("list_name") or "", int(sid)))

        inventory[coord] = TargetAggregate(
            coord=coord,
            target_name=target_name,
            primary_owner_village=primary_owner_village,
            total_raids_all_lists=total_raids,
            total_booty_all_lists=total_booty,
            avg_loot=round(avg_loot, 2),
            last_raid_time_unix=last_raid_time,
            max_def_proxy=max_def,
            any_ct2_ct3_flag=any_ct,
            slot_instances=slot_instances,
        )

    logger.info(
        "target_inventory built: %d coords from %d slot-instances",
        len(inventory),
        len(slot_ms),
    )
    return inventory
