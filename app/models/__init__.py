"""ORM models.

Importing every model here ensures they are registered on ``Base.metadata`` for
Alembic autogenerate and metadata-based tooling. Import models from this package
(``from app.models import User``) rather than the submodules directly.
"""

# Group A — tenancy & identity
from app.models.organization import Organization
from app.models.user import User
from app.models.user_session import UserSession
from app.models.phone_number import PhoneNumber
from app.models.client_account import ClientAccount

# Group B — core PI domain
from app.models.lead import Lead
from app.models.incident import Incident
from app.models.injury import Injury
from app.models.medical_treatment import MedicalTreatment
from app.models.insurance_policy import InsurancePolicy
from app.models.party import Party
from app.models.damage import Damage

# Group C — AI outputs
from app.models.settlement_estimate import SettlementEstimate
from app.models.lead_score import LeadScore
from app.models.intake_transcript import IntakeTranscript
from app.models.transcript_segment import TranscriptSegment

# Group D — telephony & communications
from app.models.voice_call import VoiceCall
from app.models.conversation import Conversation
from app.models.message import Message

# Group E — documents & retainer
from app.models.document_request import DocumentRequest
from app.models.document import Document
from app.models.retainer import Retainer
from app.models.signature_event import SignatureEvent

# Group F — workflow & audit
from app.models.internal_note import InternalNote
from app.models.task import Task
from app.models.audit_log import AuditLog

# Group G — AI memory (vector RAG + knowledge graph + agent state)
from app.models.knowledge_chunk import KnowledgeChunk
from app.models.kg_node import KgNode
from app.models.kg_edge import KgEdge
from app.models.agent_thread import AgentThread
from app.models.agent_checkpoint import AgentCheckpoint
from app.models.agent_event import AgentEvent

# Group H — event backbone
from app.models.outbox_event import OutboxEvent
from app.models.webhook_event import WebhookEvent

__all__ = [
    "Organization",
    "User",
    "UserSession",
    "PhoneNumber",
    "ClientAccount",
    "Lead",
    "Incident",
    "Injury",
    "MedicalTreatment",
    "InsurancePolicy",
    "Party",
    "Damage",
    "SettlementEstimate",
    "LeadScore",
    "IntakeTranscript",
    "TranscriptSegment",
    "VoiceCall",
    "Conversation",
    "Message",
    "DocumentRequest",
    "Document",
    "Retainer",
    "SignatureEvent",
    "InternalNote",
    "Task",
    "AuditLog",
    "KnowledgeChunk",
    "KgNode",
    "KgEdge",
    "AgentThread",
    "AgentCheckpoint",
    "AgentEvent",
    "OutboxEvent",
    "WebhookEvent",
]
