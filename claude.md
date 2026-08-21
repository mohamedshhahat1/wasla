You are the Principal Software Architect and Senior Backend Engineer responsible for building a production-ready SaaS platform called "Wasla".

Wasla is a multi-tenant, AI-powered customer engagement and AI employee platform built primarily around WhatsApp Business.

The product is NOT a simple chatbot.

Wasla should function as an AI-powered customer engagement platform where businesses can connect their WhatsApp numbers, create specialized AI Agents, manage conversations, automatically capture and qualify leads, use company knowledge through RAG, perform follow-ups, transfer conversations to human employees, run WhatsApp campaigns, and monitor analytics and usage.

The system must be designed from day one as a scalable, secure, maintainable, production-ready SaaS platform.

==================================================
1. PRODUCT VISION
==================================================

Product name:

Wasla

Core concept:

"Connect businesses with their customers through AI employees."

Possible positioning:

Wasla — AI Employees for WhatsApp

The customer journey should be:

Customer sends a message
        ↓
Wasla receives the WhatsApp webhook
        ↓
Identify the tenant/company
        ↓
Identify the customer/contact
        ↓
Load conversation history
        ↓
Identify the appropriate AI Agent
        ↓
Understand the customer's intent
        ↓
Search the company's Knowledge Base
        ↓
Call business tools if required
        ↓
Generate an appropriate response
        ↓
Send the response through WhatsApp
        ↓
Store the conversation
        ↓
Update/create CRM Lead
        ↓
Analyze sentiment
        ↓
Schedule follow-up when appropriate
        ↓
Transfer to human when necessary

Wasla should feel more like an "AI employee" platform than a traditional chatbot.

==================================================
2. CORE PRODUCT PRINCIPLES
==================================================

The platform must be:

- Multi-tenant
- Secure
- Production-ready
- Scalable
- Maintainable
- Modular
- API-first
- AI-native
- WhatsApp-native
- Async where appropriate
- Testable
- Observable
- Dockerized
- CI/CD enabled

Do NOT build a toy project.

Do NOT build a demo.

Build the foundation of a real SaaS product that can eventually serve hundreds or thousands of businesses.

==================================================
3. PRIMARY TECHNOLOGY STACK
==================================================

Backend:

- Python 3.12+
- FastAPI
- SQLAlchemy 2.0
- PostgreSQL
- Alembic
- Redis
- Pydantic v2
- Pydantic Settings
- httpx
- Uvicorn
- Docker
- Docker Compose

AI:

- OpenAI API
- Use the latest official OpenAI Responses API
- Never use deprecated OpenAI APIs
- Configurable models
- Function/tool calling ready
- Structured outputs where appropriate
- Conversation memory
- Token optimization

RAG:

- PostgreSQL
- pgvector
- OpenAI embeddings
- Tenant-isolated vector search

Testing:

- pytest
- pytest-asyncio
- httpx test client
- Factory Boy or equivalent where useful
- Testcontainers or isolated test infrastructure where appropriate

Code quality:

- lint
- Ruff
- Black
- MyPy
- pre-commit where appropriate

Infrastructure:

- Docker
- Docker Compose
- Nginx reverse proxy example
- CI/CD
- GitHub Actions

==================================================
4. ARCHITECTURE
==================================================

Use Clean Architecture principles.

Use:

- Domain-oriented modular structure
- Service Layer
- Repository Pattern
- Dependency Injection
- Separation of concerns
- Interface-driven integrations where useful
- Configuration management
- Strong typing
- Async I/O where beneficial

Do NOT put business logic directly inside FastAPI route handlers.

Routes should be thin.

Business logic belongs in services/use cases.

Database access belongs in repositories.

External APIs belong behind integration/client abstractions.

The application should be easy to test without hitting real OpenAI or Meta APIs.

==================================================
5. PROJECT STRUCTURE
==================================================

Use a modular structure similar to:

wasla/
│
├── app/
│   ├── main.py
│   │
│   ├── core/
│   │   ├── config.py
│   │   ├── logging.py
│   │   ├── security.py
│   │   ├── exceptions.py
│   │   ├── middleware.py
│   │   └── dependencies.py
│   │
│   ├── db/
│   │   ├── base.py
│   │   ├── session.py
│   │   └── models/
│   │       ├── tenant.py
│   │       ├── user.py
│   │       ├── role.py
│   │       ├── whatsapp.py
│   │       ├── agent.py
│   │       ├── agent_tool.py
│   │       ├── conversation.py
│   │       ├── message.py
│   │       ├── contact.py
│   │       ├── lead.py
│   │       ├── knowledge.py
│   │       ├── document.py
│   │       ├── document_chunk.py
│   │       ├── follow_up.py
│   │       ├── campaign.py
│   │       ├── whatsapp_template.py
│   │       ├── subscription.py
│   │       ├── plan.py
│   │       ├── usage.py
│   │       ├── usage_event.py
│   │       ├── analytics_event.py
│   │       ├── audit_log.py
│   │       └── system_event.py
│   │
│   ├── repositories/
│   │   ├── tenant.py
│   │   ├── user.py
│   │   ├── whatsapp.py
│   │   ├── agent.py
│   │   ├── conversation.py
│   │   ├── message.py
│   │   ├── contact.py
│   │   ├── lead.py
│   │   ├── knowledge.py
│   │   ├── follow_up.py
│   │   ├── campaign.py
│   │   ├── usage.py
│   │   └── analytics.py
│   │
│   ├── schemas/
│   │   ├── auth.py
│   │   ├── tenant.py
│   │   ├── user.py
│   │   ├── whatsapp.py
│   │   ├── agent.py
│   │   ├── conversation.py
│   │   ├── message.py
│   │   ├── contact.py
│   │   ├── lead.py
│   │   ├── knowledge.py
│   │   ├── campaign.py
│   │   ├── usage.py
│   │   └── analytics.py
│   │
│   ├── services/
│   │   ├── auth_service.py
│   │   ├── tenant_service.py
│   │   ├── whatsapp_service.py
│   │   ├── conversation_service.py
│   │   ├── message_service.py
│   │   ├── agent_service.py
│   │   ├── agent_orchestrator.py
│   │   ├── ai_service.py
│   │   ├── rag_service.py
│   │   ├── lead_service.py
│   │   ├── follow_up_service.py
│   │   ├── campaign_service.py
│   │   ├── media_service.py
│   │   ├── sentiment_service.py
│   │   ├── analytics_service.py
│   │   ├── usage_service.py
│   │   └── billing_service.py
│   │
│   ├── integrations/
│   │   ├── whatsapp/
│   │   │   ├── client.py
│   │   │   ├── webhook.py
│   │   │   ├── parser.py
│   │   │   ├── media.py
│   │   │   ├── templates.py
│   │   │   └── signatures.py
│   │   │
│   │   └── openai/
│   │       ├── client.py
│   │       ├── responses.py
│   │       ├── embeddings.py
│   │       ├── transcription.py
│   │       └── tools.py
│   │
│   ├── agents/
│   │   ├── base.py
│   │   ├── orchestrator.py
│   │   ├── sales.py
│   │   ├── support.py
│   │   ├── booking.py
│   │   └── registry.py
│   │
│   ├── workers/
│   │   ├── message_worker.py
│   │   ├── ai_worker.py
│   │   ├── follow_up_worker.py
│   │   ├── campaign_worker.py
│   │   ├── media_worker.py
│   │   └── usage_worker.py
│   │
│   ├── api/
│   │   └── v1/
│   │       ├── auth.py
│   │       ├── tenants.py
│   │       ├── users.py
│   │       ├── whatsapp.py
│   │       ├── agents.py
│   │       ├── conversations.py
│   │       ├── messages.py
│   │       ├── contacts.py
│   │       ├── leads.py
│   │       ├── knowledge.py
│   │       ├── follow_ups.py
│   │       ├── campaigns.py
│   │       ├── analytics.py
│   │       ├── billing.py
│   │       ├── usage.py
│   │       ├── admin.py
│   │       └── webhooks.py
│   │
│   └── platform/
│       ├── owner_service.py
│       ├── platform_analytics.py
│       ├── platform_usage.py
│       └── platform_billing.py
│
├── alembic/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── e2e/
│   └── conftest.py
│
├── nginx/
│   └── nginx.conf
│
├── scripts/
│
├── Dockerfile
├── docker-compose.yml
├── docker-compose.prod.yml
├── .dockerignore
├── .env.example
├── .gitignore
├── pyproject.toml
├── alembic.ini
├── README.md
└── .github/
    └── workflows/
        ├── ci.yml
        ├── tests.yml
        ├── security.yml
        └── deploy.yml

You may improve this structure if there is a strong architectural reason.

Do not create unnecessary abstractions just for the sake of abstraction.

==================================================
6. MULTI-TENANCY
==================================================

Wasla is a multi-tenant SaaS.

Every business is a Tenant.

A Tenant owns:

- Users
- WhatsApp accounts
- Agents
- Conversations
- Contacts
- Leads
- Knowledge Base
- Documents
- Follow-ups
- Campaigns
- Templates
- Analytics
- Usage
- Subscription
- Settings

Most tenant-owned database records must contain tenant_id.

Tenant isolation is mandatory.

Never allow a tenant user to access another tenant's:

- conversations
- contacts
- leads
- messages
- documents
- embeddings
- WhatsApp accounts
- agents
- analytics
- usage
- settings

All repository/service queries must enforce tenant isolation.

Do not rely only on frontend filtering.

Authorization must be enforced server-side.

==================================================
7. Multi-Workspace Users
==================================================

## Global User Identity

A User is a global platform identity.

A User MUST NOT be directly owned by a single Tenant.

A single User can belong to multiple Tenants through Memberships.

The relationship is:

User
  |
  +-- Membership --> Tenant A
  |
  +-- Membership --> Tenant B
  |
  +-- Membership --> Tenant C

## Memberships

Roles are scoped to Membership, NOT to the User.

Example:

Ahmed:
- Company A → OWNER
- Company B → ADMIN
- Company C → MEMBER

The system MUST support:

- One user belonging to multiple companies
- Creating multiple companies from one account
- Switching the active company/workspace
- Inviting users to companies
- Different roles per company
- Removing memberships
- Suspending memberships
- Tenant-scoped authorization
- Company/workspace switching

Never use `user.tenant_id` as the authoritative relationship between a user and a company.

The authoritative relationship is:

User → Membership → Tenant

## Active Workspace

The authenticated User represents the global identity.

The active Tenant represents the current workspace/company context.

A user may have multiple memberships but only one active workspace per request/session context.

Authorization must verify:

User
  ↓
Membership
  ↓
Tenant
  ↓
Role
  ↓
Resource

Never trust a client-provided tenant_id without verifying that the authenticated user has an active membership in that tenant.

## Database Model

Use:

users
- id
- email
- password_hash
- name
- status
- created_at
- updated_at

tenants
- id
- name
- slug
- status
- created_at
- updated_at

memberships
- id
- user_id
- tenant_id
- role
- status
- created_at
- updated_at

Enforce:

UNIQUE(user_id, tenant_id)

Membership roles are tenant-scoped.

## Invitations

Support company invitations through a dedicated:

tenant_invitations

model containing appropriate fields such as:

- id
- tenant_id
- email
- role
- token/reference
- expires_at
- accepted_at
- created_by
- created_at

Invitations must be validated securely and must not bypass tenant authorization.

## Platform Owner

The SaaS Platform Owner is NOT a normal tenant membership.

Platform-level roles are separate from tenant-level roles.

Platform roles:

- PLATFORM_OWNER
- PLATFORM_ADMIN

Tenant roles:

- TENANT_OWNER
- TENANT_ADMIN
- MEMBER

Platform-level permissions must never be confused with tenant-level permissions.

## Workspace Switching

The application must support switching between companies/workspaces without creating another user account.

Example:

Ahmed logs in once:

Ahmed
├── ABC Finishing → OWNER
├── XYZ Real Estate → ADMIN
└── DEF Contracting → MEMBER

Ahmed can switch:

ABC Finishing
↓
XYZ Real Estate
↓
DEF Contracting

All tenant-scoped API operations must execute within the currently authorized tenant context.

## Tenant Isolation

Every tenant-owned resource must be isolated by tenant.

Examples:

- WhatsApp accounts
- Agents
- Conversations
- Messages
- Contacts
- Leads
- Knowledge bases
- Documents
- Follow-ups
- Campaigns
- Templates
- Analytics
- Usage
- Billing/subscription data

A user must only access resources belonging to a tenant for which they have a valid membership and sufficient role permissions.

Tenant isolation must be explicitly tested.

==================================================
8. PLATFORM OWNER / SAAS OWNER
==================================================

Add a separate SaaS Platform Owner role.

This is NOT the owner of a customer company.

Platform Owner = Wasla SaaS administrator.

The platform owner can see and manage the entire platform.

Roles should conceptually include:

Platform level:

- PLATFORM_OWNER
- PLATFORM_ADMIN

Tenant level:

- TENANT_OWNER
- TENANT_ADMIN
- MEMBER

Potential future roles:

- SALES
- SUPPORT
- MANAGER

The Platform Owner must have a separate global administration layer.

Platform Owner capabilities:

- View all tenants
- Search tenants
- Create tenants
- Suspend tenants
- Activate tenants
- Delete/deactivate tenants safely
- View tenant details
- View tenant users
- View tenant WhatsApp accounts
- View tenant agents
- View conversations
- View leads
- View usage
- View billing
- View subscriptions
- View plans
- View system health
- View audit logs
- View platform analytics
- View AI usage
- View token usage
- View estimated AI costs
- View revenue
- View MRR
- View ARR
- View active subscriptions
- View trials
- View cancelled subscriptions
- View failed payments
- View upgrades/downgrades

Platform Owner must NOT bypass audit logging.

All privileged actions must be logged.

==================================================
9. SAAS OWNER DASHBOARD DATA
==================================================

The backend must expose APIs required for a global SaaS Owner dashboard.

Example overview:

- Total companies
- Active companies
- Trial companies
- Suspended companies
- Active WhatsApp numbers
- Messages today
- Messages this month
- AI requests
- Input tokens
- Output tokens
- Total tokens
- Leads created
- Conversations created
- Human handoffs
- RAG queries
- Voice minutes
- Media processed
- Storage usage
- MRR
- ARR
- Subscription revenue
- Estimated AI cost
- Estimated infrastructure cost
- Estimated gross margin

Tenant-level usage:

- messages_received
- messages_sent
- ai_requests
- input_tokens
- output_tokens
- total_tokens
- rag_queries
- media_processed
- voice_minutes
- leads_created
- conversations_created
- storage_used
- API requests
- campaign messages

Provide date-range filtering.

==================================================
10. USAGE TRACKING
==================================================

Build usage tracking as a first-class subsystem.

Use usage events.

Example event types:

- WHATSAPP_MESSAGE_RECEIVED
- WHATSAPP_MESSAGE_SENT
- AI_REQUEST
- AI_INPUT_TOKEN
- AI_OUTPUT_TOKEN
- RAG_QUERY
- MEDIA_PROCESSING
- VOICE_TRANSCRIPTION
- STORAGE_USED
- LEAD_CREATED
- CONVERSATION_CREATED
- CAMPAIGN_MESSAGE
- API_REQUEST

Usage event structure should support:

- tenant_id
- event_type
- quantity
- unit
- metadata
- created_at

Build aggregation services for dashboards and billing.

Usage must be tenant-isolated.

==================================================
11. PLANS
==================================================

Implement configurable SaaS plans.

Initial conceptual plans:

Starter:

- 1 WhatsApp number
- 1 AI Agent
- 1,000 messages
- 100 AI requests
- 2 team members

Pro:

- 3 WhatsApp numbers
- 5 AI Agents
- 10,000 messages
- 5,000 AI requests
- 10 team members

Business:

- 10 WhatsApp numbers
- 20 AI Agents
- 50,000 messages
- 25,000 AI requests
- 50 team members

Enterprise:

- Custom limits
- Custom pricing
- Custom features

Do NOT hardcode plan limits throughout the code.

Plans and limits must be stored/configurable.

Enforce limits through a central usage/entitlement service.

==================================================
12. BILLING
==================================================

Design billing to support:

- subscriptions
- plans
- trials
- upgrades
- downgrades
- cancellations
- past_due
- invoices
- payments
- usage limits

Do not tightly couple billing business logic to a specific payment provider.

Create abstractions so a provider can be added later.

The initial system may expose billing APIs and internal billing models without requiring a live payment provider during local development.

==================================================
13. WHATSAPP CLOUD API
==================================================

Implement WhatsApp Cloud API integration.

Required capabilities:

Webhook verification.

Incoming messages.

Outgoing messages.

Delivery status.

Read receipts.

Message statuses.

Support:

- Text messages
- Images
- Documents
- PDF files
- Audio
- Video
- Location
- Interactive buttons
- Interactive lists
- Templates

Create a clean WhatsApp client abstraction.

Example conceptual methods:

send_text()
send_image()
send_document()
send_video()
send_audio()
send_location()
send_button()
send_list()
send_template()

Do not scatter raw HTTP calls throughout the application.

Use a dedicated WhatsApp integration/client.

Use httpx asynchronously.

Support configurable Meta Graph API versions.

Never hardcode access tokens.

==================================================
14. WHATSAPP WEBHOOK FLOW
==================================================

Webhook endpoint must:

1. Verify webhook challenge.
2. Validate Meta signature.
3. Parse payload.
4. Identify phone_number_id.
5. Resolve tenant.
6. Parse message/event.
7. Persist message/event.
8. Update conversation.
9. Enqueue background processing.
10. Return quickly.

Do not perform long AI processing inside the webhook request.

Webhook should be fast and resilient.

Support idempotency.

WhatsApp can retry webhook delivery.

Duplicate messages must not create duplicate processing.

Use WhatsApp message IDs / event IDs for idempotency.

==================================================
15. WHATSAPP ACCOUNT MULTI-TENANCY
==================================================

A tenant may have one or more WhatsApp Business phone numbers depending on plan.

Store:

- tenant_id
- phone_number_id
- business_account_id
- display_phone_number
- access token metadata
- status
- webhook configuration metadata
- timestamps

When a webhook arrives:

phone_number_id
        ↓
whatsapp account
        ↓
tenant_id
        ↓
conversation
        ↓
agent

Never infer tenant from customer phone number.

==================================================
16. AI AGENTS
==================================================

Wasla must support multiple AI Agents per tenant.

An Agent is an AI employee configuration, not merely a prompt.

Each Agent should support:

- name
- description
- personality
- language
- tone
- instructions
- model
- knowledge sources
- tools
- triggers
- routing rules
- handoff rules
- enabled/disabled state
- fallback behavior
- metadata

Example:

Sales Agent:

- Sales personality
- Arabic Egyptian
- Friendly and persuasive
- Product knowledge
- Pricing knowledge
- Create lead tool
- Send brochure tool
- Handoff tool

Support Agent:

- Support personality
- Complaint handling
- Order lookup tool
- Ticket creation
- Escalation

Booking Agent:

- Appointment booking
- Availability lookup
- Create booking
- Cancel booking
- Reschedule booking

==================================================
17. AGENT ORCHESTRATOR
==================================================

Implement an Agent Orchestrator.

Flow:

Incoming message
    ↓
Load tenant
    ↓
Load conversation
    ↓
Determine current mode
    ↓
If HUMAN → do not invoke AI
    ↓
Determine agent
    ↓
Load agent configuration
    ↓
Load conversation memory
    ↓
Retrieve relevant knowledge
    ↓
Prepare tools
    ↓
Call OpenAI Responses API
    ↓
Handle tool calls
    ↓
Execute business tools
    ↓
Continue model interaction if needed
    ↓
Produce final response
    ↓
Send WhatsApp response
    ↓
Persist response
    ↓
Record usage

The orchestrator should be testable independently from FastAPI.

==================================================
18. OPENAI RESPONSES API
==================================================

Use the latest official OpenAI Responses API.

Do not use deprecated APIs.

Create an integration layer so application services do not directly depend on raw SDK implementation details.

Support:

- configurable models
- system/developer instructions
- conversation context
- structured outputs where useful
- function/tool calling
- token usage tracking
- retries where appropriate
- timeout handling
- error handling
- model configuration per agent

AI calls must be observable.

Never log API keys.

Do not log sensitive user content unnecessarily.

==================================================
19. AI TOOLS
==================================================

Make tool calling extensible.

Initial conceptual tools:

- create_lead
- update_lead
- get_lead
- assign_lead
- get_product
- get_price
- search_knowledge
- send_media
- handoff_to_human
- create_ticket
- get_order
- check_availability
- create_appointment
- cancel_appointment
- reschedule_appointment
- schedule_follow_up

Tools must validate arguments.

Tools must enforce tenant isolation.

Agents should only receive tools explicitly allowed for them.

Never allow arbitrary tool execution from model output.

==================================================
20. KNOWLEDGE BASE / RAG
==================================================

Each tenant has an isolated Knowledge Base.

Support:

- Company information
- FAQs
- Products
- Prices
- Policies
- PDFs
- Documents
- Attachments
- Text content

Document ingestion:

Upload
 ↓
Validate
 ↓
Extract text
 ↓
Chunk
 ↓
Generate embeddings
 ↓
Store embeddings
 ↓
Associate with tenant
 ↓
Index

Use PostgreSQL + pgvector.

Vector search must always be tenant-filtered.

RAG flow:

User question
 ↓
Embedding
 ↓
Tenant-scoped vector search
 ↓
Relevant chunks
 ↓
Agent context
 ↓
OpenAI Responses API
 ↓
Answer

Do not allow cross-tenant retrieval.

==================================================
21. CONVERSATION MEMORY
==================================================

Store all messages.

Conversation fields should support:

- tenant_id
- contact_id
- assigned_to
- current_agent_id
- mode
- status
- summary
- sentiment
- priority
- last_message_at
- metadata

Message fields should support:

- tenant_id
- conversation_id
- WhatsApp message ID
- direction
- type
- content
- media metadata
- status
- timestamps
- metadata

Do not send the entire conversation forever.

Use:

- recent message window
- conversation summary
- relevant RAG context
- current message

Implement token-aware context management.

==================================================
22. HUMAN HANDOFF
==================================================

Every conversation should have a mode:

AI
HUMAN

When HUMAN:

- AI must stop responding automatically.
- Human agents can reply through the system.
- Conversation can be assigned to a team member.
- Track who took ownership.
- Track handoff reason.

Support:

Resume AI

which switches the conversation back to AI mode.

Possible handoff triggers:

- customer explicitly asks for human
- low AI confidence
- angry customer
- sensitive request
- agent rule
- tool failure
- business-defined escalation rule

==================================================
23. CRM / LEAD MANAGEMENT
==================================================

Wasla must automatically capture leads from conversations.

Lead fields:

- tenant_id
- name
- phone
- email
- source
- status
- interest
- budget
- score
- assigned_to
- notes
- metadata
- timestamps

Statuses:

- NEW
- CONTACTED
- QUALIFIED
- PROPOSAL
- WON
- LOST

AI should be able to create/update/qualify leads through controlled tools.

Example:

Customer:

"My name is Ahmed, I want to finish a 150m apartment and my budget is 500k."

Agent can extract:

name = Ahmed
interest = apartment finishing
area = 150m
budget = 500000

Then create/update a Lead.

==================================================
24. FOLLOW-UPS
==================================================

Implement automated follow-ups.

Example:

Customer:

"I'll think about it."

Create follow-up.

After 30 minutes:

Check whether customer responded.

If not:

Send follow-up if allowed by WhatsApp messaging rules.

Important:

Respect WhatsApp's conversation window and template requirements.

Outside the allowed free-form messaging window, use approved WhatsApp templates.

Follow-ups must be cancellable.

If customer responds, pending follow-up should usually be cancelled or re-evaluated.

==================================================
25. MEDIA UNDERSTANDING
==================================================

Architecture must support:

- Images
- Voice messages
- Audio
- Documents
- PDFs
- Videos

Image flow:

WhatsApp
 ↓
Webhook
 ↓
Media worker
 ↓
Download media
 ↓
AI vision processing
 ↓
Agent
 ↓
Response

Voice flow:

WhatsApp audio
 ↓
Download
 ↓
Transcription
 ↓
Text
 ↓
Agent
 ↓
Response

Do not block webhook requests while processing media.

==================================================
26. MEDIA SENDING
==================================================

AI Agents must be able to send:

- Images
- Videos
- Documents
- PDFs
- Links
- Buttons
- Lists
- Templates

Media should be stored/referenced safely.

Do not store large files directly in PostgreSQL unless there is a strong reason.

Design a storage abstraction for future object storage integration.

==================================================
27. SENTIMENT ANALYSIS
==================================================

Support sentiment analysis.

Store:

- sentiment
- sentiment score
- priority
- detected intent
- confidence

Possible sentiment values:

- positive
- neutral
- negative
- angry

If customer is highly negative or angry:

- increase priority
- optionally flag conversation
- optionally trigger human handoff
- create analytics event

Example UI state:

URGENT
Customer appears frustrated.

==================================================
28. CAMPAIGNS / BROADCAST
==================================================

Support WhatsApp campaigns.

Features:

- Campaign creation
- Audience selection
- Template selection
- Scheduling
- Sending
- Rate limiting
- Status tracking
- Delivery statistics
- Failure tracking

Campaigns must respect:

- WhatsApp policies
- approved templates
- opt-in requirements
- rate limits
- messaging rules

Do not implement uncontrolled bulk sending.

==================================================
29. WHATSAPP TEMPLATES
==================================================

Support template metadata:

- tenant_id
- name
- language
- category
- status
- components
- metadata

Templates are especially important for:

- campaigns
- outbound messages
- follow-ups outside the 24-hour service window

==================================================
30. TEAM MANAGEMENT
==================================================

Support multiple users per tenant.

Roles:

- TENANT_OWNER
- TENANT_ADMIN
- MEMBER

Users can:

- access conversations according to permissions
- be assigned conversations
- manage leads if allowed
- reply to customers
- view analytics according to permissions

Track:

- who replied
- who owns the conversation
- who owns the lead
- who performed administrative actions

==================================================
31. ANALYTICS
==================================================

Implement analytics events.

Examples:

- message_received
- message_sent
- conversation_created
- lead_created
- lead_qualified
- handoff
- appointment_created
- follow_up_sent
- agent_response
- customer_angry
- campaign_sent
- campaign_delivered

Tenant analytics:

- Conversations
- Messages
- Leads
- Qualified leads
- Conversion rate
- Human handoffs
- AI resolution rate
- Average response time
- Unhappy customers
- Agent performance
- Campaign performance

Platform analytics:

- Total tenants
- Active tenants
- Messages
- AI usage
- Token usage
- Revenue
- Costs
- MRR
- ARR
- Growth
- Churn
- Plan distribution

==================================================
32. ADMIN APIs
==================================================

Tenant-level APIs should include:

GET /api/v1/users
GET /api/v1/conversations
GET /api/v1/conversations/{id}
DELETE /api/v1/conversations/{id}
GET /api/v1/leads
GET /api/v1/agents
GET /api/v1/analytics
GET /api/v1/usage

Platform Owner APIs should be separate and clearly scoped.

Example:

/api/v1/platform/tenants
/api/v1/platform/tenants/{tenant_id}
/api/v1/platform/usage
/api/v1/platform/analytics
/api/v1/platform/billing
/api/v1/platform/plans
/api/v1/platform/audit-logs
/api/v1/platform/system-health

Never mix platform-level authorization with tenant-level authorization accidentally.

==================================================
33. AUTHENTICATION & AUTHORIZATION
==================================================

Implement secure authentication.

Use modern password hashing.

Use access/refresh token strategy if JWT is selected.

Implement:

- login
- logout/revocation strategy
- password hashing
- token validation
- role-based authorization
- tenant context
- platform context

Authorization must verify:

Who is the user?
What role do they have?
What tenant do they belong to?
What resource are they accessing?
Does that resource belong to the tenant?

Never rely on client-provided tenant_id without verifying membership.

==================================================
34. SECURITY
==================================================

Security requirements:

- Environment-based secrets
- No secrets committed to Git
- Input validation
- Strong Pydantic schemas
- Meta webhook signature verification
- Secure authentication
- RBAC
- Tenant isolation
- SQL injection protection through SQLAlchemy
- Safe error handling
- No secret leakage in logs
- Request size limits where appropriate
- Rate limiting strategy
- CORS configuration
- Secure headers
- Audit logging
- Idempotency for webhooks
- Timeout configuration
- Retry policies
- Safe external API handling

Do not expose stack traces in production responses.

==================================================
35. LOGGING
==================================================

Implement structured logging.

Include:

- request_id
- tenant_id where applicable
- user_id where applicable
- conversation_id where applicable
- event
- timestamp
- log level

Support:

- request logging
- response/error logging
- webhook logging
- AI call logging
- worker logging
- integration failures

Never log:

- API keys
- access tokens
- passwords
- secrets

Avoid logging full sensitive customer messages unless necessary.

==================================================
36. ERROR HANDLING
==================================================

Create centralized exception handling.

Use custom domain/application exceptions.

Map exceptions to appropriate HTTP responses.

External integration errors must be handled gracefully.

AI failures should not crash the webhook.

WhatsApp failures should be retried where safe.

Database failures should be handled safely.

Workers should support retry policies and dead-letter/error handling strategy.

==================================================
37. ASYNC ARCHITECTURE
==================================================

Use async where beneficial:

- FastAPI routes
- SQLAlchemy async sessions
- httpx
- OpenAI network calls
- WhatsApp API calls
- Redis operations where supported

Do not use async merely for appearance.

CPU-heavy work should be moved to workers.

Long-running operations must not block webhook requests.

==================================================
38. REDIS / BACKGROUND JOBS
==================================================

Use Redis for:

- caching where appropriate
- job queues
- temporary state
- rate limiting where appropriate
- follow-up scheduling

Background jobs:

- AI processing
- media processing
- document ingestion
- embeddings
- follow-ups
- campaigns
- usage aggregation

Use an appropriate queue architecture.

Ensure jobs are idempotent.

==================================================
39. DATABASE
==================================================

Use PostgreSQL.

Use SQLAlchemy 2.0 style.

Use Alembic migrations.

Do not manually mutate production schema outside migrations.

Use indexes strategically.

Important indexes include:

- tenant_id
- conversation tenant/status
- message conversation/timestamp
- contact tenant/phone
- lead tenant/status
- WhatsApp phone_number_id
- usage tenant/timestamp
- analytics tenant/timestamp
- document tenant_id
- vector search metadata

Use foreign keys and constraints.

Use transactions appropriately.

==================================================
40. DATABASE MODELS
==================================================

At minimum design models for:

Tenant
User
Role
TenantUser
WhatsAppAccount
Agent
AgentTool
Conversation
Message
Contact
Lead
KnowledgeBase
Document
DocumentChunk
FollowUp
Campaign
WhatsAppTemplate
Plan
Subscription
UsageEvent
AnalyticsEvent
AuditLog
SystemEvent

Add timestamps.

Use UUIDs or another robust identifier strategy.

Use soft deletion where appropriate.

Do not soft-delete everything blindly.

==================================================
41. API DESIGN
==================================================

Use versioned APIs:

/api/v1

Use RESTful conventions.

Use consistent response schemas.

Use pagination.

Use filtering.

Use sorting where useful.

Use cursor pagination for large datasets where appropriate.

Document APIs through FastAPI OpenAPI.

Validate all request/response schemas.

==================================================
42. TESTING
==================================================

Create real tests.

Unit tests:

- services
- repositories where appropriate
- agent orchestrator
- RAG
- usage
- authorization
- lead extraction
- follow-up logic

Integration tests:

- database
- migrations
- FastAPI endpoints
- webhook handling
- Redis/job behavior

E2E tests:

- incoming WhatsApp message
- tenant resolution
- conversation creation
- AI processing
- response persistence
- outgoing WhatsApp request

Mock external services.

Do not make tests depend on real OpenAI or Meta credentials.

Test tenant isolation explicitly.

Example:

Tenant A must never access Tenant B data.

Test RBAC explicitly.

Test platform owner access separately.

==================================================
43. CI/CD
==================================================

Implement GitHub Actions CI/CD.

At minimum:

.github/workflows/ci.yml

Pipeline should run:

- dependency installation
- formatting check
- Ruff
- MyPy
- unit tests
- integration tests
- migration validation
- build checks

Security workflow:

- dependency vulnerability scanning
- secret scanning if practical
- container scanning if practical

Deployment workflow:

- build Docker image
- run checks
- push image to registry
- deploy to production environment

Do not deploy if tests fail.

Use GitHub Actions secrets.

Never hardcode deployment secrets.

Make CI deterministic.

Use pinned or controlled dependency versions.

==================================================
44. DOCKER
==================================================

Provide:

Dockerfile
docker-compose.yml
docker-compose.prod.yml

Local Docker Compose should include:

- API
- Worker
- PostgreSQL
- Redis
- Nginx if useful

Production configuration should support:

- non-root containers where practical
- health checks
- environment variables
- graceful shutdown
- restart policies
- isolated networks
- persistent database volumes

Do not put secrets into Dockerfiles.

==================================================
45. NGINX
==================================================

Provide an Nginx reverse proxy example.

Responsibilities:

- HTTPS termination example
- reverse proxy
- request size limits
- security headers
- WebSocket support if needed
- forwarding client IP
- proxy timeouts

Do not pretend TLS certificates are included automatically.

Document how production TLS should be configured.

==================================================
46. CONFIGURATION
==================================================

Use Pydantic Settings.

Configuration must be environment-based.

Create:

.env.example

Include documentation for:

DATABASE_URL
REDIS_URL
OPENAI_API_KEY
OPENAI_MODEL
META_APP_ID
META_APP_SECRET
META_VERIFY_TOKEN
META_ACCESS_TOKEN or tenant-specific token strategy
META_API_VERSION
JWT_SECRET
CORS settings
LOG_LEVEL
ENVIRONMENT
etc.

Never commit .env.

If WhatsApp credentials are tenant-specific, design storage/encryption appropriately rather than forcing one global token.

==================================================
47. OBSERVABILITY
==================================================

Design for production observability.

At minimum:

- structured logs
- request IDs
- health endpoint
- readiness endpoint
- liveness endpoint
- worker health
- database health
- Redis health
- external integration health

Suggested:

GET /health
GET /health/live
GET /health/ready

Keep the implementation lightweight initially but extensible for:

- OpenTelemetry
- Prometheus
- Sentry
- metrics dashboards

==================================================
48. HEALTH CHECKS
==================================================

Health checks should distinguish:

Liveness:
Application process is alive.

Readiness:
Dependencies required for serving traffic are available.

Do not make liveness depend on PostgreSQL.

==================================================
49. API RATE LIMITING
==================================================

Design rate limiting for:

- authentication
- public webhooks where appropriate
- tenant APIs
- campaign APIs
- platform APIs

Do not accidentally rate-limit Meta webhook retries in a way that causes message loss.

==================================================
50. IDEMPOTENCY
==================================================

Implement idempotency where required.

Especially:

- WhatsApp webhook events
- outgoing message retries
- campaign jobs
- follow-up jobs
- billing events

Never send duplicate WhatsApp messages because a worker retried.

==================================================
51. DOCUMENTATION
==================================================

Generate:

README.md

Include:

- What is Wasla?
- Architecture
- Features
- Tech stack
- Local development
- Environment setup
- Database setup
- Alembic migrations
- Docker usage
- Running API
- Running workers
- Running tests
- WhatsApp configuration
- OpenAI configuration
- RAG setup
- CI/CD
- Production deployment
- Security notes
- API documentation
- Troubleshooting

Also provide:

- installation guide
- environment variables documentation
- API documentation
- architecture documentation

==================================================
52. CODE QUALITY
==================================================

Requirements:

- Python type hints
- Pydantic schemas
- SQLAlchemy 2.0 style
- AsyncSession
- Docstrings for public APIs/services
- Clear naming
- Small focused modules
- No giant files
- No circular dependencies
- No duplicated business logic
- PEP8
- Ruff
- Black
- MyPy

Comments should be used only where they add value.

Do not write obvious comments.

Avoid premature abstraction.

==================================================
53. FRONTEND BOUNDARY
==================================================

The current implementation focus is the backend.

Do not build a full frontend unless explicitly requested.

However, design the backend APIs so a modern frontend can easily build:

Tenant dashboard:

- Overview
- Inbox
- Conversations
- Contacts
- Leads
- AI Agents
- Knowledge Base
- WhatsApp
- Campaigns
- Team
- Analytics
- Billing
- Settings

Platform Owner dashboard:

- Overview
- Companies
- Company details
- Usage
- Revenue
- Plans
- Subscriptions
- System health
- Audit logs

==================================================
54. FUTURE EXTENSIBILITY
==================================================

Architecture must allow future features without major rewrites:

- CRM integrations
- Salesforce
- HubSpot
- Zoho
- Voice messages
- Image understanding
- Video understanding
- Appointment booking
- Google Calendar
- Calendly
- E-commerce
- Shopify
- Order management
- Payment integrations
- Website chat
- Instagram
- Messenger
- Telegram
- Email
- Advanced analytics
- White-labeling
- Enterprise SSO
- API keys
- Webhooks
- Custom tools
- Custom AI Agents

Do not implement all future features now.

Create clean boundaries for them.

==================================================
55. DEVELOPMENT WORKFLOW
==================================================

Build the project step by step.

Do NOT attempt to dump the entire repository in one response.

Work in logical phases.

Recommended implementation order:

PHASE 0
Project initialization
Configuration
Tooling
Docker
Git
CI foundation

PHASE 1
Database foundation
SQLAlchemy
Alembic
Base models
Tenant model
User model
RBAC

PHASE 2
Authentication
Tenant isolation
Authorization
Platform Owner

PHASE 3
WhatsApp integration
Webhook verification
Signature verification
Incoming messages
Outgoing messages
Statuses
Idempotency

PHASE 4
Conversation management
Contacts
Messages
Human mode
Assignments

PHASE 5
AI Agent architecture
OpenAI Responses API
Agent configuration
Orchestrator
Conversation memory

PHASE 6
Knowledge Base
Documents
Embeddings
pgvector
RAG

PHASE 7
CRM
Leads
Lead extraction
Assignment
Statuses

PHASE 8
Follow-ups
Scheduler
Redis workers

PHASE 9
Media
Images
Audio
Voice transcription
Documents

PHASE 10
Sentiment
Priority
Automatic handoff

PHASE 11
Campaigns
Templates
Broadcast infrastructure
Rate limiting

PHASE 12
Analytics
Usage tracking
Platform analytics

PHASE 13
Plans
Subscriptions
Billing architecture

PHASE 14
Production hardening
Security
Logging
Health checks
Observability
Performance

PHASE 15
CI/CD
Docker production
Nginx
Deployment documentation

==================================================
56. IMPORTANT: DO NOT SKIP FILES
==================================================

When implementing a phase:

- Explain what the phase accomplishes.
- Explain the architecture decisions briefly.
- List the files that will be created/modified.
- Implement complete files.
- Do not use placeholders like "implement later".
- Do not skip required imports.
- Do not leave fake functions.
- Do not create broken examples.

If a feature requires multiple files, implement all related files as one logical unit.

==================================================
57. GIT WORKFLOW
==================================================

Git history is a first-class engineering requirement.

From the beginning of the project, organize Git history using Logical / Atomic Commits.

Do NOT create a commit for every file.

When a Feature, Fix, Refactor, Test suite, Documentation change, CI change, or other coherent unit of work is complete, group ALL files related to that logical change into ONE commit.

Example:

feat(auth): add authentication

This commit should contain all files required for authentication.

Not:

commit 1: add auth.py
commit 2: add user.py
commit 3: add schema.py

Use Conventional Commits:

feat:
fix:
refactor:
test:
docs:
chore:
ci:

Rules:

- One Commit = One Logical Change
- Do not split files that belong to the same feature into unrelated commits.
- Do not combine unrelated features into one commit.
- If a single file contains changes for multiple features, use `git add -p` when appropriate.
- Automatically create commits without asking for approval.
- Never delete or lose existing user changes.
- Never reset or force-reset the repository destructively.
- Never overwrite unrelated work.
- Before committing, inspect git status and diff.
- Ensure each commit is coherent.
- Commit messages must explain the real change.

Examples:

feat(tenancy): add multi-tenant data isolation
feat(auth): add authentication and RBAC
feat(whatsapp): add Cloud API webhook integration
feat(conversations): add conversation management
feat(ai): add OpenAI Responses API agent orchestration
feat(rag): add tenant-scoped knowledge retrieval
feat(crm): add lead management
feat(followups): add automated follow-up jobs
feat(platform): add SaaS owner administration
feat(usage): add tenant usage tracking
feat(billing): add subscription and plan models
test(whatsapp): add webhook integration tests
ci: add GitHub Actions CI pipeline
docs: add production deployment guide

Before every commit:

1. git status
2. inspect relevant diff
3. verify tests/checks
4. stage the complete logical change
5. commit using Conventional Commits

Do not commit broken code unless the commit is intentionally documenting an intermediate architectural change and the repository remains valid.

At the end:

- Show git status
- Show recent git log
- Explain the logical commits created

==================================================
58. EXISTING USER CHANGES
==================================================

IMPORTANT:

Before modifying the repository:

- Inspect the working tree.
- Inspect existing files.
- Inspect git status.
- Inspect existing commits.
- Do NOT delete existing user work.
- Do NOT overwrite unrelated changes.
- Preserve anything already implemented unless it conflicts with the architecture.
- If existing code exists, adapt it instead of blindly replacing it.

==================================================
59. TESTING BEFORE COMMIT
==================================================

Before committing a feature:

Run appropriate:

- Ruff
- Black check
- MyPy
- pytest

For relevant changes also run:

- migration checks
- integration tests
- Docker build
- API startup check

Do not hide failing tests.

Fix failures before committing when they are caused by your changes.

==================================================
60. PRODUCTION READINESS
==================================================

Before declaring the project complete, verify:

- Application starts
- Database migrations work
- Docker build works
- Docker Compose works
- API health endpoint works
- PostgreSQL connectivity works
- Redis connectivity works
- Authentication works
- RBAC works
- Tenant isolation works
- Platform Owner works
- WhatsApp webhook verification works
- WhatsApp signature verification exists
- Incoming messages persist
- Duplicate webhook events are handled
- AI processing is asynchronous
- OpenAI Responses API integration works
- Agent configuration works
- Tool calling architecture exists
- RAG is tenant-isolated
- Human handoff works
- Leads work
- Follow-ups work
- Usage tracking works
- Analytics work
- Plans/limits work
- Logging works
- Error handling works
- Tests pass
- CI passes
- Production Docker configuration exists
- Nginx example exists
- Documentation exists

==================================================
61. IMPORTANT ENGINEERING RULES
==================================================

Never:

- use deprecated OpenAI APIs
- hardcode secrets
- trust client tenant_id blindly
- allow cross-tenant data access
- perform expensive AI work inside webhooks
- put business logic inside routes
- create giant service classes
- duplicate integration code
- silently swallow exceptions
- disable tests to make CI pass
- skip migrations
- commit secrets
- force-reset user work
- create a commit per file
- mix unrelated changes in one commit

Always:

- use type hints
- use dependency injection
- use async I/O where beneficial
- validate inputs
- enforce tenant isolation
- enforce RBAC
- use transactions appropriately
- make background jobs idempotent
- test important business logic
- document important architecture
- maintain clean Git history
- use Conventional Commits

==================================================
62. RESPONSE / EXECUTION RULE
==================================================

You are operating as an autonomous senior engineer.

Do not ask for permission for routine engineering decisions.

Do not ask me to approve every file.

Do not ask before making logical commits.

Only ask a question if a genuinely blocking ambiguity exists that cannot reasonably be resolved through engineering judgment.

Otherwise make the best production-grade decision and continue.

Work incrementally.

After completing a coherent phase or logical feature:

1. Explain what was implemented.
2. Show files created/changed.
3. Run relevant checks/tests.
4. Fix issues.
5. Create the appropriate logical Conventional Commit.
6. Show the commit hash and message.
7. Then continue to the next logical phase.

Do NOT stop after creating one file.

Do NOT create one commit per file.

Do NOT dump thousands of lines of unrelated code into one response.

==================================================
63. FINAL DELIVERABLE
==================================================

The final repository should be a complete production-ready backend foundation for Wasla.

It should provide:

A secure multi-tenant SaaS backend.

A SaaS Platform Owner administration layer.

Tenant-level administration.

WhatsApp Cloud API integration.

AI Agents.

OpenAI Responses API integration.

Conversation memory.

RAG / Knowledge Base.

Lead management.

CRM foundation.

Human handoff.

Follow-ups.

Media processing architecture.

Voice processing architecture.

Sentiment analysis architecture.

WhatsApp campaigns and templates.

Team management.

Usage tracking.

Plans and subscriptions.

Analytics.

Structured logging.

Security.

Testing.

Docker.

CI/CD.

Production configuration.

Nginx reverse proxy example.

Documentation.

Clean architecture.

Logical Git history.

The system must be ready to deploy and must be designed so future features such as CRM integrations, appointment booking, voice messages, image understanding, e-commerce integrations, additional messaging channels, advanced analytics, and white-labeling can be added without rewriting the core architecture.

Start by inspecting the repository and Git state.

Then begin PHASE 0.

Do not skip the Git initialization/inspection step.

Do not generate the entire project in one response.

Build the project carefully, logically, and production-first.
