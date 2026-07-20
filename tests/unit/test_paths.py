from __future__ import annotations

import pytest

from environment_runtime.core.errors import ValidationError
from environment_runtime.core.paths import WorkspacePath


@pytest.mark.unit
def test_workspace_path_uri_is_stable() -> None:
    path = WorkspacePath(workspace_id="workspace_1", root_id="root_1", relative_path="src/main.py")
    assert path.as_uri() == "workspace://workspace_1/root_1/src/main.py"


@pytest.mark.unit
def test_workspace_path_rejects_parent_traversal() -> None:
    with pytest.raises(ValidationError):
        WorkspacePath(workspace_id="workspace_1", root_id="root_1", relative_path="../secret.txt")
