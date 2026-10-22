# SPDX-FileCopyrightText: 2026 Peter Lemenkov <lemenkov@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""Server-side role allowlist: what each role means is decided here, never by
the caller. A caller picks a role name; preambles, image and workspace access
follow from it."""

from dataclasses import dataclass
from pathlib import Path

from . import config


@dataclass(frozen=True)
class Role:
    skill: str          # SKILLS/<skill>.md, appended after AGENT.md
    image: str          # local image in the service user's podman store
    workspace: str      # "ro" or "rw"
    description: str

    @property
    def preamble_files(self) -> list[Path]:
        home = config.AGENTBRIDGE_HOME
        return [home / "AGENT.md", home / "SKILLS" / f"{self.skill}.md"]


ROLES: dict[str, Role] = {
    "researcher": Role(
        skill="researcher",
        image="agentbridge-base",
        workspace="ro",
        description="Reads, searches and summarises. Workspace is read-only.",
    ),
    "reviewer": Role(
        skill="reviewer",
        image="agentbridge-pydev",
        workspace="ro",
        description="Reviews code in the workspace and reports findings. "
                    "Workspace is read-only.",
    ),
    "implementer": Role(
        skill="implementer",
        image="agentbridge-pydev",
        workspace="rw",
        description="Edits code in the workspace; commits signed-off on a "
                    "branch; never pushes. Workspace is read-write.",
    ),
}
