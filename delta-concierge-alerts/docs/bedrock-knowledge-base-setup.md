# Bedrock Knowledge Base Setup — Country Requirements RAG

This document describes how to set up an Amazon Bedrock Knowledge Base backed by S3 to enable retrieval-augmented generation (RAG) for country-specific travel document requirements.

## Architecture

```
EventBridge (daily) → sync_knowledge_base Lambda → S3 bucket
                                                       ↓
                                            Bedrock Knowledge Base
                                            (OpenSearch Serverless)
                                                       ↓
                                              Bedrock Agent (RAG)
```

## Step 1: Create the S3 Bucket

```bash
aws s3 mb s3://delta-concierge-kb --region us-east-1
```

Enable versioning (recommended for Knowledge Base data sources):

```bash
aws s3api put-bucket-versioning \
  --bucket delta-concierge-kb \
  --versioning-configuration Status=Enabled
```

## Step 2: Deploy the Sync Lambda

The sync Lambda is located at `src/handlers/sync_knowledge_base.py`. It reads from `COUNTRY_REQUIREMENTS` and uploads a JSON document per country to the S3 bucket.

### Environment Variables

| Variable               | Value                    |
|------------------------|--------------------------|
| `KNOWLEDGE_BASE_BUCKET`| `delta-concierge-kb`     |

### IAM Policy (minimum)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:PutObject"],
      "Resource": "arn:aws:s3:::delta-concierge-kb/country-requirements/*"
    }
  ]
}
```

## Step 3: Create the Bedrock Knowledge Base

### Via AWS Console

1. Navigate to **Amazon Bedrock → Knowledge Bases → Create Knowledge Base**.
2. Name: `DeltaConciergeCountryRequirements`
3. Data source: select **Amazon S3** and point to `s3://delta-concierge-kb/country-requirements/`.
4. Embeddings model: choose **Amazon Titan Embeddings V2** (or Cohere Embed).
5. Vector store: select **Quick create a new vector store** — Bedrock will automatically provision an OpenSearch Serverless collection.
6. Click **Create Knowledge Base** and then **Sync** to perform the initial indexing.

### Via AWS CLI / SDK

```bash
# 1. Create the Knowledge Base
aws bedrock-agent create-knowledge-base \
  --name DeltaConciergeCountryRequirements \
  --role-arn arn:aws:iam::ACCOUNT_ID:role/BedrockKBRole \
  --knowledge-base-configuration '{
    "type": "VECTOR",
    "vectorKnowledgeBaseConfiguration": {
      "embeddingModelArn": "arn:aws:bedrock:us-east-1::foundation-model/amazon.titan-embed-text-v2:0"
    }
  }' \
  --storage-configuration '{
    "type": "OPENSEARCH_SERVERLESS",
    "opensearchServerlessConfiguration": {
      "collectionArn": "arn:aws:aoss:us-east-1:ACCOUNT_ID:collection/COLLECTION_ID",
      "fieldMapping": {
        "metadataField": "metadata",
        "textField": "text",
        "vectorField": "vector"
      },
      "vectorIndexName": "country-requirements-index"
    }
  }'

# 2. Create an S3 data source
aws bedrock-agent create-data-source \
  --knowledge-base-id KB_ID \
  --name CountryRequirementsS3 \
  --data-source-configuration '{
    "type": "S3",
    "s3Configuration": {
      "bucketArn": "arn:aws:s3:::delta-concierge-kb",
      "inclusionPrefixes": ["country-requirements/"]
    }
  }'

# 3. Start an ingestion job
aws bedrock-agent start-ingestion-job --knowledge-base-id KB_ID --data-source-id DS_ID
```

## Step 4: OpenSearch Serverless Collection

When you create the Knowledge Base through the console with the **Quick create** option, Bedrock automatically provisions an OpenSearch Serverless collection with:

- An **encryption policy** (AWS-owned key)
- A **network policy** (public access from Bedrock service)
- A **data access policy** granting the KB execution role index/read/write permissions

If you need to create the collection manually:

```bash
aws opensearchserverless create-collection \
  --name delta-concierge-kb \
  --type VECTORSEARCH
```

Then create the required security policies (encryption, network, data access) per [AWS documentation](https://docs.aws.amazon.com/opensearch-service/latest/developerguide/serverless-manage.html).

## Step 5: Schedule the Sync Lambda

Use EventBridge to run the sync Lambda daily to keep the Knowledge Base fresh as country requirements are updated.

```bash
aws events put-rule \
  --name DeltaConciergeKBSync \
  --schedule-expression "rate(1 day)" \
  --state ENABLED

aws events put-targets \
  --rule DeltaConciergeKBSync \
  --targets "Id"="SyncKBTarget","Arn"="arn:aws:lambda:us-east-1:ACCOUNT_ID:function:sync-knowledge-base"
```

Grant EventBridge permission to invoke the Lambda:

```bash
aws lambda add-permission \
  --function-name sync-knowledge-base \
  --statement-id EventBridgeInvoke \
  --action lambda:InvokeFunction \
  --principal events.amazonaws.com \
  --source-arn arn:aws:events:us-east-1:ACCOUNT_ID:rule/DeltaConciergeKBSync
```

## Verifying the Setup

After the first sync and ingestion:

```bash
# Check S3 documents
aws s3 ls s3://delta-concierge-kb/country-requirements/

# Check Knowledge Base status
aws bedrock-agent get-knowledge-base --knowledge-base-id KB_ID

# Test retrieval
aws bedrock-agent-runtime retrieve \
  --knowledge-base-id KB_ID \
  --retrieval-query '{"text": "What are the visa requirements for traveling to Japan?"}'
```
