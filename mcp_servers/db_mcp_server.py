#!/usr/bin/env python3
import json
import sys
from mcp.server.fastmcp import FastMCP

# Initialize FastMCP Server
mcp = FastMCP("Database-MCP-Server")

# In-memory candidate profiles
CANDIDATE_DB = {
    "Alice Smith": {
        "experience": "5 years as Senior Software Engineer",
        "background_check": "Passed",
        "expected_salary": "$120,000",
        "previous_company": "TechCorp",
        "certifications": ["AWS Solutions Architect", "Certified Scrum Master"],
        "notes": "Highly recommended for cloud native roles."
    },
    "Bob Jones": {
        "experience": "8 years as Data Scientist / Machine Learning Engineer",
        "background_check": "Passed",
        "expected_salary": "$140,000",
        "previous_company": "AI Labs",
        "certifications": ["TensorFlow Developer"],
        "notes": "Strong background in NLP and deep learning."
    },
    "Charlie Brown": {
        "experience": "2 years as Junior Frontend Developer",
        "background_check": "Passed",
        "expected_salary": "$80,000",
        "previous_company": "DesignStudio",
        "certifications": [],
        "notes": "Familiar with React and Vue."
    },
    "John Doe": {
        "experience": "7 years as Senior Python Developer",
        "background_check": "Passed",
        "expected_salary": "$130,000",
        "previous_company": "CloudTech",
        "certifications": ["AWS Certified Developer", "LangChain Certified Developer"],
        "notes": "Excellent match for agentic workflows and LangGraph pipelines."
    }
}

@mcp.tool()
def get_candidate_profile(name: str) -> str:
    """
    Retrieve detailed candidate profile metadata from the database.
    Includes verification data, salary expectations, notes, and certifications.
    """
    for db_name, profile in CANDIDATE_DB.items():
        if name.lower() in db_name.lower():
            return json.dumps({"name": db_name, **profile}, indent=2)
    return f"Candidate '{name}' not found in the HR database."

@mcp.resource("db://candidates")
def list_db_candidates() -> str:
    """List all candidate names available in the HR database."""
    return json.dumps(list(CANDIDATE_DB.keys()), indent=2)

if __name__ == "__main__":
    transport = "stdio"
    port = 8002
    
    if len(sys.argv) > 1:
        if sys.argv[1] == "sse":
            transport = "sse"
            if len(sys.argv) > 2:
                try:
                    port = int(sys.argv[2])
                except ValueError:
                    pass
                    
    if transport == "sse":
        print(f"Starting DB MCP Server on SSE (port {port})...", file=sys.stderr)
        mcp.settings.port = port
        mcp.run(transport="sse")
    else:
        print("Starting DB MCP Server on stdio...", file=sys.stderr)
        mcp.run(transport="stdio")
