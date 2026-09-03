"""The confirmed plan as a YAML file the operator can keep and diff.

Profile section 10 fixes the order of operations:

    Output order: readable plan first -> operator confirms -> then generate
    YAML/code.

So this module is the last step of that sequence and nothing more: it renders a
plan that has already been computed and read. It plans nothing, reads nothing
and spends no game request -- it takes the ``/plan`` response as plain JSON
data, plus the request that produced it, and writes a document.

**Why the response and not the plan object.** The figures in here are the ones
the operator was shown, which means the document must come off the same
structure the page renders. Re-deriving them from :class:`DistributionPlan`
would be a second implementation of the response layer's naming and grouping,
and two implementations of one mapping drift -- this repository has the scars.
The field names are therefore the response's own, verbatim, so a value in the
file can be found in the API and vice versa; only the SHAPE differs, and only
where a person reads it better (the merchant budget and the per-resource
figures are gathered under one village instead of living in two parallel
lists).

**Determinism is a feature, not an accident.** There is no timestamp, no
hostname and no run id anywhere in the document: the same plan renders
byte-identical YAML, so two exports of one plan diff empty and a diff shows
only what actually changed. That is most of the value of having a file at all,
and it is why :func:`plan_digest` can be trusted as this plan's identity.

Conventions follow ``plans/new-village.yaml``, which is the only YAML this
repository writes by hand: a leading comment block that says what the file is,
snake_case keys, block style throughout, two-space indent. ``pyyaml`` is
already a dependency (the build-plan files are read with it), so nothing new
was added to write this.

The document contains village names and coordinates -- the operator's own
account data. It contains no credential of any kind, and it is never logged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any

import yaml

from .allocation import village_label

PLAN_DOCUMENT = "travian-distribution-plan"
"""What kind of file this is, stamped in ``meta`` so a stray copy identifies
itself. Deliberately not the setup document's `travian-planner-owned-state`:
that one is INPUT the operator maintains, this one is OUTPUT and nothing reads
it back."""

PLAN_DOCUMENT_VERSION = 1
"""Bumped when a section is renamed or removed, so a reader can tell. Adding a
field does not need it -- the document is a record for a person, and nothing in
this repository parses one back."""

_YAML_WIDTH = 1_000_000
"""Wide enough that no scalar is ever wrapped. A finding's message is a whole
paragraph and the operator greps these files; a line broken at column 80 is a
line `grep` cannot find. Wrapping is deterministic either way, so this costs
the diff nothing."""

_HEADER = f"""\
# Travian distribution plan -- the sheet as it was read and confirmed.
#
# Profile section 10: readable plan first -> the operator confirms -> then this
# file. `meta.plan_digest` is the sha256 of the /plan response this was
# rendered from, and the export refuses to run unless the caller hands back the
# digest it was shown -- so this document cannot describe a plan nobody read.
#
# There is no timestamp in here on purpose. The same plan renders byte-identical
# YAML, so two exports of one plan diff empty and a diff shows only what really
# moved.
#
# `inputs` is the /plan request verbatim: post it back to /api/distribution/plan
# and you get this plan again, which is what makes the file self-describing a
# month from now.
#
# Village names and coordinates are your own account data. There are no
# credentials in here.
# Format: {PLAN_DOCUMENT} v{PLAN_DOCUMENT_VERSION}

"""


def plan_digest(plan: Mapping[str, Any]) -> str:
    """This plan's identity: sha256 over the ``/plan`` response that showed it.

    Over the RESPONSE rather than over the request, which is the point. Two
    requests differing in a field the planner ignores are the same plan and
    must digest the same; two requests differing in one that moves a single
    cargo figure are different plans and must not. Only the answer can say
    which, and the answer is what the operator confirmed.

    Canonicalised with sorted keys and no whitespace, so the digest depends on
    the CONTENT and not on the order a dict happened to be built in. The
    planner itself is pure -- no clock, no randomness -- so the same inputs
    reproduce the same digest for as long as the code is unchanged. It is
    deliberately not a version-stable identifier across releases: a planner
    change that moves a figure SHOULD move the digest, because the plan the
    operator read no longer exists.
    """
    canonical = json.dumps(plan, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _names(inputs: Mapping[str, Any], plan: Mapping[str, Any]) -> dict[int, str]:
    """Village id -> the name the operator uses, for both kinds of destination.

    The snapshot names every own village. A route-eligible foreign tribute has
    no snapshot row at all -- it rides through the optimizer as a pseudo-village
    with a negative id -- so its name is taken off the rows that reach it, which
    is where the response already resolved it.

    A snapshot row with an empty name is dropped rather than kept, so
    :func:`village_label` applies the fallback it applies everywhere else --
    kept, it would render a village as blank here while the routes above it call
    the same village "village 5".
    """
    names = {row["village_id"]: row["name"] for row in inputs["snapshot"] if row["name"]}
    for row in plan["rows"]:
        names.setdefault(row["origin"], row["origin_name"])
        names.setdefault(row["destination"], row["destination_name"])
    return names


def _villages(inputs: Mapping[str, Any], plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    """One entry per village: its merchant bill and its per-resource figures.

    The response keeps these in two parallel lists (`budgets` keyed by village,
    `village_nets` keyed by village AND resource) because the grid reads them
    that way. A file is read down the page instead, so a village's whole story
    is gathered in one place. Every key inside is the response's own.
    """
    names = _names(inputs, plan)
    resources: dict[int, list[dict[str, Any]]] = {}
    for net in plan["village_nets"]:
        resources.setdefault(net["village_id"], []).append(
            {key: value for key, value in net.items() if key != "village_id"}
        )
    out: list[dict[str, Any]] = []
    for budget in plan["budgets"]:
        village_id = budget["village_id"]
        out.append(
            {
                "village_id": village_id,
                "name": village_label(village_id, names),
                "merchants": {
                    key: budget[key]
                    for key in (
                        "committed",
                        "spare",
                        "free",
                        "over_budget",
                        "trade_office_levels_needed",
                        "explanation",
                    )
                },
                "legs": budget["legs"],
                "resources": resources.get(village_id, []),
            }
        )
    return out


def build_plan_document(
    *, inputs: Mapping[str, Any], plan: Mapping[str, Any], digest: str
) -> dict[str, Any]:
    """The document as plain data, ready to dump.

    ``inputs`` is the ``/plan`` request and ``plan`` its response, both already
    reduced to JSON-shaped primitives (``model_dump(mode="json")``) so nothing
    in here has to know about Pydantic, enums or tuples -- and so ``yaml`` never
    meets a type its safe dumper would refuse.

    Every key is read explicitly rather than by iterating the response, so a
    field renamed upstream fails loudly here instead of silently dropping a
    section out of the operator's record.
    """
    return {
        "meta": {
            "document": PLAN_DOCUMENT,
            "version": PLAN_DOCUMENT_VERSION,
            "plan_digest": digest,
        },
        "verdict": {
            "feasible": plan["feasible"],
            "clean": plan["verdict"]["clean"],
            "executable": plan["verdict"]["executable"],
            "blockers": plan["verdict"]["blockers"],
            "covers": plan["verdict"]["covers"],
            "unweighed": plan["verdict"]["unweighed"],
            "critical_findings": plan["verdict"]["critical_findings"],
            "total_merchants": plan["total_merchants"],
        },
        "routes": plan["rows"],
        "villages": _villages(inputs, plan),
        "relays": plan["relays"],
        "shortfalls": plan["shortfalls"],
        "unallocated": plan["unallocated"],
        # Section 7's two halves under one heading: what the conversion budget
        # was sized at, and where the operator should press the button.
        "npc": {"reserves": plan["npc_reserves"], "triggers": plan["npc_triggers"]},
        "night_overruns": plan["night_overruns"],
        "role_deviations": plan["role_deviations"],
        # The ranked, grouped form and not the flat `warnings` list: the groups
        # carry the same messages plus what each group costs and the one action
        # that answers all of them, which is what a person reads.
        "findings": plan["diagnostics"],
        "inputs": dict(inputs),
    }


def render_plan_yaml(*, inputs: Mapping[str, Any], plan: Mapping[str, Any], digest: str) -> str:
    """The whole file, header comment included.

    ``sort_keys=False`` keeps the order :func:`build_plan_document` chose, which
    is reading order: the verdict, then the routes, then the villages, then the
    findings, then the inputs the whole thing came from. Alphabetising it would
    put `inputs` second and the verdict near the bottom.
    """
    document = build_plan_document(inputs=inputs, plan=plan, digest=digest)
    return _HEADER + yaml.safe_dump(
        document,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
        width=_YAML_WIDTH,
    )
