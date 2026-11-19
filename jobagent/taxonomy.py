"""Industries, roles and experience levels for every kind of job seeker (not only software engineers).

* `classify(title, hints)` puts a job into (industry, role) from its title, falling back to the source's own
  category (Jobicy industry, Adzuna category, Greenhouse department, ...). Pure regex; no AI, no network.
* Users choose up to 3 industries and up to 3 roles in onboarding; discovery fetches and keeps jobs for the union of
  everyone's roles, and scoring rewards jobs in the user's own roles.
* `persona()` labels a user for the administrator ("Early-career · Healthcare · Nurse", career switcher, ...)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .normalize import normalize_title


@dataclass(frozen=True)
class Role:
    id: str
    label: str
    pattern: str                  # regex on the normalised (lower-case) title
    search: str = ""              # keyword query for search APIs (default: label)

    @property
    def query(self) -> str:
        return self.search or self.label


@dataclass(frozen=True)
class Industry:
    id: str
    label: str
    icon: str
    blurb: str
    roles: tuple[Role, ...]
    jobicy: tuple[str, ...] = ()                  # Jobicy industry slugs
    hints: tuple[str, ...] = field(default=())    # words in a source category/department that mean this industry


def R(id_, label, pattern, search=""):
    return Role(id_, label, pattern, search)


INDUSTRIES: tuple[Industry, ...] = (
    Industry("technology", "Technology & Software", "studio", "Software, data, cloud, IT and cybersecurity", (
        R("software_engineer", "Software Engineer / Developer",
          r"\b(software|backend|back end|frontend|front end|full ?stack|web|mobile|ios|android|\.net|java|python|"
          r"golang|node|react|application|applications|platform|integration|ai|ml|machine learning|llm|product)\b.*"
          r"\b(engineer|developer|programmer)\b|\b(developer|programmer|sde|swe|entwickler)\b|forward deployed",
          "software engineer"),
        R("data_ai", "Data & AI (analyst, scientist, engineer)",
          r"\bdata (scientist|engineer|analyst|architect)|\banalytics engineer|\bmachine learning|\bml engineer|"
          r"\bai (engineer|scientist|researcher)|\bbusiness intelligence|\bbi (developer|analyst)|\bdata science",
          "data analyst"),
        R("devops_cloud", "DevOps, Cloud & Infrastructure",
          r"\bdevops|\bsite reliability|\bsre\b|\bcloud (engineer|architect)|\binfrastructure engineer|"
          r"\bplatform engineer|\bsystems? (engineer|administrator)|\bsysadmin|\bnetwork engineer", "devops engineer"),
        R("qa_testing", "QA & Test Automation", r"\b(qa|quality assurance|test|testing|sdet)\b.*\b(engineer|analyst|"
          r"lead|automation|specialist)|\btester\b", "qa engineer"),
        R("it_support", "IT Support & Helpdesk", r"\b(it support|help ?desk|service desk|desktop support|"
          r"technical support|it technician|it specialist|it administrator)\b", "it support"),
        R("cybersecurity", "Cybersecurity", r"\b(security|cyber|soc|penetration|infosec|appsec)\b.*\b(engineer|analyst|"
          r"specialist|architect|consultant|tester)|\bcybersecurity\b", "cyber security"),
        R("product_manager", "Product Manager", r"\bproduct (manager|owner|lead)\b|\bhead of product\b",
          "product manager"),
    ), jobicy=("engineering", "data-science", "admin", "qa-testing", "technical-support", "cybersecurity"),
        hints=("engineering", "software", "technology", "it ", "data", "devops", "security", "r&d", "product")),
    Industry("finance", "Finance, Banking & Accounting", "money", "Accounting, audit, banking, investment and tax", (
        R("accountant", "Accountant", r"\baccount(ant|ing)\b(?!.*\b(manager|executive)\b.*\bsales)|\bbookkeep|"
          r"\baccounts (payable|receivable|assistant)|\bchartered accountant|\bca\b", "accountant"),
        R("financial_analyst", "Financial Analyst / FP&A", r"\bfinanc(e|ial) (analyst|planning|controller|manager)|"
          r"\bfp&?a\b|\bcontroller\b|\bfinance business partner", "financial analyst"),
        R("audit_tax", "Audit & Tax", r"\b(audit|auditor|tax|taxation)\b", "auditor"),
        R("banking", "Banking & Credit", r"\b(bank|banking|banker|credit|loan|mortgage|teller|relationship manager|"
          r"kyc|aml|anti money)\b", "banking"),
        R("investment", "Investment & Risk", r"\b(investment|portfolio|equity|asset management|trader|trading|"
          r"risk (analyst|manager)|actuar|wealth|treasury)\b", "investment analyst"),
        R("payroll", "Payroll", r"\bpayroll\b", "payroll"),
    ), jobicy=("accounting-finance",), hints=("finance", "accounting", "banking", "tax", "audit", "treasury")),
    Industry("sales", "Sales & Business Development", "target", "Selling, account management and partnerships", (
        R("sales_executive", "Sales Executive / Account Executive", r"\b(account executive|sales (executive|"
          r"representative|rep|associate|consultant|specialist)|inside sales|field sales|ae\b)", "sales executive"),
        R("business_development", "Business Development", r"\bbusiness development|\bbdr\b|\bsdr\b|\bsales "
          r"development|\bpartnerships?\b|\blead generation", "business development"),
        R("account_manager", "Account Manager", r"\b(key |strategic |client )?account manager\b|\bclient manager",
          "account manager"),
        R("sales_manager", "Sales Manager", r"\b(sales|regional sales|area sales|territory) (manager|lead|director|"
          r"head)\b|\bhead of sales\b", "sales manager"),
        R("presales", "Pre-sales / Solutions Consultant", r"\b(pre ?sales|solutions? (consultant|engineer)|sales "
          r"engineer)\b", "solutions consultant"),
    ), jobicy=("seller", "business"), hints=("sales", "business development", "revenue", "partnership")),
    Industry("marketing", "Marketing, Media & Communications", "send", "Digital marketing, content, PR and brand", (
        R("digital_marketing", "Digital / Performance Marketing", r"\b(digital|performance|growth|paid|ppc|"
          r"email|crm|lifecycle|ecommerce) marketing|\bmarketing (executive|specialist|manager|coordinator|associate|"
          r"lead)|\bgrowth (manager|marketer)", "digital marketing"),
        R("content_writer", "Content Writer / Copywriter", r"\b(content|copy) ?(writer|editor|strategist|manager)|"
          r"\bcopywriter|\bjournalist|\beditor\b|\btechnical writer", "content writer"),
        R("social_media", "Social Media", r"\bsocial media|\bcommunity manager|\binfluencer", "social media"),
        R("seo", "SEO / SEM", r"\bseo\b|\bsem\b|\bsearch engine", "seo specialist"),
        R("pr_comms", "PR & Communications", r"\b(public relations|pr |communications?|comms|media relations)\b",
          "communications"),
        R("brand_manager", "Brand & Product Marketing", r"\b(brand|product marketing|marketing) manager\b|\bbrand\b",
          "brand manager"),
    ), jobicy=("marketing", "copywriting", "seo"), hints=("marketing", "communications", "media", "brand", "content")),
    Industry("healthcare", "Healthcare & Life Sciences", "shield-check", "Clinical, nursing, pharma and research", (
        R("nurse", "Nurse", r"\bnurs(e|ing)\b|\bmidwife|\brn\b", "nurse"),
        R("doctor", "Doctor / Physician", r"\b(doctor|physician|surgeon|gp\b|general practitioner|consultant "
          r"(physician|psychiatrist)|registrar|medical officer|dentist|psychiatrist)\b", "doctor"),
        R("pharmacist", "Pharmacist", r"\bpharmac(ist|y)\b", "pharmacist"),
        R("allied_health", "Allied Health (physio, OT, therapist)", r"\b(physiotherap|occupational therap|"
          r"therapist|radiograph|dietitian|paramedic|optometrist|psycholog|counsell?or|speech)", "physiotherapist"),
        R("clinical_research", "Clinical Research & Lab", r"\b(clinical (research|trial|data)|laboratory|lab "
          r"(technician|scientist)|scientist|research associate|biolog|chemist|cra\b|pharmacovigilance|regulatory "
          r"affairs|drug safety)", "clinical research"),
        R("care_assistant", "Care & Support Worker", r"\b(care (assistant|worker)|support worker|healthcare "
          r"assistant|caregiver|carer)\b", "care assistant"),
        R("medical_admin", "Medical Administration", r"\b(medical (secretary|receptionist|coder|billing)|"
          r"patient (coordinator|services)|practice manager|health information)\b", "medical administrator"),
    ), jobicy=("healthcare",), hints=("health", "medical", "clinical", "pharma", "nursing", "life science")),
    Industry("education", "Education & Training", "prep", "Teaching, tutoring, academia and e-learning", (
        R("teacher", "Teacher", r"\bteacher\b|\bteaching\b|\beducator\b", "teacher"),
        R("tutor", "Tutor / Trainer", r"\btutor\b|\btrainer\b|\bcoach\b|\binstructor\b", "tutor"),
        R("lecturer", "Lecturer / Academic", r"\b(lecturer|professor|academic|postdoc|research fellow|teaching "
          r"assistant)\b", "lecturer"),
        R("instructional_designer", "Instructional Designer / E-learning", r"\b(instructional design|e ?learning|"
          r"curriculum|learning (designer|experience))", "instructional designer"),
        R("education_admin", "School Administration & Counselling", r"\b(school|admissions|student|academic) "
          r"(administrator|counsell?or|advisor|coordinator|services)|\bprincipal\b", "education administrator"),
    ), jobicy=("education",), hints=("education", "teaching", "school", "learning", "academic")),
    Industry("hr", "Human Resources & Recruitment", "users", "Recruiting, HR, people operations and L&D", (
        R("recruiter", "Recruiter / Talent Acquisition", r"\brecruit(er|ment|ing)\b|\btalent (acquisition|partner|"
          r"sourcer)|\bsourcer\b|\bheadhunter", "recruiter"),
        R("hr_generalist", "HR Generalist / Executive", r"\b(hr|human resources|people) (generalist|executive|"
          r"officer|coordinator|assistant|specialist|administrator|advisor|operations)", "hr executive"),
        R("hr_business_partner", "HR Business Partner / Manager", r"\b(hr|human resources|people) (business partner|"
          r"manager|lead|director)|\bhrbp\b|\bhead of people", "hr business partner"),
        R("learning_development", "Learning & Development", r"\blearning (and|&) development|\bl&d\b|\btraining "
          r"(manager|coordinator|specialist)", "learning and development"),
        R("compensation", "Compensation, Benefits & Payroll", r"\b(compensation|benefits|reward|total rewards)\b",
          "compensation and benefits"),
    ), jobicy=("hr",), hints=("human resources", "people", "talent", "recruit", "hr")),
    Industry("operations", "Operations, Supply Chain & Logistics", "globe", "Operations, procurement, logistics", (
        R("operations_manager", "Operations Manager / Analyst", r"\boperations? (manager|analyst|associate|lead|"
          r"coordinator|specialist|executive|director)|\bbusiness operations", "operations manager"),
        R("supply_chain", "Supply Chain & Planning", r"\bsupply chain|\bdemand plann|\binventory|\bplanner\b|\bs&op",
          "supply chain"),
        R("procurement", "Procurement & Purchasing", r"\b(procurement|purchasing|buyer|sourcing|category manager|"
          r"vendor management)\b", "procurement"),
        R("logistics", "Logistics, Warehouse & Transport", r"\b(logistics|warehouse|fleet|transport|shipping|"
          r"dispatch|freight|distribution|driver|courier)\b", "logistics"),
    ), jobicy=("management",), hints=("operations", "supply chain", "logistics", "procurement")),
    Industry("customer_service", "Customer Service & Success", "mail", "Customer support, success and call centres", (
        R("customer_support", "Customer Support / Service", r"\bcustomer (support|service|care|experience)\b|"
          r"\bsupport (agent|specialist|representative|associate)|\bcall cent(er|re)|\bcontact cent(er|re)",
          "customer service"),
        R("customer_success", "Customer Success Manager", r"\bcustomer success|\bclient success|\bonboarding "
          r"(specialist|manager)|\bimplementation (specialist|consultant)", "customer success"),
    ), jobicy=("supporting",), hints=("customer", "support", "success", "service")),
    Industry("design", "Design & Creative", "sparkle", "UX/UI, graphic, motion and video", (
        R("ux_ui_designer", "UX / UI / Product Designer", r"\b(ux|ui|product|interaction|user experience|web) "
          r"designer|\bux (researcher|writer)|\bdesign lead", "ux designer"),
        R("graphic_designer", "Graphic / Visual Designer", r"\b(graphic|visual|brand|creative|motion) designer|"
          r"\billustrator|\bart director|\bcreative director", "graphic designer"),
        R("video_media", "Video, Photo & Media Production", r"\b(video|film|photograph|animator|animation|editor "
          r"\(video\)|producer|multimedia|3d artist)", "video editor"),
    ), jobicy=("design-multimedia", "web-app-design"), hints=("design", "creative", "ux")),
    Industry("engineering", "Engineering & Manufacturing", "settings", "Mechanical, electrical, civil and production", (
        R("mechanical", "Mechanical Engineer", r"\bmechanical\b|\bmechatronic|\bhvac\b|\bdesign engineer",
          "mechanical engineer"),
        R("electrical", "Electrical / Electronics Engineer", r"\b(electrical|electronics?|hardware|embedded|"
          r"firmware|rf|pcb|fpga|asic)\b.*\bengineer", "electrical engineer"),
        R("civil", "Civil / Structural Engineer", r"\b(civil|structural|geotechnical|site engineer|quantity "
          r"surveyor|construction)\b", "civil engineer"),
        R("manufacturing", "Manufacturing & Production", r"\b(manufacturing|production|process|industrial|plant|"
          r"maintenance)\b.*\b(engineer|manager|supervisor|technician|operator)|\btechnician\b", "production engineer"),
        R("quality", "Quality & Safety (QA/QC, HSE)", r"\b(quality (engineer|manager|inspector|control)|qc\b|hse\b|"
          r"health and safety|safety (officer|engineer|specialist))", "quality engineer"),
    ), hints=("manufacturing", "hardware", "mechanical", "electrical", "industrial")),
    Industry("legal", "Legal & Compliance", "lock", "Law, paralegal, compliance and contracts", (
        R("lawyer", "Lawyer / Solicitor / Counsel", r"\b(lawyer|solicitor|attorney|counsel|advocate|barrister|"
          r"legal (director|manager|advisor))\b", "lawyer"),
        R("paralegal", "Paralegal / Legal Assistant", r"\b(paralegal|legal (assistant|secretary|executive|"
          r"associate|analyst)|contracts? (manager|specialist|administrator))\b", "paralegal"),
        R("compliance", "Compliance & Risk", r"\b(compliance|regulatory|governance|privacy|data protection|grc)\b",
          "compliance"),
    ), jobicy=("legal",), hints=("legal", "compliance", "law")),
    Industry("admin", "Administration & Office", "applications", "Admin, assistants, office and data entry", (
        R("admin_assistant", "Administrative Assistant", r"\b(admin(istrative)?|office) (assistant|coordinator|"
          r"executive|officer|administrator)|\breceptionist|\bsecretary\b", "administrative assistant"),
        R("executive_assistant", "Executive / Personal Assistant", r"\b(executive|personal|virtual) assistant|"
          r"\bpa to\b|\bchief of staff", "executive assistant"),
        R("office_manager", "Office / Facilities Manager", r"\b(office|facilities|workplace) manager", "office manager"),
        R("data_entry", "Data Entry & Back Office", r"\bdata entry|\bback office|\bclerk\b|\btypist|"
          r"\bdocument controller", "data entry"),
    ), jobicy=("admin-support",), hints=("administration", "admin", "office")),
    Industry("hospitality", "Hospitality, Retail & Tourism", "pin", "Hotels, restaurants, retail stores and travel", (
        R("retail", "Retail Sales / Store Staff", r"\b(retail|store|shop|cashier|merchandis|sales assistant|"
          r"sales associate)\b", "retail assistant"),
        R("store_manager", "Store / Restaurant Manager", r"\b(store|shop|restaurant|branch|outlet) manager|"
          r"\bassistant manager\b", "store manager"),
        R("chef_kitchen", "Chef & Kitchen", r"\b(chef|cook|kitchen|baker|barista|sous)\b", "chef"),
        R("hotel_travel", "Hotel, Travel & Events", r"\b(hotel|front desk|front office|concierge|housekeep|"
          r"guest (relations|services)|travel|tour|event|events|cabin crew|flight attendant)\b", "hotel"),
    ), hints=("hospitality", "retail", "travel", "restaurant", "hotel")),
    Industry("business", "Business, Consulting & Management", "chart", "Business analysis, consulting, projects", (
        R("business_analyst", "Business Analyst", r"\bbusiness (analyst|systems analyst)|\bfunctional (analyst|"
          r"consultant)", "business analyst"),
        R("consultant", "Management / Strategy Consultant", r"\b(management|strategy|business|associate) "
          r"consultant|\bconsulting\b|\bstrategy (analyst|manager|associate)", "management consultant"),
        R("project_manager", "Project / Program Manager", r"\b(project|program|programme|delivery|pmo) (manager|"
          r"coordinator|lead|officer|director)|\bscrum master\b", "project manager"),
        R("general_manager", "General / Senior Management", r"\b(general manager|managing director|ceo|coo|cfo|"
          r"chief [a-z]+ officer|vice president|vp\b|country manager)", "general manager"),
    ), jobicy=("project-management", "management"), hints=("consulting", "strategy", "project", "management")),
)

BY_ID = {i.id: i for i in INDUSTRIES}
ROLES = {r.id: (ind.id, r) for ind in INDUSTRIES for r in ind.roles}
_COMPILED = [(ind.id, r.id, re.compile(r.pattern)) for ind in INDUSTRIES for r in ind.roles]
# precedence for overlapping titles: most specific first (e.g. "Sales Engineer" is pre-sales, not software)
_ORDER = ["presales", "store_manager", "product_manager", "data_ai", "devops_cloud", "qa_testing", "cybersecurity", "it_support",
          "ux_ui_designer", "electrical", "mechanical", "civil", "quality", "manufacturing", "software_engineer"]

EXPERIENCE = {
    "student": ("Student / Intern", "Internships, graduate schemes and part-time roles"),
    "entry": ("Entry level (0-2 years)", "Graduate, junior and associate roles"),
    "mid": ("Mid level (2-5 years)", "Experienced individual contributors"),
    "senior": ("Senior (5-10 years)", "Senior, specialist and lead roles"),
    "manager": ("Manager / Lead", "People managers and team leads"),
    "executive": ("Director / Executive", "Director, head of, VP and C-level"),
}
EMPLOYMENT_TYPES = {"full_time": "Full-time", "part_time": "Part-time", "contract": "Contract / Freelance",
                    "internship": "Internship"}
# title words that are WRONG for each level (used for excluded_title_terms)
LEVEL_EXCLUDES = {
    "student": ["senior", "sr.", "staff", "principal", "lead", "head of", "director", "manager", "vp ", "chief"],
    "entry": ["senior", "sr.", "staff", "principal", "lead", "head of", "director", "vp ", "chief"],
    "mid": ["intern", "internship", "graduate", "staff", "principal", "head of", "director", "vp ", "chief"],
    "senior": ["intern", "internship", "graduate", "junior", "trainee", "apprentice"],
    "manager": ["intern", "internship", "graduate", "junior", "trainee", "apprentice", "assistant"],
    "executive": ["intern", "internship", "graduate", "junior", "trainee", "apprentice", "assistant", "associate"],
}
# job seniority (normalize.seniority) -> how well it suits each experience level
LEVEL_FIT = {
    "student": {"intern": 1.0, "junior": 0.9, "unspecified": 0.6, "mid": 0.2},
    "entry": {"junior": 1.0, "unspecified": 0.85, "intern": 0.4, "mid": 0.55},
    "mid": {"mid": 1.0, "unspecified": 0.85, "junior": 0.6, "senior": 0.5},
    "senior": {"senior": 1.0, "staff": 0.8, "lead": 0.85, "unspecified": 0.8, "mid": 0.6, "principal": 0.6},
    "manager": {"lead": 1.0, "senior": 0.8, "staff": 0.7, "principal": 0.7, "unspecified": 0.75, "mid": 0.4},
    "executive": {"principal": 0.9, "lead": 0.8, "unspecified": 0.7, "senior": 0.5},
}
MANAGER_TITLE = re.compile(r"\b(manager|head of|director|vp|vice president|chief|lead)\b")


def industry_label(iid: str | None) -> str:
    return BY_ID[iid].label if iid in BY_ID else "Other"


def role_label(rid: str | None) -> str:
    return ROLES[rid][1].label if rid in ROLES else (rid or "")


def classify(title: str | None, hints: list[str] | tuple[str, ...] = ()) -> tuple[str | None, str | None]:
    """(industry_id, role_id) for a job title; role None when only the industry is known (from hints)."""
    t = normalize_title(title)
    if t:
        hits = [(ind, rid) for ind, rid, rx in _COMPILED if rx.search(t)]
        if hits:
            hits.sort(key=lambda h: _ORDER.index(h[1]) if h[1] in _ORDER else len(_ORDER))
            return hits[0]
    for h in list(hints or ()) + ([t] if t else []):          # the title's own words come last
        hl = f" {str(h).lower()} "
        for ind in INDUSTRIES:
            if any(w in hl for w in ind.hints) or any(s.replace("-", " ") in hl for s in ind.jobicy):
                return ind.id, None
    return None, None


def matches_custom(title: str | None, custom_roles: list[str]) -> bool:
    t = normalize_title(title)
    return any(c and normalize_title(c) in t for c in custom_roles or [])


# ---------------------------------------------------------------- per-user profile helpers
def career_scope(career: dict | None) -> dict:
    c = career or {}
    return {"industries": set(c.get("industries") or []), "roles": set(c.get("roles") or []),
            "custom": [x for x in (c.get("custom_roles") or []) if x]}


def has_career(career: dict | None) -> bool:
    c = career or {}
    return bool(c.get("industries") and (c.get("roles") or c.get("custom_roles")))


def job_fit(career: dict, industry: str | None, role: str | None, title: str | None) -> tuple[float, str]:
    """Role alignment 0..1 with an explanation, for a user with a chosen career profile."""
    sc = career_scope(career)
    if role and role in sc["roles"]:
        return 1.0, f"Matches your chosen role: {role_label(role)}."
    if sc["custom"] and matches_custom(title, sc["custom"]):
        return 1.0, "Matches a role you typed in your preferences."
    if industry and industry in sc["industries"]:
        return (0.55 if role else 0.6), f"In your industry ({industry_label(industry)}), different role."
    return 0.1, "Outside the industries and roles you chose."


def preferred_role_terms(career: dict) -> list[str]:
    """Lower-case title fragments for the legacy `preferred_roles` setting."""
    out = []
    for rid in career.get("roles") or []:
        if rid in ROLES:
            out.append(ROLES[rid][1].query.lower())
    out += [normalize_title(c) for c in career.get("custom_roles") or [] if c]
    return list(dict.fromkeys(x for x in out if x))[:20]


def excluded_terms_for(level: str | None, career: dict) -> list[str]:
    """Title words to exclude for this experience level, minus words that are part of the chosen roles
    ("Store Manager" must never be excluded by "manager")."""
    terms = list(LEVEL_EXCLUDES.get(level or "", []))
    chosen = " ".join([role_label(r).lower() for r in career.get("roles") or []] +
                      [c.lower() for c in career.get("custom_roles") or []])
    return [t for t in terms if t.strip(" .") not in chosen]


def suggest_from_resume(structured: dict | None) -> dict:
    """Industries/roles suggested from the resume's job titles (most recent first)."""
    inds: list[str] = []
    roles: list[str] = []
    for e in (structured or {}).get("experience") or []:
        if not isinstance(e, dict):
            continue
        ind, rid = classify(e.get("title"))
        if ind and ind not in inds:
            inds.append(ind)
        if rid and rid not in roles:
            roles.append(rid)
    head = (structured or {}).get("headline") or (structured or {}).get("title")
    if head and not roles:
        ind, rid = classify(head)
        if ind:
            inds.append(ind)
        if rid:
            roles.append(rid)
    return {"industries": inds[:3], "roles": roles[:3]}


def persona(career: dict | None, *, resume_industry: str | None = None, needs_sponsorship: bool | None = None,
            remote_only: bool = False, inferred_from: list[str] | None = None) -> dict:
    """A short, human label for the administrator plus tags. Works for users who skipped onboarding too
    (inferred from their preferred role titles)."""
    c = career or {}
    inds = list(c.get("industries") or [])
    roles = list(c.get("roles") or [])
    inferred = False
    if not inds and inferred_from:
        for t in inferred_from:
            ind, rid = classify(t)
            if ind and ind not in inds:
                inds.append(ind)
            if rid and rid not in roles:
                roles.append(rid)
        inferred = bool(inds)
    level = c.get("experience")
    lvl = {"student": "Student", "entry": "Early-career", "mid": "Mid-level", "senior": "Senior",
           "manager": "Manager", "executive": "Executive"}.get(level or "", "")
    primary_role = role_label(roles[0]) if roles else ((c.get("custom_roles") or [None])[0] or "")
    primary_ind = inds[0] if inds else None
    parts = [p for p in (lvl, industry_label(primary_ind) if primary_ind else "", primary_role.split(" /")[0]) if p]
    tags = []
    if resume_industry and primary_ind and resume_industry not in inds:
        tags.append("Career switcher")
    if len(inds) > 1:
        tags.append("Multi-industry")
    if needs_sponsorship:
        tags.append("Needs visa sponsorship")
    if remote_only:
        tags.append("Remote only")
    if inferred:
        tags.append("Inferred")
    if not inds:
        return {"label": "Not set up yet", "industry": None, "tags": tags or ["No preferences"], "level": level}
    return {"label": " · ".join(parts), "industry": primary_ind, "tags": tags, "level": level}


def job_scope_sql(career: dict | None, alias: str = "j") -> tuple[str, list]:
    """SQL (" AND ...") limiting jobs to the user's industries/roles (jobs not yet classified always count).
    Users without a career profile keep the original software-engineering scope."""
    if not has_career(career):
        return (f" AND ({alias}.industry IS NULL OR {alias}.industry='technology' OR {alias}.role_key='presales')", [])
    inds = list((career or {}).get("industries") or [])
    roles = list((career or {}).get("roles") or [])
    conds = [f"{alias}.industry IS NULL"]
    params: list = []
    if inds:
        conds.append(f"{alias}.industry IN ({','.join('?' * len(inds))})")
        params += inds
    if roles:
        conds.append(f"{alias}.role_key IN ({','.join('?' * len(roles))})")
        params += roles
    for c in (career or {}).get("custom_roles") or []:
        conds.append(f"LOWER({alias}.title) LIKE ?")
        params.append(f"%{c.lower()}%")
    return " AND (" + " OR ".join(conds) + ")", params
