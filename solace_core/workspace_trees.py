"""Verified materialized trees and read-only workspace comparisons."""
import copy
from pathlib import Path
from .tasks import utcnow
from .workspace_paths import protected_excluder


def compare_tree(repo,checkpoint,root,profile):
    root=Path(root)
    inspector=copy.copy(repo)
    inspector.project_root=root
    inspector._project_identity=root.stat()
    expected={entry.path:entry for entry in repo.get_file_entries(checkpoint)}
    entries,warnings=inspector.inspect_tree(profile['exclusions'],excluded=protected_excluder(root,profile,[repo.store_dir]))
    observed={entry.path:entry for entry in entries}
    changes=[]
    for path in sorted(expected.keys() & observed.keys()):
        old,new=expected[path],observed[path];reasons=[]
        if old.kind!=new.kind: reasons.append('type')
        elif old.kind=='symlink':
            if old.file_digest!=new.file_digest: reasons.append('link_target')
        else:
            if (old.file_digest,old.size)!=(new.file_digest,new.size): reasons.append('content')
            if old.mode!=new.mode: reasons.append('mode')
        if reasons: changes.append(dict(path=path,reasons=reasons))
    added=sorted(observed.keys()-expected.keys());removed=sorted(expected.keys()-observed.keys())
    return dict(checkpoint_id=checkpoint,observed_at=utcnow().isoformat(),clean=not (added or removed or changes),
                added=added,removed=removed,changed=changes,logical_bytes=sum(e.size for e in entries),warnings=warnings)
