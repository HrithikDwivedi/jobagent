# JobAgent automated test report

- **Date:** 2026-11-01 23:56
- **Python:** 3.12.5 (Windows 11)
- **Database dialects tested:** PostgreSQL, SQLite
- **Source:** `reports/junit.xml` (pytest) + test docstrings
- **Test data:** synthetic (generated in code) plus the real resume opened read-only (sha256 verified unchanged)

## Totals

**310 tests** — PASS 310 · FAIL 0 · SKIP 0

| Category | Tests | Pass | Fail | Skip |
|---|---|---|---|---|
| A. Resume handling | 28 | 28 | 0 | 0 |
| B. Discovery & normalisation | 32 | 32 | 0 | 0 |
| C. Application workflow | 55 | 55 | 0 | 0 |
| D. Security | 65 | 65 | 0 | 0 |
| E. Persistence & concurrency | 32 | 32 | 0 | 0 |
| F. Dashboard & usability | 52 | 52 | 0 | 0 |
| G. AI & cost controls | 12 | 12 | 0 | 0 |
| Unit. Unit tests | 34 | 34 | 0 | 0 |

**Release blockers (failures in C/D/E):** 0

## A. Resume handling

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_a_resume::test_correct_pdf_discovery_file` | an explicit PDF path is returned unchanged by discover_resume. | As expected | PASS | — | No |
| `test_a_resume::test_correct_pdf_discovery_directory` | a folder holding exactly one resume resolves to that file. | As expected | PASS | — | No |
| `test_a_resume::test_missing_resume_directory` | a path inside a missing folder raises not_found mentioning the missing folder. | As expected | PASS | — | No |
| `test_a_resume::test_resume_not_configured` | no configured path raises not_configured. | As expected | PASS | — | No |
| `test_a_resume::test_incorrect_filename_suggests_nearby` | a misspelt filename raises not_found and lists the nearby resume as a suggestion. | As expected | PASS | — | No |
| `test_a_resume::test_multiple_candidate_resumes` | a folder with several PDFs raises multiple_candidates listing every candidate (never guesses). | As expected | PASS | — | No |
| `test_a_resume::test_corrupted_pdf` | garbage bytes with a .pdf extension raise code 'corrupted'. | As expected | PASS | — | No |
| `test_a_resume::test_password_protected_pdf` | an AES-256 encrypted PDF raises code 'password_protected'. | As expected | PASS | — | No |
| `test_a_resume::test_scanned_image_only_pdf` | an image-only PDF raises 'scanned_no_text' (no OCR, no guessing). | As expected | PASS | — | No |
| `test_a_resume::test_empty_pdf` | a PDF with one blank page raises code 'empty'. | As expected | PASS | — | No |
| `test_a_resume::test_unsupported_format` | a .png resume raises 'unsupported_format' from discovery and from import. | As expected | PASS | — | No |
| `test_a_resume::test_unsupported_format_import_bytes` | import_bytes refuses a .png upload with 'unsupported_format'. | As expected | PASS | — | No |
| `test_a_resume::test_docx_resume_parses` | a .docx resume parses into experience roles with dates and bullets. | As expected | PASS | — | No |
| `test_a_resume::test_synthetic_pdf_structure` | the synthetic PDF yields name, email, 2 dated roles, skills and education. | As expected | PASS | — | No |
| `test_a_resume::test_missing_experience_dates_flagged` | a role without a parseable start date is flagged as a consistency issue. | As expected | PASS | — | No |
| `test_a_resume::test_duplicate_skills_flagged` | a skill listed twice (case-insensitive, across categories) is flagged. | As expected | PASS | — | No |
| `test_a_resume::test_inconsistent_dates_flagged[end_before_start-Jan 2024-Jan 2023-Engineer.-ends before it starts]` | end-before-start, future start and summary-vs-dated-experience mismatches are flagged high. | As expected | PASS | — | No |
| `test_a_resume::test_inconsistent_dates_flagged[future_start-Jan 2030-Present-Engineer.-starts in the future]` | end-before-start, future start and summary-vs-dated-experience mismatches are flagged high. | As expected | PASS | — | No |
| `test_a_resume::test_inconsistent_dates_flagged[summary_overclaims-Jan 2024-Jan 2025-Engineer with 10+ years of experience.-Summary claims 10+]` | end-before-start, future start and summary-vs-dated-experience mismatches are flagged high. | As expected | PASS | — | No |
| `test_a_resume::test_variant_fact_guard_rejects_new_numbers_and_skills` | verify_no_new_facts reports a new number and a new skill injected into a variant. | As expected | PASS | — | No |
| `test_a_resume::test_variant_generation_failure_is_reported` | create_variant raises ResumeError('fact_guard') and stores nothing when the builder adds facts. | As expected | PASS | — | No |
| `test_a_resume::test_variant_summary_edit_guard` | a user summary edit adding an unverified number is rejected with fact_guard. | As expected | PASS | — | No |
| `test_a_resume::test_original_synthetic_file_preserved_and_idempotent` | importing leaves the file's sha256 unchanged and re-import returns the same version (by content). | As expected | PASS | — | No |
| `test_a_resume::test_real_resume_preserved` | parsing and importing the REAL resume never modifies it (sha256 unchanged) and is idempotent. | As expected | PASS | — | No |
| `test_a_resume::test_import_bytes_strips_path_traversal` | a '../../x.pdf' upload is stored under its basename 'x.pdf' only. | As expected | PASS | — | No |
| `test_a_resume::test_import_bytes_wrong_magic` | a .pdf whose bytes are not a PDF is rejected as 'corrupted' before parsing. | As expected | PASS | — | No |
| `test_a_resume::test_import_bytes_too_large` | uploads over 5 MB are rejected with 'too_large'. | As expected | PASS | — | No |
| `test_a_resume::test_import_bytes_empty` | an empty upload is rejected with 'empty'. | As expected | PASS | — | No |

## B. Discovery & normalisation

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_b_discovery::test_canonical_url_strips_tracking_keeps_identity` | utm_*, gh_src, fragments, www and trailing slash are removed; gh_jid is kept. | As expected | PASS | — | No |
| `test_b_discovery::test_same_requisition_different_urls_one_job` | two listings with different URLs but the same employer requisition ID map to ONE job. | As expected | PASS | — | No |
| `test_b_discovery::test_same_canonical_url_with_tracking_is_same_listing` | the same URL with extra tracking parameters is recognised as the same listing. | As expected | PASS | — | No |
| `test_b_discovery::test_redirects_followed` | HttpClient follows a 301 redirect and returns the final 200 JSON. | As expected | PASS | — | No |
| `test_b_discovery::test_duplicate_listings_across_boards` | same employer+title+country on two boards -> one job with two listings. | As expected | PASS | — | No |
| `test_b_discovery::test_different_roles_same_employer_distinct` | different roles at one employer remain separate jobs. | As expected | PASS | — | No |
| `test_b_discovery::test_similar_titles_distinct` | 'Backend Engineer' and 'Frontend Engineer' are different jobs. | As expected | PASS | — | No |
| `test_b_discovery::test_different_requisitions_same_title_two_jobs` | same title with different requisition IDs -> two jobs. | As expected | PASS | — | No |
| `test_b_discovery::test_repost_after_closure_reopened` | a repost of a closed vacancy with the same requisition reopens the SAME job (not new). | As expected | PASS | — | No |
| `test_b_discovery::test_mark_closures_complete_fetch` | after a COMPLETE board fetch, a job missing from the board is closed; seen jobs stay open. | As expected | PASS | — | No |
| `test_b_discovery::test_closures_only_after_complete_fetch[True]` | a missing job is closed by an agent run only if the source fetch was complete. | As expected | PASS | — | No |
| `test_b_discovery::test_closures_only_after_complete_fetch[False]` | a missing job is closed by an agent run only if the source fetch was complete. | As expected | PASS | — | No |
| `test_b_discovery::test_missing_salary_ok` | a listing without salary is stored with NULL salary fields. | As expected | PASS | — | No |
| `test_b_discovery::test_missing_publication_date_ok` | published_at=None is accepted (first_discovered_at is still set). | As expected | PASS | — | No |
| `test_b_discovery::test_conflicting_location_reported` | structured country hint wins over conflicting text and the conflict is reported. | As expected | PASS | — | No |
| `test_b_discovery::test_unknown_sponsorship` | a posting with no sponsorship statement is classified 'unknown'. | As expected | PASS | — | No |
| `test_b_discovery::test_unsupported_local_language_disqualifies` | a required German (C1) posting produces a language disqualifier and recommendation 'skip'. | As expected | PASS | — | No |
| `test_b_discovery::test_malformed_api_json[{not json-Malformed JSON]` | malformed provider JSON yields a SourceResult with an error and complete=False (no exception). | As expected | PASS | — | No |
| `test_b_discovery::test_malformed_api_json[body1-Malformed Greenhouse response]` | malformed provider JSON yields a SourceResult with an error and complete=False (no exception). | As expected | PASS | — | No |
| `test_b_discovery::test_provider_timeout_bounded_retries` | repeated timeouts raise a retryable FetchError after max_retries+1 attempts. | As expected | PASS | — | No |
| `test_b_discovery::test_429_retry_after_honoured` | 429 with Retry-After: 2 sleeps 2s then succeeds. | As expected | PASS | — | No |
| `test_b_discovery::test_429_huge_retry_after_gives_up` | a Retry-After longer than max_delay makes the client give up immediately with RateLimited. | As expected | PASS | — | No |
| `test_b_discovery::test_empty_results` | an empty job board returns zero jobs and complete=True. | As expected | PASS | — | No |
| `test_b_discovery::test_arbeitnow_pagination_max_pages_incomplete` | hitting max_pages while more pages exist returns the jobs fetched and complete=False. | As expected | PASS | — | No |
| `test_b_discovery::test_arbeitnow_partial_failure` | a failing second page keeps page-1 jobs, records the error and marks complete=False. | As expected | PASS | — | No |
| `test_b_discovery::test_upsert_idempotent` | upserting the same raw listing twice returns 'duplicate' and counts stay at 1. | As expected | PASS | — | No |
| `test_b_discovery::test_out_of_scope_titles_filtered[Patent Engineer]` | non-software / non-engineering titles are returned as out_of_scope and not stored. | As expected | PASS | — | No |
| `test_b_discovery::test_out_of_scope_titles_filtered[Mechanical Engineer]` | non-software / non-engineering titles are returned as out_of_scope and not stored. | As expected | PASS | — | No |
| `test_b_discovery::test_out_of_scope_titles_filtered[Sales Manager]` | non-software / non-engineering titles are returned as out_of_scope and not stored. | As expected | PASS | — | No |
| `test_b_discovery::test_out_of_scope_titles_filtered[Senior Hardware Engineer]` | non-software / non-engineering titles are returned as out_of_scope and not stored. | As expected | PASS | — | No |
| `test_b_discovery::test_out_of_scope_titles_filtered[Firmware Engineer (Embedded)]` | non-software / non-engineering titles are returned as out_of_scope and not stored. | As expected | PASS | — | No |
| `test_b_discovery::test_outside_target_country_filtered` | a software role located only outside target countries (e.g. Brazil) is out_of_scope. | As expected | PASS | — | No |

## C. Application workflow

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_c_workflow::test_approval_succeeds_with_matching_fingerprint[sqlite]` | approval with the reviewed fingerprint moves awaiting_approval -> ready_to_apply with one active approval. | As expected | PASS | — | No |
| `test_c_workflow::test_approval_succeeds_with_matching_fingerprint[postgres]` | approval with the reviewed fingerprint moves awaiting_approval -> ready_to_apply with one active approval. | As expected | PASS | — | No |
| `test_c_workflow::test_rejection_revoke[sqlite]` | revoking an approval returns the application to awaiting_approval with no active approval. | As expected | PASS | — | No |
| `test_c_workflow::test_rejection_revoke[postgres]` | revoking an approval returns the application to awaiting_approval with no active approval. | As expected | PASS | — | No |
| `test_c_workflow::test_expired_approval[sqlite]` | start with an expired approval raises approval_expired AND persists awaiting_approval + revocation. | As expected | PASS | — | No |
| `test_c_workflow::test_expired_approval[postgres]` | start with an expired approval raises approval_expired AND persists awaiting_approval + revocation. | As expected | PASS | — | No |
| `test_c_workflow::test_approval_with_other_applications_fingerprint[sqlite]` | approving app A with app B's fingerprint is refused as stale_review and A stays unapproved. | As expected | PASS | — | No |
| `test_c_workflow::test_approval_with_other_applications_fingerprint[postgres]` | approving app A with app B's fingerprint is refused as stale_review and A stays unapproved. | As expected | PASS | — | No |
| `test_c_workflow::test_changed_url_after_approval[sqlite]` | if the apply URL changes after approval, start raises approval_mismatch and the approval is revoked. | As expected | PASS | — | No |
| `test_c_workflow::test_changed_url_after_approval[postgres]` | if the apply URL changes after approval, start raises approval_mismatch and the approval is revoked. | As expected | PASS | — | No |
| `test_c_workflow::test_missing_resume_blocks_gate[sqlite]` | an application without a resume cannot be approved (gate: 'No resume selected'). | As expected | PASS | — | No |
| `test_c_workflow::test_missing_resume_blocks_gate[postgres]` | an application without a resume cannot be approved (gate: 'No resume selected'). | As expected | PASS | — | No |
| `test_c_workflow::test_missing_mandatory_answer_blocks_gate[sqlite]` | a job with unknown country creates a blocking missing item and approval is refused. | As expected | PASS | — | No |
| `test_c_workflow::test_missing_mandatory_answer_blocks_gate[postgres]` | a job with unknown country creates a blocking missing item and approval is refused. | As expected | PASS | — | No |
| `test_c_workflow::test_invalid_email_phone_rejected[email-not-an-email-email]` | invalid candidate email/phone values are rejected by settings validation. | As expected | PASS | — | No |
| `test_c_workflow::test_invalid_email_phone_rejected[phone-call me maybe-phone]` | invalid candidate email/phone values are rejected by settings validation. | As expected | PASS | — | No |
| `test_c_workflow::test_invalid_email_phone_rejected[email-a@b-email]` | invalid candidate email/phone values are rejected by settings validation. | As expected | PASS | — | No |
| `test_c_workflow::test_non_user_actor_refused[sqlite]` | approve/start by actor 'agent' is refused with not_user. | As expected | PASS | — | No |
| `test_c_workflow::test_non_user_actor_refused[postgres]` | approve/start by actor 'agent' is refused with not_user. | As expected | PASS | — | No |
| `test_c_workflow::test_job_closed_cannot_approve_or_start[sqlite]` | a closed job blocks approval (gate) and blocks start of an existing approval (job_not_open). | As expected | PASS | — | No |
| `test_c_workflow::test_job_closed_cannot_approve_or_start[postgres]` | a closed job blocks approval (gate) and blocks start of an existing approval (job_not_open). | As expected | PASS | — | No |
| `test_c_workflow::test_manual_portal_never_automated[sqlite]` | start only returns the destination URL and sets application_started; nothing is recorded as submitted. | As expected | PASS | — | No |
| `test_c_workflow::test_manual_portal_never_automated[postgres]` | start only returns the destination URL and sets application_started; nothing is recorded as submitted. | As expected | PASS | — | No |
| `test_c_workflow::test_network_failure_before_submit_retry_needs_new_approval[sqlite]` | failed/before_submit -> submission_failed; retry allowed -> awaiting_approval; old approval unusable. | As expected | PASS | — | No |
| `test_c_workflow::test_network_failure_before_submit_retry_needs_new_approval[postgres]` | failed/before_submit -> submission_failed; retry allowed -> awaiting_approval; old approval unusable. | As expected | PASS | — | No |
| `test_c_workflow::test_timeout_after_submit_is_uncertain_no_retry[sqlite]` | failed/after_submit_unknown -> submission_uncertain; retry_after_failure is refused. | As expected | PASS | — | No |
| `test_c_workflow::test_timeout_after_submit_is_uncertain_no_retry[postgres]` | failed/after_submit_unknown -> submission_uncertain; retry_after_failure is refused. | As expected | PASS | — | No |
| `test_c_workflow::test_duplicate_start_refused[sqlite]` | a second start of the same application is refused (approval consumed / not_ready). | As expected | PASS | — | No |
| `test_c_workflow::test_duplicate_start_refused[postgres]` | a second start of the same application is refused (approval consumed / not_ready). | As expected | PASS | — | No |
| `test_c_workflow::test_uncertain_never_auto_resubmitted[sqlite]` | sweep_stale_starts only moves stale application_started -> submission_uncertain; nothing else changes. | As expected | PASS | — | No |
| `test_c_workflow::test_uncertain_never_auto_resubmitted[postgres]` | sweep_stale_starts only moves stale application_started -> submission_uncertain; nothing else changes. | As expected | PASS | — | No |
| `test_c_workflow::test_submitted_without_evidence_refused[sqlite]` | 'submitted' without reference/confirmation/screenshot is refused with no_evidence. | As expected | PASS | — | No |
| `test_c_workflow::test_submitted_without_evidence_refused[postgres]` | 'submitted' without reference/confirmation/screenshot is refused with no_evidence. | As expected | PASS | — | No |
| `test_c_workflow::test_submitted_with_reference_accepted[sqlite]` | 'submitted' with a reference number is accepted and the evidence stored. | As expected | PASS | — | No |
| `test_c_workflow::test_submitted_with_reference_accepted[postgres]` | 'submitted' with a reference number is accepted and the evidence stored. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[sqlite-http://boards.greenhouse.io/acme/jobs/1-insecure_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[sqlite-https://evil.example.com/apply-unverified_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[sqlite-https://127.0.0.1/apply-unverified_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[sqlite-None-no_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[postgres-http://boards.greenhouse.io/acme/jobs/1-insecure_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[postgres-https://evil.example.com/apply-unverified_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[postgres-https://127.0.0.1/apply-unverified_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_destination_unverified_blocks[postgres-None-no_destination]` | http://, unknown hosts, IPs or missing URLs are refused by verify_destination and the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_user_cancellation_withdraw[sqlite]` | user withdrawal moves an awaiting application to 'withdrawn' (terminal). | As expected | PASS | — | No |
| `test_c_workflow::test_user_cancellation_withdraw[postgres]` | user withdrawal moves an awaiting application to 'withdrawn' (terminal). | As expected | PASS | — | No |
| `test_c_workflow::test_mark_applied_externally[sqlite]` | external application -> submitted+external=1 with declared evidence; later prepare refused. | As expected | PASS | — | No |
| `test_c_workflow::test_mark_applied_externally[postgres]` | external application -> submitted+external=1 with declared evidence; later prepare refused. | As expected | PASS | — | No |
| `test_c_workflow::test_equivalent_vacancy_already_submitted[sqlite]` | get_or_create for an equivalent vacancy (same employer/title/country) raises already_applied. | As expected | PASS | — | No |
| `test_c_workflow::test_equivalent_vacancy_already_submitted[postgres]` | get_or_create for an equivalent vacancy (same employer/title/country) raises already_applied. | As expected | PASS | — | No |
| `test_c_workflow::test_manual_correction_cannot_set_submitted[sqlite]` | correct_status/update_progress refuse to fabricate 'submitted'. | As expected | PASS | — | No |
| `test_c_workflow::test_manual_correction_cannot_set_submitted[postgres]` | correct_status/update_progress refuse to fabricate 'submitted'. | As expected | PASS | — | No |
| `test_c_workflow::test_daily_limit_enforced[sqlite]` | once max_applications_per_day starts are reached, further approvals are blocked by the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_daily_limit_enforced[postgres]` | once max_applications_per_day starts are reached, further approvals are blocked by the gate. | As expected | PASS | — | No |
| `test_c_workflow::test_stale_listing_blocks_approval[sqlite]` | a listing not verified within verification_max_age_hours cannot be approved. | As expected | PASS | — | No |
| `test_c_workflow::test_stale_listing_blocks_approval[postgres]` | a listing not verified within verification_max_age_hours cannot be approved. | As expected | PASS | — | No |

## D. Security

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/jobs]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/jobs/1]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/applications]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/applications/1]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/sponsorship]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/prep]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/studio]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/runs]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/runs/x]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/audit]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/settings]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/settings/export.json]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/jobs/export.csv]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/files/1]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/studio/1/download.pdf]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/jobs/compare]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_pages_redirect_to_login[/letters/1]` | every protected page redirects (303) an anonymous visitor to /login?next=... | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_api_401[/api/stats]` | every protected JSON API returns 401 without a session. | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_api_401[/api/jobs]` | every protected JSON API returns 401 without a session. | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/jobs/1/apply]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/applications/1/approve]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/applications/1/start]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/settings]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/runs/start]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/studio/upload]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/logout]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/jobs/1/external]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/applications/1/outcome]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_unauthenticated_posts_401[/prep/skill]` | every state-changing POST returns 401 without a session (nothing executed). | As expected | PASS | — | No |
| `test_d_security::test_invalid_login_generic_401[alex-Wrong-Password-1]` | wrong password and unknown user both return 401 with the same generic message and no session. | As expected | PASS | — | No |
| `test_d_security::test_invalid_login_generic_401[nobody-Correct-Horse-42x]` | wrong password and unknown user both return 401 with the same generic message and no session. | As expected | PASS | — | No |
| `test_d_security::test_repeated_failed_logins_lock_out` | after 5 failures the 6th attempt is refused even with the correct password. | As expected | PASS | — | No |
| `test_d_security::test_session_idle_expiration` | a session idle past the timeout is rejected and the user is redirected to /login. | As expected | PASS | — | No |
| `test_d_security::test_session_absolute_expiration` | a session past its absolute expiry is rejected. | As expected | PASS | — | No |
| `test_d_security::test_logout_invalidates_server_side` | after logout the old session cookie no longer authenticates (session row deleted). | As expected | PASS | — | No |
| `test_d_security::test_csrf_required[None]` | an authenticated POST without/with a wrong CSRF token is rejected with 403 and changes nothing. | As expected | PASS | — | No |
| `test_d_security::test_csrf_required[wrong-token]` | an authenticated POST without/with a wrong CSRF token is rejected with 403 and changes nothing. | As expected | PASS | — | No |
| `test_d_security::test_xss_job_title_escaped` | a job title containing <script> is rendered HTML-escaped on /jobs and /jobs/{id}. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params0]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params1]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params2]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params3]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params4]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params5]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_sql_injection_filters[params6]` | SQL-injection payloads in filters return 200, match nothing, leak nothing and break nothing. | As expected | PASS | — | No |
| `test_d_security::test_path_traversal_not_served[/static/../jobagent/config.py]` | traversal attempts never serve source files (no 200 with Python source). | As expected | PASS | — | No |
| `test_d_security::test_path_traversal_not_served[/static/..%2f..%2fconfig.py]` | traversal attempts never serve source files (no 200 with Python source). | As expected | PASS | — | No |
| `test_d_security::test_path_traversal_not_served[/static/%2e%2e/%2e%2e/jobagent/config.py]` | traversal attempts never serve source files (no 200 with Python source). | As expected | PASS | — | No |
| `test_d_security::test_path_traversal_not_served[/files/..%2F]` | traversal attempts never serve source files (no 200 with Python source). | As expected | PASS | — | No |
| `test_d_security::test_path_traversal_not_served[/files/..%2Fconfig.py]` | traversal attempts never serve source files (no 200 with Python source). | As expected | PASS | — | No |
| `test_d_security::test_malicious_resume_upload_rejected` | a .pdf upload containing non-PDF (EXE) bytes is rejected and nothing is stored. | As expected | PASS | — | No |
| `test_d_security::test_malicious_evidence_upload_rejected` | an evidence 'screenshot' named .png but containing EXE bytes is rejected; status unchanged. | As expected | PASS | — | No |
| `test_d_security::test_secrets_never_leak` | API key, secret key and password never appear in run records, audit log or any page/export. | As expected | PASS | — | No |
| `test_d_security::test_direct_approve_wrong_fingerprint_refused` | a crafted POST approve with a forged fingerprint is refused; application stays awaiting_approval. | As expected | PASS | — | No |
| `test_d_security::test_direct_start_without_approval_refused` | POST /start on an unapproved application is refused and nothing starts. | As expected | PASS | — | No |
| `test_d_security::test_session_cookie_flags` | the session cookie is HttpOnly and SameSite=strict (no Secure on plain local HTTP). | As expected | PASS | — | No |
| `test_d_security::test_session_cookie_secure_with_https` | with cfg.https=True the session cookie also carries Secure and HSTS is sent. | As expected | PASS | — | No |
| `test_d_security::test_security_headers[/login]` | CSP without unsafe-inline, X-Frame-Options DENY, nosniff and no-store are set. | As expected | PASS | — | No |
| `test_d_security::test_security_headers[/healthz]` | CSP without unsafe-inline, X-Frame-Options DENY, nosniff and no-store are set. | As expected | PASS | — | No |
| `test_d_security::test_setup_locked_after_first_user` | once a user exists, GET and POST /setup redirect to /login and cannot create another user. | As expected | PASS | — | No |
| `test_d_security::test_setup_requires_token_on_vercel` | on Vercel /setup is locked without JOBAGENT_SETUP_TOKEN, and requires the correct token when set. | As expected | PASS | — | No |
| `test_d_security::test_cron_requires_bearer_secret` | /api/cron/run is 403 without a configured secret or with a wrong bearer; 200 with the right one. | As expected | PASS | — | No |
| `test_d_security::test_prompt_injection_does_not_change_state` | an injected JD + lying AI output cannot add skills/sponsorship or change any application/approval state. | As expected | PASS | — | No |
| `test_d_security::test_ai_cover_letter_with_new_facts_rejected` | an AI letter inventing numbers/skills is rejected (audited) and the deterministic template is used. | As expected | PASS | — | No |

## E. Persistence & concurrency

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_e_persistence::test_database_unavailable[sqlite]` | an unreachable database raises DatabaseError with a clear message and no credential leak. | As expected | PASS | — | No |
| `test_e_persistence::test_database_unavailable[postgres]` | an unreachable database raises DatabaseError with a clear message and no credential leak. | As expected | PASS | — | No |
| `test_e_persistence::test_failed_migration_rolls_back[sqlite]` | a failing migration leaves no partial tables and no schema_migrations row. | As expected | PASS | — | No |
| `test_e_persistence::test_failed_migration_rolls_back[postgres]` | a failing migration leaves no partial tables and no schema_migrations row. | As expected | PASS | — | No |
| `test_e_persistence::test_transaction_rollback_on_exception[sqlite]` | an exception inside db.transaction() rolls back every write in it. | As expected | PASS | — | No |
| `test_e_persistence::test_transaction_rollback_on_exception[postgres]` | an exception inside db.transaction() rolls back every write in it. | As expected | PASS | — | No |
| `test_e_persistence::test_restart_persists_data_and_dedup[sqlite]` | after close/reopen, data persists and the same listing is still recognised as duplicate. | As expected | PASS | — | No |
| `test_e_persistence::test_restart_persists_data_and_dedup[postgres]` | after close/reopen, data persists and the same listing is still recognised as duplicate. | As expected | PASS | — | No |
| `test_e_persistence::test_repeated_agent_runs_idempotent[sqlite]` | a second identical agent run creates no new jobs or applications but records its own run. | As expected | PASS | — | No |
| `test_e_persistence::test_repeated_agent_runs_idempotent[postgres]` | a second identical agent run creates no new jobs or applications but records its own run. | As expected | PASS | — | No |
| `test_e_persistence::test_two_simultaneous_agent_runs[sqlite]` | with two concurrent runs (separate connections) exactly one runs and the other is 'aborted'. | As expected | PASS | — | No |
| `test_e_persistence::test_two_simultaneous_agent_runs[postgres]` | with two concurrent runs (separate connections) exactly one runs and the other is 'aborted'. | As expected | PASS | — | No |
| `test_e_persistence::test_concurrent_approvals[sqlite]` | two simultaneous approvals of the same application -> exactly one succeeds, one active approval. | As expected | PASS | — | No |
| `test_e_persistence::test_concurrent_approvals[postgres]` | two simultaneous approvals of the same application -> exactly one succeeds, one active approval. | As expected | PASS | — | No |
| `test_e_persistence::test_concurrent_start[sqlite]` | two simultaneous starts consume the approval exactly once. | As expected | PASS | — | No |
| `test_e_persistence::test_concurrent_start[postgres]` | two simultaneous starts consume the approval exactly once. | As expected | PASS | — | No |
| `test_e_persistence::test_unique_key_conflicts_raise_integrity_error[sqlite]` | duplicate jobs.job_key and a second application for the same job raise IntegrityError. | As expected | PASS | — | No |
| `test_e_persistence::test_unique_key_conflicts_raise_integrity_error[postgres]` | duplicate jobs.job_key and a second application for the same job raise IntegrityError. | As expected | PASS | — | No |
| `test_e_persistence::test_interrupted_discovery[sqlite]` | a source crashing mid-run records a partial/failed run with the error, keeps earlier jobs and frees the lock. | As expected | PASS | — | No |
| `test_e_persistence::test_interrupted_discovery[postgres]` | a source crashing mid-run records a partial/failed run with the error, keeps earlier jobs and frees the lock. | As expected | PASS | — | No |
| `test_e_persistence::test_interrupted_submission_swept[sqlite]` | an application_started with no outcome survives a restart and is swept to submission_uncertain. | As expected | PASS | — | No |
| `test_e_persistence::test_interrupted_submission_swept[postgres]` | an application_started with no outcome survives a restart and is swept to submission_uncertain. | As expected | PASS | — | No |
| `test_e_persistence::test_backup_restore[sqlite]` | SQLite backup+restore round-trips data; PostgreSQL backup is refused with guidance. | As expected | PASS | — | No |
| `test_e_persistence::test_backup_restore[postgres]` | SQLite backup+restore round-trips data; PostgreSQL backup is refused with guidance. | As expected | PASS | — | No |
| `test_e_persistence::test_audit_chain_valid[sqlite]` | verify_audit_chain returns (True, None) for an untampered log. | As expected | PASS | — | No |
| `test_e_persistence::test_audit_chain_valid[postgres]` | verify_audit_chain returns (True, None) for an untampered log. | As expected | PASS | — | No |
| `test_e_persistence::test_audit_tampering_detected[sqlite]` | after bypassing the trigger and editing a row, verify_audit_chain returns (False, that id). | As expected | PASS | — | No |
| `test_e_persistence::test_audit_tampering_detected[postgres]` | after bypassing the trigger and editing a row, verify_audit_chain returns (False, that id). | As expected | PASS | — | No |
| `test_e_persistence::test_audit_append_only[sqlite-UPDATE audit_events SET actor='x']` | UPDATE and DELETE on audit_events are blocked by the append-only trigger. | As expected | PASS | — | No |
| `test_e_persistence::test_audit_append_only[sqlite-DELETE FROM audit_events]` | UPDATE and DELETE on audit_events are blocked by the append-only trigger. | As expected | PASS | — | No |
| `test_e_persistence::test_audit_append_only[postgres-UPDATE audit_events SET actor='x']` | UPDATE and DELETE on audit_events are blocked by the append-only trigger. | As expected | PASS | — | No |
| `test_e_persistence::test_audit_append_only[postgres-DELETE FROM audit_events]` | UPDATE and DELETE on audit_events are blocked by the append-only trigger. | As expected | PASS | — | No |

## F. Dashboard & usability

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_f_dashboard::test_empty_database_pages_render[/]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/jobs]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/applications]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/studio]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/runs]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/prep]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/sponsorship]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/audit]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_empty_database_pages_render[/settings]` | every main page renders 200 on an empty database. | As expected | PASS | — | No |
| `test_f_dashboard::test_large_dataset_paginates_fast` | with 1500 jobs, /jobs renders page 1 in <3s, totals are correct and pages slice correctly. | As expected | PASS | — | No |
| `test_f_dashboard::test_long_strings_render` | very long employer and title strings render without error. | As expected | PASS | — | No |
| `test_f_dashboard::test_missing_data_renders_dash` | a job with no salary/city/country/date renders '—' placeholders without errors. | As expected | PASS | — | No |
| `test_f_dashboard::test_international_characters_render` | München, São Paulo, 日本語 and emoji render intact. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params0-expected0]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params1-expected1]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params2-expected2]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params3-expected3]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params4-expected4]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params5-expected5]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params6-expected6]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params7-expected7]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params8-expected8]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params9-expected9]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params10-expected10]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params11-expected11]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params12-expected12]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params13-expected13]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params14-expected14]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params15-expected15]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params16-expected16]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params17-expected17]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params18-expected18]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params19-expected19]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params20-expected20]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_filters[params21-expected21]` | each filter param returns 200 on /jobs and exactly the matching jobs via the same query. | As expected | PASS | — | No |
| `test_f_dashboard::test_sorting[recent-order0]` | every sort option returns 200 with the expected order. | As expected | PASS | — | No |
| `test_f_dashboard::test_sorting[company-order1]` | every sort option returns 200 with the expected order. | As expected | PASS | — | No |
| `test_f_dashboard::test_sorting[relevant-order2]` | every sort option returns 200 with the expected order. | As expected | PASS | — | No |
| `test_f_dashboard::test_sorting[salary-order3]` | every sort option returns 200 with the expected order. | As expected | PASS | — | No |
| `test_f_dashboard::test_pagination_links` | with 25 jobs and per_page=10 the page shows a link to page 2 and page 3 lists 5 jobs. | As expected | PASS | — | No |
| `test_f_dashboard::test_csv_export_formula_injection` | CSV export is text/csv with a header and neutralises cells starting with '=' (apostrophe prefix). | As expected | PASS | — | No |
| `test_f_dashboard::test_charts_empty_datasets` | charts with empty/zero data return the accessible 'No data yet' element. | As expected | PASS | — | No |
| `test_f_dashboard::test_deep_link_after_expiry_returns_to_page` | an anonymous deep link redirects to /login?next=<path>, and logging in returns to that path. | As expected | PASS | — | No |
| `test_f_dashboard::test_open_redirect_blocked` | a next= pointing off-site is replaced by '/'. | As expected | PASS | — | No |
| `test_f_dashboard::test_accessibility_basics[/settings]` | every visible form control has a label/aria-label; pages declare <html lang> (and a skip link when signed in). | As expected | PASS | — | No |
| `test_f_dashboard::test_accessibility_basics[/jobs]` | every visible form control has a label/aria-label; pages declare <html lang> (and a skip link when signed in). | As expected | PASS | — | No |
| `test_f_dashboard::test_accessibility_basics[/login]` | every visible form control has a label/aria-label; pages declare <html lang> (and a skip link when signed in). | As expected | PASS | — | No |
| `test_f_dashboard::test_db_unavailable_503` | an unreachable database yields a 503 plain-text message, not a stack trace. | As expected | PASS | — | No |
| `test_f_dashboard::test_unknown_entity_404[/jobs/99999]` | unknown jobs/applications/runs/letters render a 404 page. | As expected | PASS | — | No |
| `test_f_dashboard::test_unknown_entity_404[/applications/99999]` | unknown jobs/applications/runs/letters render a 404 page. | As expected | PASS | — | No |
| `test_f_dashboard::test_unknown_entity_404[/runs/nope]` | unknown jobs/applications/runs/letters render a 404 page. | As expected | PASS | — | No |
| `test_f_dashboard::test_unknown_entity_404[/letters/99999]` | unknown jobs/applications/runs/letters render a 404 page. | As expected | PASS | — | No |

## G. AI & cost controls

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_g_ai::test_provider_unavailable_falls_back_to_rules` | network errors -> AIError after 3 attempts; scoring falls back to rules and no application is created. | As expected | PASS | — | No |
| `test_g_ai::test_invalid_api_key_disables_ai` | HTTP 401 -> permanent AIError and the AIService is disabled for the rest of the run. | As expected | PASS | — | No |
| `test_g_ai::test_rate_limit_then_success` | a 429 (Retry-After: 1) is retried after sleeping and the next 200 response is returned. | As expected | PASS | — | No |
| `test_g_ai::test_malformed_model_output_uses_fallback` | non-JSON output from the primary model is rejected and the fallback model's JSON is used. | As expected | PASS | — | No |
| `test_g_ai::test_hallucinated_requirements_dropped` | skills/years not literally supported by the posting are dropped by validate_job_analysis. | As expected | PASS | — | No |
| `test_g_ai::test_hallucinated_sponsorship_dropped` | a sponsorship claim whose quote is not in the posting is dropped; a verbatim quote is kept. | As expected | PASS | — | No |
| `test_g_ai::test_incorrect_resume_facts_in_letter_rejected` | letter_violations flags numbers and skills that are not in the resume. | As expected | PASS | — | No |
| `test_g_ai::test_excessive_tokens_run_budget` | a call whose estimated cost exceeds the run budget raises BudgetExceeded BEFORE calling the provider. | As expected | PASS | — | No |
| `test_g_ai::test_repeated_request_served_from_cache` | an identical second request is served from ai_cache (provider called once, usage row cached=1). | As expected | PASS | — | No |
| `test_g_ai::test_monthly_budget_reached` | when this month's recorded spend exceeds the monthly budget, BudgetExceeded is raised. | As expected | PASS | — | No |
| `test_g_ai::test_both_models_fail` | if primary and fallback models both fail, AIError is raised and nothing is written to applications. | As expected | PASS | — | No |
| `test_g_ai::test_ai_disabled_in_settings_never_calls` | with AI disabled in Settings the service is unavailable and the provider is never called. | As expected | PASS | — | No |

## Unit. Unit tests

| Test name | Expected result | Actual result | Status | Error details | Blocks release? |
|---|---|---|---|---|---|
| `test_unit::test_senior_title_disqualified` | a 'Senior' title gets a seniority disqualifier and recommendation 'skip'. | As expected | PASS | — | No |
| `test_unit::test_dotnet_junior_ireland_sponsorship_apply` | a junior .NET role in Ireland with confirmed sponsorship scores 'apply' with reasons. | As expected | PASS | — | No |
| `test_unit::test_weights_configurable_change_score` | changing weights changes the total score deterministically. | As expected | PASS | — | No |
| `test_unit::test_low_score_keeps_reasons` | a poor match is not silently dropped: it returns a score, recommendation and explicit gaps/notes. | As expected | PASS | — | No |
| `test_unit::test_experience_years[You have at least 3 years of experience with C#.-3.0]` | the smallest explicit years-of-experience requirement is extracted (None if absent). | As expected | PASS | — | No |
| `test_unit::test_experience_years[2-4 years experience in software development-2.0]` | the smallest explicit years-of-experience requirement is extracted (None if absent). | As expected | PASS | — | No |
| `test_unit::test_experience_years[Minimum of 5+ years of professional experience-5.0]` | the smallest explicit years-of-experience requirement is extracted (None if absent). | As expected | PASS | — | No |
| `test_unit::test_experience_years[Great team, free lunch.-None]` | the smallest explicit years-of-experience requirement is extracted (None if absent). | As expected | PASS | — | No |
| `test_unit::test_language_requirements[Fluent German (C1) is required.-German-True]` | spoken-language requirements are detected with the correct required flag. | As expected | PASS | — | No |
| `test_unit::test_language_requirements[Dutch language skills are a plus.-Dutch-False]` | spoken-language requirements are detected with the correct required flag. | As expected | PASS | — | No |
| `test_unit::test_language_requirements[Wir suchen dich! Dein Profil: C#-German-True]` | spoken-language requirements are detected with the correct required flag. | As expected | PASS | — | No |
| `test_unit::test_salary_from_text[Salary: \u20ac50,000 - \u20ac70,000 per year-expect0]` | salary ranges are parsed with currency; vague text yields None. | As expected | PASS | — | No |
| `test_unit::test_salary_from_text[\xa345k-\xa355k plus bonus-expect1]` | salary ranges are parsed with currency; vague text yields None. | As expected | PASS | — | No |
| `test_unit::test_salary_from_text[Competitive salary-None]` | salary ranges are parsed with currency; vague text yields None. | As expected | PASS | — | No |
| `test_unit::test_sponsorship_classify[We offer visa sponsorship for the right candidate.-None-IE-confirmed]` | positive -> confirmed, negative -> not_supported, conflicting -> unknown, UK register listed -> likely. | As expected | PASS | — | No |
| `test_unit::test_sponsorship_classify[Unfortunately we are unable to offer visa sponsorship.-None-IE-not_supported]` | positive -> confirmed, negative -> not_supported, conflicting -> unknown, UK register listed -> likely. | As expected | PASS | — | No |
| `test_unit::test_sponsorship_classify[Visa sponsorship is available. Note: we cannot provide sponsorship for contractors.-None-IE-unknown]` | positive -> confirmed, negative -> not_supported, conflicting -> unknown, UK register listed -> likely. | As expected | PASS | — | No |
| `test_unit::test_sponsorship_classify[A normal job ad.-employer3-GB-likely]` | positive -> confirmed, negative -> not_supported, conflicting -> unknown, UK register listed -> likely. | As expected | PASS | — | No |
| `test_unit::test_sponsorship_classify[A normal job ad.-None-IE-unknown]` | positive -> confirmed, negative -> not_supported, conflicting -> unknown, UK register listed -> likely. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[nope-1]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[weights-value1]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[operating_mode-auto_submit]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[schedule-value3]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[target_countries-value4]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[max_applications_per_day-1000]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[sources-value6]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_settings_validate_errors[ai-value7]` | invalid settings (unknown key, zero weights, auto-submit mode, bad time, bad codes) raise SettingsError. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Dublin, Ireland-countries0-Dublin-unknown-None]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Edinburgh, Scotland-countries1-Edinburgh-unknown-None]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Dubai, UAE-countries2-Dubai-unknown-None]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Toronto, ON-countries3-Toronto-unknown-None]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Sydney NSW (Hybrid)-countries4-Sydney-hybrid-None]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Tokyo, Japan-countries5-Tokyo-unknown-None]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
| `test_unit::test_parse_location[Remote - Europe-countries6-None-remote-EU_REGION]` | locations map to ISO countries, city, remote type and region. | As expected | PASS | — | No |
