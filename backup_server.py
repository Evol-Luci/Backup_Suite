#!/usr/bin/env python3
"""
Solace Backup Suite - HTTP REST API Server
Provides RESTful API for backup management via HTTP requests.

This server allows external projects (like Good Vibes Cosmetic) to integrate
with Solace Backup Suite via HTTP calls from any language/framework.

Usage:
    # Start server (foreground)
    python backup_server.py

    # Start server on specific host/port
    python backup_server.py --host 0.0.0.0 --port 8080

    # Start as daemon
    python backup_server.py --daemon

    # Stop daemon
    python backup_server.py --stop

    # Check status
    python backup_server.py --status

API Examples:
    # Create job
    curl -X POST http://localhost:8777/api/jobs \
      -H "Content-Type: application/json" \
      -d '{
        "name": "test_backup",
        "source_dir": "/tmp/source",
        "destination_base": "/tmp/backup",
        "schedule": "manual"
      }'

    # List jobs
    curl http://localhost:8777/api/jobs

    # Get job details
    curl http://localhost:8777/api/jobs/test_backup

    # Run job (non-blocking)
    curl -X POST http://localhost:8777/api/jobs/test_backup/run

    # Run job (blocking - wait for completion)
    curl -X POST http://localhost:8777/api/jobs/test_backup/run \
      -H "Content-Type: application/json" \
      -d '{"blocking": true}'

    # Enable/disable job
    curl -X POST http://localhost:8777/api/jobs/test_backup/enable
    curl -X POST http://localhost:8777/api/jobs/test_backup/disable

    # Delete job
    curl -X DELETE http://localhost:8777/api/jobs/test_backup

    # Get scheduler status
    curl http://localhost:8777/api/scheduler/status

    # Start/stop scheduler
    curl -X POST http://localhost:8777/api/scheduler/start
    curl -X POST http://localhost:8777/api/scheduler/stop

    # Get logs
    curl "http://localhost:8777/api/logs?lines=50"
    curl "http://localhost:8777/api/logs/test_backup?lines=50"

    # Health check
    curl http://localhost:8777/api/health
"""

import json
import sys
import os
import re
import signal
import argparse
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

# Import from backup_api.py
from backup_api import BackupManager, APS_AVAILABLE

# ==============================================================================
# CONFIGURATION
# ==============================================================================
DEFAULT_HOST = "127.0.0.1"  # localhost only by default for security
DEFAULT_PORT = 8777  # SOLACE -> 8777 on T9 keypad
DEFAULT_CONFIG_DIR = "Settings"
PID_FILE = "/tmp/solace_backup_server.pid"
VERSION = "1.0.0"

# Optional API key for simple auth (set via env var)
API_KEY = os.environ.get("SOLACE_BACKUP_API_KEY")


# ==============================================================================
# HTTP REQUEST HANDLER
# ==============================================================================
class BackupAPIHandler(BaseHTTPRequestHandler):
    """HTTP request handler for backup API."""

    # Reference to BackupManager (set by server)
    backup_manager: Optional[BackupManager] = None
    server_start_time: Optional[datetime] = None

    def log_message(self, format: str, *args) -> None:
        """Override to use our own logging format."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {self.client_address[0]} - {format % args}")

    def _send_cors_headers(self) -> None:
        """Send CORS headers restricted to localhost only."""
        self.send_header("Access-Control-Allow-Origin", "http://localhost")
        self.send_header(
            "Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS"
        )
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-API-Key")

    def _send_json_response(
        self, data: Dict[str, Any], status_code: int = 200, success: bool = True
    ) -> None:
        """Send a JSON response with proper headers."""
        response = {
            "success": success,
            "timestamp": datetime.now().isoformat(),
        }
        if success:
            response.update(data)
        else:
            response["error"] = data.get("error", "Unknown error")
            response["code"] = data.get("code", "UNKNOWN_ERROR")
            if "details" in data:
                response["details"] = data["details"]

        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        self._send_cors_headers()
        self.end_headers()
        self.wfile.write(json.dumps(response, indent=2).encode("utf-8"))

    def _parse_request_body(self) -> Dict[str, Any]:
        """Parse JSON request body."""
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            return {}

        body = self.rfile.read(content_length).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError as e:
            raise ValueError(f"Invalid JSON: {e}")

    def _check_auth(self) -> bool:
        """Check API key if configured."""
        if not API_KEY:
            return True

        api_key_header = self.headers.get("X-API-Key")
        if api_key_header != API_KEY:
            self._send_json_response(
                {"error": "Invalid or missing API key", "code": "UNAUTHORIZED"},
                status_code=401,
                success=False,
            )
            return False
        return True

    def _get_path_parts(self) -> List[str]:
        """Parse URL path into parts."""
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        return [p for p in path.split("/") if p]

    def _get_query_params(self) -> Dict[str, List[str]]:
        """Parse query parameters."""
        parsed = urlparse(self.path)
        return parse_qs(parsed.query)

    def do_OPTIONS(self) -> None:
        """Handle CORS preflight requests."""
        self.send_response(204)
        self._send_cors_headers()
        self.end_headers()

    def do_GET(self) -> None:
        """Handle GET requests."""
        if not self._check_auth():
            return

        try:
            path_parts = self._get_path_parts()
            query_params = self._get_query_params()

            if not path_parts:
                self._send_json_response(
                    {"message": "Solace Backup Suite API", "version": VERSION}
                )
                return

            # Route: /api/jobs
            if path_parts == ["api", "jobs"]:
                self._handle_list_jobs()
                return

            # Route: /api/jobs/<id>
            if len(path_parts) == 3 and path_parts[:2] == ["api", "jobs"]:
                job_id = path_parts[2]
                self._handle_get_job(job_id)
                return

            # Route: /api/scheduler/status
            if path_parts == ["api", "scheduler", "status"]:
                self._handle_get_scheduler_status()
                return

            # Route: /api/status
            if path_parts == ["api", "status"]:
                self._handle_get_status()
                return

            # Route: /api/logs or /api/logs/<job_id>
            if path_parts[:2] == ["api", "logs"]:
                job_id = path_parts[2] if len(path_parts) > 2 else None
                lines = int(query_params.get("lines", ["100"])[0])
                self._handle_get_logs(job_id, lines)
                return

            # Route: /api/health
            if path_parts == ["api", "health"]:
                self._handle_health_check()
                return

            # Unknown endpoint
            self._send_json_response(
                {"error": f"Endpoint not found: {self.path}", "code": "NOT_FOUND"},
                status_code=404,
                success=False,
            )

        except Exception as e:
            self._send_json_response(
                {"error": str(e), "code": "INTERNAL_ERROR"},
                status_code=500,
                success=False,
            )

    def do_POST(self) -> None:
        """Handle POST requests."""
        if not self._check_auth():
            return

        try:
            path_parts = self._get_path_parts()
            body = self._parse_request_body()

            # Route: /api/jobs (Create job)
            if path_parts == ["api", "jobs"]:
                self._handle_create_job(body)
                return

            # Route: /api/jobs/<id>/enable
            if (
                len(path_parts) == 4
                and path_parts[:2] == ["api", "jobs"]
                and path_parts[3] == "enable"
            ):
                job_id = path_parts[2]
                self._handle_enable_job(job_id)
                return

            # Route: /api/jobs/<id>/disable
            if (
                len(path_parts) == 4
                and path_parts[:2] == ["api", "jobs"]
                and path_parts[3] == "disable"
            ):
                job_id = path_parts[2]
                self._handle_disable_job(job_id)
                return

            # Route: /api/jobs/<id>/run
            if (
                len(path_parts) == 4
                and path_parts[:2] == ["api", "jobs"]
                and path_parts[3] == "run"
            ):
                job_id = path_parts[2]
                self._handle_run_job(job_id, body)
                return

            # Route: /api/scheduler/start
            if path_parts == ["api", "scheduler", "start"]:
                self._handle_start_scheduler()
                return

            # Route: /api/scheduler/stop
            if path_parts == ["api", "scheduler", "stop"]:
                self._handle_stop_scheduler()
                return

            # Route: /api/scheduler/reload
            if path_parts == ["api", "scheduler", "reload"]:
                self._handle_reload_scheduler()
                return

            # Unknown endpoint
            self._send_json_response(
                {"error": f"Endpoint not found: {self.path}", "code": "NOT_FOUND"},
                status_code=404,
                success=False,
            )

        except ValueError as e:
            self._send_json_response(
                {"error": str(e), "code": "BAD_REQUEST"}, status_code=400, success=False
            )
        except Exception as e:
            self._send_json_response(
                {"error": str(e), "code": "INTERNAL_ERROR"},
                status_code=500,
                success=False,
            )

    def do_PUT(self) -> None:
        """Handle PUT requests."""
        if not self._check_auth():
            return

        try:
            path_parts = self._get_path_parts()
            body = self._parse_request_body()

            # Route: /api/jobs/<id> (Update job)
            if len(path_parts) == 3 and path_parts[:2] == ["api", "jobs"]:
                job_id = path_parts[2]
                self._handle_update_job(job_id, body)
                return

            # Unknown endpoint
            self._send_json_response(
                {"error": f"Endpoint not found: {self.path}", "code": "NOT_FOUND"},
                status_code=404,
                success=False,
            )

        except ValueError as e:
            self._send_json_response(
                {"error": str(e), "code": "BAD_REQUEST"}, status_code=400, success=False
            )
        except Exception as e:
            self._send_json_response(
                {"error": str(e), "code": "INTERNAL_ERROR"},
                status_code=500,
                success=False,
            )

    def do_DELETE(self) -> None:
        """Handle DELETE requests."""
        if not self._check_auth():
            return

        try:
            path_parts = self._get_path_parts()

            # Route: /api/jobs/<id> (Delete job)
            if len(path_parts) == 3 and path_parts[:2] == ["api", "jobs"]:
                job_id = path_parts[2]
                self._handle_delete_job(job_id)
                return

            # Unknown endpoint
            self._send_json_response(
                {"error": f"Endpoint not found: {self.path}", "code": "NOT_FOUND"},
                status_code=404,
                success=False,
            )

        except Exception as e:
            self._send_json_response(
                {"error": str(e), "code": "INTERNAL_ERROR"},
                status_code=500,
                success=False,
            )

    # ==========================================================================
    # HANDLER METHODS - Job Management
    # ==========================================================================

    def _handle_list_jobs(self) -> None:
        """List all backup jobs."""
        jobs = self.backup_manager.list_jobs()

        # Enhance with status info
        enhanced_jobs = []
        for job in jobs:
            status_result = self.backup_manager.get_job_status(job["name"])
            if status_result.get("success"):
                job["status"] = status_result.get("data", {})
            enhanced_jobs.append(job)

        self._send_json_response({"data": enhanced_jobs, "count": len(enhanced_jobs)})

    def _handle_get_job(self, job_id: str) -> None:
        """Get a single job by ID."""
        job = self.backup_manager.get_job(job_id)

        if not job:
            self._send_json_response(
                {"error": f"Job '{job_id}' not found", "code": "JOB_NOT_FOUND"},
                status_code=404,
                success=False,
            )
            return

        # Enhance with status info
        status_result = self.backup_manager.get_job_status(job_id)
        if status_result.get("success"):
            job["status"] = status_result.get("data", {})

        self._send_json_response({"data": job})

    def _handle_create_job(self, body: Dict[str, Any]) -> None:
        """Create a new backup job."""
        # Validate required fields
        required = ["name", "source_dir", "destination_base"]
        missing = [f for f in required if f not in body]
        if missing:
            self._send_json_response(
                {
                    "error": f"Missing required fields: {', '.join(missing)}",
                    "code": "MISSING_FIELDS",
                },
                status_code=400,
                success=False,
            )
            return

        # Create job
        result = self.backup_manager.create_job(
            name=body["name"],
            source_dir=body["source_dir"],
            destination_base=body["destination_base"],
            schedule=body.get("schedule", "manual"),
            enabled=body.get("enabled", True),
            exclusions=body.get("exclusions", []),
            volumes_to_keep=body.get("volumes_to_keep"),
        )

        if result.get("success"):
            self._send_json_response(
                {"data": result.get("data"), "message": result.get("message")},
                status_code=201,
            )
        else:
            code = result.get("code", "CREATE_ERROR")
            status = 409 if code == "DUPLICATE_JOB" else 400
            self._send_json_response(
                {
                    "error": result.get("error"),
                    "code": code,
                    "details": result.get("details"),
                },
                status_code=status,
                success=False,
            )

    def _handle_update_job(self, job_id: str, body: Dict[str, Any]) -> None:
        """Update an existing job."""
        # Rename volumes_to_keep to volumes_to_keep_override for API
        if "volumes_to_keep" in body:
            body["volumes_to_keep_override"] = body.pop("volumes_to_keep")

        result = self.backup_manager.update_job(job_id, **body)

        if result.get("success"):
            self._send_json_response(
                {"data": result.get("data"), "message": result.get("message")}
            )
        else:
            code = result.get("code", "UPDATE_ERROR")
            status = 404 if code == "NOT_FOUND" else 400
            self._send_json_response(
                {"error": result.get("error"), "code": code},
                status_code=status,
                success=False,
            )

    def _handle_delete_job(self, job_id: str) -> None:
        """Delete a job."""
        result = self.backup_manager.delete_job(job_id)

        if result.get("success"):
            self._send_json_response({"message": result.get("message")})
        else:
            code = result.get("code", "DELETE_ERROR")
            status = 404 if code == "NOT_FOUND" else 500
            self._send_json_response(
                {"error": result.get("error"), "code": code},
                status_code=status,
                success=False,
            )

    def _handle_enable_job(self, job_id: str) -> None:
        """Enable a job."""
        result = self.backup_manager.enable_job(job_id)

        if result.get("success"):
            self._send_json_response(
                {"data": result.get("data"), "message": result.get("message")}
            )
        else:
            code = result.get("code", "ENABLE_ERROR")
            status = 404 if code == "NOT_FOUND" else 500
            self._send_json_response(
                {"error": result.get("error"), "code": code},
                status_code=status,
                success=False,
            )

    def _handle_disable_job(self, job_id: str) -> None:
        """Disable a job."""
        result = self.backup_manager.disable_job(job_id)

        if result.get("success"):
            self._send_json_response(
                {"data": result.get("data"), "message": result.get("message")}
            )
        else:
            code = result.get("code", "DISABLE_ERROR")
            status = 404 if code == "NOT_FOUND" else 500
            self._send_json_response(
                {"error": result.get("error"), "code": code},
                status_code=status,
                success=False,
            )

    def _handle_run_job(self, job_id: str, body: Dict[str, Any]) -> None:
        """Execute a backup job."""
        blocking = body.get("blocking", False)

        result = self.backup_manager.run_job(job_id, blocking=blocking)

        if result.get("success"):
            if blocking:
                # Job completed, return full result
                data = result.get("data", {})
                self._send_json_response(
                    {
                        "data": {
                            "job_id": job_id,
                            "status": "completed",
                            "duration": data.get("duration"),
                            "files_processed": data.get("files_processed"),
                            "backup_size": data.get("backup_size"),
                            "zip_file": data.get("zip_file"),
                        }
                    }
                )
            else:
                # Job queued
                self._send_json_response(
                    {
                        "data": {
                            "job_id": job_id,
                            "status": "started",
                            "message": "Backup job started in background",
                        }
                    },
                    status_code=202,
                )
        else:
            code = result.get("code", "RUN_ERROR")
            if code == "NOT_FOUND":
                status = 404
            elif code == "JOB_DISABLED":
                status = 409
            else:
                status = 500
            self._send_json_response(
                {"error": result.get("error"), "code": code},
                status_code=status,
                success=False,
            )

    # ==========================================================================
    # HANDLER METHODS - Scheduler
    # ==========================================================================

    def _handle_get_scheduler_status(self) -> None:
        """Get scheduler status."""
        result = self.backup_manager.get_scheduler_status()

        if result.get("success"):
            data = result.get("data", {})
            data["jobs_scheduled"] = len(data.get("jobs", []))

            # Calculate uptime
            if self.server_start_time:
                uptime = datetime.now() - self.server_start_time
                days = uptime.days
                hours, remainder = divmod(uptime.seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                uptime_str = ""
                if days:
                    uptime_str += f"{days} day{'s' if days != 1 else ''}, "
                if hours:
                    uptime_str += f"{hours} hour{'s' if hours != 1 else ''}, "
                uptime_str += f"{minutes} minute{'s' if minutes != 1 else ''}"
                data["uptime"] = uptime_str

            self._send_json_response({"data": data})
        else:
            self._send_json_response(
                {"error": result.get("error"), "code": result.get("code")},
                status_code=503
                if result.get("code") == "SCHEDULER_UNAVAILABLE"
                else 500,
                success=False,
            )

    def _handle_start_scheduler(self) -> None:
        """Start the scheduler."""
        result = self.backup_manager.start_scheduler()

        if result.get("success"):
            self._send_json_response({"message": result.get("message")})
        else:
            self._send_json_response(
                {"error": result.get("error"), "code": result.get("code")},
                status_code=503
                if result.get("code") == "SCHEDULER_UNAVAILABLE"
                else 500,
                success=False,
            )

    def _handle_stop_scheduler(self) -> None:
        """Stop the scheduler."""
        result = self.backup_manager.stop_scheduler()

        if result.get("success"):
            self._send_json_response({"message": result.get("message")})
        else:
            self._send_json_response(
                {"error": result.get("error"), "code": result.get("code")},
                status_code=503
                if result.get("code") == "SCHEDULER_UNAVAILABLE"
                else 500,
                success=False,
            )

    def _handle_reload_scheduler(self) -> None:
        """Reload scheduler jobs."""
        result = self.backup_manager.reload_scheduler()

        if result.get("success"):
            self._send_json_response({"message": result.get("message")})
        else:
            self._send_json_response(
                {"error": result.get("error"), "code": result.get("code")},
                status_code=503
                if result.get("code") == "SCHEDULER_UNAVAILABLE"
                else 500,
                success=False,
            )

    # ==========================================================================
    # HANDLER METHODS - System
    # ==========================================================================

    def _handle_get_status(self) -> None:
        """Get overall system status."""
        jobs = self.backup_manager.list_jobs()
        enabled_jobs = sum(1 for j in jobs if j.get("enabled", False))

        sched_result = self.backup_manager.get_scheduler_status()
        scheduler_running = (
            sched_result.get("data", {}).get("running", False)
            if sched_result.get("success")
            else False
        )

        current_job = self.backup_manager.get_current_job()

        self._send_json_response(
            {
                "data": {
                    "status": "running" if current_job else "idle",
                    "current_job": current_job,
                    "job_count": len(jobs),
                    "enabled_jobs": enabled_jobs,
                    "scheduler_running": scheduler_running,
                    "apscheduler_available": APS_AVAILABLE,
                    "version": VERSION,
                }
            }
        )

    def _handle_get_logs(self, job_id: Optional[str], lines: int) -> None:
        """Get log entries."""
        logs = self.backup_manager.get_logs(job_id=job_id, lines=lines)
        self._send_json_response({"data": logs, "count": len(logs)})

    def _handle_health_check(self) -> None:
        """Health check endpoint."""
        self._send_json_response(
            {
                "status": "healthy",
                "timestamp": datetime.now().isoformat() + "Z",
                "version": VERSION,
            }
        )


# ==============================================================================
# SERVER CLASS
# ==============================================================================
class ThreadedHTTPServer(HTTPServer):
    """Threaded HTTP server to handle concurrent requests."""

    allow_reuse_address = True
    daemon_threads = True


class BackupAPIServer:
    """Main API server class."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        config_dir: str = DEFAULT_CONFIG_DIR,
    ):
        self.host = host
        self.port = port
        self.config_dir = config_dir
        self.backup_manager: Optional[BackupManager] = None
        self.server: Optional[ThreadedHTTPServer] = None
        self.server_thread: Optional[threading.Thread] = None
        self.running = False

    def start(self) -> bool:
        """Start the HTTP server."""
        try:
            # Initialize backup manager
            print(f"Initializing BackupManager (config: {self.config_dir})...")
            self.backup_manager = BackupManager(config_dir=self.config_dir)

            # Create server
            self.server = ThreadedHTTPServer((self.host, self.port), BackupAPIHandler)

            # Inject dependencies into handler
            BackupAPIHandler.backup_manager = self.backup_manager
            BackupAPIHandler.server_start_time = datetime.now()

            print(f"Starting Solace Backup API Server...")
            print(f"  Host: {self.host}")
            print(f"  Port: {self.port}")
            print(f"  Config: {self.config_dir}")
            print(f"  PID: {os.getpid()}")

            if API_KEY:
                print("  API Key authentication: ENABLED")
            else:
                print(
                    "  API Key authentication: DISABLED (set SOLACE_BACKUP_API_KEY env var to enable)"
                )

            print(f"\nAPI endpoints available at http://{self.host}:{self.port}/api/")
            print("Press Ctrl+C to stop\n")

            self.running = True

            # Start server in a thread (for non-blocking operation)
            self.server_thread = threading.Thread(
                target=self.server.serve_forever, daemon=True
            )
            self.server_thread.start()

            # Keep main thread alive
            while self.running:
                time.sleep(1)

            return True

        except Exception as e:
            print(f"ERROR: Failed to start server: {e}")
            return False

    def stop(self) -> None:
        """Stop the server gracefully."""
        print("\nShutting down server...")
        self.running = False

        if self.server:
            self.server.shutdown()
            self.server.server_close()

        if self.server_thread:
            self.server_thread.join(timeout=5)

        print("Server stopped.")

    def run_foreground(self) -> int:
        """Run server in foreground."""
        try:
            # Setup signal handlers
            def signal_handler(sig, frame):
                self.stop()
                sys.exit(0)

            signal.signal(signal.SIGINT, signal_handler)
            signal.signal(signal.SIGTERM, signal_handler)

            if not self.start():
                return 1

            return 0
        except Exception as e:
            print(f"ERROR: {e}")
            return 1


# ==============================================================================
# DAEMON MANAGEMENT
# ==============================================================================
def write_pid_file(pid: int) -> bool:
    """Write PID to file."""
    try:
        with open(PID_FILE, "w") as f:
            f.write(str(pid))
        return True
    except Exception as e:
        print(f"ERROR: Could not write PID file: {e}")
        return False


def read_pid_file() -> Optional[int]:
    """Read PID from file."""
    try:
        if os.path.exists(PID_FILE):
            with open(PID_FILE, "r") as f:
                return int(f.read().strip())
    except Exception:
        pass
    return None


def remove_pid_file() -> None:
    """Remove PID file."""
    try:
        if os.path.exists(PID_FILE):
            os.remove(PID_FILE)
    except Exception:
        pass


def is_process_running(pid: int) -> bool:
    """Check if a process is running."""
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def start_daemon(host: str, port: int, config_dir: str) -> int:
    """Start server as a daemon."""
    # Check if already running
    existing_pid = read_pid_file()
    if existing_pid and is_process_running(existing_pid):
        print(f"Server is already running (PID: {existing_pid})")
        return 1

    # Fork to background
    try:
        pid = os.fork()
        if pid > 0:
            # Parent process - write PID and exit
            write_pid_file(pid)
            print(f"Server started as daemon (PID: {pid})")
            return 0
    except OSError as e:
        print(f"ERROR: Failed to fork: {e}")
        return 1

    # Child process - start server
    os.setsid()  # Create new session
    os.umask(0)

    # Redirect stdout/stderr to log file
    log_dir = os.path.expanduser("~/.local/share/solace/logs")
    os.makedirs(log_dir, mode=0o700, exist_ok=True)
    log_file = os.path.join(log_dir, "backup_server.log")
    sys.stdout = open(log_file, "a")
    sys.stderr = open(log_file, "a")

    print(f"[{datetime.now()}] Daemon starting...")

    server = BackupAPIServer(host=host, port=port, config_dir=config_dir)
    return server.run_foreground()


def stop_daemon() -> int:
    """Stop the daemon."""
    pid = read_pid_file()
    if not pid:
        print("Server is not running (no PID file found)")
        return 1

    if not is_process_running(pid):
        print(f"Server is not running (PID {pid} not found)")
        remove_pid_file()
        return 1

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Sent stop signal to server (PID: {pid})")

        # Wait for process to exit
        for _ in range(10):
            if not is_process_running(pid):
                break
            time.sleep(0.5)

        remove_pid_file()
        print("Server stopped.")
        return 0
    except Exception as e:
        print(f"ERROR: Failed to stop server: {e}")
        return 1


def check_status() -> int:
    """Check server status."""
    pid = read_pid_file()

    if pid and is_process_running(pid):
        print(f"Server is running (PID: {pid})")

        # Try to connect to API
        try:
            import http.client

            conn = http.client.HTTPConnection("127.0.0.1", DEFAULT_PORT, timeout=2)
            conn.request("GET", "/api/health")
            response = conn.getresponse()

            if response.status == 200:
                data = json.loads(response.read().decode())
                print(f"  API Status: {data.get('status')}")
                print(f"  Version: {data.get('version')}")
                return 0
            else:
                print(f"  API Status: Error (HTTP {response.status})")
                return 1
        except Exception as e:
            print(f"  API Status: Unreachable ({e})")
            return 1
    else:
        print("Server is not running")
        if pid:
            remove_pid_file()
        return 1


# ==============================================================================
# MAIN ENTRY POINT
# ==============================================================================
def main() -> int:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Solace Backup Suite - HTTP REST API Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Start server (foreground)
  python backup_server.py

  # Start server on specific host/port
  python backup_server.py --host 0.0.0.0 --port 8080

  # Start as daemon
  python backup_server.py --daemon

  # Stop daemon
  python backup_server.py --stop

  # Check status
  python backup_server.py --status

Environment Variables:
  SOLACE_BACKUP_API_KEY    Set API key for authentication
        """,
    )

    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"Host to bind to (default: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to bind to (default: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--config-dir",
        default=DEFAULT_CONFIG_DIR,
        help=f"Configuration directory (default: {DEFAULT_CONFIG_DIR})",
    )
    parser.add_argument(
        "--daemon", action="store_true", help="Run as daemon (background)"
    )
    parser.add_argument("--stop", action="store_true", help="Stop running daemon")
    parser.add_argument("--status", action="store_true", help="Check server status")

    args = parser.parse_args()

    if args.stop:
        return stop_daemon()

    if args.status:
        return check_status()

    if args.daemon:
        return start_daemon(args.host, args.port, args.config_dir)

    # Run in foreground
    server = BackupAPIServer(host=args.host, port=args.port, config_dir=args.config_dir)
    return server.run_foreground()


if __name__ == "__main__":
    sys.exit(main())
