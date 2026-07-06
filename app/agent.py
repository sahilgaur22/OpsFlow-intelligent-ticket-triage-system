# ruff: noqa
import sys
import os
import re
import json
import logging
from typing import Optional, AsyncGenerator
from pydantic import BaseModel, Field

from google.adk.agents import LlmAgent
from google.adk.apps import App
from google.adk.workflow import Workflow, START
from google.adk.tools import AgentTool
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters
from google.adk.events.event import Event
from google.adk.events.request_input import RequestInput
from google.adk.agents.context import Context
from google.genai import types

from app.config import config

# Set up logging for audit trail
logger = logging.getLogger("security_audit")

# --- 1. Define Pydantic Schemas for Input, Output, and State ---

class TicketInput(BaseModel):
    ticket_text: str = Field(description="The raw text of the incoming IT support ticket.")

class DiagnosticsResult(BaseModel):
    service_name: str = Field(description="Name of the service diagnosed.")
    status: str = Field(description="Status of the service (e.g. UP, DOWN, DEGRADED).")
    diagnostic_details: str = Field(description="Detailed diagnostic findings.")
    action_taken: str = Field(description="Action taken to address the issue.")
    resolved: bool = Field(description="Whether the issue has been successfully resolved.")

class EscalationResult(BaseModel):
    urgency: str = Field(description="Urgency tier: LOW, MEDIUM, HIGH, or CRITICAL.")
    reason: str = Field(description="Reason for escalation.")
    escalated_to: str = Field(description="Target escalation team (e.g. Database Administrator, DevOps Team, Security Team).")
    needs_human_approval: bool = Field(description="True if escalation urgency is HIGH or CRITICAL.")

class TriageResult(BaseModel):
    classification: str = Field(description="Classification of the ticket: diagnostics or escalation.")
    urgency: str = Field(description="Urgency of the ticket: LOW, MEDIUM, HIGH, or CRITICAL.")
    summary: str = Field(description="Brief summary of the issue.")
    recommended_action: str = Field(description="Recommended next steps or resolution description.")

class TicketState(BaseModel):
    diagnostics_result: Optional[dict] = None
    escalation_result: Optional[dict] = None
    orchestrator_output: Optional[dict] = None
    escalation_approved: Optional[bool] = None

# --- 2. Initialize MCP Toolset ---

current_dir = os.path.dirname(os.path.abspath(__file__))
mcp_server_path = os.path.join(current_dir, "mcp_server.py")

mcp_toolset = McpToolset(
    connection_params=StdioConnectionParams(
        server_params=StdioServerParameters(
            command=sys.executable,
            args=[mcp_server_path],
        )
    )
)

# --- 3. Define Sub-Agents (Specialists) ---

diagnostics_agent = LlmAgent(
    name="diagnostics_agent",
    model=config.model,
    instruction=(
        "You are the Diagnostics Specialist Agent.\n"
        "Your task is to diagnose the support ticket using your tools (search_kb, get_system_status, reboot_service).\n"
        "1. Check if the ticket relates to database, auth, disk, or email.\n"
        "2. Retrieve the status of the related service.\n"
        "3. Search the knowledge base for recommended fixes.\n"
        "4. If the service is DOWN or DEGRADED and KB recommends a reboot, perform the reboot using reboot_service.\n"
        "5. Provide a summary of the status, diagnosis, and action taken, and indicate whether the issue is resolved."
    ),
    tools=[mcp_toolset],
    output_schema=DiagnosticsResult,
    output_key="diagnostics_result"
)

escalation_agent = LlmAgent(
    name="escalation_agent",
    model=config.model,
    instruction=(
        "You are the Escalation Specialist Agent.\n"
        "Your task is to handle issues that cannot be resolved automatically or require escalation.\n"
        "1. Assess the urgency of the issue based on the impact (e.g., database down is CRITICAL, email delay is LOW).\n"
        "2. Identify the target team to escalate to (e.g. Database Administrator, DevOps Team, Security Team, Network Team).\n"
        "3. Determine if this escalation requires human manager approval (any CRITICAL or HIGH urgency escalation requires manager approval).\n"
        "4. Provide a structured escalation plan."
    ),
    tools=[mcp_toolset],
    output_schema=EscalationResult,
    output_key="escalation_result"
)

# --- 4. Define Orchestrator ---

triage_orchestrator = LlmAgent(
    name="triage_orchestrator",
    model=config.model,
    instruction=(
        "You are the OpsFlow Ticket Triage Orchestrator.\n"
        "You receive IT support tickets. Your goal is to coordinate their resolution.\n"
        "1. Analyze the ticket and call diagnostics_agent to troubleshoot/resolve if appropriate.\n"
        "2. If diagnostics fails, the issue is not auto-resolvable, or needs human escalation, call escalation_agent to determine the escalation plan.\n"
        "3. Return a structured triage result summarizing the classification, urgency, findings, and final recommendation."
    ),
    tools=[
        AgentTool(diagnostics_agent),
        AgentTool(escalation_agent)
    ],
    output_schema=TriageResult,
    output_key="orchestrator_output"
)

# --- 5. Define Workflow Nodes (Functions) ---

def security_checkpoint(ctx: Context, node_input: types.Content | str) -> Event:
    """Security node to check for injection, scrub PII, and log audit trails."""
    if isinstance(node_input, str):
        ticket_text = node_input
    else:
        ticket_text = "".join(part.text for part in node_input.parts if part.text)
    scrubbed = ticket_text

    # PII Scrubbing: detect passwords, private keys, API keys, and credit cards
    credentials_pattern = re.compile(
        r"(?i)(password|passwd|api_key|apikey|secret|private_key|token)\s*[:=]\s*[^\s]+", 
        re.IGNORECASE
    )
    cc_pattern = re.compile(r"\b\d{4}[-\s]?\d{4}[-\s]?\d{4}[-\s]?\d{4}\b")
    
    scrubbed = credentials_pattern.sub(r"\1=[REDACTED]", scrubbed)
    scrubbed = cc_pattern.sub(r"[REDACTED_CC]", scrubbed)
    was_scrubbed = (scrubbed != ticket_text)
    
    # Prompt Injection Detection
    injection_keywords = [
        "ignore previous instructions", 
        "system prompt", 
        "ignore the above", 
        "you are now", 
        "override settings"
    ]
    is_injection = any(kw in ticket_text.lower() for kw in injection_keywords)
    
    if is_injection:
        audit_log = {
            "event": "security_violation",
            "type": "prompt_injection",
            "session_id": ctx.session.id,
            "severity": "CRITICAL",
            "message": "Potential prompt injection detected. Request blocked."
        }
        logger.warning(json.dumps(audit_log))
        print(f"SECURITY AUDIT: {json.dumps(audit_log)}")
        
        return Event(
            output="Error: Security violation. Potential prompt injection detected.",
            route="SECURITY_EVENT"
        )
    
    # Domain Specific Rule: check for highly restricted actions
    restricted_keywords = ["delete production", "wipe db", "drop database"]
    is_restricted = any(kw in ticket_text.lower() for kw in restricted_keywords)
    if is_restricted:
        audit_log = {
            "event": "security_violation",
            "type": "restricted_action",
            "session_id": ctx.session.id,
            "severity": "WARNING",
            "message": "Restricted administrative action requested."
        }
        logger.warning(json.dumps(audit_log))
        print(f"SECURITY AUDIT: {json.dumps(audit_log)}")
        return Event(
            output="Error: Security violation. Requested administrative action is restricted.",
            route="SECURITY_EVENT"
        )
        
    audit_log = {
        "event": "security_check_passed",
        "session_id": ctx.session.id,
        "pii_scrubbed": was_scrubbed,
        "severity": "INFO"
    }
    logger.info(json.dumps(audit_log))
    print(f"SECURITY AUDIT: {json.dumps(audit_log)}")
    
    return Event(output=scrubbed, route="PROCEED")

async def escalation_gate(ctx: Context, node_input: dict):
    """Checks if escalation requires human approval and requests it."""
    escalation_res = ctx.state.get("escalation_result")
    
    if escalation_res and escalation_res.get("needs_human_approval"):
        if not ctx.resume_inputs or "approve_escalation" not in ctx.resume_inputs:
            yield RequestInput(
                interrupt_id="approve_escalation",
                message=f"Human Review Required: Escalation urgency is {escalation_res.get('urgency')}. Approve escalation to {escalation_res.get('escalated_to')}? (yes/no)"
            )
            return
        
        response = ctx.resume_inputs["approve_escalation"].strip().lower()
        if response == "yes":
            yield Event(
                output={"status": "Escalation Approved", "details": escalation_res},
                state={"escalation_approved": True}
            )
        else:
            yield Event(
                output={"status": "Escalation Denied by Manager", "details": escalation_res},
                state={"escalation_approved": False}
            )
    else:
        yield Event(output={"status": "Processed", "details": node_input})

def final_output(ctx: Context, node_input: dict):
    """Produces the final output message for user presentation and API returns."""
    message_lines = []
    
    if "Error: Security violation" in str(node_input):
        message_lines.append(f"❌ **Security Event Triggered**\n{node_input}")
    else:
        escalation_res = ctx.state.get("escalation_result")
        diagnostics_res = ctx.state.get("diagnostics_result")
        
        if escalation_res:
            approved = ctx.state.get("escalation_approved")
            status_str = "✅ Approved" if approved else "❌ Denied" if approved is False else "N/A"
            message_lines.append(f"🚨 **Escalation Summary**")
            message_lines.append(f"- **Urgency**: {escalation_res.get('urgency')}")
            message_lines.append(f"- **Target Team**: {escalation_res.get('escalated_to')}")
            message_lines.append(f"- **Reason**: {escalation_res.get('reason')}")
            message_lines.append(f"- **Manager Approval**: {status_str}")
        elif diagnostics_res:
            message_lines.append(f"🛠️ **Diagnostics & Resolution Summary**")
            message_lines.append(f"- **Service**: {diagnostics_res.get('service_name')}")
            message_lines.append(f"- **Status**: {diagnostics_res.get('status')}")
            message_lines.append(f"- **Action Taken**: {diagnostics_res.get('action_taken')}")
            message_lines.append(f"- **Resolved**: {diagnostics_res.get('resolved')}")
            
        orchestrator_res = ctx.state.get("orchestrator_output")
        if orchestrator_res:
            message_lines.append(f"\n📋 **Orchestrator Summary**")
            message_lines.append(f"- **Classification**: {orchestrator_res.get('classification')}")
            message_lines.append(f"- **Urgency**: {orchestrator_res.get('urgency')}")
            message_lines.append(f"- **Summary**: {orchestrator_res.get('summary')}")
            message_lines.append(f"- **Recommendation**: {orchestrator_res.get('recommended_action')}")
            
    final_text = "\n".join(message_lines)
    
    yield Event(content=types.Content(role='model', parts=[types.Part.from_text(text=final_text)]))
    yield Event(output=node_input)

# --- 6. Construct Workflow Graph ---

root_agent = Workflow(
    name="opsflow_workflow",
    edges=[
        (START, security_checkpoint),
        (security_checkpoint, {
            "PROCEED": triage_orchestrator,
            "SECURITY_EVENT": final_output
        }),
        (triage_orchestrator, escalation_gate),
        (escalation_gate, final_output)
    ],
    state_schema=TicketState
)

app = App(
    name="app",
    root_agent=root_agent
)
