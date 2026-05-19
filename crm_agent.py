import anthropic
import json

client = anthropic.Anthropic()

# Define the CRM Adapter Tool (The Bridge)
CRM_ADAPTER_TOOL = {
    "name": "universal_crm_connector",
    "description": (
        "Translates and pushes structural updates to the target CRM "
        "(Salesforce, Bloomerang, Kindful). Use this whenever a donor profile "
        "needs to be created or updated, or an interaction needs logging."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "crm_system": {
                "type": "string",
                "enum": ["salesforce", "bloomerang", "kindful"],
            },
            "action_type": {
                "type": "string",
                "enum": ["create_donor", "log_chat_transcript", "update_donation_intent"],
            },
            "payload": {
                "type": "object",
                "properties": {
                    "first_name": {"type": "string"},
                    "last_name": {"type": "string"},
                    "email": {"type": "string"},
                    "summary_notes": {"type": "string"},
                    "estimated_value": {"type": "number"},
                },
                "required": ["last_name", "summary_notes"],
            },
        },
        "required": ["crm_system", "action_type", "payload"],
    },
}


def _simulate_crm_call(tool_input: dict) -> str:
    """Simulate the CRM API call. Replace with real API calls in production."""
    crm = tool_input["crm_system"]
    action = tool_input["action_type"]
    payload = tool_input["payload"]
    print(f"\n[CRM BRIDGE TRIGGERED] → {crm}.{action}")
    print(json.dumps(payload, indent=2))
    # In production: POST to Salesforce / Bloomerang / Kindful endpoint here
    return f"Success: record written to {crm} via {action}."


def run_chat_agent(
    user_message: str,
    chat_history: list | None = None,
    target_crm: str = "salesforce",
) -> str:
    """
    Run a single turn of the CRM chat agent.

    Returns the agent's final text reply after any CRM tool calls complete.
    """
    system_prompt = (
        f"You are an expert AI Chat Agent representing a premium non-profit organization. "
        f"Your goal is to guide web/SMS users through inquiries, donation verification, or volunteer setup.\n\n"
        f"CRITICAL BEHAVIORS:\n"
        f"1. Grounding: Rely ONLY on verifiable facts. Never hallucinate donor parameters or matching gift rules.\n"
        f"2. CRM Hygiene: You are integrated with {target_crm} via an adapter layer. "
        f"As soon as you collect vital details (name, email, or intent), "
        f"execute the 'universal_crm_connector' tool.\n"
        f"3. Formatting: Use clear, scannable markdown with bullet points for long text."
    )

    messages = (chat_history or []) + [{"role": "user", "content": user_message}]

    # Agentic loop — runs until the agent stops requesting tool calls
    while True:
        with client.messages.stream(
            model="claude-opus-4-7",
            max_tokens=4096,
            # Cache the stable system prompt — saves ~90% on repeated turns
            system=[
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[CRM_ADAPTER_TOOL],
            messages=messages,
        ) as stream:
            response = stream.get_final_message()

        # Collect any tool calls from this response
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]

        if not tool_use_blocks:
            # No tool calls → agent is done; return the text reply
            text = next(
                (b.text for b in response.content if b.type == "text"), ""
            )
            return text

        # Append the assistant's response (including tool_use blocks) to history
        messages.append({"role": "assistant", "content": response.content})

        # Execute each tool and collect results
        tool_results = []
        for block in tool_use_blocks:
            result = _simulate_crm_call(block.input)
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                }
            )

        # Feed results back so the agent can compose its final reply
        messages.append({"role": "user", "content": tool_results})


if __name__ == "__main__":
    reply = run_chat_agent(
        "Hi, I want to update my email to test@example.com. My name is Alex Smith.",
        target_crm="kindful",
    )
    print("\nAgent reply:")
    print(reply)