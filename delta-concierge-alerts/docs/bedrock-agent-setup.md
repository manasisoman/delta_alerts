# Bedrock Agent Setup — Delta Concierge

This document describes how to create and configure an Amazon Bedrock Agent that acts as the conversational front-end for the Delta Concierge Alert system.

## Architecture

```
User (Mobile App / Connect) → API Gateway → Bedrock Agent
                                                  ├── Knowledge Base (country requirements RAG)
                                                  └── Action Group (alert lifecycle + evaluation)
                                                        └── bedrock_action_handler Lambda
                                                              ├── alert_store (DynamoDB)
                                                              └── lambda_handler (evaluation pipeline)
```

## Step 1: Create the Bedrock Agent

### Via Console

1. Navigate to **Amazon Bedrock → Agents → Create Agent**.
2. Agent name: `DeltaConciergeAgent`
3. Foundation model: **Anthropic Claude 3 Sonnet** (or Claude 3 Haiku for lower latency).
4. Agent instruction (system prompt):

```
You are Delta Concierge, a helpful travel document assistant for Delta Air Lines SkyMiles members.

Your responsibilities:
1. ALWAYS check for active alerts first when a member asks about their travel status.
2. Use the Knowledge Base to answer questions about country-specific travel document requirements (visa rules, passport validity, embassy contacts, e-visa portals).
3. For each alert, suggest concrete next steps:
   - CRITICAL passport alerts: "Visit travel.state.gov to expedite your passport renewal (estimated 5-7 weeks for expedited service)."
   - CRITICAL visa alerts: Provide the country's e-visa portal URL or embassy URL from the knowledge base.
   - WARNING alerts: Explain the specific requirement and timeline.
   - INFO alerts: Note the upcoming expiration and recommend planning ahead.
4. When a member confirms they have taken action, use the resolveAlert action to mark the alert as resolved with a description of what was done.
5. Be professional, concise, and proactive. Always reference specific flight numbers and dates from the alert data.
6. If you are unsure about a country's requirements, say so and recommend the member check the embassy website directly.
```

5. Click **Create**.

### Via AWS CLI

```bash
aws bedrock-agent create-agent \
  --agent-name DeltaConciergeAgent \
  --foundation-model "anthropic.claude-3-sonnet-20240229-v1:0" \
  --instruction "You are Delta Concierge, a helpful travel document assistant..." \
  --agent-resource-role-arn arn:aws:iam::ACCOUNT_ID:role/BedrockAgentRole
```

## Step 2: Configure the Action Group

1. In the Agent details page, click **Add Action Group**.
2. Action group name: `DeltaConciergeActions`
3. Action group type: **Define with API schemas**
4. Upload or paste the OpenAPI schema from `docs/action-group-openapi.yaml`.
5. Lambda function: select the deployed `bedrock_action_handler` Lambda.
6. Click **Save**.

### Lambda Permissions

The Bedrock Agent service needs permission to invoke the Lambda:

```bash
aws lambda add-permission \
  --function-name bedrock-action-handler \
  --statement-id BedrockAgentInvoke \
  --action lambda:InvokeFunction \
  --principal bedrock.amazonaws.com \
  --source-arn arn:aws:bedrock:us-east-1:ACCOUNT_ID:agent/AGENT_ID
```

## Step 3: Associate the Knowledge Base

1. In the Agent details page, click **Add Knowledge Base**.
2. Select the `DeltaConciergeCountryRequirements` Knowledge Base (see `bedrock-knowledge-base-setup.md`).
3. Knowledge Base instruction:

```
Use this knowledge base to answer questions about country-specific travel document requirements, including visa requirements, passport validity rules, visa-exempt nationalities, embassy URLs, e-visa portals, and estimated processing times. Always cite the specific country when providing information.
```

4. Click **Save**.

## Step 4: Create an Agent Alias

An alias is required for invoking the agent at runtime.

```bash
aws bedrock-agent create-agent-alias \
  --agent-id AGENT_ID \
  --agent-alias-name "prod"
```

Store the returned `agentAliasId` in the `BEDROCK_AGENT_ALIAS_ID` environment variable.

## Step 5: Expose via API Gateway

### Create a REST API

```bash
# Create API
aws apigateway create-rest-api \
  --name DeltaConciergeAPI \
  --description "API Gateway for Delta Concierge Bedrock Agent"

# Create /chat resource
aws apigateway create-resource \
  --rest-api-id API_ID \
  --parent-id ROOT_RESOURCE_ID \
  --path-part chat

# Create POST method with Lambda proxy integration
# The integration Lambda should:
#   1. Parse the user message from the request body
#   2. Call bedrock-agent-runtime invoke_agent()
#   3. Stream or return the agent's response
```

### Integration Lambda (thin proxy)

```python
import boto3
import json
import os

bedrock_agent_runtime = boto3.client("bedrock-agent-runtime")

AGENT_ID = os.environ["BEDROCK_AGENT_ID"]
AGENT_ALIAS_ID = os.environ["BEDROCK_AGENT_ALIAS_ID"]

def handler(event, context):
    body = json.loads(event["body"])
    session_id = body.get("session_id", "default")
    user_message = body["message"]

    response = bedrock_agent_runtime.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=session_id,
        inputText=user_message,
    )

    # Collect streamed response
    completion = ""
    for event_chunk in response["completion"]:
        if "chunk" in event_chunk:
            completion += event_chunk["chunk"]["bytes"].decode("utf-8")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "response": completion,
            "session_id": session_id,
        }),
    }
```

## Step 6 (Optional): Amazon Connect Integration

For voice channel support:

1. Create an Amazon Connect instance.
2. Create a contact flow that:
   - Greets the caller
   - Captures their SkyMiles number via DTMF or speech
   - Invokes a Lambda to call the Bedrock Agent with the transcribed speech
   - Plays back the agent's response using Amazon Polly
3. Associate the Lambda with the contact flow's "Invoke AWS Lambda" block.

## Environment Variables Summary

| Variable                  | Description                          |
|---------------------------|--------------------------------------|
| `BEDROCK_AGENT_ID`        | The Bedrock Agent ID                 |
| `BEDROCK_AGENT_ALIAS_ID`  | The Agent Alias ID (e.g., `prod`)    |
| `KNOWLEDGE_BASE_BUCKET`   | S3 bucket for KB documents           |
| `DYNAMODB_TABLE_NAME`     | DynamoDB table for alerts            |

## Testing the Agent

### Via Console

Use the **Test** panel in the Bedrock Agent console to interact with the agent.

### Via CLI

```bash
aws bedrock-agent-runtime invoke-agent \
  --agent-id AGENT_ID \
  --agent-alias-id ALIAS_ID \
  --session-id "test-session-1" \
  --input-text "I'm SkyMiles member 1234567890. Do I have any travel alerts?"
```

### Example Conversation

```
User: I'm SkyMiles member 1234567890. Do I have any travel alerts?
Agent: I found 2 active alerts for your upcoming trip (DL-ABC123):

1. **CRITICAL — Visa Alert**: No visa on file for China (CN) for flight DL200 on June 20, 2026.
   → Apply for a Chinese tourist visa at your nearest embassy. Processing typically takes 4-7 business days.

2. **INFO — Passport Alert**: Your passport expires within 6 months of travel on flight DL100 to Germany.
   → Consider renewing your passport before your trip. Expedited renewal takes 5-7 weeks at travel.state.gov.

Would you like me to mark either of these as acknowledged, or have you already taken action on any?
```
