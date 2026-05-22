"""Shared string constants for the eventing workload scripts.

Kept tiny and stable so analyze_impact / notify / incident_bundle /
maintenance / identify_targets can all import from the same place.
The Go renderer mirrors these in tools/render/constants.go.
"""

# Severity classes — match the emoji map in notify.py.
SEVERITY_HIGH    = "high"
SEVERITY_MEDIUM  = "medium"
SEVERITY_WARNING = "warning"
SEVERITY_LOW     = "low"

# Device kinds (matches spec/atlanta.yaml's `kind:` field).
KIND_SRLINUX = "srlinux"
KIND_FRR     = "frr"

# Device roles (matches spec/atlanta.yaml's `role:` field).
ROLE_TMC          = "tmc"
ROLE_CORRIDOR_HUB = "corridor-hub"
ROLE_FIELD_CABINET = "field-cabinet"

# Link kinds (matches spec/atlanta.yaml's `links[].kind` field).
LINK_KIND_BACKBONE = "backbone"
LINK_KIND_CABINET  = "cabinet"

# Cabinet device-name prefix — used by analyze_impact to identify
# downstream cabinets without round-tripping NetBox.
CABINET_NAME_PREFIX = "fc-"
