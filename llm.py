"""Every Claude call lives here, and each call's tokens and cost are logged to usage.csv.

DELPHI_MODEL writes what employers see (form answers). DELPHI_FAST_MODEL does the cheap bulk
work: ranking the daily list and pulling postings out of Quercus pages.
"""
import csv
import json
from datetime import datetime, timedelta
from functools import cache

import anthropic
from pydantic import BaseModel

import library
from config import DATA, PROFILE, env

MODEL = env("DELPHI_MODEL", "claude-opus-5-5")
FAST_MODEL = env("DELPHI_FAST_MODEL", "claude-haiku-5-5")
# $ per million tokens: (input, output, cache read). Cache writes cost 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
USAGE_CSV = DATA / "usage.csv"
USAGE_FIELDS = ["time", "kind", "model", "input", "cache_write", "cache_read", "output", "cost"]


@cache
def _client():
    return anthropic.Anthropic()


def cost_of(model, input, cache_write, cache_read, output):
    price_in, price_out, price_read = PRICES[model]
    return (input * price_in + cache_write * price_in * 1.25 + cache_read * price_read + output * price_out) / 1e6


def _log(kind, model, usage, path=USAGE_CSV):
    tokens = {"input": usage.input_tokens, "cache_write": usage.cache_creation_input_tokens or 0,
              "cache_read": usage.cache_read_input_tokens or 0, "output": usage.output_tokens}
    new = not path.exists()
    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, USAGE_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow({"time": datetime.now().isoformat(timespec="seconds"), "kind": kind, "model": model,
                         **tokens, "cost": round(cost_of(model, **tokens), 6)})


def _ask(kind, prompt, schema, model=MODEL, effort=None, cached=""):
    """`cached` is a stable prompt prefix (profile, resume, rules) that later calls read from cache at ~5-10%."""
    content = [{"type": "text", "text": prompt}]
    if cached:
        content.insert(0, {"type": "text", "text": cached, "cache_control": {"type": "ephemeral"}})
    kwargs = {"output_config": {"effort": effort}} if effort else {}
    if not model.startswith("claude-haiku"):  # Haiku has no server-side fallback
        # If a safety classifier declines, the API retries on Anthropic's recommended fallback model.
        kwargs |= {"extra_headers": {"anthropic-beta": "server-side-fallback-2026-07-01"},
                   "extra_body": {"fallbacks": "default"}}
    r = _client().messages.parse(model=model, max_tokens=16000, output_format=schema,
                                 messages=[{"role": "user", "content": content}], **kwargs)
    _log(kind, model, r.usage)  # priced at the requested model even if a fallback served it
    if r.stop_reason == "refusal" or r.parsed_output is None:
        raise RuntimeError(f"Claude returned no usable output (stop_reason={r.stop_reason})")
    return r.parsed_output


def spend_since(days, path=None):
    """Dollars spent on Claude in the last `days` days."""
    path = path or USAGE_CSV
    since = (datetime.now() - timedelta(days=days)).isoformat()
    rows = list(csv.DictReader(open(path, newline=""))) if path.exists() else []
    return sum(float(r["cost"]) for r in rows if r["time"] >= since)


def usage_summary(path=USAGE_CSV):
    """Average and total spend per kind of call, for /cost."""
    rows = list(csv.DictReader(open(path, newline=""))) if path.exists() else []
    if not rows:
        return "No Claude calls logged yet."
    month_ago = (datetime.now() - timedelta(days=30)).isoformat()
    lines = [f"Last 30 days: ${sum(float(r['cost']) for r in rows if r['time'] >= month_ago):.2f}  "
             f"(all time ${sum(float(r['cost']) for r in rows):.2f}, {len(rows)} calls)"]
    for kind, name in [("rank", "Daily digest"), ("map_fields", "Per form page"), ("cover_letter", "Cover letter"),
                       ("tailor", "Tailored resume"), ("claim_check", "Tailored resume check"),
                       ("library", "Library card"),
                       ("extract", "Quercus scan")]:
        costs = [float(r["cost"]) for r in rows if r["kind"] == kind]
        if costs:
            lines.append(f"{name}: avg ${sum(costs) / len(costs):.4f} over {len(costs)}")
    return "\n".join(lines)


class Pick(BaseModel):
    index: int
    score: int
    why: str


class Ranking(BaseModel):
    picks: list[Pick]


def rank(jobs, n):
    """The n jobs this candidate is best qualified for, best first, each with a score and a one-line reason."""
    if not jobs:
        return []
    listing = "\n".join(f"[{i}] {j['title']} @ {j['company']} ({'; '.join(j['locations'])}) {j['description'][:200]}"
                        for i, j in enumerate(jobs))
    picks = _ask(
        "rank",
        f"Candidate resume:\n{PROFILE.get('resume_text', '')}\n\n"
        f"Work authorization: {PROFILE.get('work_auth', 'unknown')}. Rank down roles the candidate "
        "likely can't take because of it.\n\n"
        f"Below are internship postings. Pick the {n} this candidate is most qualified for and most likely to get "
        f"an interview at. Score each 0-10 and give a one-line reason (max 15 words). Best first.\n\n{listing}",
        Ranking, model=FAST_MODEL, effort="low").picks
    seen = set()  # Claude sometimes lists a job twice
    picks = [p for p in picks if 0 <= p.index < len(jobs) and not (p.index in seen or seen.add(p.index))]
    return [dict(jobs[p.index], score=p.score, why=p.why) for p in picks][:n]


class Posting(BaseModel):
    company: str
    title: str
    location: str
    url: str


class Postings(BaseModel):
    postings: list[Posting]


def extract_jobs(text):
    """Job/internship postings mentioned in free text (Quercus pages and announcements)."""
    return _ask("extract",
                "Extract every job, internship or co-op posting from these course posts. Links appear as "
                "'Title (url)'; use the posting's own link as url (empty string if none). Put the term type in the "
                "title when the post groups jobs that way, e.g. 'Software Engineer I (Co-op, Winter 2027)'. "
                "location: city and country if stated, else empty. Skip events, info sessions and workshops. "
                "Return an empty list if there are no postings.\n\n" + text,
                Postings, model=FAST_MODEL, effort="low").postings


class Fill(BaseModel):
    index: int
    value: str


class FormPlan(BaseModel):
    fills: list[Fill]
    unknown: list[int]


def _applicant(answers):
    """The prompt prefix every form-filling call shares (so it's cached once for all of them): profile, resume, rules
    and saved answers. Job, posting and fields change and go after it."""
    profile = {k: v for k, v in PROFILE.items() if k not in ("resume_text", "search_terms", "experience", "skills")}
    return (
        "You are filling out internship applications for this applicant.\n\n"
        f"Applicant profile: {json.dumps(profile, default=str)}\n\n"
        f"Resume:\n{PROFILE.get('resume_text', '')}\n\n"
        "Rules:\n"
        "- Only use facts from the profile, resume or earlier answers. Never invent facts.\n"
        "- Questions that restate a profile fact (graduation date, school, degree, work authorization) are "
        "answerable: reformat the fact to fit the field (e.g. April 2028 -> 04/2028 or 2028-04-30).\n"
        "- Dates in text boxes: follow the field's placeholder format; with none, use MM/DD/YYYY.\n"
        "- For fields with options, value must be exactly one of the options "
        "(checkbox groups: comma-separate several).\n"
        "- Self-identification questions with no profile answer: pick the decline/prefer-not-to-say option.\n"
        "- Short free-text questions like 'why this company': 2-3 honest sentences grounded in the resume, and in "
        "the job posting when one is given (name what the team actually does; no generic praise).\n"
        "- GPA: fill it only when the field is required; leave optional GPA fields out.\n"
        "- Skip file fields. Skip optional fields you can't answer.\n"
        "- Put the index of every required field you can't answer from these facts in `unknown`.\n\n"
        # Saved answers change only when you answer something new, so they're cached too (they're most of the input).
        f"Answers the applicant gave to earlier application questions: {json.dumps(answers)}"
    )


def map_fields(job, fields, answers, posting=""):
    """What to put in each form field. Required fields we can't answer truthfully come back in `unknown`.
    `posting` is the job ad's text, passed when there are free-text questions to ground the answers in."""
    keep = ("index", "type", "label", "options", "required", "placeholder")
    return _ask(
        "map_fields",
        f"Job: {job['title']} at {job['company']}.\n\n"
        + (f"Job posting:\n{posting[:6000]}\n\n" if posting else "")
        + f"Form fields: {json.dumps([{k: f[k] for k in keep if k == 'index' or f.get(k)} for f in fields])}",
        FormPlan, cached=_applicant(answers))


class CoverLetter(BaseModel):
    paragraphs: list[str]


def _examples(kind, job, posting):
    """The library's best matching examples for this job, framed so none of their facts end up in yours."""
    found = library.examples(kind, job, posting)
    if not found:
        return ""
    label = kind.replace("_", " ")
    return (f"Example {label}s by OTHER people, from a shared library, that got them interviews. Use them only for "
            f"structure, tone, length and how specific to be. They are NOT about the applicant: never take a fact, "
            f"project, employer, skill, number or sentence from them.\n\n"
            + "\n\n".join(f"<example tier={e.get('tier')}>\n{text[:3000]}\n</example>" for e, text in found)
            + "\n\n")


def cover_letter(job, posting, answers):
    """A short cover letter for this job, as paragraphs (no greeting or sign-off: the PDF adds those)."""
    return _ask(
        "cover_letter",
        f"Write a cover letter for {job['title']} at {job['company']}.\n\n"
        + (f"Job posting:\n{posting[:6000]}\n\n" if posting else "")
        + _examples("cover_letter", job, posting)
        + "3 short paragraphs, 200-300 words in all, plain and specific: why this role and team (from the posting), "
          "the 2-3 most relevant things the applicant has done (from the resume, with real numbers where the resume "
          "has them), and availability (term, start date) from the profile. Only facts from the profile, resume and "
          "answers; no invented skills, no flattery, no clichés like 'I am writing to express my interest'. "
          "No greeting, no sign-off, no placeholders.",
        CoverLetter, cached=_applicant(answers)).paragraphs


class ResumeEntry(BaseModel):
    heading: str  # "Backend Intern, Shopify" or a project name
    detail: str  # dates, location, stack: whatever the original puts beside it
    bullets: list[str]


class ResumeSection(BaseModel):
    title: str
    entries: list[ResumeEntry]


class TailoredResume(BaseModel):
    sections: list[ResumeSection]
    changes: str  # one sentence: what was moved, cut or reworded for this job


TAILOR_RULES = (
    "You tailor this applicant's resume to one job at a time.\n\n"
    "Applicant's resume (the only source of facts):\n{resume}\n\n"
    "Rules:\n"
    "- Every fact comes from this resume. Keep employers, titles, dates, schools, numbers and tool names exactly as "
    "they are. Never add a skill, tool, project, result or number it doesn't have.\n"
    "- You may reorder sections, entries and bullets, cut what doesn't help for this job, merge or reword bullets "
    "to lead with what the posting asks for, and use the posting's words where the resume supports them.\n"
    "- One page: about 450-600 words. Strong verbs, results first, no first person, no summary paragraph unless the "
    "original has one.\n"
    "- Leave out the name and contact line: the page adds them.\n"
)


def tailor_resume(job, posting):
    """The applicant's resume rewritten for this job (same facts, chosen and ordered for it)."""
    return _ask(
        "tailor",
        f"Tailor the resume for {job['title']} at {job['company']}.\n\n"
        + (f"Job posting:\n{posting[:6000]}\n\n" if posting else "")
        + _examples("resume", job, posting),
        TailoredResume, cached=TAILOR_RULES.format(resume=PROFILE.get("resume_text", "")))


def resume_as_text(resume):
    return "\n\n".join(f"{s.title}\n" + "\n".join(
        f"{e.heading} | {e.detail}\n" + "\n".join(f"- {b}" for b in e.bullets) for e in s.entries)
        for s in resume.sections)


class Claims(BaseModel):
    unsupported: list[str]


def check_claims(resume):
    """Statements in a tailored resume that the original doesn't back up (a second read, so you see what to check)."""
    return _ask(
        "claim_check",
        f"Original resume:\n{PROFILE.get('resume_text', '')}\n\nRewritten resume:\n{resume_as_text(resume)}\n\n"
        "List each statement in the rewritten resume that the original doesn't support: an employer, title, date, "
        "number, skill, tool, project or result that isn't there, or a claim made stronger than the original's. "
        "Rewording, reordering and leaving things out are fine. Quote the rewritten text briefly. Empty if all of it "
        "is supported.",
        Claims, model=FAST_MODEL).unsupported
