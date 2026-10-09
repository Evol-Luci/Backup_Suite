import os
import json
import zipfile
import shutil
import hashlib
import fnmatch
import tempfile
from datetime import datetime


def _safe_extract(zf: zipfile.ZipFile, dest: str) -> int:
    """Extract zip to dest, raising ValueError if any member escapes the destination."""
    dest = os.path.realpath(dest)
    for member in zf.namelist():
        member_path = os.path.realpath(os.path.join(dest, member))
        if not member_path.startswith(dest + os.sep) and member_path != dest:
            raise ValueError(f"Zip path traversal attempt detected: {member!r}")
    zf.extractall(dest)
    return len(zf.namelist())


def _files_equal(path_a: str, path_b: str, chunk_size: int = 64 * 1024) -> bool:
    """Compare file bytes with bounded memory use and no metadata-based cache."""
    with open(path_a, "rb") as file_a, open(path_b, "rb") as file_b:
        while True:
            chunk_a = file_a.read(chunk_size)
            chunk_b = file_b.read(chunk_size)
            if chunk_a != chunk_b:
                return False
            if not chunk_a:
                return True


class ProjectVault:
    def __init__(self, project_path):
        self.project_path = project_path
        self.vault_path = os.path.join(project_path, ".solace_vault")
        self.branches_dir = os.path.join(self.vault_path, "branches")
        self.meta_file = os.path.join(self.vault_path, "meta.json")
        self._ensure_structure()

    def _ensure_structure(self):
        """Ensures the .solace_vault directory and basic structure exists."""
        if not os.path.exists(self.vault_path):
            os.makedirs(self.vault_path)
            # Create hidden attribute on Windows if needed, strictly speaking not required for logic but good for polish
            # subprocess.check_call(["attrib", "+H", self.vault_path]) 

        if not os.path.exists(self.branches_dir):
            os.makedirs(self.branches_dir)
        
        # Ensure 'main' branch folder exists as default
        main_branch_path = os.path.join(self.branches_dir, "main")
        if not os.path.exists(main_branch_path):
            os.makedirs(main_branch_path)

        if not os.path.exists(self.meta_file):
            self._write_meta({
                "current_branch": "main",
                "created_at": datetime.now().isoformat(),
                "branches": ["main"],
                "head_commit": None
            })

        # Auto-migrate legacy commits for all existing branches
        meta = self._load_meta()
        for branch in meta.get('branches', []):
            self._migrate_legacy_commits(branch)

    def _migrate_legacy_commits(self, branch_name):
        """Auto-generates a commits.json manifest from legacy __message filenames."""
        manifest_path = self._get_manifest_path(branch_name)
        if os.path.exists(manifest_path):
            return  # Already migrated

        branch_path = os.path.join(self.branches_dir, branch_name)
        if not os.path.exists(branch_path):
            return

        zips = sorted([f for f in os.listdir(branch_path) if f.endswith('.zip')])
        if not zips:
            return

        manifest = {"version": 1, "commits": {}}
        prev_commit = None

        for commit_file in zips:
            if '__' in commit_file:
                # Legacy format: YYYY-MM-DD_HH-MM-SS__message.zip
                try:
                    ts_str, message_sanitized = commit_file.replace('.zip', '').split('__', 1)
                    timestamp_dt = datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
                    message = message_sanitized.replace('_', ' ')

                    manifest["commits"][commit_file] = {
                        "message": message,
                        "timestamp": timestamp_dt.isoformat(),
                        "parent": prev_commit,
                        "tags": [],
                        "migrated": True
                    }
                    prev_commit = commit_file
                except (ValueError, IndexError):
                    continue

        if manifest["commits"]:
            self._save_manifest(branch_name, manifest)

    def _load_meta(self):
        with open(self.meta_file, 'r') as f:
            return json.load(f)

    def _write_meta(self, data):
        with open(self.meta_file, 'w') as f:
            json.dump(data, f, indent=4)

    # --- Manifest Infrastructure ---

    def _get_manifest_path(self, branch_name):
        """Returns the path to commits.json for a given branch."""
        return os.path.join(self.branches_dir, branch_name, "commits.json")

    def _load_manifest(self, branch_name):
        """Loads the branch manifest. Returns empty manifest if file doesn't exist."""
        path = self._get_manifest_path(branch_name)
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {"version": 1, "commits": {}}

    def _save_manifest(self, branch_name, manifest):
        """Atomically writes the branch manifest via temp file + rename."""
        path = self._get_manifest_path(branch_name)
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(manifest, f, indent=4)
        os.replace(tmp_path, path)

    def _generate_hash(self, timestamp_str, message):
        """Generates an 8-char hex hash from timestamp + message + random salt."""
        salt = os.urandom(4)
        raw = f"{timestamp_str}{message}".encode() + salt
        return hashlib.sha256(raw).hexdigest()[:8]

    def get_commit_metadata(self, commit_filename, branch_name=None):
        """
        Looks up metadata for a commit from its branch manifest.
        Walks parent branches for inherited commits.
        Falls back to parsing legacy filenames if no manifest entry exists.
        """
        meta = self._load_meta()
        start_branch = branch_name if branch_name else meta['current_branch']

        # Walk branch ancestry looking for manifest entry
        origins = meta.get('branch_origins', {})
        current_b = start_branch
        visited = set()
        while current_b and current_b not in visited:
            visited.add(current_b)
            manifest = self._load_manifest(current_b)
            if commit_filename in manifest.get("commits", {}):
                return manifest["commits"][commit_filename]
            # Move to parent
            if current_b in origins:
                current_b = origins[current_b].get('parent')
            else:
                break

        # Fallback: parse legacy filename (YYYY-MM-DD_HH-MM-SS__message.zip)
        if '__' in commit_filename:
            try:
                ts_str, message_part = commit_filename.replace('.zip', '').split('__', 1)
                timestamp_dt = datetime.strptime(ts_str, "%Y-%m-%d_%H-%M-%S")
                return {
                    "message": message_part.replace('_', ' '),
                    "timestamp": timestamp_dt.isoformat(),
                    "parent": None,
                    "tags": []
                }
            except (ValueError, IndexError):
                pass

        return None

    def get_commit_display_name(self, commit_filename, branch_name=None):
        """Returns a human-readable display string for a commit."""
        md = self.get_commit_metadata(commit_filename, branch_name)
        if md:
            try:
                dt = datetime.fromisoformat(md['timestamp'])
                ts_display = dt.strftime("%Y-%m-%d %H:%M")
            except (ValueError, KeyError):
                ts_display = "Unknown time"
            message = md.get('message', 'No message')
            return f"{ts_display} - {message}"
        # Ultimate fallback: return the raw filename without .zip
        return commit_filename.replace('.zip', '')

    # --- Incremental File Manifest ---

    def _get_file_manifest_path(self):
        """Returns path to the incremental file manifest inside the vault."""
        return os.path.join(self.vault_path, ".solace_vault_manifest.json")

    def _load_file_manifest(self):
        """Load {rel_path: mtime} manifest. Returns empty dict if not found or unreadable."""
        path = self._get_file_manifest_path()
        if os.path.exists(path):
            try:
                with open(path, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_file_manifest(self, manifest_data):
        """Atomically write the file manifest via temp file + rename."""
        path = self._get_file_manifest_path()
        tmp_path = path + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(manifest_data, f)
        os.replace(tmp_path, path)

    def _scan_files_incremental(self, exclusions):
        """
        Build file list using the incremental manifest + mtime check.
        Known files are checked via os.stat(); deleted files are dropped.
        A supplemental os.walk detects new files not yet in the manifest.
        Falls back to full walk if no manifest exists.
        Returns list of (full_path, rel_path).
        """
        manifest = self._load_file_manifest()
        if not manifest:
            return self._scan_files_full(exclusions)

        file_list = []
        seen = set()

        # Check known files via stat (faster than walk for stable trees)
        for rel_path in manifest:
            full_path = os.path.join(self.project_path, rel_path)
            if os.path.isfile(full_path) and not self._is_excluded(rel_path, exclusions):
                file_list.append((full_path, rel_path))
                seen.add(rel_path)
            # If file no longer exists it is implicitly dropped (deleted)

        # Walk to detect new files absent from the manifest
        for root, dirs, files in os.walk(self.project_path):
            rel_root = os.path.relpath(root, self.project_path)
            if ".solace_vault" in rel_root.split(os.sep):
                continue
            dirs[:] = [
                d for d in dirs
                if not self._is_excluded(os.path.join(rel_root, d), exclusions)
            ]
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, self.project_path)
                if rel_path not in seen and not self._is_excluded(rel_path, exclusions):
                    file_list.append((full_path, rel_path))

        return file_list

    def _scan_files_full(self, exclusions):
        """Full directory walk to build file list. Used when no manifest exists."""
        file_list = []
        for root, dirs, files in os.walk(self.project_path):
            rel_root = os.path.relpath(root, self.project_path)
            if ".solace_vault" in rel_root.split(os.sep):
                continue
            dirs[:] = [
                d for d in dirs
                if not self._is_excluded(os.path.join(rel_root, d), exclusions)
            ]
            for file in files:
                full_path = os.path.join(root, file)
                rel_path = os.path.relpath(full_path, self.project_path)
                if self._is_excluded(rel_path, exclusions):
                    continue
                file_list.append((full_path, rel_path))
        return file_list

    # --- Exclusion Helpers ---

    def _load_exclusions(self):
        """
        Loads exclusion patterns from .gitignore and .vaultignore in the project root.
        Returns a list of pattern strings.
        """
        patterns = []
        for filename in (".gitignore", ".vaultignore"):
            ignore_file = os.path.join(self.project_path, filename)
            if os.path.exists(ignore_file):
                with open(ignore_file, 'r') as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            patterns.append(line.rstrip('/'))
        return patterns

    def _is_excluded(self, rel_path, patterns):
        """
        Returns True if rel_path matches any exclusion pattern.
        Handles directory-name patterns (e.g. 'node_modules'),
        path-prefix patterns (e.g. 'dist/'), and fnmatch globs.
        """
        parts = rel_path.replace('\\', '/').split('/')
        for pattern in patterns:
            norm_pattern = pattern.replace('\\', '/')
            # Match against each path component (catches directory names anywhere in the tree)
            for part in parts:
                if fnmatch.fnmatch(part, norm_pattern):
                    return True
            # Match against the full relative path
            if fnmatch.fnmatch(rel_path.replace('\\', '/'), norm_pattern):
                return True
        return False

    def commit(self, message, progress_callback=None):
        """Snapshots the folder, ignoring the vault itself and any .gitignore/.vaultignore patterns."""
        cb = progress_callback or (lambda msg: None)
        meta = self._load_meta()
        branch = meta['current_branch']
        now = datetime.now()
        timestamp = now.strftime("%Y-%m-%d_%H-%M-%S")

        # Generate unique filename with hash
        hash_val = self._generate_hash(timestamp, message)
        snapshot_name = f"{timestamp}_{hash_val}.zip"
        save_path = os.path.join(self.branches_dir, branch, snapshot_name)

        # Scan project files (incremental if manifest exists, else full walk)
        cb("Scanning project files...")
        exclusions = self._load_exclusions()
        file_list = self._scan_files_incremental(exclusions)

        file_count = len(file_list)
        cb(f"Found {file_count} files to snapshot")
        cb(f"Creating archive: {snapshot_name}")

        with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for i, (full_path, rel_path) in enumerate(file_list):
                zf.write(full_path, rel_path)
                if file_count > 50 and (i + 1) % 100 == 0:
                    cb(f"Compressing... {i + 1}/{file_count} files")

        archive_size = os.path.getsize(save_path) / (1024 * 1024)
        cb(f"Archive created: {archive_size:.2f} MB")

        # Verify archive integrity
        cb("Verifying archive integrity...")
        with zipfile.ZipFile(save_path, 'r') as zf:
            bad_file = zf.testzip()
        if bad_file is not None:
            os.remove(save_path)
            raise RuntimeError(f"Archive integrity check failed: corrupt file '{bad_file}'. Commit aborted.")

        # Update incremental file manifest
        cb("Updating file manifest...")
        new_manifest = {rel_path: os.path.getmtime(full_path) for full_path, rel_path in file_list}
        self._save_file_manifest(new_manifest)

        # Determine parent commit (previous head on this branch)
        parent_commit = meta.get('head_commit')

        # Update manifest
        cb("Updating manifest...")
        manifest = self._load_manifest(branch)
        manifest["commits"][snapshot_name] = {
            "message": message,
            "timestamp": now.isoformat(),
            "parent": parent_commit,
            "tags": []
        }
        self._save_manifest(branch, manifest)

        # Update head commit
        meta['head_commit'] = snapshot_name
        self._write_meta(meta)

        return snapshot_name

    def create_branch(self, branch_name, linked=True):
        meta = self._load_meta()
        if branch_name in meta['branches']:
            raise ValueError(f"Branch '{branch_name}' already exists.")

        new_branch_path = os.path.join(self.branches_dir, branch_name)
        os.makedirs(new_branch_path)

        # Initialize empty manifest for new branch
        self._save_manifest(branch_name, {"version": 1, "commits": {}})

        meta['branches'].append(branch_name)
        
        if linked:
            # Record origin for visualization and history inheritance
            origins = meta.get('branch_origins', {})
            current_branch = meta.get('current_branch', 'main')
            
            # Find latest commit of current branch to serve as origin point
            parent_commits = self.get_commits(current_branch)
            latest_commit = parent_commits[0] if parent_commits else None
            
            origins[branch_name] = {
                "parent": current_branch,
                "commit": latest_commit
            }
            meta['branch_origins'] = origins
        
        self._write_meta(meta)

    def switch_branch(self, branch_name):
        # NOTE: This only switches the logical branch pointer. 
        # Actual file restoration (checkout) is a separate operation in the plan (Phase 5).
        meta = self._load_meta()
        if branch_name not in meta['branches']:
            raise ValueError(f"Branch '{branch_name}' does not exist.")
        
        meta['current_branch'] = branch_name
        self._write_meta(meta)

    def get_commits(self, branch_name=None, include_inherited=True):
        """
        Returns a time-sorted list of commits for the branch.
        If include_inherited is True, includes commits from parent branches recursively.
        """
        meta = self._load_meta()
        target_branch = branch_name if branch_name else meta['current_branch']
        
        if not include_inherited:
            branch_path = os.path.join(self.branches_dir, target_branch)
            if not os.path.exists(branch_path):
                return []
            commits = [f for f in os.listdir(branch_path) if f.endswith('.zip')]
            commits.sort(reverse=True)
            return commits
        
        # 1. Collect commits from the target branch and all its ancestors
        collected_commits = set()
        branches_to_visit = [target_branch]
        visited_branches = set()
        
        origins = meta.get('branch_origins', {})
        
        while branches_to_visit:
            current_b = branches_to_visit.pop(0)
            if current_b in visited_branches:
                continue
            visited_branches.add(current_b)
            
            # Get local commits for this branch
            branch_path = os.path.join(self.branches_dir, current_b)
            if os.path.exists(branch_path):
                local_commits = [f for f in os.listdir(branch_path) if f.endswith('.zip')]
                for c in local_commits:
                    collected_commits.add(c)
            
            # Check for parent
            if current_b in origins:
                parent = origins[current_b]['parent']
                if parent:
                    branches_to_visit.append(parent)

        # 2. Sort all collected commits
        # Commits named "YYYY-MM-DD_HH-MM-SS_hash.zip" or legacy "YYYY-MM-DD_HH-MM-SS__message.zip"
        sorted_commits = sorted(list(collected_commits), reverse=True)
        return sorted_commits

    def find_snapshot_path(self, snapshot_name, start_branch):
        """
        Locates a snapshot zip file by searching the start_branch and its ancestors.
        Returns the absolute path to the zip file.
        """
        meta = self._load_meta()
        branch = start_branch
        origins = meta.get('branch_origins', {})
        
        # Traverse up to find where the snapshot actually lives
        # Limit loop to avoid infinite recursion if circular (shouldn't happen)
        for _ in range(100): 
            potential_path = os.path.join(self.branches_dir, branch, snapshot_name)
            if os.path.exists(potential_path):
                return potential_path
            
            # Not found in current branch, check parent
            if branch in origins:
                branch = origins[branch]['parent']
            else:
                break
                
        raise FileNotFoundError(f"Snapshot {snapshot_name} not found in branch {start_branch} or ancestors.")

    def compare_snapshot(self, snapshot_name, progress_callback=None, search_branch=None):
        """
        Compares the current project state against a snapshot.
        Returns a tuple: (diff_list, temp_dir)
        User is responsible for calling shutil.rmtree(temp_dir) later, OR use merge_files to handle it.

        search_branch: if provided, start search from that branch. If None, searches current branch
        first, then falls back to all known branches so cross-branch merges work.
        """
        cb = progress_callback or (lambda msg: None)
        meta = self._load_meta()
        branch = search_branch or meta['current_branch']

        cb(f"Locating snapshot...")
        snapshot_path = None
        try:
            snapshot_path = self.find_snapshot_path(snapshot_name, branch)
        except FileNotFoundError:
            pass

        # Fallback: search all known branches (enables cross-branch merges)
        if snapshot_path is None:
            for alt_branch in meta.get('branches', []):
                if alt_branch == branch:
                    continue
                try:
                    snapshot_path = self.find_snapshot_path(snapshot_name, alt_branch)
                    break
                except FileNotFoundError:
                    continue

        if snapshot_path is None:
            raise FileNotFoundError(f"Snapshot {snapshot_name} not found in any branch.")

        # 1. Unzip to temp
        cb("Extracting snapshot for comparison...")
        temp_dir = tempfile.mkdtemp(dir=self.vault_path, prefix="solace_vault_")
        with zipfile.ZipFile(snapshot_path, 'r') as zf:
            _safe_extract(zf, temp_dir)
        cb("Comparing files...")

        diffs = []
        
        # 2. Walk Source Temp (Snapshot Content)
        # Check for NEW (in snapshot, not in project) and MODIFIED
        for root, dirs, files in os.walk(temp_dir):
            for file in files:
                temp_file_path = os.path.join(root, file)
                rel_path = os.path.relpath(temp_file_path, temp_dir)
                project_file_path = os.path.join(self.project_path, rel_path)
                
                if not os.path.exists(project_file_path):
                    diffs.append({
                        "file": rel_path,
                        "status": "NEW", 
                        "size_diff": os.path.getsize(temp_file_path),
                        "temp_path": temp_file_path
                    })
                else:
                    # Skip content reads when sizes already prove a difference.
                    stat_proj = os.stat(project_file_path)
                    stat_temp = os.stat(temp_file_path)
                    if (stat_proj.st_size != stat_temp.st_size
                            or not _files_equal(project_file_path, temp_file_path)):
                        diffs.append({
                            "file": rel_path,
                            "status": "MODIFIED",
                            "size_diff": stat_temp.st_size - stat_proj.st_size,
                            "temp_path": temp_file_path
                        })

        # 3. Check for MISSING (in project, not in snapshot)
        exclusions = self._load_exclusions()
        for root, dirs, files in os.walk(self.project_path):
            rel_root = os.path.relpath(root, self.project_path)
            if ".solace_vault" in rel_root.split(os.sep):
                continue
            dirs[:] = [
                d for d in dirs
                if not self._is_excluded(os.path.join(rel_root, d), exclusions)
            ]
            for file in files:
                project_file_path = os.path.join(root, file)
                rel_path = os.path.relpath(project_file_path, self.project_path)
                if self._is_excluded(rel_path, exclusions):
                    continue
                temp_file_path = os.path.join(temp_dir, rel_path)
                if not os.path.exists(temp_file_path):
                    diffs.append({
                        "file": rel_path,
                        "status": "MISSING",
                        "size_diff": 0,
                        "temp_path": None
                    })

        cb(f"Comparison complete: {len(diffs)} difference(s) found")
        return diffs, temp_dir

    def restore_snapshot(self, snapshot_name, progress_callback=None):
        """Restore a snapshot only after verifying it in a sibling staging tree."""
        cb = progress_callback or (lambda msg: None)
        meta = self._load_meta()
        branch = meta['current_branch']

        cb("Locating snapshot...")
        snapshot_path = self.find_snapshot_path(snapshot_name, branch)
        archive_size = os.path.getsize(snapshot_path) / (1024 * 1024)
        cb(f"Found snapshot: {archive_size:.2f} MB")

        project_path = os.path.abspath(self.project_path)
        project_parent = os.path.dirname(project_path)
        project_name = os.path.basename(project_path)
        stage_dir = tempfile.mkdtemp(
            dir=project_parent, prefix=f".{project_name}.solace-restore-"
        )
        backup_root = None
        backup_path = None
        old_project_moved = False

        try:
            cb("Verifying archive integrity...")
            with zipfile.ZipFile(snapshot_path, 'r') as zf:
                bad_file = zf.testzip()
                if bad_file is not None:
                    raise RuntimeError(
                        f"Archive '{snapshot_name}' is corrupted (bad file: '{bad_file}'). "
                        "Restore aborted — the current project was left unchanged."
                    )

                # The vault is state owned by ProjectVault, not snapshot content.
                # Never allow an archive to replace the live metadata directory.
                for member in zf.namelist():
                    normalized = os.path.normpath(member.replace('\\', '/'))
                    if normalized == '.solace_vault' or normalized.startswith('.solace_vault/'):
                        raise RuntimeError(
                            f"Archive '{snapshot_name}' contains reserved .solace_vault data; "
                            "restore aborted — the current project was left unchanged."
                        )

                cb("Extracting snapshot to staging...")
                file_count = _safe_extract(zf, stage_dir)

            # Keep branch history and metadata with the restored project. Copy it
            # only after the archive has been fully checked and extracted.
            shutil.copytree(
                self.vault_path,
                os.path.join(stage_dir, ".solace_vault"),
                symlinks=True,
            )

            # Rename the complete old tree aside, then publish the prepared tree.
            # If publication fails, put the old tree back before returning.
            backup_root = tempfile.mkdtemp(
                dir=project_parent, prefix=f".{project_name}.solace-restore-backup-"
            )
            backup_path = os.path.join(backup_root, "previous-project")
            os.rename(project_path, backup_path)
            old_project_moved = True
            try:
                os.rename(stage_dir, project_path)
                stage_dir = None
            except BaseException as publication_error:
                try:
                    os.rename(backup_path, project_path)
                    old_project_moved = False
                    backup_path = None
                except OSError as rollback_error:
                    raise RuntimeError(
                        "Restore publication failed and rollback could not restore the "
                        f"previous project. Its files are preserved at {backup_path}: "
                        f"{rollback_error}"
                    ) from publication_error
                raise

            cb(f"Restored {file_count} files from snapshot")
            try:
                shutil.rmtree(backup_root)
                backup_root = None
                backup_path = None
            except OSError as cleanup_error:
                cb(
                    "Restore completed, but the previous project copy could not be "
                    f"removed from {backup_path}: {cleanup_error}"
                )
        except BaseException:
            if old_project_moved and backup_path and not os.path.lexists(project_path):
                try:
                    os.rename(backup_path, project_path)
                    old_project_moved = False
                    backup_path = None
                except OSError as rollback_error:
                    raise RuntimeError(
                        "Restore failed and rollback could not restore the previous "
                        f"project. Its files are preserved at {backup_path}: "
                        f"{rollback_error}"
                    )
            raise
        finally:
            if stage_dir and os.path.exists(stage_dir):
                shutil.rmtree(stage_dir, ignore_errors=True)
            if (backup_root and os.path.isdir(backup_root)
                    and (not backup_path or not os.path.lexists(backup_path))):
                shutil.rmtree(backup_root, ignore_errors=True)
            
    def apply_merge(self, diffs, temp_dir):
        """
        Applies selected diffs (merges files from temp_dir to project).
        diffs: List of dicts checks from compare_snapshot.
        temp_dir: Optional extraction directory to remove after applying the merge.
        """
        try:
            for item in diffs:
                if item['status'] in ["NEW", "MODIFIED"]:
                    # Copy from temp to project
                    dest_path = os.path.join(self.project_path, item['file'])
                    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                    shutil.copy2(item['temp_path'], dest_path)
                elif item['status'] == "MISSING":
                    # File is in project but not in snapshot.
                    # "Merge" here means accepting the snapshot state (deleting local file)
                    target_path = os.path.join(self.project_path, item['file'])
                    if os.path.exists(target_path):
                        if os.path.isdir(target_path):
                             shutil.rmtree(target_path)
                        else:
                             os.remove(target_path)
        finally:
            if temp_dir and os.path.exists(temp_dir):
                shutil.rmtree(temp_dir)

    def export_branch_head(self, branch_name, destination_path, progress_callback=None):
        """
        Exports the latest snapshot (HEAD) of the specified branch to the destination path.
        """
        cb = progress_callback or (lambda msg: None)

        if not os.path.exists(destination_path):
            os.makedirs(destination_path)

        cb(f"Finding latest commit on '{branch_name}'...")
        commits = self.get_commits(branch_name)
        if not commits:
            raise ValueError(f"No commits found for branch '{branch_name}'.")

        latest_snapshot = commits[0]
        cb(f"Locating snapshot: {latest_snapshot}")
        snapshot_path = self.find_snapshot_path(latest_snapshot, branch_name)

        if not os.path.exists(snapshot_path):
            raise FileNotFoundError(f"Snapshot file not found: {snapshot_path}")

        archive_size = os.path.getsize(snapshot_path) / (1024 * 1024)
        cb(f"Exporting snapshot ({archive_size:.2f} MB)...")

        with zipfile.ZipFile(snapshot_path, 'r') as zf:
            file_count = _safe_extract(zf, destination_path)

        cb(f"Exported {file_count} files to {destination_path}")
        return latest_snapshot

    def delete_branch(self, branch_name):
        """
        Deletes a branch if it is not the current branch and has no dependent child branches.
        """
        meta = self._load_meta()
        
        # 1. Check existence
        if branch_name not in meta['branches']:
            raise ValueError(f"Branch '{branch_name}' does not exist.")
            
        # 2. Check if current
        if branch_name == meta['current_branch']:
            raise ValueError(f"Cannot delete the current active branch '{branch_name}'. Please switch to another branch first.")
            
        # 3. Check for dependents (children)
        origins = meta.get('branch_origins', {})
        dependents = []
        for b, origin in origins.items():
            if origin.get('parent') == branch_name:
                dependents.append(b)
        
        if dependents:
            raise ValueError(f"Cannot delete branch '{branch_name}' because the following branches depend on it: {', '.join(dependents)}")
            
        # 4. Perform Deletion
        
        # Remove from meta
        meta['branches'].remove(branch_name)
        if branch_name in origins:
            del origins[branch_name] # Remove its own origin record
        meta['branch_origins'] = origins
        
        self._write_meta(meta)
        
        # Remove directory
        branch_path = os.path.join(self.branches_dir, branch_name)
        if os.path.exists(branch_path):
            shutil.rmtree(branch_path)
