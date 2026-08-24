#!/usr/bin/env python3
import os
import sys
import time
import json
import threading
import concurrent.futures
from mcp.server.fastmcp import FastMCP

# 1. Initialize FastMCP Server
mcp = FastMCP("Filesystem-MCP-Server")

# 2. Configuration Management
CONFIG_FILE = "mcp_config.json"
DEFAULT_CONFIG = {
    "allowed_directories": [
        os.path.abspath("."),
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ],
    "resumes_directory": os.path.abspath("./resumes"),
    "reports_directory": os.path.abspath("./reports")
}

def load_config():
    """Loads configuration and returns standard paths."""
    config = DEFAULT_CONFIG.copy()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                user_config = json.load(f)
                for key in ["resumes_directory", "reports_directory"]:
                    if key in user_config:
                        config[key] = os.path.abspath(user_config[key])
                if "allowed_directories" in user_config:
                    config["allowed_directories"] = [
                        os.path.abspath(p) for p in user_config["allowed_directories"]
                    ]
        except Exception as e:
            print(f"[Config] Error loading config file, using defaults: {e}", file=sys.stderr)
    
    # Ensure allowed directories contains the resumes and reports folders
    config["allowed_directories"].append(config["resumes_directory"])
    config["allowed_directories"].append(config["reports_directory"])
    # Resolve all allowed dirs to absolute
    config["allowed_directories"] = list(set(os.path.abspath(p) for p in config["allowed_directories"]))
    return config

config = load_config()

def check_path_permission(path: str):
    """
    Validates that the path exists within allowed directories.
    Prevents directory traversal outside safe boundaries.
    """
    abs_path = os.path.abspath(path)
    allowed_dirs = config["allowed_directories"]
    allowed = False
    for allowed_dir in allowed_dirs:
        if abs_path == allowed_dir or abs_path.startswith(allowed_dir + os.sep):
            allowed = True
            break
    if not allowed:
        raise PermissionError(
            f"Access Denied: Path '{path}' (resolved to '{abs_path}') is outside allowed directories: {allowed_dirs}"
        )

# 3. PDF Parsing Helper
def extract_text_from_pdf(pdf_path: str) -> str:
    """Helper to extract text from a PDF file using pypdf."""
    try:
        import pypdf
        reader = pypdf.PdfReader(pdf_path)
        text = ""
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                text += f"\n--- Page {i+1} ---\n{page_text}"
        return text.strip()
    except ImportError:
        return "[Error] pypdf library is not installed on the server. Cannot parse PDF."
    except Exception as e:
        return f"[Error] Failed to read PDF file '{os.path.basename(pdf_path)}': {str(e)}"

# 4. Watch Directory State
watch_lock = threading.Lock()
watched_dirs = {}      # path -> set(filenames)
new_files_queues = {}  # path -> list(new_filenames)
watch_threads = {}

def poll_directory(dir_path: str):
    """Worker thread function to poll a directory for changes."""
    print(f"[Watcher] Started background thread for: {dir_path}", file=sys.stderr)
    while True:
        time.sleep(2)
        with watch_lock:
            if dir_path not in watched_dirs:
                # Stop watching if directory removed from tracking
                print(f"[Watcher] Stopped background thread for: {dir_path}", file=sys.stderr)
                break
            if not os.path.exists(dir_path) or not os.path.isdir(dir_path):
                continue
            try:
                current_files = set(os.listdir(dir_path))
                old_files = watched_dirs[dir_path]
                new_files = current_files - old_files
                if new_files:
                    print(f"[Watcher] New files detected: {new_files}", file=sys.stderr)
                    new_files_queues[dir_path].extend(list(new_files))
                    watched_dirs[dir_path].update(new_files)
            except Exception as e:
                print(f"[Watcher Error] Error polling {dir_path}: {e}", file=sys.stderr)

# 5. Core MCP Tools (Milestone 1 equivalents)

@mcp.tool()
def list_directory(path: str) -> str:
    """
    List all files in the given directory path.
    Enforces security boundary checks. Returns a JSON-serialized list of filenames.
    """
    try:
        check_path_permission(path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Directory not found: {path}")
        if not os.path.isdir(path):
            raise ValueError(f"Path is not a directory: {path}")
        return json.dumps(os.listdir(path))
    except Exception as e:
        print(f"[Error] list_directory failed: {e}", file=sys.stderr)
        return json.dumps([f"Error: {str(e)}"])

@mcp.tool()
def read_file(path: str) -> str:
    """
    Read the contents of a file. Supports text files and PDF file parsing.
    Enforces security boundary checks.
    """
    try:
        check_path_permission(path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"File not found: {path}")
        
        ext = os.path.splitext(path)[1].lower()
        if ext == ".pdf":
            return extract_text_from_pdf(path)
        else:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        print(f"[Error] read_file failed: {e}", file=sys.stderr)
        return f"Error: {str(e)}"

@mcp.tool()
def write_file(path: str, content: str) -> str:
    """
    Write content to a file at the specified path. Creates directories if necessary.
    Enforces security boundary checks.
    """
    try:
        check_path_permission(path)
        dir_name = os.path.dirname(path)
        if dir_name:
            os.makedirs(dir_name, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        return f"Successfully wrote file to: {path}"
    except Exception as e:
        print(f"[Error] write_file failed: {e}", file=sys.stderr)
        return f"Error: {str(e)}"

# 6. New MCP-Specific Capabilities

@mcp.tool()
def watch_directory(path: str) -> str:
    """
    Monitor the specified directory for new files.
    First Call: Starts the polling watcher.
    Subsequent Calls: Returns any newly added files since the last check.
    """
    try:
        check_path_permission(path)
        if not os.path.exists(path) or not os.path.isdir(path):
            raise ValueError(f"Path '{path}' is not a valid directory.")
        
        abs_path = os.path.abspath(path)
        with watch_lock:
            if abs_path not in watched_dirs:
                try:
                    initial_files = set(os.listdir(abs_path))
                    watched_dirs[abs_path] = initial_files
                    new_files_queues[abs_path] = []
                    
                    # Spawn background monitoring thread
                    thread = threading.Thread(target=poll_directory, args=(abs_path,), daemon=True)
                    thread.start()
                    watch_threads[abs_path] = thread
                    return f"Watcher initialized. Monitoring directory '{abs_path}' with {len(initial_files)} initial files."
                except Exception as ex:
                    return f"Error starting watcher: {str(ex)}"
            else:
                # Return queued new files
                new_files = new_files_queues[abs_path]
                new_files_queues[abs_path] = []  # Clear queue
                if new_files:
                    return f"New files detected since last check: {', '.join(new_files)}"
                else:
                    return "No new files detected since last check."
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.tool()
def batch_process(paths: list[str]) -> str:
    """
    Process multiple files in parallel and return a JSON-serialized mapping of path to file contents.
    Enforces security boundary checks on each file path.
    """
    results = {}
    try:
        # Resolve and validate all paths first
        for p in paths:
            check_path_permission(p)
            
        with concurrent.futures.ThreadPoolExecutor() as executor:
            # Map future to filepath
            future_to_path = {executor.submit(read_file, p): p for p in paths}
            for future in concurrent.futures.as_completed(future_to_path):
                p = future_to_path[future]
                try:
                    results[p] = future.result()
                except Exception as exc:
                    results[p] = f"Error in batch execution: {str(exc)}"
    except Exception as e:
        print(f"[Error] batch_process failed: {e}", file=sys.stderr)
        return json.dumps({"error": str(e)})
    return json.dumps(results)

# 7. MCP Resource Endpoints

@mcp.resource("resumes://list")
def list_resumes_resource() -> str:
    """Returns a list of all resumes currently in the resumes folder."""
    res_dir = config["resumes_directory"]
    if not os.path.exists(res_dir):
        return f"Resumes directory not found at: {res_dir}"
    try:
        files = os.listdir(res_dir)
        return json.dumps(files, indent=2)
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.resource("resumes://file/{filename}")
def read_resume_resource(filename: str) -> str:
    """Retrieves the text content of a specific resume file in the resumes directory."""
    res_dir = config["resumes_directory"]
    file_path = os.path.join(res_dir, filename)
    try:
        check_path_permission(file_path)
        if not os.path.exists(file_path):
            return f"Error: Resume file '{filename}' not found."
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".pdf":
            return extract_text_from_pdf(file_path)
        else:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read()
    except Exception as e:
        return f"Error: {str(e)}"

@mcp.resource("reports://latest")
def read_latest_report_resource() -> str:
    """Retrieves the content of the latest evaluation report from the reports directory."""
    rep_dir = config["reports_directory"]
    report_path = os.path.join(rep_dir, "latest_report.md")
    try:
        check_path_permission(report_path)
        if not os.path.exists(report_path):
            return "No evaluation reports generated yet."
        with open(report_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error: {str(e)}"

if __name__ == "__main__":
    # Ensure default folders exist
    os.makedirs(config["resumes_directory"], exist_ok=True)
    os.makedirs(config["reports_directory"], exist_ok=True)
    print("Starting Filesystem MCP Server on stdio...", file=sys.stderr)
    mcp.run(transport="stdio")
