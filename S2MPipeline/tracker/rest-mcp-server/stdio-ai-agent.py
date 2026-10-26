#!/usr/bin/env python3
"""
LangGraph AI Agent using Ollama (qwen3:1.7b) with a RISCOR system prompt,
connected to the Tracker MCP Server over STDIO.
"""

import asyncio
import sys

from langchain.mcp import MCPAdapter
from langchain.agents import create_agent
from langchain_ollama import ChatOllama

# 1. MCP Server connection details (STDIO transport)
MCP_SERVER_SCRIPT = "stdio-mcp-server.py"

# 2. RISCOR System Prompt
SYSTEM_PROMPT = """
Role: You are a Project Tracker Assistant. You manage deliverables, tickets,
and bugs in the Tracker system using the tools available to you.

Steps:
1. Read the user's request. Decide if it is a READ (query) or WRITE
   (create / transition) operation.
2. For READs, first call list_projects or get_project_board to orient.
   For lookups by key (e.g. "MOB-3"), call find_issue_by_key first.
3. Use the appropriate tool. Never guess ids or keys.
4. Summarize the result using issue keys (e.g. MOB-3), never raw ids.

Context:
- The tracker holds Projects → Deliverables → Tickets → Bugs.
- Bugs auto-link as "Blocks" to their source ticket AND the Deliverable.
- A Ticket can only reach Done when all blocking bugs are closed.
- A Deliverable can only be Accepted when all its tickets are Done,
  no blocking bugs remain, and the criteria have been verified.
- Bug lifecycle is three steps: Fixed → Verified → Closed.

Output:
- READ: state keys, statuses, and counts. Be brief.
- WRITE: confirm the new entity's issue key and current status.
- TRANSITION: report the new status, or the exact blocker if refused.

Rules:
- Never fabricate issue keys, ids, statuses, or balances of work.
- Before transitioning a Deliverable to "accept", call get_readiness
  and report why it is or is not ready.
- Do not call seed_demo without explicit instruction.
- Do not expose raw JSON unless the user asks for it.
"""


async def main() -> None:
    config = {
        "mcpServers": {
            "tracker": {
                "command": sys.executable,
                "args": [MCP_SERVER_SCRIPT],
                "env": {"TRACKER_BASE_URL": "http://127.0.0.1:5000"},
            }
        }
    }

    async with MCPAdapter(config) as mcp_client:
        tools = await mcp_client.list_tools()
        print(f"Discovered {len(tools)} tool(s):")
        for tool in tools:
            print(f"  · {tool.name}")
        print()

        model = ChatOllama(
            model="qwen3:1.7b",
            temperature=0,
            base_url="http://localhost:11434",
        )

        agent = create_agent(
            model=model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
        )

        print("Tracker Agent ready. Type 'exit' or 'quit' to stop.\n")

        while True:
            try:
                user_instruction = input("You: ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nGoodbye.")
                break
            if not user_instruction:
                continue
            if user_instruction.lower() in {"exit", "quit"}:
                print("Goodbye.")
                break
            try:
                response = await agent.ainvoke(
                    {"messages": [{"role": "user", "content": user_instruction}]}
                )
                print(f"\nAgent: {response['messages'][-1].content}\n")
            except Exception as exc:
                print(f"\n[Error] {exc}\n")


if __name__ == "__main__":
    asyncio.run(main())