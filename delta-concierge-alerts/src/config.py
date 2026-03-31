"""Configuration constants for the Delta Concierge Alert system."""

import os

DEFAULT_PASSPORT_VALIDITY_MONTHS = 6
VISA_EXPIRY_WARNING_DAYS = 30
DYNAMODB_TABLE_NAME = "ConciergeAlerts"

# --- Layer 2: Bedrock Knowledge Base ---
KNOWLEDGE_BASE_BUCKET = os.environ.get("KNOWLEDGE_BASE_BUCKET", "delta-concierge-kb")

# --- Layer 3: Bedrock Agent ---
BEDROCK_AGENT_ID = os.environ.get("BEDROCK_AGENT_ID", "")
BEDROCK_AGENT_ALIAS_ID = os.environ.get("BEDROCK_AGENT_ALIAS_ID", "")
