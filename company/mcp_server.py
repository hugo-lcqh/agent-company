"""MCP server exposing the company tools (gates, worktrees, manifest, Postiz) to Claude Code."""

from mcp.server.mcpserver import MCPServer

import tools

server = MCPServer("company-tools")
for tool_function in (
    tools.create_task_worktree,
    tools.cleanup_task_worktree,
    tools.verify_git_scope,
    tools.run_engineering_gate,
    tools.run_marketing_gate,
    tools.validate_handoff_manifest,
    tools.get_postiz_channels,
    tools.upload_postiz_asset,
    tools.publish_to_postiz,
    tools.list_postiz_posts,
    tools.edit_postiz_post,
):
    server.add_tool(tool_function)

if __name__ == "__main__":
    server.run()
