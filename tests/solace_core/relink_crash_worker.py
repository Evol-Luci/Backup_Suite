"""Exit between the registry and project-catalog halves of explicit relink."""
import os
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from solace_core.registry import Registry
from solace_core.service import CoreService
from solace_core.storage import StoragePolicy
from solace_core.workspace_paths import CoreConfig

registry,project,root,store,workspaces,request_id=sys.argv[1:]
policy=StoragePolicy(mountinfo='1 0 0:1 / / rw - ext4 x rw\n',sync_roots=[])
service=CoreService(Registry(Path(registry),policy),config=CoreConfig((),Path(workspaces)))
with patch.object(service,'_rebase_recovery',side_effect=lambda *args:os._exit(88)):
    result=service.handle(dict(version=1,id=request_id,method='project.relink',actor='test',
        params=dict(project=project,root=root,store=store,confirmed_root=root)))
raise RuntimeError(result)
