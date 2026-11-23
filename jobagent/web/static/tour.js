/* "Tour me": a short guided walk through the page you are on. Vanilla JS, no inline handlers (CSP).
   Each tour is a list of steps; a step points at the first VISIBLE element matching its selector (steps whose
   element is not on the page are skipped, so one tour works for every state of a page). A step without a
   selector is shown as a centred card. Keyboard: Enter/→ next, ← back, Esc closes. */
(function () {
  "use strict";
  var doc = document;

  // ------------------------------------------------------------------------------------------------ tours
  // `match`: path regexp. `when`: optional selector that must exist (to tell two pages on one path apart).
  var TOURS = [
    { match: /^\/$/, when: ".home-head", steps: [
      { title: "Your home", text: "Everything about your job search in one place. This tour takes about a minute." },
      { sel: ".home-head h1", title: "Your search", text: "The roles, level and countries you're searching for. Use “Change preferences” to update them at any time." },
      { sel: ".home-kpis", title: "Your activity", text: "Jobs found for you (and how many are new this week), applications sent, ones in progress, interviews and offers. Click any box to open that list." },
      { sel: "#rec-h", title: "Recommended for your roles", text: "Open jobs that best match your roles and resume, which you haven't applied to yet. The circle is your match score out of 100." },
      { sel: "#apps-h", title: "Your applications", text: "Your latest applications and where each one stands." },
      { sel: "#crs-h", title: "Courses to strengthen your profile", text: "Free courses first, then paid certifications, all recognised by employers in your field and linked to the official provider." },
      { sel: ".home-head .actions .btn-primary", title: "Browse jobs", text: "See every job picked for you, with filters." }
    ]},
    { match: /^\/$/, steps: [
      { title: "Overview", text: "The administrator dashboard: the whole installation at a glance." },
      { sel: ".kpis", title: "Key figures", text: "Jobs tracked, open jobs, what the last run found, and your own application funnel. Click a figure to open that list." },
      { sel: "#onboard", title: "Get started", text: "The setup checklist. It disappears once a resume is uploaded and jobs have been discovered." },
      { sel: "#metrics", title: "Funnel metrics", text: "Response, interview and offer rates, with the exact definition of each." },
      { sel: "#upcoming", title: "Upcoming", text: "Interviews, follow-ups and application deadlines coming up." },
      { sel: "#lastrun", title: "Last agent run", text: "When jobs were last discovered and scored. Runs happen automatically every day." },
      { sel: ".section-title", title: "Charts", text: "Applications per week, open jobs by country, seniority and sponsorship evidence." }
    ]},
    { match: /^\/jobs\/compare/, steps: [
      { title: "Compare jobs", text: "The jobs you ticked, side by side: match score, salary, location, sponsorship and the skills each asks for." },
      { sel: "main table", title: "Side by side", text: "Read across a row to compare one thing; open a job's title to see it in full." }
    ]},
    { match: /^\/jobs\/\d+/, steps: [
      { sel: ".job-head", title: "The job", text: "Title, employer, location and badges for remote work, sponsorship and recommendation." },
      { sel: "form[action$='/apply']", title: "Apply", text: "Starts an application: JobAgent tailors your resume, drafts a cover letter and prepares the screening answers for you to check." },
      { sel: "form[action$='/bookmark']", title: "Save for later", text: "Bookmark the job to come back to it." },
      { sel: "form[action$='/external']", title: "Applied somewhere else?", text: "Record it here so it is tracked with your other applications." },
      { sel: "[id^='match-']", title: "Match breakdown", text: "Why the job scored what it did: skills, experience, seniority, location and more, with the reasons written out." },
      { sel: "[id^='skills-']", title: "Skills", text: "The skills the job asks for, and which ones your resume already shows." },
      { sel: "[id^='spon-']", title: "Sponsorship & work rights", text: "What the listing says about visas, with the exact sentences as evidence." },
      { sel: "[id^='desc-']", title: "Full description", text: "The original job description." }
    ]},
    { match: /^\/jobs/, steps: [
      { title: "Jobs", text: "Every open job picked for your roles, ranked by how well it matches you." },
      { sel: ".view-tabs", title: "For you or all jobs", text: "“For you” shows only your industries and roles. “All jobs” shows everything, with an industry filter." },
      { sel: ".searchbar", title: "Search", text: "Search by keyword, company or location." },
      { sel: ".pillbar", title: "Filters", text: "Narrow by date posted, experience, job type, remote, sponsorship, salary and more. Each filter applies straight away." },
      { sel: ".job-list", title: "Results", text: "Each card shows the match score. Tick the box on two or more cards to compare them." },
      { sel: ".detail-pane", title: "Job details", text: "The selected job: why it matches, the skills it needs, sponsorship evidence and the Apply button." },
      { sel: ".detail-pane form[action$='/apply']", title: "Apply", text: "JobAgent prepares a tailored resume, cover letter and screening answers. You check them before anything is sent." }
    ]},
    { match: /^\/mnc/, steps: [
      { title: "Top MNCs", text: "Large employers in your industries, with their openings in your target countries." },
      { sel: "nav[aria-label='Filter by industry']", title: "Industries", text: "Switch between your industries." },
      { sel: "nav[aria-label='Filter by sector']", title: "Sectors", text: "Narrow the companies to one sector." },
      { sel: ".mnc-card", title: "A company", text: "How many of its jobs match you, a link to its jobs here, and its official careers site." },
      { sel: "#top-h", title: "Best-matching roles", text: "The best matches for you across all of these companies." }
    ]},
    { match: /^\/applications\/\d+/, steps: [
      { sel: ".page-head", title: "Your application", text: "Everything about this one application, from preparation to the final outcome." },
      { sel: ".stepper", title: "Where it stands", text: "Prepare → Apply → Applied. The highlighted step is the one you're on." },
      { sel: "#sent", title: "What will be sent", text: "The tailored resume, cover letter and screening answers. Check them and change anything before you apply." },
      { sel: "#approve-h", title: "Approve", text: "Approve the application so it can be submitted, or open the employer's form and apply yourself." },
      { sel: "#apply-h", title: "Apply", text: "Open the employer's form; the Application Companion can fill it in for you. Then press “I've applied”." },
      { sel: "#prog-h", title: "Progress", text: "Record what happens next: interview, assessment, offer or rejection, with dates." },
      { sel: "#tl-h", title: "Timeline", text: "Every change to this application, with the time it happened." },
      { sel: "#mgmt-h", title: "Manage", text: "Notes, follow-up reminders and withdrawing." }
    ]},
    { match: /^\/applications/, steps: [
      { title: "Applications", text: "Every job you've started or applied to, grouped by stage." },
      { sel: ".chips", title: "Filter by status", text: "Show only one stage, for example interviews." },
      { sel: ".kanban", title: "Your pipeline", text: "Each column is a stage, from shortlisted to offer. The number shows how many are in it." },
      { sel: ".app-card", title: "An application", text: "Open it to see what was sent, record progress or add notes." }
    ]},
    { match: /^\/answers/, steps: [
      { title: "Screening Answers", text: "Answers to the questions application forms ask (notice period, salary, work rights...). Save them once and they are reused on every form." },
      { sel: "#pending-h", title: "Questions waiting for you", text: "Questions a form asked that JobAgent couldn't answer from your profile. Answer them here and the held-back applications are retried automatically." },
      { sel: "#why-h", title: "Why applications were handed back", text: "The reasons an application needed you, such as a visible CAPTCHA or a missing answer." },
      { sel: "#consent-h", title: "Required privacy boxes", text: "Choose whether JobAgent may tick the privacy acknowledgements that some forms require." },
      { sel: "#lib-h", title: "Your saved answers", text: "Everything you've answered so far. Edit or delete any answer, or add a new one." }
    ]},
    { match: /^\/recruiters\/compose|^\/recruiters\/send/, steps: [
      { sel: "#to", title: "Recipient", text: "The recruiter's email address." },
      { sel: "#subject", title: "Subject", text: "A short, specific subject line." },
      { sel: "#body", title: "Message", text: "A draft is written for you from the job and your resume. Edit it until it sounds like you." }
    ]},
    { match: /^\/recruiters/, steps: [
      { title: "Recruiters", text: "Find recruiters and hiring contacts for jobs you like, and keep track of who you've written to." },
      { sel: "form[action='/recruiters']", title: "Search", text: "Search the jobs and their contacts." },
      { sel: ".job-list", title: "Jobs and contacts", text: "Add a contact's name and email to a job, then write to them." },
      { sel: "#hist-h", title: "Outreach history", text: "Every message you've sent, so you never contact someone twice by mistake." }
    ]},
    { match: /^\/companion/, steps: [
      { title: "Application Companion", text: "A browser extension that fills in application forms on employer sites with your details. You always press Submit yourself." },
      { sel: "#h-install", title: "Set up in 2 minutes", text: "Download the extension, add it to Chrome or Edge, and connect it with a token." },
      { sel: "#h-ready", title: "How ready your profile is", text: "The details the Companion can fill. The more complete this is, the less you type." },
      { sel: "#h-tokens", title: "Access tokens", text: "Create the token that connects the extension to your account. Revoke one if you lose a device." }
    ]},
    { match: /^\/sponsorship/, steps: [
      { title: "Relocation & Sponsorship", text: "Jobs in your industries and target countries that mention visa sponsorship or relocation, plus the official visa routes." },
      { sel: "#reg-h", title: "UK sponsor register", text: "UK employers are checked against the Home Office list of licensed sponsors." },
      { sel: "#jobs-h", title: "Jobs with evidence", text: "Each job shows whether sponsorship is confirmed, likely or not offered, with the sentences that say so." },
      { sel: "[aria-labelledby^='g-']", title: "Visa routes", text: "The official visa routes for each of your target countries, with links to the government pages." }
    ]},
    { match: /^\/studio/, steps: [
      { title: "Resume Studio", text: "Upload your resume, see how it reads to employers, and make tailored versions." },
      { sel: "#up-h", title: "Upload", text: "Upload your resume (PDF or Word). It becomes the one used for matching and applying." },
      { sel: "#cur-h", title: "Your resume", text: "What JobAgent read from your resume. Check the details are right." },
      { sel: "#ats-h", title: "Resume check", text: "How well applicant-tracking systems can read your resume, with fixes." },
      { sel: "#var-h", title: "Tailored versions", text: "Make a version for a type of role or a country. Facts are only reordered, never invented." },
      { sel: "#ver-h", title: "Versions", text: "All your versions. Download them as PDF or Word." },
      { sel: "#cl-h", title: "Cover letters", text: "Cover letters written for your applications. Open one to edit or download it." }
    ]},
    { match: /^\/letters/, steps: [
      { sel: "#letter-text", title: "Cover letter", text: "The letter for this application. Edit the text and save." },
      { sel: ".page-head .actions", title: "Download", text: "Download it to attach to an application." }
    ]},
    { match: /^\/prep/, steps: [
      { title: "Skills & Prep", text: "What to learn next for your roles, and interview preparation for each job you apply to." },
      { sel: "#roles-h", title: "Skills for your roles", text: "The core skills employers expect for each of your roles. ✓ means your resume already shows it; ○ means it's worth learning or adding to your resume." },
      { sel: "#ask-h", title: "What employers are asking for", text: "Required skills in your best-matching jobs that your resume doesn't show yet. Press “I'm learning this” to track it." },
      { sel: "#courses-h", title: "Free and paid courses", text: "Free courses (some only charge for the certificate) and paid certifications, from the official providers. Ones that need work experience are marked “Later in your career”." },
      { sel: "#gaps-h", title: "Skill gaps per application", text: "For jobs you're applying to: the missing skills, with topics to study and your progress." },
      { sel: "details.disclosure", title: "Interview preparation", text: "A preparation plan for each application: what you've shown, what's missing, and likely interview topics." }
    ]},
    { match: /^\/welcome/, steps: [
      { sel: ".wizard-steps", title: "Your job preferences", text: "Five short steps: resume, industries, roles, experience and countries. You can come back and change them any time." },
      { sel: ".wizard-card form", title: "This step", text: "Choose what fits you. Required questions are marked." },
      { sel: ".wizard-actions", title: "Save and continue", text: "Each step is saved on its own, so you can stop and come back." }
    ]},
    { match: /^\/support\/\d+/, steps: [
      { sel: ".page-head", title: "Your request", text: "One support request and its status." },
      { sel: "#h-thread", title: "Conversation", text: "Messages between you and the administrator, newest last." },
      { sel: "#reply", title: "Reply", text: "Add more detail or answer a question here." }
    ]},
    { match: /^\/support/, steps: [
      { title: "Help & Support", text: "Answers to common questions, a check of your account, and a way to ask the administrator for help." },
      { sel: "#h-find", title: "Find an answer", text: "Type your question; matching answers appear straight away." },
      { sel: "#h-checks", title: "Your account check", text: "Live checks of your setup that explain most problems, each with a “Fix” button." },
      { sel: "#h-ai", title: "AI features", text: "Whether AI features are on for your account, and why." },
      { sel: "#h-new", title: "Raise a request", text: "Still stuck? Describe the problem and the administrator will reply here." },
      { sel: "#h-mine", title: "Your requests", text: "Your requests and their replies. A badge in the menu tells you when there's an answer." }
    ]},
    { match: /^\/settings/, steps: [
      { title: "Settings", text: "Your details and how JobAgent works for you. Each section saves on its own." },
      { sel: ".settings-nav", title: "Sections", text: "Jump straight to a section." },
      { sel: "#h-candidate", title: "Candidate facts", text: "Your contact details, work rights, notice period, languages and expected salary. Forms are filled from these, so keep them accurate." },
      { sel: "#h-search", title: "Search preferences", text: "Target countries, remote work, minimum salary and titles to avoid." },
      { sel: "#h-auto", title: "Auto-apply", text: "Let JobAgent submit applications that clearly qualify, within the limits you set." },
      { sel: "#h-rstyle", title: "Resume style", text: "How tailored resumes look: spelling, one page, AI rewording." },
      { sel: "#h-notifications", title: "Notifications", text: "Which emails you get." },
      { sel: "#h-password", title: "Password", text: "Change your password." },
      { sel: "#h-data", title: "Your data", text: "Download everything JobAgent holds about you." },
      { sel: "#h-sources", title: "Job sources (administrator)", text: "Which job boards and APIs are searched." },
      { sel: "#h-schedule", title: "Schedule (administrator)", text: "When the agent runs and who is notified." }
    ]},
    { match: /^\/runs\/[^/]+/, steps: [
      { sel: "#stats-h", title: "Run statistics", text: "How many jobs were fetched, new, updated, closed and scored." },
      { sel: "#src-h", title: "Sources", text: "Each job source in this run, how many jobs it returned and any errors." },
      { sel: "#dec-h", title: "Decisions", text: "What happened to each job and why. Filter them with the box." },
      { sel: "#err-h", title: "Errors", text: "Anything that went wrong, with details." },
      { sel: "#ai-h", title: "AI usage", text: "AI calls made during the run and what they cost." }
    ]},
    { match: /^\/runs/, steps: [
      { title: "Agent Runs", text: "Each run discovers new jobs, removes closed ones and scores everything for every user." },
      { sel: "form[action='/runs/start']", title: "Run now", text: "Start a run straight away. Runs also happen automatically every day." },
      { sel: "main table", title: "Run history", text: "Every run, with what it found. Open one to see the details." }
    ]},
    { match: /^\/audit/, steps: [
      { title: "Audit Log", text: "A record of every important action: who did what, and when." },
      { sel: ".chips", title: "Filter", text: "Show only one kind of event, such as applications or settings." },
      { sel: "main table", title: "Events", text: "Each row is one action, with links to what it changed." }
    ]},
    { match: /^\/admin\/users/, steps: [
      { title: "Users & audience", text: "Approve sign-ups, see who your users are, and control what each one can use." },
      { sel: "#h-pending", title: "Pending sign-ups", text: "Approve or reject new account requests." },
      { sel: ".funnel", title: "Funnel", text: "Accounts → approved → preferences set → resume uploaded → applied." },
      { sel: "#audience", title: "Audience by industry", text: "Users grouped by the industry they target, with the roles they want and whether there are enough jobs for them. Set which screens each industry sees here." },
      { sel: ".bulk-row", title: "AI for everyone", text: "Give or remove AI access for all users at once." },
      { sel: "#h-users", title: "All accounts", text: "Every account with its profile, AI access and screens. Use “Edit…” under Screens to give a user extra screens." },
      { sel: ".matrix-box", title: "Who sees what", text: "A grid of every user and the screens they can open." },
      { sel: "#h-outbox", title: "Email", text: "Emails JobAgent sent, and a test email to check delivery." }
    ]},
    { match: /^\/admin\/support\/\d+/, steps: [
      { sel: "#h-req", title: "Requester", text: "Who asked, and their account details." },
      { sel: "#h-thread", title: "Conversation", text: "The messages so far." },
      { sel: "#h-sugg", title: "Suggested answers", text: "Saved answers that match this request. Use one to reply faster." },
      { sel: "#reply", title: "Reply", text: "Reply, change the status, and save the reply as a reusable answer if it will help others." }
    ]},
    { match: /^\/admin\/support/, steps: [
      { title: "Support desk", text: "Requests from your users, and the answers you've saved for common questions." },
      { sel: ".kpis", title: "At a glance", text: "Open, waiting and resolved requests." },
      { sel: "#h-queue", title: "Requests", text: "Filter by status or category and open a request to reply." },
      { sel: "#h-answers", title: "Suggested answers", text: "Saved answers shown to users when their question matches, before they raise a request." }
    ]}
  ];

  function tourFor(path) {
    for (var i = 0; i < TOURS.length; i++) {
      var t = TOURS[i];
      if (t.match.test(path) && (!t.when || doc.querySelector(t.when))) return t;
    }
    return null;
  }

  // ------------------------------------------------------------------------------------------------ helpers
  function visible(el) {
    if (!el) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    var cs = window.getComputedStyle(el);
    return cs.visibility !== "hidden" && cs.display !== "none";
  }
  function target(sel) {
    if (!sel) return null;
    var els;
    try { els = doc.querySelectorAll(sel); } catch (e) { return null; }
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      // a section heading stands for its whole section/card
      if (/^H[1-4]$/.test(el.tagName) && el.closest) {
        var box = el.closest("section, .card");
        if (box && box !== doc.body && visible(box)) el = box;
      }
      if (el.closest && el.closest(".whatsnew-backdrop, .sidebar")) continue;
      if (visible(el)) return el;
    }
    return null;
  }
  function make(tag, cls, text) {
    var e = doc.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function reduced() {
    return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }

  // ------------------------------------------------------------------------------------------------ engine
  var state = null;

  function start(tour, opener) {
    stop();
    var steps = tour.steps.filter(function (s) { return !s.sel || target(s.sel); });
    if (!steps.length) return;
    var shade = make("div", "tour-shade");
    var spot = make("div", "tour-spot");
    var pop = make("div", "tour-pop");
    pop.setAttribute("role", "dialog");
    pop.setAttribute("aria-modal", "true");
    pop.setAttribute("aria-labelledby", "tour-title");
    pop.setAttribute("aria-describedby", "tour-text");
    var count = make("p", "tour-count");
    var title = make("h2", "tour-title");
    title.id = "tour-title";
    title.tabIndex = -1;
    var text = make("p", "tour-text");
    text.id = "tour-text";
    var bar = make("div", "tour-bar");
    var skip = make("button", "btn btn-sm btn-ghost", "End tour");
    var back = make("button", "btn btn-sm", "Back");
    var next = make("button", "btn btn-sm btn-primary", "Next");
    [skip, back, next].forEach(function (b) { b.type = "button"; });
    bar.appendChild(skip);
    var right = make("div", "tour-nav");
    right.appendChild(back);
    right.appendChild(next);
    bar.appendChild(right);
    pop.appendChild(count);
    pop.appendChild(title);
    pop.appendChild(text);
    pop.appendChild(bar);
    doc.body.appendChild(shade);
    doc.body.appendChild(spot);
    doc.body.appendChild(pop);
    doc.body.classList.add("touring");
    state = { steps: steps, i: 0, shade: shade, spot: spot, pop: pop, title: title, text: text, count: count,
              back: back, next: next, opener: opener, el: null };
    doc.documentElement.setAttribute("data-tour-steps", String(steps.length));
    skip.addEventListener("click", stop);
    shade.addEventListener("click", stop);
    back.addEventListener("click", function () { go(state.i - 1); });
    next.addEventListener("click", function () { go(state.i + 1); });
    doc.addEventListener("keydown", onKey, true);
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    go(0);
  }

  function stop() {
    if (!state) return;
    var s = state;
    state = null;
    [s.shade, s.spot, s.pop].forEach(function (n) { if (n.parentNode) n.parentNode.removeChild(n); });
    doc.body.classList.remove("touring");
    doc.removeEventListener("keydown", onKey, true);
    window.removeEventListener("resize", place);
    window.removeEventListener("scroll", place, true);
    doc.documentElement.setAttribute("data-tour-done", "1");
    if (s.opener && s.opener.focus) s.opener.focus();
  }

  function onKey(e) {
    if (!state) return;
    if (e.key === "Escape") { e.preventDefault(); stop(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); go(state.i + 1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); go(state.i - 1); }
    else if (e.key === "Tab") {                                  // keep focus inside the tour card
      var f = Array.prototype.slice.call(state.pop.querySelectorAll("button:not([disabled])"));
      if (!f.length) return;
      var i = f.indexOf(doc.activeElement);
      e.preventDefault();
      f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    }
  }

  function go(i) {
    if (!state) return;
    if (i >= state.steps.length) { stop(); return; }
    if (i < 0) i = 0;
    state.i = i;
    var step = state.steps[i];
    state.el = target(step.sel);
    state.count.textContent = (i + 1) + " of " + state.steps.length;
    state.title.textContent = step.title;
    state.text.textContent = step.text;
    state.back.disabled = i === 0;
    state.next.textContent = i === state.steps.length - 1 ? "Done" : "Next";
    doc.documentElement.setAttribute("data-tour-step", String(i + 1));
    if (state.el) {
      state.el.scrollIntoView({ block: "center", inline: "nearest", behavior: reduced() ? "auto" : "smooth" });
    }
    place();
    setTimeout(place, reduced() ? 0 : 350);                      // after smooth scrolling settles
    state.title.focus({ preventScroll: true });
  }

  function place() {
    if (!state) return;
    var pop = state.pop, spot = state.spot, el = state.el;
    var vw = doc.documentElement.clientWidth, vh = window.innerHeight, pad = 8, gap = 12, m = 16;
    if (!el) {
      spot.style.display = "none";
      state.shade.classList.add("dim");
      pop.classList.add("center");
      pop.style.left = pop.style.top = "";
      return;
    }
    state.shade.classList.remove("dim");
    pop.classList.remove("center");
    var r = el.getBoundingClientRect();
    var top = Math.max(r.top - pad, 4), left = Math.max(r.left - pad, 4);
    var bottom = Math.min(r.bottom + pad, vh - 4), rightEdge = Math.min(r.right + pad, vw - 4);
    spot.style.display = "block";
    spot.style.top = top + "px";
    spot.style.left = left + "px";
    spot.style.width = Math.max(rightEdge - left, 0) + "px";
    spot.style.height = Math.max(bottom - top, 0) + "px";
    if (vw < 600) { pop.classList.add("docked"); pop.style.left = pop.style.top = ""; return; }
    pop.classList.remove("docked");
    var pw = pop.offsetWidth, ph = pop.offsetHeight;
    var y = bottom + gap;                                          // below the element if it fits...
    if (y + ph > vh - m) y = top - gap - ph;                       // ...else above it...
    if (y < m) y = Math.min(Math.max(m, vh - ph - m), Math.max(m, top + gap));   // ...else overlapping, on screen
    var x = Math.min(Math.max(m, r.left), vw - pw - m);
    pop.style.left = x + "px";
    pop.style.top = y + "px";
  }

  // ------------------------------------------------------------------------------------------------ button
  function addButton() {
    var tour = tourFor(location.pathname);
    if (!tour) return;
    var btn = make("button", "btn tour-btn");
    btn.type = "button";
    btn.setAttribute("data-tour-start", "");
    btn.setAttribute("aria-label", "Tour me: a guided walk through this page");
    var ico = doc.querySelector("use[href*='icons.svg']");
    var sprite = ico ? ico.getAttribute("href").split("#")[0] : "/static/icons.svg";
    var ns = "http://www.w3.org/2000/svg";
    var svg = doc.createElementNS(ns, "svg");
    svg.setAttribute("class", "ico");
    svg.setAttribute("aria-hidden", "true");
    var use = doc.createElementNS(ns, "use");
    use.setAttribute("href", sprite + "#info");
    svg.appendChild(use);
    btn.appendChild(svg);
    btn.appendChild(doc.createTextNode(" Tour me"));
    btn.addEventListener("click", function () { start(tour, btn); });
    var head = doc.querySelector("main .page-head, main .wizard-head");
    if (head) {
      var actions = head.querySelector(":scope > .actions");
      if (!actions) {
        actions = make("div", "actions");
        head.appendChild(actions);
      }
      actions.insertBefore(btn, actions.firstChild);
    } else {
      btn.classList.add("tour-float");
      doc.body.appendChild(btn);
    }
  }

  if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", addButton);
  else addButton();
})();
