#!/usr/bin/env python3
import os
import sys
import re
import json
import time
import asyncio
import argparse
from typing import TypedDict, List, Dict, Any
from dotenv import load_dotenv

# Import LangGraph components
from langgraph.graph import StateGraph, START, END

# Import MCP Client components
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

# Load environment variables
load_dotenv()

# =====================================================================
# 1. State Definition
# =====================================================================
class AgentState(TypedDict):
    job_description: str
    resumes_dir: str
    resume_paths: List[str]
    resume_contents: Dict[str, str]            # path -> text
    candidate_profiles: Dict[str, Dict[str, Any]] # path -> HR database profile
    match_reports: List[Dict[str, Any]]        # list of match evaluation dicts
    final_report: str

# Global references to active sessions, ensuring clean compatibility with LangGraph node calls
GLOBAL_FS_SESSION = None
GLOBAL_DB_SESSION = None

# =====================================================================
# 2. Helper Functions
# =====================================================================
def parse_mcp_result(result) -> Any:
    """Safely extracts and parses JSON/text from an MCP tool CallToolResult."""
    if not hasattr(result, "content") or not result.content:
        return None
    text = result.content[0].text
    try:
        return json.loads(text)
    except Exception:
        return text

def extract_json_from_text(text: str) -> dict:
    """Extracts JSON object from text (handling markdown wrappers)."""
    match = re.search(r"({.*?})", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except Exception:
            pass
    try:
        return json.loads(text)
    except Exception:
        pass
    
    return {
        "score": 50,
        "match_level": "Medium",
        "matched_skills": [],
        "missing_skills": [],
        "rationale": "Failed to parse LLM response. Raw response: " + text[:200]
    }

# =====================================================================
# 3. LLM Matching Logic
# =====================================================================
def run_mock_llm_matching(resume_text: str, jd_text: str, profile_db_metadata: str) -> dict:
    """Fallback keyword-based mock matching in case no API keys are available."""
    jd_lower = jd_text.lower()
    resume_lower = resume_text.lower()
    
    tech_keywords = [
        "python", "javascript", "react", "aws", "docker", "kubernetes", 
        "sql", "java", "machine learning", "data scientist", "scrum master", 
        "cloud", "c++", "angular", "node.js", "django", "fastapi"
    ]
    
    matched = []
    missing = []
    
    for kw in tech_keywords:
        if kw in jd_lower:
            if kw in resume_lower:
                matched.append(kw.capitalize())
            else:
                missing.append(kw.capitalize())
                
    db_certs = []
    if profile_db_metadata:
        try:
            meta = json.loads(profile_db_metadata)
            db_certs = meta.get("certifications", [])
            matched.extend(db_certs)
        except Exception:
            pass
            
    total_jd = len(matched) + len(missing)
    if total_jd > 0:
        score = int((len(matched) / total_jd) * 100)
    else:
        score = 65
        
    if score >= 80:
        level = "High"
    elif score >= 50:
        level = "Medium"
    else:
        level = "Low"
        
    return {
        "score": score,
        "match_level": level,
        "matched_skills": list(set(matched)),
        "missing_skills": list(set(missing)),
        "rationale": f"Mock Matcher: Found {len(matched)} matching skills out of {total_jd} target keywords. Evaluated with database certifications: {db_certs}."
    }

def run_real_llm_matching(resume_text: str, jd_text: str, profile_db_metadata: str) -> dict:
    """Performs real LLM evaluation using OpenRouter, OpenAI, or Cohere."""
    from openai import OpenAI
    
    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    openai_key = os.getenv("OPENAI_API_KEY")
    cohere_key = os.getenv("COHERE_API_KEY")
    
    prompt = f"""
You are an expert HR recruitment agent. Analyze the following candidate resume and match it against the Job Description.
Also consider the Candidate HR Database Profile metadata (background checks, certifications, expected salary) if available.

### Job Description:
{jd_text}

### Candidate Resume:
{resume_text}

### HR Database Profile Metadata:
{profile_db_metadata}

Provide your evaluation in JSON format with the following keys:
- score: an integer between 0 and 100 representing the overall match score.
- match_level: "High", "Medium", or "Low".
- matched_skills: a list of skills from the resume that match the job description.
- missing_skills: a list of skills from the job description that are missing from the resume.
- rationale: a 2-3 sentence explanation of your scoring.

Output ONLY valid JSON. Do not write anything else.
"""

    if openrouter_key:
        client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=openrouter_key
        )
        model = os.getenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    elif openai_key:
        client = OpenAI(api_key=openai_key)
        model = "gpt-4o-mini"
    elif cohere_key:
        from cohere import ClientV2
        co = ClientV2(api_key=cohere_key)
        response = co.chat(
            model="command-r-plus",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        return extract_json_from_text(response.message.content[0].text)
    else:
        raise ValueError("No LLM key configured")
        
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2
    )
    return extract_json_from_text(response.choices[0].message.content)

# =====================================================================
# 4. LangGraph Nodes
# =====================================================================
async def read_resumes_node(state: AgentState) -> AgentState:
    """Discovers resumes in directory and parses them in parallel using MCP server."""
    fs_session = GLOBAL_FS_SESSION
    resumes_dir = state["resumes_dir"]
    
    print(f"[Node: Read Resumes] Discovering files in: {resumes_dir}", file=sys.stderr)
    
    result = await fs_session.call_tool("list_directory", arguments={"path": resumes_dir})
    filenames = parse_mcp_result(result)
    
    if not isinstance(filenames, list):
        print(f"[Node: Read Resumes] Error listing files: {filenames}", file=sys.stderr)
        state["resume_paths"] = []
        state["resume_contents"] = {}
        return state
        
    supported_files = [f for f in filenames if f.lower().endswith((".txt", ".pdf"))]
    abs_paths = [os.path.abspath(os.path.join(resumes_dir, f)) for f in supported_files]
    state["resume_paths"] = abs_paths
    
    if not abs_paths:
        print("[Node: Read Resumes] No supported files found (.txt or .pdf)", file=sys.stderr)
        state["resume_contents"] = {}
        return state
        
    print(f"[Node: Read Resumes] Reading {len(abs_paths)} files in parallel...", file=sys.stderr)
    batch_result = await fs_session.call_tool("batch_process", arguments={"paths": abs_paths})
    contents = parse_mcp_result(batch_result)
    
    if not isinstance(contents, dict):
        contents = {}
        
    state["resume_contents"] = contents
    return state

async def fetch_profiles_node(state: AgentState) -> AgentState:
    """Queries secondary Database MCP Server to get candidate database profiles (Multi-MCP)."""
    db_session = GLOBAL_DB_SESSION
    profiles = {}
    
    print("[Node: Fetch DB Profiles] Querying Database MCP server...", file=sys.stderr)
    
    for path in state["resume_paths"]:
        filename = os.path.basename(path)
        
        name_part = os.path.splitext(filename)[0]
        for term in ["_resume", "_cv", "-resume", "-cv", "resume", "cv"]:
            name_part = re.sub(term, "", name_part, flags=re.IGNORECASE)
        candidate_name = name_part.replace("_", " ").replace("-", " ").strip()
        
        result = await db_session.call_tool("get_candidate_profile", arguments={"name": candidate_name})
        profile_data = parse_mcp_result(result)
        
        if isinstance(profile_data, dict):
            profiles[path] = profile_data
        elif isinstance(profile_data, str) and profile_data.strip().startswith("{"):
            try:
                profiles[path] = json.loads(profile_data)
            except Exception:
                profiles[path] = {"error": profile_data}
        else:
            profiles[path] = {"error": profile_data or "Profile not found"}
            
    state["candidate_profiles"] = profiles
    return state

async def match_resumes_node(state: AgentState) -> AgentState:
    """Matches resume content against job description using Real or Mock LLM."""
    jd = state["job_description"]
    reports = []
    
    print("[Node: Match Resumes] Running candidate evaluation...", file=sys.stderr)
    
    for path, content in state["resume_contents"].items():
        filename = os.path.basename(path)
        profile_info = state["candidate_profiles"].get(path, {})
        profile_str = json.dumps(profile_info, indent=2)
        
        print(f" -> Processing {filename}...", file=sys.stderr)
        
        has_keys = os.getenv("OPENROUTER_API_KEY") or os.getenv("OPENAI_API_KEY") or os.getenv("COHERE_API_KEY")
        if has_keys:
            try:
                eval_res = run_real_llm_matching(content, jd, profile_str)
            except Exception as e:
                print(f"    [LLM Error] Real LLM matching failed: {e}. Falling back to keyword mock.", file=sys.stderr)
                eval_res = run_mock_llm_matching(content, jd, profile_str)
        else:
            eval_res = run_mock_llm_matching(content, jd, profile_str)
            
        eval_res["file_path"] = path
        eval_res["filename"] = filename
        eval_res["candidate_name"] = profile_info.get("name", filename.replace("_", " ").split(".")[0].title())
        reports.append(eval_res)
        
    state["match_reports"] = reports
    return state

async def generate_report_node(state: AgentState) -> AgentState:
    """Ranks candidates, compiles a Markdown report, and writes it to disk via MCP server."""
    fs_session = GLOBAL_FS_SESSION
    reports = state["match_reports"]
    
    print("[Node: Generate Report] Ranking candidates and compiling report...", file=sys.stderr)
    
    sorted_reports = sorted(reports, key=lambda x: x.get("score", 0), reverse=True)
    
    md = []
    md.append("# Candidate Matching & Ranking Report")
    md.append(f"**Generated At:** {time.strftime('%Y-%m-%d %H:%M:%S')}")
    md.append("\n## Job Description Summary")
    md.append(f"> {state['job_description'][:300]}...\n")
    
    md.append("## Executive Candidate Summary")
    md.append("| Rank | Candidate Name | Score | Fit Level | Matched Skills | Missing Skills |")
    md.append("| :--- | :--- | :---: | :---: | :--- | :--- |")
    
    for rank, r in enumerate(sorted_reports, 1):
        matched = ", ".join(r.get("matched_skills", [])) or "None"
        missing = ", ".join(r.get("missing_skills", [])) or "None"
        md.append(f"| {rank} | {r.get('candidate_name')} | **{r.get('score')}**/100 | {r.get('match_level')} | {matched} | {missing} |")
        
    md.append("\n## Detailed Evaluations")
    for rank, r in enumerate(sorted_reports, 1):
        md.append(f"### {rank}. {r.get('candidate_name')}")
        md.append(f"- **Resume File:** `{r.get('filename')}`")
        md.append(f"- **Overall Match Score:** **{r.get('score')}**/100")
        md.append(f"- **Fit Class:** {r.get('match_level')}")
        md.append(f"- **Matched Skills:** {', '.join(r.get('matched_skills', [])) or 'None'}")
        md.append(f"- **Missing Skills:** {', '.join(r.get('missing_skills', [])) or 'None'}")
        md.append(f"- **Recruiter Evaluation:** {r.get('rationale')}")
        
        profile = state["candidate_profiles"].get(r.get("file_path"), {})
        if profile and "error" not in profile:
            md.append(f"- **HR Database Integration:**")
            md.append(f"  - *Expected Salary:* {profile.get('expected_salary', 'N/A')}")
            md.append(f"  - *Background Check:* {profile.get('background_check', 'N/A')}")
            md.append(f"  - *Certifications:* {', '.join(profile.get('certifications', [])) or 'None'}")
            md.append(f"  - *DB Notes:* *{profile.get('notes', 'N/A')}*")
        md.append("")
        
    report_content = "\n".join(md)
    state["final_report"] = report_content
    
    report_path = os.path.abspath("./reports/latest_report.md")
    write_result = await fs_session.call_tool(
        "write_file", 
        arguments={"path": report_path, "content": report_content}
    )
    print(f"[Node: Generate Report] Saved report to {report_path}: {parse_mcp_result(write_result)}", file=sys.stderr)
    return state

# =====================================================================
# 5. LangGraph Assembly
# =====================================================================
def build_agent_graph():
    builder = StateGraph(AgentState)
    builder.add_node("read_resumes", read_resumes_node)
    builder.add_node("fetch_profiles", fetch_profiles_node)
    builder.add_node("match_resumes", match_resumes_node)
    builder.add_node("generate_report", generate_report_node)
    
    builder.add_edge(START, "read_resumes")
    builder.add_edge("read_resumes", "fetch_profiles")
    builder.add_edge("fetch_profiles", "match_resumes")
    builder.add_edge("match_resumes", "generate_report")
    builder.add_edge("generate_report", END)
    
    return builder.compile()

# =====================================================================
# 6. Main Execution Entrypoint
# =====================================================================
async def run_matching_workflow(jd: str, resumes_dir: str):
    """Launches the MCP servers, establishes stdio sessions, and executes the LangGraph."""
    agent = build_agent_graph()
    
    # Stdio parameters pointing to modular script locations under mcp_servers/
    fs_server_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_servers/filesystem_mcp_server.py"]
    )
    
    db_server_params = StdioServerParameters(
        command=sys.executable,
        args=["mcp_servers/db_mcp_server.py"]
    )
    
    print("[Agent] Starting Filesystem and Database MCP servers...", file=sys.stderr)
    
    async with stdio_client(fs_server_params) as (fs_read, fs_write), \
               stdio_client(db_server_params) as (db_read, db_write):
               
        print("[Agent] Handshaking and initializing sessions...", file=sys.stderr)
        
        async with ClientSession(fs_read, fs_write) as fs_session, \
                   ClientSession(db_read, db_write) as db_session:
                   
            await fs_session.initialize()
            await db_session.initialize()
            
            print("[Agent] Sessions initialized successfully.", file=sys.stderr)
            
            global GLOBAL_FS_SESSION, GLOBAL_DB_SESSION
            GLOBAL_FS_SESSION = fs_session
            GLOBAL_DB_SESSION = db_session
            
            initial_state = {
                "job_description": jd,
                "resumes_dir": os.path.abspath(resumes_dir),
                "resume_paths": [],
                "resume_contents": {},
                "candidate_profiles": {},
                "match_reports": [],
                "final_report": ""
            }
            
            print("[Agent] Starting workflow graph...", file=sys.stderr)
            final_state = await agent.ainvoke(initial_state)
            print("[Agent] Workflow graph completed.", file=sys.stderr)
            
            return final_state["final_report"]

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LangGraph Resume Matching Agent using MCP Clients")
    parser.add_argument("--jd", type=str, default="Senior Python Developer with LangChain, LangGraph, and cloud experience.", help="Job Description string or filepath")
    parser.add_argument("--resumes", type=str, default="./resumes", help="Directory containing resumes")
    args = parser.parse_args()
    
    jd_content = args.jd
    if os.path.exists(args.jd):
        with open(args.jd, "r", encoding="utf-8") as f:
            jd_content = f.read()
            
    loop = asyncio.get_event_loop()
    report = loop.run_until_complete(run_matching_workflow(jd_content, args.resumes))
    
    print("\n================== GENERATED REPORT ==================")
    print(report)
    print("======================================================")
