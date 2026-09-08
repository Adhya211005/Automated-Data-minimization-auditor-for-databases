VIT – School of Computer Science and Engineering (SCOPE)
BCSE410L – Cyber Security | Project Review–I
Automated Data Minimization Auditor for Databases
1. Problem Understanding
1.1 Problem Statement
Organizations routinely collect and retain more personal/sensitive data in their databases than is actually required for the stated business purpose — a direct violation of the data minimization and storage limitation principles under GDPR Article 5(1)(c) and 5(1)(e), and similar clauses under India's DPDP Act. In practice this happens silently: a column gets added “just in case,” a table keeps growing, and nobody circles back to check whether the data is still used or still necessary.
1.2 Why This Problem Matters
There are two separate reasons this is a serious problem, not just a tidiness issue:
●	It's against the law. GDPR in Europe and the DPDP Act in India both legally require companies to only collect and keep data they actually need for a stated purpose. Regulators are actively enforcing this — cumulative GDPR fines have crossed roughly €7.1 billion, with a large share landed since 2023, and several recent penalties (e.g. France's CNIL fining Free Mobile €27M in early 2026) were specifically for over-collection or over-retention, not for a data breach.
●	It's a security risk. Every extra piece of sensitive data sitting in a database is one more thing that leaks if the company gets hacked. Data that was deleted on time simply can't be stolen — so minimization is also a practical security control, not just a legal checkbox.
1.3 Real-World Applications
Any organization running a relational database with user-facing PII — fintech, healthtech, ed-tech, SaaS platforms — needs periodic audits to check whether it is still storing only what it needs. Today this is done manually: a legal/compliance person sits with a spreadsheet and asks engineering, table by table, “do we still need this column?” This is slow, boring, easy to skip under deadline pressure, and goes stale the moment the schema changes again next sprint.
1.4 Target Users
This tool is built for three kinds of people who currently do this job manually:
●	Database Administrators (DBAs) — who own the schema and want to know what's safe to clean up.
●	Data Protection Officers (DPOs) / compliance teams — who are legally responsible for proving the company follows minimization rules.
●	Backend engineering leads — who get asked “do we still use this field?” and currently have no fast way to answer besides grepping through code.
1.5 Current Challenges (why this is still an unsolved, painful problem)
●	Manual audits don't scale. A spreadsheet-based review might happen once a year, but schemas change every sprint — so the audit is stale before it's even finished.
●	PII classification is usually a one-time decision. A column gets labeled “sensitive” or “not sensitive” when it's created, and nobody revisits that label even after the column's actual purpose changes.
●	“Necessity” is not obvious from the column name alone. A column named ssn is obviously necessary-looking but might never actually be queried anymore. A column named last_ip sounds harmless but is legally sensitive. You can't tell just by reading the schema — you have to look at real usage.
●	Existing tools only answer “is this PII?” (yes/no), not “is this PII still needed right now?” — which is the actual legal test (“necessary to fulfil the stated purpose”) and the actual security question that matters.

2. Literature Survey (2022–2026)
Paper	Objective	Methodology	Advantages	Limitations
Goldsteen, A., Ezov, G., Shmelkin, R., Moffie, M., Farkash, A. (2022). Data Minimization for GDPR Compliance in Machine Learning Models. AI and Ethics (Springer), 2, 477–491.	Reduce the number/granularity of input features an ML model needs at inference time to satisfy GDPR minimization.	Feature generalization/suppression on trained models using k-anonymity-style techniques; evaluated via accuracy retention.	First systematic method to operationalize minimization as a measurable, automatable process.	Scoped only to ML model inputs — does not address minimization of data already stored in operational relational databases.
Tauqeer, A., Kurteva, A., Chhetri, T.R., Ahmeti, A., Fensel, A. (2022). Automated GDPR Contract Compliance Verification Using Knowledge Graphs. Information (MDPI), 13(10), 447.	Automatically check whether data-processing contracts/policies comply with GDPR clauses.	Builds a GDPR knowledge graph and runs graph-based consistency queries against contract clauses.	Automates a task normally done fully manually (legal contract review); reusable ontology.	Operates on legal text/contracts, not live system state — cannot verify what the database actually does.
Amaral Cejas, O., Azeem, I., Abualhaija, S., Briand, L.C. (2022). NLP-based Automated Compliance Checking of Data Processing Agreements against GDPR. SnT, University of Luxembourg (arXiv:2209.09722).	Use NLP to verify whether Data Processing Agreements (DPAs) satisfy GDPR Article 28.	NLP pipeline (requirement extraction + classification) applied to legal-document text, benchmarked against annotated ground truth.	Strong accuracy on legal-document compliance checking; reduces manual legal audit effort.	Document-centric, not data-centric — cannot detect technical minimization failures inside a running database.
Chhetri, T.R., Kurteva, A., DeLong, R.J., Hilscher, R., Korte, K., Fensel, A. (2022). Data Protection by Design Tool for Automated GDPR Compliance Verification Based on Semantically Modeled Informed Consent. Sensors (MDPI), 22(7), 2763.	Provide a scalable tool for automated GDPR compliance verification and auditability based on informed consent modeled as a knowledge graph.	“Regulation-to-code” process translating GDPR clauses into technical/organizational measures and software checks; demoed in insurance and smart-city domains.	Brings verification closer to runtime enforcement rather than static documents; adaptable across domains.	Focused on consent-flow verification, not general-purpose relational database auditing, retention scheduling, or usage analysis.
Zhang, K., Jiang, X. (2024). Sensitive Data Detection with High-Throughput Machine Learning Models in Electronic Health Records. AMIA Annual Symposium Proceedings, 2023, 814–823. PMCID: PMC10785837.	Detect PHI/sensitive fields at scale across heterogeneous database schemas without relying on brittle rule-based matching.	Engineered 37 metadata-based features per column; gradient-boosted tree classifier; evaluated via 5-fold cross-validation (AUROC, AUPRC, F1).	ML-based classification generalizes far better across differently-structured schemas than fixed regex rules (near-perfect accuracy).	Purely a detection system — no usage/access analysis and no retention logic; stops at “this column is sensitive.”

Summary of the gap across all five papers: the field has strong, separate solutions for (a) minimizing ML model inputs, (b) verifying legal documents/contracts against GDPR, (c) verifying consent-flow compliance via regulation-to-code, and (d) detecting sensitive columns using ML — but no paper combines live usage analysis (is this data actually being queried by the application) with sensitivity classification and retention-age tracking on operational relational databases to produce a continuously-updated, automated minimization audit. That combination is the space this project occupies.



3. Existing Work Analysis
Existing Solution	Technology Used	Advantages	Limitations
Commercial PII/DLP discovery tools	Regex + NER/NLP entity recognition over stored data	Mature, fast, wide format coverage	Detect presence of PII only; don't verify actual usage by application code — high false-positive “noise” for compliance teams
GDPR/DPA legal-document compliance checkers (NLP-based)	NLP requirement extraction on legal text	Automates legal review, high accuracy on document classification	Checks what the policy says, not what the database actually does
Knowledge-graph GDPR verification tools	Ontology + graph queries over consent/processing metadata	Structured, explainable, reusable across organizations	Requires manually modeling every data flow up front; no retention-age or usage-frequency signal; doesn't scale to arbitrary/evolving schemas

Research Gap: 
None of the above close the loop between what is collected, what is actually used, and how long it has been sitting there — all three signals are needed to make an automatable, defensible minimization decision. Existing tools give at most one or two of the three.
4. Novelty
Every existing tool answers-  “is this data sensitive?” This project answers a harder, more useful question: “is this sensitive data still worth keeping?” — which needs three signals combined, not one: (1) is it sensitive, (2) is anyone actually using it, and (3) is it overdue by policy. A column is only flagged as a real violation when all three line up together — sensitive, unused, and stale.
What is new in your project?
A combined, usage-aware minimization auditor. Instead of flagging a column purely because it looks like PII, it cross-references three signals before flagging: (1) sensitivity classification, (2) actual query/access frequency pulled from real application query logs, and (3) data age against a configurable retention policy. These combine into a single Necessity Score per column, instead of a flat yes/no PII flag.
Worked example, to make the Necessity Score concrete
A simple way to think about the formula: Necessity Score = Sensitivity × (1 − Usage) × Staleness. A high score means “urgent, flag this”; a low score means “leave it alone.” For example:
Column	Sensitivity	Usage (last 90 days)	Age past policy	Verdict
email	High (0.9)	Very high (0.95)	N/A	Keep — actively used
mothers_maiden_name	High (0.9)	Never queried (0.0)	400 days overdue	Flag for deletion
favorite_color	Low (0.1)	Never queried (0.0)	Overdue	Not a real compliance risk

This is the crucial difference from a plain PII scanner: a scanner would flag both email and mothers_maiden_name identically, just because both are “sensitive.” This project only escalates the column that is genuinely a problem — sensitive AND unused AND stale — and leaves actively-used sensitive data alone.



How is your solution different from existing work?
Prior work either stops at classification and detects sensitive columns without checking usage (Zhang & Jiang, 2024), checks static legal documents/contracts instead of the live database (Tauqeer et al. 2022; Amaral Cejas et al. 2022; Chhetri et al. 2022), or minimizes ML model input features rather than operational database columns (Goldsteen et al. 2022). This project applies minimization logic directly and continuously to a running relational database, driven by observed usage rather than declared intent.
Which limitation of existing solutions are you solving?
The high false-positive burden of pure PII scanners, which flag every sensitive-looking column regardless of whether it is actually being used. In practice this means compliance teams get an undifferentiated list of hundreds of “PII columns” and don't know where to start — they lose trust in the tool because it can't tell them which flags are genuinely urgent.
Why should users choose your solution?
It gives a prioritized, evidence-backed remediation list (“this column is PII, unused for 9 months, past retention policy — archive it”) instead of an undifferentiated list of every PII column, which is what most existing tools produce. Instead of “here are 200 columns that are PII, go figure it out,” the user gets “here are your top 10 columns that are sensitive, unused, and overdue — fix these first,” plus a ready-to-review SQL script to do it.
What makes your project unique?
The query-log-driven Usage Analyzer is the core novel component — it treats “necessity” as an empirically observable property (is this column ever actually read by the application) rather than something declared once at design time and never revisited.
Novelty Category Mapping (per rubric)
●	AI/ML integration — the sensitivity classifier and Necessity Scoring Engine both use ML, not just static rules.
●	Automation of manual tasks — replaces a slow, manual compliance-audit process with a continuous automated pipeline.
●	Lower false positives — usage-weighting suppresses false alarms on sensitive-but-actively-used columns that a plain PII scanner would flag unnecessarily.
5. Proposed Solution
System Architecture (4 layers)
●	Ingestion Layer — connects to the target DB (Postgres/MySQL) via read-only credentials; pulls schema metadata, sample data, and query logs.
●	Analysis Layer — three parallel engines: Sensitivity Classifier, Usage Analyzer, and Retention Checker.
●	Scoring & Correlation Engine — combines the three signals into a single Necessity Score per column/table, with a rules layer defining what counts as a violation.
●	Presentation & Remediation Layer — React dashboard showing flagged columns/tables, drill-down evidence, and auto-generated remediation suggestions (archive script / SQL migration draft).



Modules and Role of Each Module
Module	Role
1. DB Connector & Metadata Extractor	Feeds all downstream modules with schema and sample data; the “eyes” of the system.
2. Sensitivity / PII Classifier	Answers “is this risky?” using regex + a lightweight ML model over column metadata and sample values.
3. Query Log Parser & Usage Scorer	Answers “is anyone using it?” — the project's core novelty. Computes per-column read/write frequency from real query logs.
4. Retention Policy Engine	Answers “is it overdue?” by evaluating timestamp columns against a configurable retention policy.
5. Necessity Scoring / Correlation Engine	The “brain” — combines modules 2, 3, and 4 into one Necessity Score and applies violation rules.
6. Remediation Script Generator	The “hands” — auto-drafts SQL/archive scripts for top-flagged columns so findings are directly actionable.
7. Dashboard / Reporting UI (React)	Human interface for DPOs/DBAs: ranked flagged columns, drill-down evidence, and a before/after risk-reduction view.

Technology Stack
●	Backend: FastAPI (Python)
●	DB target support: PostgreSQL / MySQL via SQLAlchemy introspection
●	Classification: scikit-learn + regex hybrid model
●	Frontend: React dashboard
●	Audit history storage: SQLite / Postgres
●	Optional: Groq/LLM API for generating human-readable remediation explanations


Resources Required
●	Sample Postgres/MySQL instance seeded with synthetic (non-real) PII data
●	Synthetically generated application query logs mimicking realistic access patterns
●	Python libraries: scikit-learn, FastAPI, SQLAlchemy
●	React for the frontend dashboard
●	Optional: Groq API access for remediation-text generation (reusable familiarity from PathAI capstone)
●	No special hardware requirements — runs on a standard laptop

