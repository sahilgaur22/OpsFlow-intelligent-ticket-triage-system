from mcp.server.fastmcp import FastMCP

# Initialize FastMCP server
mcp = FastMCP("OpsFlow IT Ops Tools")

# Mock database of KB articles
KB_ARTICLES = {
    "database": "DATABASE ERROR: If database connection is down, check database container status. Try rebooting database-service using the reboot_service tool. If that fails, escalate to Database Administrator.",
    "auth": "AUTH ERROR: For login or auth failures, check auth-service status. Ensure authentication service is UP. Try restarting auth-service. If failed, escalate to Security Team.",
    "disk": "DISK ERROR: For disk space issues, check if /var/log is full. Run logs clean script. If still full, escalate to DevOps Team.",
    "email": "EMAIL ERROR: For email delivery issues, check SMTP gateway status. Try rebooting SMTP relay. If failed, escalate to Network Team."
}

# Mock system status database
SYSTEM_STATUS = {
    "database-service": "DOWN",
    "auth-service": "UP",
    "billing-service": "DEGRADED",
    "smtp-gateway": "UP"
}

@mcp.tool()
def search_kb(query: str) -> str:
    """Search the IT knowledge base articles for troubleshooting steps.

    Args:
        query: The search query (e.g. 'database', 'auth', 'disk', 'email').
    """
    query_lower = query.lower()
    for key, content in KB_ARTICLES.items():
        if key in query_lower:
            return f"Found article for '{key}':\n{content}"
    return f"No articles found matching '{query}'. Available topics: {', '.join(KB_ARTICLES.keys())}."

@mcp.tool()
def get_system_status(service_name: str) -> str:
    """Retrieve the current status of a critical system service.

    Args:
        service_name: The name of the service (e.g. 'database-service', 'auth-service', 'billing-service', 'smtp-gateway').
    """
    name_lower = service_name.lower()
    if name_lower in SYSTEM_STATUS:
        return f"Service '{service_name}' status is currently: {SYSTEM_STATUS[name_lower]}"
    return f"Unknown service '{service_name}'. Available services: {', '.join(SYSTEM_STATUS.keys())}."

@mcp.tool()
def reboot_service(service_name: str) -> str:
    """Reboot a critical system service to attempt auto-recovery.

    Args:
        service_name: The name of the service to reboot (e.g. 'database-service', 'auth-service', 'billing-service', 'smtp-gateway').
    """
    name_lower = service_name.lower()
    if name_lower in SYSTEM_STATUS:
        SYSTEM_STATUS[name_lower] = "UP"  # Restore status to UP
        return f"Service '{service_name}' has been successfully rebooted. Current status: UP."
    return f"Failed to reboot: Unknown service '{service_name}'."

if __name__ == "__main__":
    mcp.run()
