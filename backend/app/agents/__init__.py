"""The review pipeline's agents: classifier, reviewer, coverage, and the orchestrator that fans
them out and merges their output deterministically (plan §4.2). Each agent is one small file with
the same shape (:class:`app.agents.base.Agent`) — the teaching point of this package.
"""

from __future__ import annotations
