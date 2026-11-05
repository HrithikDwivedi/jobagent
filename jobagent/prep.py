"""Skill-gap analysis and tailored interview preparation plans."""
from __future__ import annotations

TOPICS = {
    "C#": ["Value vs reference types, boxing", "async/await, Task vs Thread, deadlocks", "LINQ deferred execution",
           "Generics and constraints", "IDisposable / using", "Records, pattern matching"],
    ".NET": ["Dependency injection lifetimes (transient/scoped/singleton)", "Middleware pipeline", "Configuration "
             "& options pattern", "Garbage collection basics"],
    "ASP.NET Core": ["Middleware order", "Model binding & validation", "Filters", "Authentication vs authorisation, "
                     "JWT", "Global exception handling / ProblemDetails", "Caching & rate limiting"],
    "Web API": ["REST design, status codes", "Versioning", "Idempotency", "Pagination"],
    "Entity Framework": ["Tracking vs AsNoTracking", "N+1 problem & Include", "Migrations", "Concurrency tokens",
                         "IQueryable pitfalls"],
    "SQL": ["Joins", "Window functions", "Indexes (clustered vs non-clustered)", "Transactions & isolation levels",
            "Query plans", "Nth highest salary style questions"],
    "MySQL": ["InnoDB clustered PK", "EXPLAIN", "Composite indexes / leftmost prefix"],
    "PostgreSQL": ["MVCC basics", "EXPLAIN ANALYZE", "JSONB vs relational", "Differences from MySQL you should know"],
    "Python": ["Generators, decorators, context managers", "GIL & asyncio", "Type hints", "Data classes"],
    "FastAPI": ["Dependency injection", "Pydantic validation", "Async endpoints", "Background tasks"],
    "JavaScript": ["Closures & this", "Event loop, promises", "== vs ===", "Debounce/throttle"],
    "TypeScript": ["Interfaces vs types", "Generics", "Narrowing", "Utility types"],
    "React": ["Hooks & state", "Rendering & keys", "Effects & cleanup"],
    "Angular": ["Components & DI", "RxJS basics", "Change detection"],
    "AWS": ["EC2 vs ECS vs Lambda", "S3 & presigned URLs", "RDS Multi-AZ vs read replicas", "IAM roles", "VPC basics"],
    "Azure": ["App Service vs AKS vs Functions", "Azure SQL", "Managed identity"],
    "Docker": ["Image vs container", "Multi-stage Dockerfile", "Layers & caching", "Compose"],
    "Kubernetes": ["Pods, deployments, services", "Probes", "ConfigMaps/Secrets"],
    "CI/CD": ["Pipeline stages", "Blue-green vs canary", "Secrets in pipelines"],
    "Microservices": ["When to split a monolith", "Sync vs async communication", "Saga / outbox", "Observability"],
    "Message Queues": ["At-least-once delivery", "Idempotent consumers", "Dead-letter queues"],
    "Kafka": ["Topics, partitions, consumer groups", "Ordering guarantees"],
    "LLMs": ["Tokens & context windows", "Prompt design & structured output", "Hallucination mitigation",
             "Cost & latency control"],
    "RAG": ["Chunking & embeddings", "Retrieval quality & re-ranking", "Evaluation"],
    "REST APIs": ["Resource naming", "Status codes", "Error contracts", "Auth (OAuth2/JWT)"],
    "Unit Testing": ["Arrange-act-assert", "Mocks vs fakes", "Testing async code", "What to test"],
    "System Design": ["Scaling stateless services", "Caching strategies", "Database scaling", "Queues"],
    "Distributed Systems": ["CAP theorem", "Consistency models", "Retries, timeouts, circuit breakers"],
}
RESOURCES = {
    "C#": "https://learn.microsoft.com/dotnet/csharp/",
    ".NET": "https://learn.microsoft.com/dotnet/",
    "ASP.NET Core": "https://learn.microsoft.com/aspnet/core/",
    "Entity Framework": "https://learn.microsoft.com/ef/core/",
    "SQL": "https://use-the-index-luke.com/",
    "MySQL": "https://dev.mysql.com/doc/",
    "PostgreSQL": "https://www.postgresql.org/docs/current/tutorial.html",
    "Python": "https://docs.python.org/3/tutorial/",
    "FastAPI": "https://fastapi.tiangolo.com/tutorial/",
    "JavaScript": "https://developer.mozilla.org/docs/Web/JavaScript/Guide",
    "TypeScript": "https://www.typescriptlang.org/docs/handbook/intro.html",
    "React": "https://react.dev/learn",
    "Angular": "https://angular.dev/tutorials",
    "AWS": "https://aws.amazon.com/getting-started/",
    "Azure": "https://learn.microsoft.com/training/azure/",
    "Docker": "https://docs.docker.com/get-started/",
    "Kubernetes": "https://kubernetes.io/docs/tutorials/kubernetes-basics/",
    "Kafka": "https://kafka.apache.org/quickstart",
    "Go": "https://go.dev/tour/",
    "Java": "https://dev.java/learn/",
    "LLMs": "https://platform.openai.com/docs/guides/text",
    "RAG": "https://www.pinecone.io/learn/retrieval-augmented-generation/",
    "System Design": "https://github.com/donnemartin/system-design-primer",
    "Unit Testing": "https://learn.microsoft.com/dotnet/core/testing/",
    "Terraform": "https://developer.hashicorp.com/terraform/tutorials",
}
DSA_CORE = ["Arrays & hashing (two sum, duplicates)", "Two pointers & sliding window", "Stacks/queues (valid "
            "parentheses)", "Binary search", "Trees (traversals, BST validation)", "BFS/DFS on graphs/grids",
            "Big-O analysis of your own solutions"]
BEHAVIORAL = [
    "Tell me about yourself (2 minutes, end with why this role/country).",
    "Walk me through the RBAC migration that cut onboarding time by 40%: what you owned, how you measured it.",
    "Describe the Zoom-to-Vimeo pipeline: failures, retries, what you'd improve.",
    "A disagreement in code review and how you resolved it.",
    "A production bug you debugged end-to-end.",
    "Why relocate, and how do you handle the visa timeline? (Be factual: you require sponsorship.)",
]


def build_plan(job: dict, score: dict, analysis: dict) -> dict:
    have, partial, missing = [], [], []
    for d in score.get("must_have", []) + score.get("nice_to_have", []):
        (have if d["credit"] >= 1 else partial if d["credit"] >= 0.35 else missing).append(d)
    must_names = {d["skill"] for d in score.get("must_have", [])}
    ordered = sorted(missing, key=lambda d: d["skill"] not in must_names) + \
        sorted(partial, key=lambda d: d["skill"] not in must_names)
    study = []
    for d in ordered:
        sk = d["skill"]
        study.append({
            "skill": sk, "priority": "high" if sk in must_names else "medium",
            "why": ("Required" if sk in must_names else "Nice to have") + f"; {d['why']}.",
            "topics": TOPICS.get(sk, [f"Core concepts of {sk}", f"How {sk} compares to what you know"]),
            "resource": RESOURCES.get(sk),
            "practice": f"Build a small feature using {sk} and be ready to explain trade-offs honestly; "
                        "do not claim production experience you do not have.",
        })
    interview = []
    for d in have:
        if d["skill"] in TOPICS:
            interview.append({"skill": d["skill"], "topics": TOPICS[d["skill"]]})
    text = (job.get("description_text") or "").lower()
    sd = ["Design the main entity CRUD API for this product with pagination and auth",
          "Scale a read-heavy endpoint with caching", "Process work asynchronously with a queue and retries"]
    if "payment" in text:
        sd.append("Idempotent payment API")
    if "notification" in text or "messaging" in text:
        sd.append("Notification service (email/push) with retries")
    assessment = []
    if any(w in text for w in ("take-home", "take home", "coding challenge", "assignment", "hackerrank", "codility")):
        assessment.append("Expect a take-home / online coding test: practise timed problems and clean code with tests.")
    return {
        "demonstrated": [d["skill"] for d in have],
        "partial": [d["skill"] for d in partial],
        "missing": [d["skill"] for d in missing],
        "study_plan": study,
        "interview_topics": interview,
        "dsa": DSA_CORE,
        "system_design": sd if (score.get("seniority") in ("mid", "unspecified") or "system design" in text) else sd[:2],
        "behavioral": BEHAVIORAL,
        "assessment": assessment,
        "languages": [l for l in analysis.get("languages", []) if l["language"] != "English"],
    }
