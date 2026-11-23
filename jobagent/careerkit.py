"""What to learn next, per role: core skills employers ask for, and industry-recognised courses/certifications.

* Only established, widely recognised credentials (professional bodies, vendor certifications, accredited
  institutes and well-known university/industry programmes) — no paid "course mills".
* Every course links to the provider's own official page.
* `level`: "start" suits students and freshers; "experienced" usually needs work experience before you can sit it
  (shown later in a career, with a note).
* `cost`: "free" (learning and certificate/badge free), "free_learn" (free to learn; the certificate or exam has a
  fee, e.g. SWAYAM/NPTEL, edX/Coursera audit), "paid". Pages show Free and Paid in separate sections.
* `skill_plan()` combines three signals: the role's core skills, which of them the resume already shows, and the
  required skills that the user's scored jobs say are missing ("resume score" gaps)."""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import settings as settings_mod
from . import taxonomy
from .db import Database, loads


@dataclass(frozen=True)
class Course:
    name: str
    provider: str
    url: str
    level: str = "start"          # start | experienced
    note: str = ""
    cost: str = "paid"            # free | free_learn | paid


def C(name, provider, url, level="start", note="", cost="paid"):
    return Course(name, provider, url, level, note, cost)


def F(name, provider, url, note="", cost="free"):
    """A free course (cost="free_learn": free to learn, the certificate has a fee)."""
    return Course(name, provider, url, "start", note, cost)


# ----------------------------------------------------------------------------------------------- shared courses
GOOGLE_DATA = C("Google Data Analytics Professional Certificate", "Google · Coursera",
                "https://www.coursera.org/professional-certificates/google-data-analytics")
GOOGLE_PM = C("Google Project Management Professional Certificate", "Google · Coursera",
              "https://www.coursera.org/professional-certificates/google-project-management")
GOOGLE_IT = C("Google IT Support Professional Certificate", "Google · Coursera",
              "https://www.coursera.org/professional-certificates/google-it-support")
GOOGLE_UX = C("Google UX Design Professional Certificate", "Google · Coursera",
              "https://www.coursera.org/professional-certificates/google-ux-design")
GOOGLE_DM = C("Google Digital Marketing & E-commerce Certificate", "Google · Coursera",
              "https://www.coursera.org/professional-certificates/google-digital-marketing-ecommerce")
GOOGLE_SKILLSHOP = C("Google Ads & Google Analytics certifications (free)", "Google Skillshop",
                     "https://skillshop.withgoogle.com/", cost="free")
META_SMM = C("Meta Social Media Marketing Professional Certificate", "Meta · Coursera",
             "https://www.coursera.org/professional-certificates/facebook-social-media-marketing")
HUBSPOT_INBOUND = C("Inbound Marketing Certification (free)", "HubSpot Academy",
                    "https://academy.hubspot.com/courses/inbound-marketing", cost="free")
HUBSPOT_CONTENT = C("Content Marketing Certification (free)", "HubSpot Academy",
                    "https://academy.hubspot.com/courses/content-marketing", cost="free")
HUBSPOT_SALES = C("Inbound Sales Certification (free)", "HubSpot Academy",
                  "https://academy.hubspot.com/courses/inbound-sales", cost="free")
SEMRUSH = C("SEO Fundamentals and SEO Toolkit courses (free)", "Semrush Academy", "https://www.semrush.com/academy/",
            cost="free")
PMP = C("Project Management Professional (PMP)", "PMI", "https://www.pmi.org/certifications/project-management-pmp",
        "experienced", "Needs 3+ years of project experience.")
CAPM = C("Certified Associate in Project Management (CAPM)", "PMI",
         "https://www.pmi.org/certifications/certified-associate-capm")
PSM = C("Professional Scrum Master I (PSM I)", "Scrum.org",
        "https://www.scrum.org/assessments/professional-scrum-master-i-certification")
PSPO = C("Professional Scrum Product Owner I (PSPO I)", "Scrum.org",
         "https://www.scrum.org/assessments/professional-scrum-product-owner-i-certification")
SIX_SIGMA = C("Six Sigma Green Belt (CSSGB)", "ASQ", "https://www.asq.org/cert/six-sigma-green-belt", "experienced",
              "Usually taken with a few years of process work.")
ITIL = C("ITIL 4 Foundation", "PeopleCert / AXELOS", "https://www.axelos.com/certifications/itil-service-management")
MOS = C("Microsoft Office Specialist (Excel, Word)", "Microsoft · Certiport",
        "https://certiport.pearsonvue.com/Certifications/Microsoft/MOS/Overview")
ADOBE_ACP = C("Adobe Certified Professional (Photoshop, Illustrator, Premiere Pro)", "Adobe · Certiport",
              "https://certiport.pearsonvue.com/Certifications/Adobe/ACP/Overview")
AUTODESK = C("Autodesk Certified User / Professional (AutoCAD, Revit, Fusion)", "Autodesk",
             "https://www.autodesk.com/certification")
BLS = C("Basic Life Support (BLS) / ACLS", "American Heart Association", "https://cpr.heart.org/")
GCP = C("Good Clinical Practice (free)", "NIDA Clinical Trials Network", "https://gcp.nihtraining.com/",
        cost="free")
NRF_RISE = C("RISE Up retail credentials", "NRF Foundation", "https://nrffoundation.org/riseup")
UAL_SHORT = C("Fashion short courses (styling, buying, visual merchandising, design)",
              "London College of Fashion · UAL", "https://www.arts.ac.uk/study-at-ual/short-courses")
NIFT_CE = C("Continuing Education programmes (fashion styling, merchandising, design)", "NIFT (India)",
            "https://www.nift.ac.in/")
PEARL = C("Short courses in fashion styling, styling for media and design", "Pearl Academy (India)",
          "https://pearlacademy.com/")
CIPD = C("CIPD Level 3 / Level 5 in People Management", "CIPD", "https://www.cipd.org/uk/learning/qualifications/")
SHRM = C("SHRM Certified Professional (SHRM-CP)", "SHRM", "https://www.shrm.org/credentials/certification",
         "experienced", "Eligibility depends on HR experience; students can take it in their final year.")
CIPS = C("CIPS Diploma in Procurement and Supply", "CIPS", "https://www.cips.org/qualifications")
ASCM_CPIM = C("Certified in Planning and Inventory Management (CPIM)", "ASCM",
              "https://www.ascm.org/learning-development/certifications-credentials/cpim/")
ASCM_CSCP = C("Certified Supply Chain Professional (CSCP)", "ASCM",
              "https://www.ascm.org/learning-development/certifications-credentials/cscp/", "experienced")
ASCM_CLTD = C("Certified in Logistics, Transportation and Distribution (CLTD)", "ASCM",
              "https://www.ascm.org/learning-development/certifications-credentials/cltd/", "experienced")
IIBA = C("Entry / Certification of Capability in Business Analysis (ECBA, CCBA)", "IIBA",
         "https://www.iiba.org/business-analysis-certifications/")
CFA = C("CFA Program", "CFA Institute", "https://www.cfainstitute.org/programs/cfa-program")
ACCA = C("ACCA Qualification", "ACCA", "https://www.accaglobal.com/gb/en/qualifications/glance/acca.html")
ICAI = C("Chartered Accountancy (CA)", "ICAI (India)", "https://www.icai.org/")
CMA_US = C("Certified Management Accountant (CMA)", "IMA", "https://www.imanet.org/cma-certification")
FMVA = C("Financial Modeling & Valuation Analyst (FMVA)", "Corporate Finance Institute",
         "https://corporatefinanceinstitute.com/certifications/fmva/")
CIA = C("Certified Internal Auditor (CIA)", "The IIA", "https://www.theiia.org/en/certifications/cia/")
FRM = C("Financial Risk Manager (FRM)", "GARP", "https://www.garp.org/frm")
NISM = C("NISM certifications (securities markets)", "NISM (India)", "https://www.nism.ac.in/")
IIBF = C("JAIIB / banking certifications", "IIBF (India)", "https://www.iibf.org.in/")
CXPA = C("Certified Customer Experience Professional (CCXP)", "CXPA", "https://www.cxpa.org/", "experienced")
CELTA = C("CELTA (teaching English)", "Cambridge English",
          "https://www.cambridgeenglish.org/teaching-english/teaching-qualifications/celta/")
CTET = C("Central Teacher Eligibility Test (CTET)", "CBSE (India)", "https://ctet.nic.in/")
UGC_NET = C("UGC NET (Assistant Professor eligibility)", "NTA (India)", "https://ugcnet.nta.ac.in/")
ATD = C("Associate Professional in Talent Development (APTD)", "ATD",
        "https://www.td.org/certification")
IAPP = C("Certified Information Privacy Professional (CIPP)", "IAPP", "https://iapp.org/certify/cipp/")
ICA = C("ICA certificates in compliance and AML", "International Compliance Association",
        "https://www.int-comp.org/")
AWS_CP = C("AWS Certified Cloud Practitioner", "Amazon Web Services",
           "https://aws.amazon.com/certification/certified-cloud-practitioner/")
AWS_SAA = C("AWS Certified Solutions Architect – Associate", "Amazon Web Services",
            "https://aws.amazon.com/certification/certified-solutions-architect-associate/")
AWS_DEV = C("AWS Certified Developer – Associate", "Amazon Web Services",
            "https://aws.amazon.com/certification/certified-developer-associate/")
AZ_900 = C("Microsoft Azure Fundamentals (AZ-900)", "Microsoft",
           "https://learn.microsoft.com/en-us/credentials/certifications/azure-fundamentals/")
PL_300 = C("Microsoft Power BI Data Analyst (PL-300)", "Microsoft",
           "https://learn.microsoft.com/en-us/credentials/certifications/power-bi-data-analyst-associate/")
CKA = C("Certified Kubernetes Administrator (CKA)", "The Linux Foundation",
        "https://training.linuxfoundation.org/certification/certified-kubernetes-administrator-cka/")
ISTQB = C("ISTQB Certified Tester Foundation Level", "ISTQB",
          "https://www.istqb.org/certifications/certified-tester-foundation-level-ctfl-v4-0/")
COMPTIA_A = C("CompTIA A+", "CompTIA", "https://www.comptia.org/certifications/a")
COMPTIA_SEC = C("CompTIA Security+", "CompTIA", "https://www.comptia.org/certifications/security")
CISSP = C("CISSP", "ISC2", "https://www.isc2.org/certifications/cissp", "experienced",
          "Needs 5 years of security work experience.")
SALESFORCE_ADMIN = C("Salesforce Certified Administrator", "Salesforce",
                     "https://trailhead.salesforce.com/credentials/administrator")
NEBOSH = C("NEBOSH International General Certificate", "NEBOSH",
           "https://www.nebosh.org.uk/qualifications/international-general-certificate/")
ASQ_CQE = C("Certified Quality Engineer (CQE)", "ASQ", "https://www.asq.org/cert/quality-engineer", "experienced")
SOLIDWORKS = C("Certified SOLIDWORKS Associate (CSWA)", "Dassault Systèmes",
               "https://www.solidworks.com/certifications")
SERVSAFE = C("ServSafe Food Handler / Manager", "National Restaurant Association", "https://www.servsafe.com/")
FOSTAC = C("FoSTaC food safety training", "FSSAI (India)", "https://fostac.fssai.gov.in/")
WSET = C("WSET Level 1–2 (wine, spirits)", "Wine & Spirit Education Trust", "https://www.wsetglobal.com/")
AHLEI = C("Hospitality certifications (front office, guest service)", "AHLEI", "https://www.ahlei.org/")
AAPC = C("Certified Professional Coder (CPC)", "AAPC", "https://www.aapc.com/certifications/cpc")
OET = C("OET (English test for healthcare professionals)", "OET", "https://oet.com/")
NCLEX = C("NCLEX-RN (US nursing licence exam)", "NCSBN", "https://www.nclex.com/", "experienced",
          "For nurses planning to work in the US or Canada.")
PLAB = C("PLAB (UK medical licensing)", "General Medical Council",
         "https://www.gmc-uk.org/")
USMLE = C("USMLE (US medical licensing)", "USMLE", "https://www.usmle.org/")
ACRP = C("ACRP certifications (CCRC, CCRA)", "ACRP", "https://acrpnet.org/certifications", "experienced")
CLO3D = C("CLO 3D garment design training", "CLO Virtual Fashion", "https://www.clo3d.com/")
TEXTILE_INST = C("Textile Institute professional qualifications", "The Textile Institute",
                 "https://www.textileinstitute.org/")
CIPR = C("CIPR qualifications in PR", "CIPR", "https://cipr.co.uk/")
AIBE = C("All India Bar Examination (AIBE)", "Bar Council of India", "https://allindiabarexamination.com/")
NALA = C("Certified Paralegal (CP)", "NALA", "https://www.nala.org/certification")
WORLDATWORK = C("Certified Compensation Professional (CCP)", "WorldatWork",
                "https://worldatwork.org/certification", "experienced")
IAAP = C("Certified Administrative Professional (CAP)", "IAAP", "https://www.iaap-hq.org/", "experienced")

# -------------------------------------------------------------------------------------------- free courses
# Recognised in their field: run by universities, governments, professional bodies or the vendors employers use.
NO_CERT = "Free learning (no certificate)."
CS50 = F("CS50: Introduction to Computer Science", "Harvard University", "https://cs50.harvard.edu/x/",
         "Free, including a certificate from CS50.")
FCC = F("freeCodeCamp certifications (web, JavaScript, Python, data)", "freeCodeCamp",
        "https://www.freecodecamp.org/learn")
MS_LEARN = F("Microsoft Learn training paths (Azure, Power BI, Excel, Microsoft 365)", "Microsoft",
             "https://learn.microsoft.com/en-us/training/", "Free training; certification exams are paid.")
AWS_SB = F("AWS Skill Builder free digital training", "Amazon Web Services", "https://skillbuilder.aws/",
           "Free courses; certification exams are paid.")
GCLOUD = F("Google Cloud Skills Boost", "Google Cloud", "https://www.cloudskillsboost.google/",
           "Free courses and skill badges; some labs need credits.")
KAGGLE = F("Kaggle Learn (Python, SQL, pandas, machine learning)", "Kaggle (Google)", "https://www.kaggle.com/learn",
           "Free, with certificates of completion.")
LINUX101 = F("Introduction to Linux (LFS101)", "The Linux Foundation",
             "https://training.linuxfoundation.org/training/introduction-to-linux/")
TAU = F("Test Automation University", "Applitools", "https://testautomationu.applitools.com/",
        "Free, with certificates per course.")
NETACAD = F("Cisco Networking Academy (networking, cybersecurity, IT essentials)", "Cisco",
            "https://www.netacad.com/", "Free self-paced courses with certificates and badges.")
ISC2_CC = F("Certified in Cybersecurity (CC)", "ISC2", "https://www.isc2.org/certifications/cc",
            "Training and exam free through ISC2's One Million Certified programme (yearly fee after passing).")
SCRUM_OPEN = F("Scrum.org Open Assessments (Scrum, Product Owner)", "Scrum.org",
               "https://www.scrum.org/open-assessments", "Free practice tests (no certificate).")
AGILE_COACH = F("Agile Coach guides (Scrum, Kanban, roadmaps)", "Atlassian", "https://www.atlassian.com/agile", NO_CERT)
CFI_FREE = F("Free finance courses (accounting, Excel, modelling)", "Corporate Finance Institute",
             "https://corporatefinanceinstitute.com/collections/free-courses/", "Free, with certificates of completion.")
VARSITY = F("Varsity (markets, trading, personal finance)", "Zerodha (India)", "https://zerodha.com/varsity/", NO_CERT)
TRAILHEAD = F("Trailhead (CRM, sales, service)", "Salesforce", "https://trailhead.salesforce.com/",
              "Free modules and badges; certifications are paid.")
HUBSPOT_SALES_MGMT = F("Sales Management Training (free)", "HubSpot Academy",
                       "https://academy.hubspot.com/courses/sales-management")
HUBSPOT_SOCIAL = F("Social Media Marketing Certification (free)", "HubSpot Academy",
                   "https://academy.hubspot.com/courses/social-media")
HUBSPOT_SERVICE = F("Customer Service Certification (free)", "HubSpot Academy",
                    "https://academy.hubspot.com/courses/customer-service")
DIGITAL_GARAGE = F("Fundamentals of Digital Marketing", "Google Digital Garage",
                   "https://learndigital.withgoogle.com/digitalgarage/course/digital-marketing",
                   "Free, with a certificate (accredited by IAB Europe).")
OPENWHO = F("OpenWHO courses (infection prevention, emergencies, public health)", "World Health Organization",
            "https://openwho.org/", "Free, with records of achievement.")
WHO_ACADEMY = F("WHO Academy courses for health workers", "World Health Organization",
                "https://www.who.int/about/who-academy")
DIKSHA = F("Teacher training courses", "DIKSHA, Ministry of Education (India)", "https://diksha.gov.in/",
           "Free, with certificates.")
SWAYAM = F("SWAYAM courses from IITs, IIMs and Indian universities", "Government of India", "https://swayam.gov.in/",
           "Free to learn; the proctored exam for a certificate has a small fee.", cost="free_learn")
NPTEL = F("NPTEL engineering and science courses", "IITs & IISc (India)", "https://nptel.ac.in/",
          "Free to learn; the certificate exam has a small fee.", cost="free_learn")
MIT_OCW = F("MIT OpenCourseWare", "Massachusetts Institute of Technology", "https://ocw.mit.edu/", NO_CERT)
MITX_SCM = F("MicroMasters in Supply Chain Management", "MITx · edX",
             "https://www.edx.org/masters/micromasters/mitx-supply-chain-management",
             "Free to audit; the verified certificate is paid.", cost="free_learn")
EDX_PM = F("Project management courses", "edX (universities)", "https://www.edx.org/learn/project-management",
           "Free to audit; certificates are paid.", cost="free_learn")
DARDEN_PM = F("Fundamentals of Project Planning and Management", "University of Virginia Darden · Coursera",
              "https://www.coursera.org/learn/uva-darden-project-management",
              "Free to audit; the certificate is paid.", cost="free_learn")
LEARN_HOW = F("Learning How to Learn", "Deep Teaching Solutions · Coursera",
              "https://www.coursera.org/learn/learning-how-to-learn", "Free to audit; the certificate is paid.",
              cost="free_learn")
CANVA_SCHOOL = F("Design School (layout, colour, mood boards, social graphics)", "Canva",
                 "https://www.canva.com/designschool/", NO_CERT)
FIGMA_LIB = F("Figma resource library (UI, prototyping, design systems)", "Figma",
              "https://www.figma.com/resource-library/", NO_CERT)
CIPD_KNOW = F("CIPD knowledge hub (guides, factsheets, research)", "CIPD", "https://www.cipd.org/uk/knowledge/",
              NO_CERT)
SHRM_TOOLS = F("SHRM topics and tools (HR guides and templates)", "SHRM", "https://www.shrm.org/topics-tools",
               "Free articles and tools; some resources are for members.")
LCF_SUSTAIN = F("Fashion and Sustainability: Understanding Luxury Fashion", "London College of Fashion & Kering · "
                "FutureLearn", "https://www.futurelearn.com/courses/fashion-and-sustainability",
                "Free to join for a limited time; the certificate is paid.", cost="free_learn")

FREE: dict[str, tuple[Course, ...]] = {
    "software_engineer": (CS50, FCC, AWS_SB, MS_LEARN),
    "data_ai": (KAGGLE, MS_LEARN, FCC),
    "devops_cloud": (AWS_SB, LINUX101, GCLOUD, MS_LEARN),
    "qa_testing": (TAU, FCC),
    "it_support": (NETACAD, MS_LEARN, LINUX101),
    "cybersecurity": (ISC2_CC, NETACAD),
    "product_manager": (SCRUM_OPEN, AGILE_COACH, DARDEN_PM),
    "accountant": (CFI_FREE, SWAYAM, MS_LEARN),
    "financial_analyst": (CFI_FREE, VARSITY),
    "audit_tax": (CFI_FREE, SWAYAM),
    "banking": (SWAYAM, VARSITY, CFI_FREE),
    "investment": (VARSITY, CFI_FREE),
    "payroll": (MS_LEARN, SWAYAM),
    "sales_executive": (HUBSPOT_SALES, TRAILHEAD),
    "business_development": (HUBSPOT_SALES, TRAILHEAD),
    "account_manager": (HUBSPOT_SALES_MGMT, TRAILHEAD),
    "sales_manager": (HUBSPOT_SALES_MGMT, TRAILHEAD),
    "presales": (AWS_SB, TRAILHEAD, MS_LEARN),
    "digital_marketing": (DIGITAL_GARAGE, GOOGLE_SKILLSHOP, HUBSPOT_SOCIAL),
    "content_writer": (HUBSPOT_CONTENT, SEMRUSH, DIGITAL_GARAGE),
    "social_media": (HUBSPOT_SOCIAL, DIGITAL_GARAGE, CANVA_SCHOOL),
    "seo": (SEMRUSH, GOOGLE_SKILLSHOP, DIGITAL_GARAGE),
    "pr_comms": (HUBSPOT_CONTENT, DIGITAL_GARAGE),
    "brand_manager": (DIGITAL_GARAGE, HUBSPOT_INBOUND),
    "nurse": (OPENWHO, WHO_ACADEMY),
    "doctor": (OPENWHO, WHO_ACADEMY),
    "pharmacist": (OPENWHO, GCP),
    "allied_health": (OPENWHO, WHO_ACADEMY),
    "clinical_research": (GCP, OPENWHO),
    "care_assistant": (OPENWHO, WHO_ACADEMY),
    "medical_admin": (OPENWHO, MS_LEARN),
    "teacher": (DIKSHA, SWAYAM, LEARN_HOW),
    "tutor": (DIKSHA, LEARN_HOW),
    "lecturer": (SWAYAM, NPTEL),
    "instructional_designer": (SWAYAM, CANVA_SCHOOL),
    "education_admin": (MS_LEARN, DIKSHA),
    "recruiter": (SHRM_TOOLS, CIPD_KNOW),
    "hr_generalist": (CIPD_KNOW, SHRM_TOOLS, SWAYAM),
    "hr_business_partner": (CIPD_KNOW, SHRM_TOOLS),
    "learning_development": (CIPD_KNOW, SWAYAM),
    "compensation": (SHRM_TOOLS, MS_LEARN),
    "operations_manager": (DARDEN_PM, NPTEL, MITX_SCM),
    "supply_chain": (MITX_SCM, NPTEL),
    "procurement": (MITX_SCM, SWAYAM),
    "logistics": (MITX_SCM, NPTEL),
    "customer_support": (HUBSPOT_SERVICE, TRAILHEAD),
    "customer_success": (HUBSPOT_SERVICE, TRAILHEAD),
    "ux_ui_designer": (FIGMA_LIB, FCC),
    "graphic_designer": (CANVA_SCHOOL, FIGMA_LIB),
    "video_media": (CANVA_SCHOOL, DIGITAL_GARAGE),
    "fashion_stylist": (LCF_SUSTAIN, CANVA_SCHOOL, SWAYAM),
    "fashion_designer": (LCF_SUSTAIN, SWAYAM, CANVA_SCHOOL),
    "visual_merchandiser": (CANVA_SCHOOL, LCF_SUSTAIN, SWAYAM),
    "fashion_buying": (LCF_SUSTAIN, MS_LEARN, SWAYAM),
    "fashion_retail": (LCF_SUSTAIN, HUBSPOT_SERVICE),
    "fashion_marketing": (DIGITAL_GARAGE, HUBSPOT_SOCIAL, LCF_SUSTAIN),
    "fashion_production": (LCF_SUSTAIN, NPTEL),
    "mechanical": (NPTEL, MIT_OCW),
    "electrical": (NPTEL, MIT_OCW),
    "civil": (NPTEL, MIT_OCW),
    "manufacturing": (NPTEL, MIT_OCW),
    "quality": (NPTEL, MIT_OCW),
    "lawyer": (SWAYAM,),
    "paralegal": (SWAYAM, MS_LEARN),
    "compliance": (SWAYAM, CFI_FREE),
    "admin_assistant": (MS_LEARN, SWAYAM),
    "executive_assistant": (MS_LEARN, SWAYAM),
    "office_manager": (MS_LEARN, DARDEN_PM),
    "data_entry": (MS_LEARN, SWAYAM),
    "retail": (HUBSPOT_SERVICE, SWAYAM),
    "store_manager": (HUBSPOT_SERVICE, SWAYAM),
    "chef_kitchen": (SWAYAM,),
    "hotel_travel": (HUBSPOT_SERVICE, SWAYAM),
    "business_analyst": (KAGGLE, MS_LEARN, AGILE_COACH),
    "consultant": (CFI_FREE, MS_LEARN),
    "project_manager": (DARDEN_PM, EDX_PM, AGILE_COACH, SCRUM_OPEN),
    "general_manager": (EDX_PM, CFI_FREE),
}

# ------------------------------------------------------------------------------------------------- per role
# role id -> (core skills employers ask for, courses in recommended order)
KIT: dict[str, tuple[tuple[str, ...], tuple[Course, ...]]] = {
    # technology
    "software_engineer": (("Data structures & algorithms", "Git", "SQL", "REST APIs", "Unit testing", "Cloud basics",
                           "System design"), (AWS_DEV, AZ_900, AWS_SAA)),
    "data_ai": (("SQL", "Excel", "Python", "Statistics", "Power BI / Tableau", "Data cleaning", "Machine learning basics"),
                (GOOGLE_DATA, PL_300, AWS_CP)),
    "devops_cloud": (("Linux", "Networking", "Docker", "Kubernetes", "CI/CD", "Terraform", "Monitoring"),
                     (AWS_SAA, AZ_900, CKA)),
    "qa_testing": (("Test case design", "Selenium / Playwright", "API testing", "SQL", "Bug tracking (Jira)",
                    "Agile"), (ISTQB, PSM)),
    "it_support": (("Windows & macOS support", "Networking basics", "Active Directory", "Ticketing (ServiceNow)",
                    "Hardware troubleshooting", "Customer communication"), (GOOGLE_IT, COMPTIA_A, ITIL)),
    "cybersecurity": (("Networking", "Linux", "SIEM", "Incident response", "Vulnerability management",
                       "Risk frameworks (ISO 27001)"), (COMPTIA_SEC, AWS_CP, CISSP)),
    "product_manager": (("User research", "Roadmapping", "Prioritisation", "Analytics / SQL", "Agile / Scrum",
                         "Stakeholder communication"), (PSPO, GOOGLE_PM, GOOGLE_DATA)),
    # finance
    "accountant": (("Bookkeeping", "Financial statements", "Tally / QuickBooks / SAP", "Advanced Excel", "GST / VAT",
                    "Reconciliations"), (ICAI, ACCA, MOS)),
    "financial_analyst": (("Financial modelling", "Advanced Excel", "Valuation", "Budgeting & forecasting",
                           "Power BI", "Accounting basics"), (FMVA, CFA, CMA_US)),
    "audit_tax": (("Auditing standards", "Taxation", "Internal controls", "Advanced Excel", "Risk assessment"),
                  (ICAI, ACCA, CIA)),
    "banking": (("KYC / AML", "Credit appraisal", "Banking products", "Customer relationship", "Regulations"),
                (IIBF, NISM, ICA)),
    "investment": (("Financial analysis", "Valuation", "Portfolio theory", "Excel modelling", "Market research",
                    "Risk management"), (CFA, NISM, FRM)),
    "payroll": (("Payroll processing", "Statutory compliance (PF, ESI, TDS)", "Excel", "HRMS software",
                 "Confidentiality"), (MOS, CIPD)),
    # sales
    "sales_executive": (("Prospecting", "Negotiation", "CRM (Salesforce / HubSpot)", "Presentation",
                         "Objection handling", "Pipeline management"), (HUBSPOT_SALES, SALESFORCE_ADMIN)),
    "business_development": (("Lead generation", "Cold outreach", "Market research", "CRM", "Negotiation",
                              "Proposal writing"), (HUBSPOT_SALES, HUBSPOT_INBOUND)),
    "account_manager": (("Client relationship management", "Upselling", "CRM", "Reporting", "Negotiation"),
                        (HUBSPOT_SALES, SALESFORCE_ADMIN)),
    "sales_manager": (("Team leadership", "Forecasting", "Sales strategy", "CRM analytics", "Coaching"),
                      (HUBSPOT_SALES, SALESFORCE_ADMIN)),
    "presales": (("Solution demos", "Requirements gathering", "Technical writing", "Cloud basics", "Presentation"),
                 (AWS_CP, AZ_900, SALESFORCE_ADMIN)),
    # marketing
    "digital_marketing": (("Google Ads", "Meta Ads", "SEO", "Google Analytics", "Email marketing",
                           "Content strategy"), (GOOGLE_DM, GOOGLE_SKILLSHOP, META_SMM)),
    "content_writer": (("Copywriting", "SEO writing", "Editing & proofreading", "Content strategy", "CMS (WordPress)",
                        "Research"), (HUBSPOT_CONTENT, SEMRUSH)),
    "social_media": (("Content calendars", "Instagram & Reels", "Meta Business Suite", "Community management",
                      "Analytics", "Canva"), (META_SMM, HUBSPOT_INBOUND)),
    "seo": (("Keyword research", "On-page SEO", "Technical SEO", "Google Search Console", "Google Analytics",
             "Link building"), (SEMRUSH, GOOGLE_SKILLSHOP)),
    "pr_comms": (("Press releases", "Media relations", "Crisis communication", "Storytelling", "Social media"),
                 (CIPR, HUBSPOT_CONTENT)),
    "brand_manager": (("Brand strategy", "Consumer insights", "Campaign management", "Market research",
                       "Budget management"), (GOOGLE_DM, HUBSPOT_INBOUND)),
    # healthcare
    "nurse": (("Patient care", "Clinical documentation", "Medication administration", "Infection control", "BLS/ACLS"),
              (BLS, OET, NCLEX)),
    "doctor": (("Clinical diagnosis", "Patient communication", "Evidence-based practice", "Emergency care"),
               (BLS, PLAB, USMLE)),
    "pharmacist": (("Dispensing", "Drug interactions", "Patient counselling", "Pharmacy regulations",
                    "Inventory management"), (BLS, GCP)),
    "allied_health": (("Assessment & treatment planning", "Patient communication", "Clinical documentation",
                       "Rehabilitation techniques"), (BLS,)),
    "clinical_research": (("ICH-GCP", "Clinical trial protocols", "Data management", "Regulatory documentation",
                           "Medical terminology"), (GCP, ACRP)),
    "care_assistant": (("Personal care", "Safeguarding", "Communication", "First aid", "Record keeping"), (BLS,)),
    "medical_admin": (("Medical terminology", "Medical coding (ICD-10)", "Scheduling", "EHR systems", "Billing"),
                      (AAPC, MOS)),
    # education
    "teacher": (("Lesson planning", "Classroom management", "Child development", "Assessment", "Parent communication",
                 "EdTech tools"), (CTET, CELTA)),
    "tutor": (("Subject expertise", "Lesson planning", "Online teaching tools", "Assessment", "Communication"),
              (CELTA,)),
    "lecturer": (("Research & publishing", "Curriculum design", "Public speaking", "Academic writing", "Mentoring"),
                 (UGC_NET,)),
    "instructional_designer": (("ADDIE / learning design", "Articulate Storyline / Rise", "LMS administration",
                                "Storyboarding", "Assessment design"), (ATD,)),
    "education_admin": (("Admissions processes", "Student counselling", "Record keeping", "MS Office",
                         "Communication"), (MOS,)),
    # HR
    "recruiter": (("Sourcing (LinkedIn, job boards)", "Screening & interviewing", "ATS software", "Employer branding",
                   "Negotiation"), (CIPD, SHRM)),
    "hr_generalist": (("Onboarding", "HR policies", "Labour law basics", "HRMS software", "Employee relations",
                       "Excel"), (CIPD, SHRM)),
    "hr_business_partner": (("Workforce planning", "Employee relations", "Performance management", "HR analytics",
                             "Change management"), (SHRM, CIPD)),
    "learning_development": (("Training needs analysis", "Facilitation", "Learning design", "LMS",
                              "Measuring training impact"), (ATD, CIPD)),
    "compensation": (("Compensation benchmarking", "Payroll", "Excel", "Benefits administration", "HR analytics"),
                     (WORLDATWORK, CIPD)),
    # operations
    "operations_manager": (("Process improvement", "KPI reporting", "Excel", "Team coordination", "Lean"),
                           (SIX_SIGMA, GOOGLE_PM, PMP)),
    "supply_chain": (("Demand planning", "Inventory management", "ERP (SAP)", "Excel", "Forecasting"),
                     (ASCM_CPIM, ASCM_CSCP)),
    "procurement": (("Vendor management", "Negotiation", "Purchase orders", "ERP (SAP)", "Cost analysis"),
                    (CIPS, ASCM_CSCP)),
    "logistics": (("Shipping & customs", "Warehouse management", "Route planning", "ERP / WMS", "Safety"),
                  (ASCM_CLTD, NEBOSH)),
    # customer service
    "customer_support": (("Customer communication", "Ticketing (Zendesk / Freshdesk)", "Problem solving",
                          "Typing speed", "Empathy & de-escalation"), (CXPA, ITIL)),
    "customer_success": (("Onboarding customers", "Account health metrics", "CRM", "Renewals & upsell",
                          "Product demos"), (CXPA, HUBSPOT_SALES)),
    # design
    "ux_ui_designer": (("Figma", "User research", "Wireframing & prototyping", "Usability testing",
                        "Design systems", "Accessibility"), (GOOGLE_UX,)),
    "graphic_designer": (("Adobe Photoshop", "Adobe Illustrator", "Typography", "Layout", "Brand identity",
                          "Canva"), (ADOBE_ACP,)),
    "video_media": (("Premiere Pro / After Effects", "Storytelling", "Colour grading", "Sound editing",
                     "Short-form video"), (ADOBE_ACP,)),
    # fashion
    "fashion_stylist": (("Body-shape & colour analysis", "Trend forecasting", "Wardrobe planning", "Mood boards",
                         "Photoshoot styling", "Client consultation", "Instagram portfolio"),
                        (UAL_SHORT, NIFT_CE, PEARL)),
    "fashion_designer": (("Fashion illustration", "Pattern making", "Garment construction", "Adobe Illustrator",
                          "CLO 3D", "Fabric knowledge", "Tech packs"), (NIFT_CE, CLO3D, UAL_SHORT, ADOBE_ACP)),
    "visual_merchandiser": (("Window & store displays", "Planograms", "Colour & composition", "Brand guidelines",
                             "Sales-floor analysis"), (UAL_SHORT, NIFT_CE, NRF_RISE)),
    "fashion_buying": (("Range planning", "Open-to-buy (OTB)", "Excel", "Vendor negotiation", "Trend analysis",
                        "Sell-through analysis"), (UAL_SHORT, NIFT_CE, MOS)),
    "fashion_retail": (("Clienteling", "Product knowledge", "Visual standards", "Sales targets",
                        "Customer service"), (NRF_RISE, UAL_SHORT)),
    "fashion_marketing": (("Instagram & influencer marketing", "Content creation", "Brand storytelling",
                           "E-commerce", "PR & events"), (META_SMM, GOOGLE_DM, UAL_SHORT)),
    "fashion_production": (("Garment construction", "Quality control (AQL)", "Fabric sourcing", "Costing",
                            "Production planning"), (TEXTILE_INST, SIX_SIGMA, NIFT_CE)),
    # engineering
    "mechanical": (("AutoCAD", "SolidWorks / CATIA", "GD&T", "Manufacturing processes", "FEA basics"),
                   (SOLIDWORKS, AUTODESK, SIX_SIGMA)),
    "electrical": (("Circuit design", "PLC / SCADA", "Electrical safety", "AutoCAD Electrical", "Embedded C"),
                   (AUTODESK, NEBOSH)),
    "civil": (("AutoCAD", "Revit / BIM", "Estimation & BOQ", "Site supervision", "Structural basics"),
              (AUTODESK, PMP)),
    "manufacturing": (("Lean manufacturing", "5S / Kaizen", "Production planning", "Quality tools", "Safety"),
                      (SIX_SIGMA, NEBOSH)),
    "quality": (("ISO 9001", "Root-cause analysis", "SPC", "Audits", "Documentation"), (ASQ_CQE, SIX_SIGMA, NEBOSH)),
    # legal
    "lawyer": (("Legal research", "Drafting", "Contract law", "Litigation", "Negotiation"), (AIBE, IAPP)),
    "paralegal": (("Legal research", "Document management", "Drafting", "Case management software"), (NALA, MOS)),
    "compliance": (("Regulatory frameworks", "AML / KYC", "Policy writing", "Risk assessment", "Data protection"),
                   (ICA, IAPP)),
    # admin
    "admin_assistant": (("MS Office", "Scheduling", "Email etiquette", "Record keeping", "Communication"), (MOS,)),
    "executive_assistant": (("Calendar management", "Travel planning", "MS Office", "Confidentiality",
                             "Minute taking"), (MOS, IAAP)),
    "office_manager": (("Facilities coordination", "Vendor management", "Budgeting", "MS Office"), (MOS, IAAP)),
    "data_entry": (("Typing speed & accuracy", "Excel", "Data validation", "Attention to detail"), (MOS,)),
    # hospitality
    "retail": (("Customer service", "Product knowledge", "POS systems", "Visual merchandising", "Sales"), (NRF_RISE,)),
    "store_manager": (("Team leadership", "Inventory control", "Sales targets", "Rostering", "P&L basics"),
                      (NRF_RISE,)),
    "chef_kitchen": (("Food safety & hygiene", "Menu planning", "Kitchen management", "Cost control"),
                     (FOSTAC, SERVSAFE)),
    "hotel_travel": (("Guest service", "Front-office systems (Opera)", "Reservations", "Languages",
                      "Event coordination"), (AHLEI, WSET)),
    # business
    "business_analyst": (("Requirements gathering", "Process mapping (BPMN)", "SQL", "Excel", "Jira",
                          "Stakeholder management"), (IIBA, GOOGLE_DATA, PSPO)),
    "consultant": (("Problem structuring", "Excel modelling", "PowerPoint storytelling", "Market research",
                    "Stakeholder management"), (GOOGLE_DATA, SIX_SIGMA)),
    "project_manager": (("Planning & scheduling", "Risk management", "Agile / Scrum", "Stakeholder communication",
                         "MS Project / Jira"), (CAPM, PSM, PMP)),
    "general_manager": (("Strategy", "P&L ownership", "Leadership", "Change management"), (PMP,)),
}

EARLY = {"student", "entry"}


def _career(db: Database, user_id: int) -> dict:
    return settings_mod.get(db, "career", user_id) or {}


def roles_of(career: dict) -> list[str]:
    roles = [r for r in career.get("roles") or [] if r in KIT]
    if not roles:                                       # industries only: use each industry's first role
        for ind in career.get("industries") or []:
            if ind in taxonomy.BY_ID:
                roles.append(taxonomy.BY_ID[ind].roles[0].id)
    return roles


def is_free(c: Course | dict) -> bool:
    cost = c.cost if isinstance(c, Course) else c.get("cost")
    return cost in ("free", "free_learn")


def courses_for(career: dict, limit: int = 6, cost: str | None = None) -> list[dict]:
    """Recognised courses for the user's roles; early-career users see 'start' courses first.
    `cost`: None = all, "free" = free and free-to-learn, "paid" = paid only."""
    level = career.get("experience")
    out: dict[str, dict] = {}
    for rid in roles_of(career):
        pool = list(FREE.get(rid, ())) + [c for c in KIT[rid][1] if c not in FREE.get(rid, ())]
        if cost == "free":
            pool = [c for c in pool if is_free(c)]
        elif cost == "paid":
            pool = [c for c in pool if not is_free(c)]
        for i, c in enumerate(pool):
            d = out.setdefault(c.url, {"name": c.name, "provider": c.provider, "url": c.url, "level": c.level,
                                       "note": c.note, "cost": c.cost, "roles": [], "rank": i})
            d["rank"] = min(d["rank"], i)
            label = taxonomy.role_label(rid)
            if label not in d["roles"]:
                d["roles"].append(label)
    items = list(out.values())
    items.sort(key=lambda d: ((d["level"] == "experienced") if level in EARLY or not level else 0, d["rank"],
                              -len(d["roles"])))
    return items[:limit]


def _has(skill: str, text: str) -> bool:
    words = [w for w in re.split(r"[^a-z0-9+#]+", skill.lower()) if len(w) > 2 and w not in {"and", "the", "basics"}]
    if not words:
        return False
    first = skill.lower().split(" (")[0].split(" / ")
    if any(f and f in text for f in first):
        return True
    return sum(1 for w in words if w in text) >= max(1, (len(words) + 1) // 2)


def resume_text(db: Database, user_id: int) -> str:
    try:
        from .resume import store as resume_store
        r = resume_store.canonical(db, user_id=user_id)
    except Exception:  # noqa: BLE001 — a missing/odd resume just means "nothing shown yet"
        return ""
    if not r:
        return ""
    for k in ("text", "raw_text", "plain_text"):
        if isinstance(r, dict) and r.get(k):
            return str(r[k]).lower()
    if isinstance(r, dict) and r.get("structured_json"):
        return str(r["structured_json"]).lower()
    return str(r).lower()


def job_gaps(db: Database, user_id: int, limit: int = 8) -> dict:
    """Required skills the user's best-matching open jobs ask for but the resume does not show."""
    rows = db.all(
        "SELECT sc.score, sc.details_json FROM job_scores sc JOIN jobs j ON j.id=sc.job_id "
        "WHERE sc.user_id=? AND j.status='open' AND j.is_sample=0 AND sc.id = "
        "(SELECT MAX(id) FROM job_scores WHERE job_id=sc.job_id AND user_id=?) ORDER BY sc.score DESC LIMIT 60",
        (user_id, user_id))
    count: dict[str, int] = {}
    scores = []
    for r in rows:
        scores.append(float(r["score"]))
        d = loads(r["details_json"], {})
        for m in d.get("must_have") or []:
            if isinstance(m, dict) and m.get("skill") and float(m.get("credit") or 0) < 0.5:
                count[m["skill"]] = count.get(m["skill"], 0) + 1
    top = sorted(count.items(), key=lambda kv: -kv[1])[:limit]
    return {"skills": [{"skill": k, "jobs": v} for k, v in top], "jobs_scored": len(rows),
            "avg_score": round(sum(scores[:20]) / len(scores[:20]), 1) if scores else None}


def skill_plan(db: Database, user_id: int) -> dict:
    career = _career(db, user_id)
    text = resume_text(db, user_id)
    roles = []
    for rid in roles_of(career):
        skills = [{"skill": s, "have": bool(text) and _has(s, text)} for s in KIT[rid][0]]
        roles.append({"id": rid, "label": taxonomy.role_label(rid), "skills": skills,
                      "missing": sum(1 for s in skills if not s["have"])})
    return {"career_set": taxonomy.has_career(career), "has_resume": bool(text), "roles": roles,
            "gaps": job_gaps(db, user_id), "courses": courses_for(career, limit=8),
            "courses_free": courses_for(career, limit=8, cost="free"),
            "courses_paid": courses_for(career, limit=8, cost="paid"),
            "early": career.get("experience") in EARLY}
