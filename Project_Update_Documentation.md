
---

# Solace Backup: "Project Vault" Implementation Plan

**Objective:** Extend the existing `backup_suite.pyw` application to include a "Project Vault."
**Concept:** A "Git-Lite" version control system designed for creative assets (images, 3D models, code).
**Core Philosophy:** Snapshot-Centric. No staging area, no line-by-line diffs, no detached heads. Files are treated as atomic units to support binary assets robustly.

---

## Phase 1: GUI Refactoring & Architecture

*Goal: Prepare the existing application to host the new functionality without breaking current features.*

* [ ] **Implement Tabbed Interface (`ttk.Notebook`)**
* Wrap the existing layout (Left Frame/Right Frame) into a new `ttk.Frame`.
* Create a root `ttk.Notebook` widget attached to the main window.
* **Tab 1:** "Automated Backups" (Move all existing UI elements here).
* **Tab 2:** "Project Vault" (New blank `ttk.Frame` for the new functionality).


* [ ] **Isolate Logic**
* Ensure existing `run_backup_job` functions remain independent of the new Vault logic.
* Create a new class `ProjectVaultBackend` to handle the new file operations.



## Phase 2: The "Vault" Backend Structure

*Goal: Define how data is stored on disk. This replaces the complex `.git` folder with a human-readable structure.*

* [ ] **Define Directory Structure**
* The application must manage a hidden folder `.solace_vault` inside the user's target project folder.
* **Structure:**
```text
/MyProject
    /.solace_vault
        /branches
            /main
                /2023-10-27_14-00-00__Initial_Commit.zip
                /2023-10-28_09-30-00__Added_Textures.zip
            /experimental
                /2023-10-29_10-00-00__New_Shaders.zip
        meta.json

```




* [ ] **Implement `meta.json` Schema**
* Create functions to read/write this JSON file.
* **Required Fields:**
```json
{
  "current_branch": "main",
  "created_at": "timestamp",
  "branches": ["main", "experimental"],
  "head_commit": "2023-10-29_10-00-00__New_Shaders.zip"
}

```





## Phase 3: Core Vault Operations

*Goal: Implement the basic actions (Init, Commit, Branch).*

* [ ] **Action: `Init Vault**`
* **UI:** Button "Initialize Project in Folder..." inside Tab 2.
* **Logic:** Check if `.solace_vault` exists. If not, create folders and default `meta.json` (Branch: "main").


* [ ] **Action: `Commit` (Save Snapshot)**
* **Input:** Commit Message (String).
* **Logic:**
1. Read `meta.json` to get `current_branch`.
2. Generate filename: `{timestamp}__{sanitized_message}.zip`.
3. Use `zipfile` to compress the **entire project folder**.
4. **CRITICAL EXCLUSION:** The zip process MUST exclude the `.solace_vault` folder itself to prevent infinite recursion/bloat.




* [ ] **Action: `Branch**`
* **Input:** New Branch Name.
* **Logic:**
1. Create new folder in `.solace_vault/branches/{new_name}`.
2. Update `meta.json` to add new branch to list.
3. (Optional) Copy the latest ZIP from the current branch to the new branch folder so it doesn't start empty.





## Phase 4: The Visual Merge Tool (The "No-Headache" Logic)

*Goal: Allow users to move files between branches without dealing with text conflict markers.*

* [ ] **UI: The Comparison Window**
* Create a `ttk.Treeview` with columns: **File Name**, **Status**, **Size Diff**, **Action**.
* **Status Types:** `New` (Green), `Modified` (Yellow), `Deleted` (Red).


* [ ] **Logic: "Compare & Take" Algorithm**
*Unlike Git, we do not merge file contents. We replace files.*
1. **Unzip to Temp:** Extract the *Source Snapshot* (the one you are merging FROM) to a temporary hidden folder.
2. **File Walk:** Iterate through the *Source Temp* folder and the *Active Project* folder.
3. **Comparison Check:**
* If file exists in Source but not Active -> Mark **NEW**.
* If file exists in both but size/timestamp differs -> Mark **MODIFIED**.
* If file exists in Active but not Source -> Mark **MISSING**.


4. **Generate List:** Populate the Treeview.


* [ ] **Action: "Merge Selected"**
* **Input:** List of checked items from the Treeview.
* **Logic:**
* For every checked file, `shutil.copy2` from the *Source Temp* folder to the *Active Project* folder.
* **Overwrite:** Automatically overwrite existing files (this is a "Force" merge).


* **Cleanup:** Delete the temporary unzip folder immediately after operation.



## Phase 5: Restoration & Checkout (Safety)

*Goal: Allow users to go back in time safely.*

* [ ] **Action: `Checkout Snapshot` (Restore)**
* **UI:** A list of commits in the current branch. Button: "Restore this Version".
* **Safety Check:**
* **Step 1:** Calculate a quick hash/timestamp check of the current folder. Has it changed since the last commit?
* **Step 2:** If changed, pop up a **Blocking Warning**: *"You have unsaved changes. Restoring will wipe them. Commit first?"*


* **Logic:**
1. Clear the project folder (excluding `.solace_vault`).
2. Unzip the selected snapshot into the project folder.





## Phase 6: Integration & Wiring

*Goal: Connect the new `ProjectVault` backend to the existing `BackupApp` class.*

* [ ] **Main App State Management**
* Add `self.current_vault = None` to the `BackupApp.__init__`.
* When user loads a project:
1. Initialize `self.current_vault = ProjectVault(path)`.
2. Populate the "History Listbox".
3. Update window title to `Solace Backup - Vault: [Project Name]`.




* [ ] **Threaded Execution**
* **Rule:** Never run `vault.commit()` or `vault.restore()` on the main UI thread.
* Use `threading.Thread` for all zip/unzip operations.
* Use the existing `log_queue` to send status updates ("Zipping...", "Done") to the UI.



## Phase 7: Creative "Quality of Life"

*Goal: Features specific to Photographers and Modders.*

* [ ] **Image/Texture Preview**
* In the "Merge/Compare" window, if a user selects a `.png`, `.jpg`, or `.json` file, show a preview in a side panel.


* [ ] **"Quick-Look" (Mounting)**
* Feature to unzip a snapshot to a temp folder and open it in Explorer, allowing the user to grab a single asset without performing a full restore.



---

### Technical Addendum: Recommended Class Structure

**`ProjectVault` Class Skeleton:**

```python
import os
import json
import zipfile
import shutil
from datetime import datetime

class ProjectVault:
    def __init__(self, project_path):
        self.project_path = project_path
        self.vault_path = os.path.join(project_path, ".solace_vault")
        self.branches_dir = os.path.join(self.vault_path, "branches")
        self.meta_file = os.path.join(self.vault_path, "meta.json")
        self._ensure_structure()

    def commit(self, message):
        """Snapshots the folder, ignoring the vault itself."""
        meta = self._load_meta()
        branch = meta['current_branch']
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        
        snapshot_name = f"{timestamp}__{message.replace(' ', '_')}.zip"
        save_path = os.path.join(self.branches_dir, branch, snapshot_name)
        
        with zipfile.ZipFile(save_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, dirs, files in os.walk(self.project_path):
                if ".solace_vault" in root: continue # CRITICAL EXCLUSION
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, self.project_path)
                    zf.write(full_path, rel_path)
        return snapshot_name

```