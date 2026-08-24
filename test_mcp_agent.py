#!/usr/bin/env python3
import os
import sys
import time
import shutil
import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from src.agent.matching_agent import run_matching_workflow, parse_mcp_result

RESUMES_DIR = "./resumes"
REPORTS_DIR = "./reports"

# Job description to match against
JOB_DESCRIPTION = """
Looking for a Senior Python Developer with extensive experience in LangChain, LangGraph, and cloud systems (AWS/GCP).
Required Skills: Python, LangGraph, LangChain, AWS, Docker.
Nice to have: SQL, Javascript.
Responsibilities include developing multi-agent workflows and backend integrations.
"""

def setup_test_environment():
    """Sets up clean test directories and checks resumes directory."""
    print("Checking test folders and persistent resumes...", flush=True)
    if os.path.exists(REPORTS_DIR):
        shutil.rmtree(REPORTS_DIR)
        
    os.makedirs(REPORTS_DIR, exist_ok=True)
    
    # Verify that resumes directory exists and has persistent PDF files
    if not os.path.exists(RESUMES_DIR) or not os.listdir(RESUMES_DIR):
        print(f"Error: The directory '{RESUMES_DIR}' is empty or does not exist. Please ensure persistent PDF resumes are present.", flush=True)
        sys.exit(1)
    
    # Ensure sibling Resume.pdf is also copied to resumes folder if available
    sibling_pdf = "../Backend-Development-AI/03_RAGandEmbeddings/Resume.pdf"
    target_pdf = os.path.join(RESUMES_DIR, "Resume.pdf")
    if os.path.exists(sibling_pdf) and not os.path.exists(target_pdf):
        try:
            shutil.copy(sibling_pdf, target_pdf)
            print(f"Copied sibling PDF resume to {target_pdf} for parsing verification.", flush=True)
        except Exception as e:
            print(f"Warning: Could not copy sibling PDF: {e}", flush=True)

async def run_server_tests():
    """Runs tests directly against MCP server tools."""
    print("\n=== RUNNING DIRECT MCP SERVER TESTS ===", flush=True)
    
    # Run the modular server scripts under mcp_servers/
    fs_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_servers/filesystem_mcp_server.py"]
    )
    
    db_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_servers/db_mcp_server.py"]
    )
    
    async with stdio_client(fs_params) as (fs_read, fs_write), \
               stdio_client(db_params) as (db_read, db_write):
               
        async with ClientSession(fs_read, fs_write) as fs_session, \
                   ClientSession(db_read, db_write) as db_session:
                   
            await fs_session.initialize()
            await db_session.initialize()
            
            # 1. Test List Tools
            print("\n[Test 1] List Tools:", flush=True)
            fs_tools = await fs_session.list_tools()
            for t in fs_tools.tools:
                print(f"  - Tool: {t.name} ({t.description})", flush=True)
                
            db_tools = await db_session.list_tools()
            for t in db_tools.tools:
                print(f"  - Tool: {t.name} ({t.description})", flush=True)
                
            # 2. Test Resource Discovery
            print("\n[Test 2] List Resources:", flush=True)
            fs_resources = await fs_session.list_resources()
            for r in fs_resources.resources:
                print(f"  - Resource: {r.uri} ({r.description})", flush=True)
                
            db_resources = await db_session.list_resources()
            for r in db_resources.resources:
                print(f"  - Resource: {r.uri} ({r.description})", flush=True)
                
            # 3. Test list_directory tool
            print("\n[Test 3] Calling list_directory...", flush=True)
            list_res = await fs_session.call_tool("list_directory", arguments={"path": RESUMES_DIR})
            files = parse_mcp_result(list_res)
            print(f"  Files in {RESUMES_DIR}: {files}", flush=True)
            assert isinstance(files, list), "list_directory should return a list"
            
            # 4. Test read_file tool (PDF File - Alice Smith)
            print("\n[Test 4] Calling read_file for Alice_Smith_Resume.pdf...", flush=True)
            read_res = await fs_session.call_tool("read_file", arguments={"path": os.path.join(RESUMES_DIR, "Alice_Smith_Resume.pdf")})
            content = parse_mcp_result(read_res)
            print(f"  Content snippet: {content[:150].encode('ascii', errors='ignore').decode('ascii')}...", flush=True)
            assert "Alice Smith" in content, "Should read/parse PDF resume successfully"
            
            # 5. Test read_file tool (PDF File - Copied Resume)
            pdf_path = os.path.join(RESUMES_DIR, "Resume.pdf")
            print("\n[Test 5] Calling read_file for Resume.pdf...", flush=True)
            read_pdf_res = await fs_session.call_tool("read_file", arguments={"path": pdf_path})
            pdf_content = parse_mcp_result(read_pdf_res)
            print(f"  PDF content length: {len(pdf_content)} bytes", flush=True)
            print(f"  PDF snippet: {pdf_content[:150].encode('ascii', errors='ignore').decode('ascii')}...", flush=True)
            assert len(pdf_content) > 0 and "[Error]" not in pdf_content, "Should parse PDF file successfully"
            
            # 6. Test batch_process tool (Parallel Processing of PDFs)
            print("\n[Test 6] Calling batch_process on PDFs...", flush=True)
            paths = [os.path.abspath(os.path.join(RESUMES_DIR, f)) for f in os.listdir(RESUMES_DIR) if f.endswith(".pdf")]
            batch_res = await fs_session.call_tool("batch_process", arguments={"paths": paths})
            batch_data = parse_mcp_result(batch_res)
            print(f"  Batch processed keys: {list(batch_data.keys())}", flush=True)
            assert len(batch_data) == len(paths), "Batch processing count mismatch"
            
            # 7. Test watch_directory tool (Directory Polling)
            print("\n[Test 7] Initializing watch_directory...", flush=True)
            watch_init_res = await fs_session.call_tool("watch_directory", arguments={"path": RESUMES_DIR})
            watch_init_msg = parse_mcp_result(watch_init_res)
            print(f"  Watcher init: {watch_init_msg}", flush=True)
            
            # Add a new file to the directory (copying an existing resume to trigger the watcher)
            new_file_path = os.path.join(RESUMES_DIR, "Watcher_Test_Resume.pdf")
            print("  Copying an existing resume to Watcher_Test_Resume.pdf to trigger watcher...", flush=True)
            # Find an existing file to copy
            existing_pdf = [f for f in os.listdir(RESUMES_DIR) if f.endswith(".pdf")][0]
            shutil.copy(os.path.join(RESUMES_DIR, existing_pdf), new_file_path)
                
            # Wait for watcher polling thread
            print("  Waiting 3 seconds for watcher polling thread...", flush=True)
            await asyncio.sleep(3)
            
            # Check for new files
            watch_check_res = await fs_session.call_tool("watch_directory", arguments={"path": RESUMES_DIR})
            watch_check_msg = parse_mcp_result(watch_check_res)
            print(f"  Watcher check: {watch_check_msg}", flush=True)
            
            # Clean up the watcher test file immediately to keep persistent folder clean
            if os.path.exists(new_file_path):
                os.remove(new_file_path)
                
            assert "Watcher_Test_Resume.pdf" in watch_check_msg, "Watcher did not detect newly added PDF file"
            
            # 8. Test DB Server - get_candidate_profile
            print("\n[Test 8] Querying DB MCP server for Alice Smith...", flush=True)
            db_res = await db_session.call_tool("get_candidate_profile", arguments={"name": "Alice Smith"})
            alice_profile = parse_mcp_result(db_res)
            print(f"  Alice Profile: {alice_profile}", flush=True)
            assert "expected_salary" in alice_profile, "Profile search response format mismatch"
            
            # 9. Test Security Sandbox Boundaries
            print("\n[Test 9] Verifying Security Sandbox Boundaries...", flush=True)
            illegal_path = "../../" # Outside workspace
            sandbox_res = await fs_session.call_tool("list_directory", arguments={"path": illegal_path})
            sandbox_msg = parse_mcp_result(sandbox_res)
            print(f"  Access Attempt Result: {sandbox_msg}", flush=True)
            assert "Access Denied" in str(sandbox_msg) or "PermissionError" in str(sandbox_msg) or "Error" in str(sandbox_msg), "Security boundary check failed"
            
            print("\nAll individual MCP server tests passed successfully!", flush=True)

async def test_agent_workflow():
    """Runs the LangGraph agent matching workflow end-to-end."""
    print("\n=== RUNNING LANGGRAPH MATCHING AGENT WORKFLOW ===", flush=True)
    
    # Run matching workflow
    report_content = await run_matching_workflow(JOB_DESCRIPTION, RESUMES_DIR)
    
    print("\n=== VERIFYING AGENT PIPELINE OUTPUTS ===", flush=True)
    report_file = os.path.join(REPORTS_DIR, "latest_report.md")
    assert os.path.exists(report_file), "Agent did not generate latest_report.md"
    
    with open(report_file, "r", encoding="utf-8") as f:
        saved_content = f.read()
        
    print(f"Saved Report file exists: {report_file} ({len(saved_content)} characters)", flush=True)
    
    assert "Alice Smith" in saved_content, "Alice Smith should be in the report"
    assert "Bob Jones" in saved_content, "Bob Jones should be in the report"
    assert "Charlie Brown" in saved_content, "Charlie Brown should be in the report"
    assert "Rank" in saved_content, "Ranking table format missing"
    
    print("\nLangGraph agent pipeline workflow completed and verified successfully!", flush=True)

async def main():
    setup_test_environment()
    await run_server_tests()
    await test_agent_workflow()
    print("\nAll integration test scenarios passed successfully!", flush=True)

if __name__ == "__main__":
    asyncio.run(main())
