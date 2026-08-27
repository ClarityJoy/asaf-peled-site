"""A stand-in for the LinkedIn MCP server.

Speaks the real protocol over real stdio, so the adapter is exercised through
the same path it uses in production -- only the data is fake. SCENARIO picks
what search_posts does.
"""
import json, os, sys
from mcp.server.mcpserver import MCPServer

SCENARIO = os.environ.get("SCENARIO", "ok")
mcp = MCPServer("fake-linkedin")

POSTS = [
    {"text": "We're hiring a Senior Product Manager for our payments team in Tel Aviv. DM me!",
     "url": "https://www.linkedin.com/posts/dana-cohen_hiring-activity-1",
     "author": "Dana Cohen", "headline": "Talent Partner at Rapyd",
     "company": "Rapyd", "posted_at": "2 days ago"},
    {"text": "מגייסים מנהל מוצר לפינטק! רגולציה ותשלומים. קורות חיים אליי",
     "url": "https://www.linkedin.com/posts/yossi-levi_hiring-activity-2",
     "author": "יוסי לוי", "headline": "VP Product, Pepper",
     "company": "Pepper", "posted_at": "4 days ago"},
]

@mcp.tool()
def search_posts(keywords: str, date_posted: str = None, max_pages: int = 3) -> str:
    if SCENARIO == "auth":
        return "Error: not logged in. No stored session found. Run --login first."
    if SCENARIO == "ratelimit":
        return "429 Too Many Requests - blocked by LinkedIn"
    if SCENARIO == "garbage":
        return "<html><body>something entirely unexpected</body></html>"
    if SCENARIO == "empty":
        return json.dumps({"posts": []})
    return json.dumps({"posts": POSTS})

@mcp.tool()
def send_message(recipient: str, text: str) -> str:      # must never be called
    return "MESSAGE SENT"

if __name__ == "__main__":
    mcp.run(transport="stdio")
