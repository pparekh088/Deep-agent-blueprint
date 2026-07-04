"""TEMPLATE_CORE — Domain Deep Agent Service.

Everything under ``app/`` except ``app/adapters/`` and ``app/agent/prompts.py``
is template core: copy it unmodified when standing up a new domain. If you find
yourself editing core to ship a domain, the template has failed — file template
feedback instead of forking. See README.md ("Core vs. customize") and
CONTRIBUTING.md (Definition of Done).
"""

# Bump on every template release. Each domain repo records the version it was
# cut from so core fixes can be back-ported deliberately (see README.md).
TEMPLATE_VERSION = "1.1.0"
