"""Skill taxonomy shared by the resume parser and job-description analysis.

Each canonical skill maps to regex patterns. Patterns are matched case-insensitively against
plain text, with custom word boundaries so symbols like C#, .NET and C++ work."""
from __future__ import annotations

import re

# canonical name -> (category, [patterns])
SKILLS: dict[str, tuple[str, list[str]]] = {
    "C#": ("language", [r"c#", r"c sharp", r"csharp"]),
    ".NET": ("framework", [r"\.net(?! ?framework)(?: ?core| ?[5-9])?", r"dotnet"]),
    ".NET Framework": ("framework", [r"\.net framework"]),
    "ASP.NET Core": ("framework", [r"asp\.net core", r"aspnet core", r"asp\.net"]),
    "ASP.NET MVC": ("framework", [r"asp\.net mvc", r"\.net mvc"]),
    "Web API": ("framework", [r"web ?api", r"asp\.net web api"]),
    "Entity Framework": ("framework", [r"entity framework(?: core)?", r"ef core"]),
    "LINQ": ("framework", [r"linq"]),
    "Python": ("language", [r"python"]),
    "FastAPI": ("framework", [r"fastapi"]),
    "Django": ("framework", [r"django"]),
    "Flask": ("framework", [r"flask"]),
    "Java": ("language", [r"java(?!script)"]),
    "Spring": ("framework", [r"spring boot", r"spring"]),
    "Kotlin": ("language", [r"kotlin"]),
    "Go": ("language", [r"golang", r"go(?= developer| engineer|lang)", r"\bgo,"]),
    "Rust": ("language", [r"rust"]),
    "Ruby": ("language", [r"ruby(?: on rails)?", r"rails"]),
    "PHP": ("language", [r"php", r"laravel"]),
    "Scala": ("language", [r"scala"]),
    "C++": ("language", [r"c\+\+"]),
    "Swift": ("language", [r"swift"]),
    "JavaScript": ("language", [r"javascript", r"\bjs\b", r"es6"]),
    "TypeScript": ("language", [r"typescript"]),
    "Node.js": ("framework", [r"node\.?js", r"\bnode\b"]),
    "React": ("framework", [r"react(?:\.js|js)?(?! native)"]),
    "Angular": ("framework", [r"angular(?:js)?"]),
    "Vue": ("framework", [r"vue(?:\.js)?"]),
    "HTML": ("language", [r"html5?"]),
    "CSS": ("language", [r"css3?", r"tailwind", r"sass|scss"]),
    "SQL": ("database", [r"\bsql\b", r"t-sql", r"pl/sql"]),
    "MySQL": ("database", [r"mysql"]),
    "SQL Server": ("database", [r"sql server", r"mssql", r"ms sql"]),
    "PostgreSQL": ("database", [r"postgres(?:ql)?"]),
    "MongoDB": ("database", [r"mongo(?:db)?"]),
    "Redis": ("database", [r"redis"]),
    "Elasticsearch": ("database", [r"elastic ?search", r"opensearch"]),
    "DynamoDB": ("database", [r"dynamodb"]),
    "Database Design": ("database", [r"database design", r"schema design", r"data model(?:l)?ing"]),
    "Query Optimisation": ("database", [r"query optimi[sz]ation", r"indexing", r"query tuning"]),
    "Kafka": ("infra", [r"kafka"]),
    "RabbitMQ": ("infra", [r"rabbitmq"]),
    "Message Queues": ("infra", [r"message queues?", r"\bsqs\b", r"service bus", r"pub/?sub"]),
    "AWS": ("cloud", [r"\baws\b", r"amazon web services", r"\bec2\b", r"\bs3\b", r"lambda", r"\becs\b"]),
    "Azure": ("cloud", [r"azure"]),
    "GCP": ("cloud", [r"\bgcp\b", r"google cloud"]),
    "Docker": ("infra", [r"docker", r"containers?\b"]),
    "Kubernetes": ("infra", [r"kubernetes", r"\bk8s\b", r"\beks\b", r"\baks\b", r"\bgke\b"]),
    "Terraform": ("infra", [r"terraform", r"infrastructure as code", r"\biac\b"]),
    "CI/CD": ("infra", [r"ci/cd", r"ci ?/ ?cd", r"continuous integration", r"continuous delivery",
                        r"continuous deployment", r"pipelines?"]),
    "GitHub Actions": ("infra", [r"github actions"]),
    "Git": ("tool", [r"\bgit\b", r"github", r"gitlab", r"bitbucket"]),
    "Linux": ("infra", [r"linux", r"unix", r"bash"]),
    "REST APIs": ("api", [r"rest(?:ful)?(?: apis?| services| web services)?", r"\bapis?\b"]),
    "GraphQL": ("api", [r"graphql"]),
    "gRPC": ("api", [r"grpc"]),
    "OAuth": ("api", [r"oauth ?2?(?:\.0)?", r"openid connect", r"\boidc\b", r"\bjwt\b"]),
    "Swagger/OpenAPI": ("api", [r"swagger", r"openapi"]),
    "Microservices": ("architecture", [r"micro-?services?"]),
    "CQRS": ("architecture", [r"cqrs"]),
    "Event-Driven Architecture": ("architecture", [r"event[- ]driven", r"event sourcing"]),
    "Distributed Systems": ("architecture", [r"distributed systems?"]),
    "System Design": ("architecture", [r"system design", r"scalab(?:le|ility)"]),
    "Design Patterns": ("practice", [r"design patterns?", r"\bsolid\b"]),
    "Unit Testing": ("practice", [r"unit test(?:s|ing)?", r"xunit", r"nunit", r"pytest", r"jest", r"\btdd\b",
                                  r"test[- ]driven", r"automated tests?"]),
    "Agile": ("practice", [r"agile", r"scrum", r"kanban"]),
    "Code Review": ("practice", [r"code reviews?"]),
    "LLMs": ("ai", [r"\bllms?\b", r"large language models?", r"\bgpt(?:-\d)?\b", r"openai", r"claude",
                    r"llama", r"generative ai", r"gen ?ai"]),
    "RAG": ("ai", [r"\brag\b", r"retrieval[- ]augmented"]),
    "Prompt Engineering": ("ai", [r"prompt engineering", r"prompting"]),
    "LangChain": ("ai", [r"langchain", r"llamaindex", r"semantic kernel"]),
    "Vector Databases": ("ai", [r"vector (?:databases?|stores?|search)", r"embeddings?", r"pgvector",
                                r"pinecone", r"weaviate", r"qdrant"]),
    "Machine Learning": ("ai", [r"machine learning", r"\bml\b", r"pytorch", r"tensorflow", r"scikit"]),
    "Speech AI": ("ai", [r"whisper", r"speech[- ]to[- ]text", r"text[- ]to[- ]speech", r"elevenlabs"]),
    "Firebase": ("cloud", [r"firebase", r"\bfcm\b"]),
    "Power BI": ("tool", [r"power ?bi"]),
    "CRM": ("domain", [r"\bcrm\b", r"salesforce", r"dynamics 365"]),
    "RBAC": ("domain", [r"\brbac\b", r"role[- ]based access", r"access control"]),
    "Reporting/Dashboards": ("domain", [r"reporting", r"dashboards?"]),
    "Integrations": ("domain", [r"integrations?", r"third[- ]party apis?", r"webhooks?"]),
}

_COMPILED: dict[str, re.Pattern] = {}


def _boundary(p: str) -> str:
    # word-ish boundary that also works next to symbols (#, +, .)
    return r"(?<![A-Za-z0-9])(?:" + p + r")(?![A-Za-z0-9])"


for _name, (_cat, _pats) in SKILLS.items():
    _COMPILED[_name] = re.compile("|".join(_boundary(p) for p in _pats), re.IGNORECASE)

# Skills that are too generic to count as a "requirement" on their own when matched loosely.
GENERIC = {"REST APIs", "Git", "Agile", "Reporting/Dashboards", "Integrations", "SQL"}

# Implied skills: having the key implies familiarity with values (used for partial credit only).
IMPLIES = {
    "ASP.NET Core": {".NET", "C#", "Web API"},
    "ASP.NET MVC": {".NET", "C#"},
    "Entity Framework": {".NET", "SQL"},
    "MySQL": {"SQL"},
    "SQL Server": {"SQL"},
    "PostgreSQL": {"SQL"},
    "FastAPI": {"Python", "REST APIs"},
    "TypeScript": {"JavaScript"},
    "GitHub Actions": {"CI/CD", "Git"},
}

# Related skills that earn partial credit when the exact one is missing.
RELATED = {
    "PostgreSQL": {"MySQL", "SQL Server"},
    "SQL Server": {"MySQL", "PostgreSQL"},
    "MySQL": {"PostgreSQL", "SQL Server"},
    "Java": {"C#"},
    "Kotlin": {"C#", "Java"},
    "Go": {"C#", "Python"},
    "Node.js": {"JavaScript"},
    "React": {"JavaScript", "TypeScript"},
    "Angular": {"TypeScript"},
    "Vue": {"JavaScript"},
    "GCP": {"AWS", "Azure"},
    "Azure": {"AWS"},
    "AWS": {"Azure"},
    "Kubernetes": {"Docker"},
    "Django": {"FastAPI", "Python"},
    "Flask": {"FastAPI", "Python"},
    "Kafka": {"Message Queues"},
    "RabbitMQ": {"Message Queues"},
    "Spring": {"ASP.NET Core"},
}


def find_skills(text: str) -> dict[str, list[str]]:
    """Return {canonical_skill: [matched snippets]} found in text."""
    found: dict[str, list[str]] = {}
    if not text:
        return found
    for name, rx in _COMPILED.items():
        hits = [m.group(0) for m in rx.finditer(text)]
        if hits:
            found[name] = hits[:5]
    # Avoid double-reporting: "ASP.NET MVC" text also matches ASP.NET Core's bare "asp.net" pattern.
    if "ASP.NET MVC" in found and "ASP.NET Core" in found:
        if not re.search(r"asp\.net core|aspnet core", text, re.IGNORECASE):
            found.pop("ASP.NET Core")
    return found


def skill_in_text(skill: str, text: str) -> bool:
    rx = _COMPILED.get(skill)
    return bool(rx and rx.search(text or ""))


def category(skill: str) -> str:
    return SKILLS.get(skill, ("other", []))[0]
