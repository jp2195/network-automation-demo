"""Structurally read-only gNMI access for the AI analyst.

This module is the only gNMI surface reachable from the analyst's tool
layer, and it implements exactly one operation: Get. Read-only is
structural, not a prompt-level promise (wave-2 spec, feature 4) — there
is no other gNMI code path in the analyst lane. The guarantee is pinned
by test_analyst.TestGnmiStructurallyReadOnly.

pygnmi (grpcio) is imported lazily so unit tests run without it.
"""

import os
import re

__all__ = ["get"]

# Spec-shaped node names (hub-e, tmc-1, ...). Rejecting dots and slashes
# means a node name can never escape the clabernetes DNS suffix it is
# embedded into below.
_NODE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")

# SRL/OpenConfig path: leading slash, then path chars incl. [key=value]
# selectors and YANG module prefixes. No whitespace, quotes or shell
# metacharacters.
_PATH_RE = re.compile(r"^/[A-Za-z0-9\[\]=/_:.,*-]+$")

_PORT = 57400


def get(node, path, timeout=10):
    """gNMI Get of one path on one SR Linux node; returns the
    notification dict pygnmi produces (json_ietf-decoded)."""
    if not _NODE_RE.fullmatch(node or ""):
        raise ValueError(f"invalid node name: {node!r}")
    if not _PATH_RE.fullmatch(path or ""):
        raise ValueError(f"invalid gNMI path: {path!r}")
    prefix = os.environ.get("CLAB_PREFIX", "").strip()
    if not prefix:
        raise RuntimeError("CLAB_PREFIX env var is required (set by the WFT)")
    host = f"{prefix}-{node}.clabernetes.svc.cluster.local"
    from pygnmi.client import gNMIclient
    with gNMIclient(target=(host, _PORT),
                    username=os.environ.get("GNMI_USER", "admin"),
                    password=os.environ.get("GNMI_PASSWORD", ""),
                    skip_verify=True,
                    gnmi_timeout=timeout) as c:
        return c.get(path=[path], encoding="json_ietf")
